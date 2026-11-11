"""Login attempt cap and server-set flags for the FFIS session cookie.

streamlit-authenticator 0.4.x checks the bcrypt password and reads the
re-authentication JWT from ``st.context.cookies`` (the Cookie header on the
next page load). It writes that JWT with CookieManager, which assigns
``document.cookie``. Browsers ignore HttpOnly on that path, so the flag
would not be a real HttpOnly cookie.

This module keeps that authenticator. It counts failed attempts in the app
process, and it asks the browser to redeem a one-time nonce at
``POST /ffis/login-cookie``. The route's response is the Set-Cookie header.
The decision for SameSite and the cap's env name live in
``docs/CUTOVER-mdkappsvr.md``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Any, Callable, Mapping, MutableMapping, Optional

LOGIN_COOKIE_PATH = "/ffis/login-cookie"
ENV_MAX_FAILURES = "FFIS_LOGIN_MAX_FAILURES"
DEFAULT_MAX_FAILURES = 5
LOGIN_LOCKED_KEY = "ffis_login_locked"
SET_NONCE_KEY = "ffis_cookie_set_nonce"
CLEAR_NONCE_KEY = "ffis_cookie_clear_nonce"
LOCKOUT_MESSAGE = (
    "Too many failed sign-in attempts. "
    "Login is locked until the FFIS app is restarted."
)

_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")
_CLAIM_TTL_S = 120
_CLAIM_MAX = 64
_LOG = logging.getLogger("ffis.login")

_tracker_lock = threading.Lock()
_claim_lock = threading.Lock()
_claims: dict[str, dict[str, Any]] = {}
_tracker: Optional["AttemptTracker"] = None
_route_mounted = False
_mount_warned = False
_component = None


def configured_max_failures() -> int:
    """Return the failed-login cap.

    Missing, non-integer, and values below 1 all stay at the default so a
    typo in the environment cannot turn the cap off.
    """
    raw = os.environ.get(ENV_MAX_FAILURES, "").strip()
    if raw == "":
        return DEFAULT_MAX_FAILURES
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_MAX_FAILURES
    if parsed < 1:
        return DEFAULT_MAX_FAILURES
    return parsed


def _user_key(username: str) -> str:
    return str(username).strip().lower()


class AttemptTracker:
    """In-process per-username failure counts. A restart clears them."""

    def __init__(self) -> None:
        self._failures: dict[str, int] = {}
        self._lock = threading.Lock()

    def failure_count(self, username: str) -> int:
        key = _user_key(username)
        with self._lock:
            return self._failures.get(key, 0)

    def is_locked(self, username: str, max_failures: Optional[int] = None) -> bool:
        cap = configured_max_failures() if max_failures is None else max_failures
        return self.failure_count(username) >= cap

    def record_failure(self, username: str) -> int:
        key = _user_key(username)
        with self._lock:
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            return count

    def record_success(self, username: str) -> None:
        key = _user_key(username)
        with self._lock:
            self._failures.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


def get_tracker() -> AttemptTracker:
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = AttemptTracker()
        return _tracker


def apply_login_attempt(
    tracker: AttemptTracker,
    username: Optional[str],
    password: Optional[str],
    original: Callable[..., Any],
    session: MutableMapping[str, Any],
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> Any:
    """Call the authenticator login, unless this username is already locked.

    ``username`` empty means cookie re-authentication (reload). That path is
    not a password guess and is not blocked by the cap. A locked username
    does not call ``original``, so the password is not checked.
    """
    key = _user_key(username) if username is not None else ""
    if not key:
        return original(username, password, *args, **dict(kwargs))
    if tracker.is_locked(key):
        session["authentication_status"] = False
        session[LOGIN_LOCKED_KEY] = True
        return False
    result = original(username, password, *args, **dict(kwargs))
    if result is True:
        tracker.record_success(key)
        session[LOGIN_LOCKED_KEY] = False
    elif result is False:
        tracker.record_failure(key)
        if tracker.is_locked(key):
            session["authentication_status"] = False
            session[LOGIN_LOCKED_KEY] = True
    return result


def _purge_claims(now: float) -> None:
    expired = [nonce for nonce, claim in _claims.items() if claim.get("exp", 0) <= now]
    for nonce in expired:
        _claims.pop(nonce, None)
    while len(_claims) > _CLAIM_MAX:
        oldest = min(_claims.items(), key=lambda item: item[1].get("exp", 0))[0]
        _claims.pop(oldest, None)


def _put_claim(claim: dict[str, Any]) -> str:
    nonce = secrets.token_urlsafe(24)
    claim = dict(claim)
    claim["exp"] = time.time() + _CLAIM_TTL_S
    with _claim_lock:
        _purge_claims(time.time())
        _claims[nonce] = claim
    return nonce


def claim_set(cookie_name: str, token: str, max_age: int) -> str:
    if isinstance(token, bytes):
        token = token.decode("ascii")
    token = str(token)
    if not _COOKIE_NAME_RE.match(cookie_name or ""):
        raise ValueError("login cookie name is not a single token")
    if token == "" or any(char in token for char in "\r\n;"):
        raise ValueError("login cookie value cannot be stored")
    return _put_claim(
        {
            "action": "set",
            "name": cookie_name,
            "value": token,
            "max_age": int(max_age),
        }
    )


def claim_clear(cookie_name: str) -> str:
    if not _COOKIE_NAME_RE.match(cookie_name or ""):
        raise ValueError("login cookie name is not a single token")
    return _put_claim({"action": "clear", "name": cookie_name})


def redeem_claim(nonce: str) -> Optional[dict[str, Any]]:
    if not isinstance(nonce, str) or nonce == "":
        return None
    now = time.time()
    with _claim_lock:
        claim = _claims.pop(nonce, None)
        _purge_claims(now)
    if not claim or claim.get("exp", 0) <= now:
        return None
    return claim


def reset_state_for_tests() -> None:
    get_tracker().reset()
    with _claim_lock:
        _claims.clear()


def issue_set_cookie(cookie_model: Any, session: MutableMapping[str, Any]) -> Optional[str]:
    """Create the authenticator's JWT and stash a nonce. Does not touch JS cookies."""
    if cookie_model.cookie_expiry_days == 0:
        return None
    cookie_model.exp_date = cookie_model._set_exp_date()
    token = cookie_model._token_encode()
    if isinstance(token, bytes):
        token = token.decode("ascii")
    max_age = int(float(cookie_model.cookie_expiry_days) * 86400)
    nonce = claim_set(cookie_model.cookie_name, token, max_age)
    session[SET_NONCE_KEY] = nonce
    session[CLEAR_NONCE_KEY] = None
    return nonce


def issue_clear_cookie(cookie_model: Any, session: MutableMapping[str, Any]) -> str:
    nonce = claim_clear(cookie_model.cookie_name)
    session[CLEAR_NONCE_KEY] = nonce
    session[SET_NONCE_KEY] = None
    return nonce


def _session_state() -> MutableMapping[str, Any]:
    import streamlit as st

    return st.session_state


def _bind_login(model: Any, tracker: AttemptTracker) -> None:
    if getattr(model, "_ffis_cap_installed", False):
        return
    original = model.login

    def login(username: Optional[str] = None, password: Optional[str] = None, *args: Any, **kwargs: Any) -> Any:
        return apply_login_attempt(
            tracker,
            username,
            password,
            original,
            _session_state(),
            args,
            kwargs,
        )

    model.login = login
    model._ffis_cap_installed = True


def _bind_cookies(cookie_model: Any) -> None:
    if getattr(cookie_model, "_ffis_cookie_installed", False):
        return

    def set_cookie() -> None:
        issue_set_cookie(cookie_model, _session_state())

    def delete_cookie() -> None:
        nonce = issue_clear_cookie(cookie_model, _session_state())
        _emit_claim(nonce)

    cookie_model.set_cookie = set_cookie
    cookie_model.delete_cookie = delete_cookie
    cookie_model._ffis_cookie_installed = True


def install_login_hardening(authenticator: Any) -> None:
    """Cap attempts and replace the JS cookie write on one authenticator."""
    mount_on_running_server()
    _bind_login(authenticator.authentication_controller.authentication_model, get_tracker())
    _bind_cookies(authenticator.cookie_controller.cookie_model)


def _component_func() -> Any:
    global _component
    if _component is None:
        import streamlit.components.v1 as components
        from pathlib import Path

        frontend = Path(__file__).resolve().parent / "ffis_login_claim" / "frontend"
        _component = components.declare_component("ffis_login_claim", path=str(frontend))
    return _component


def _emit_claim(nonce: str) -> None:
    import streamlit as st

    st.markdown(
        """
        <style>
        iframe[title="ffis_login.ffis_login_claim"] { height: 0 !important; border: 0; display: none; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    _component_func()(nonce=nonce, key="ffis_login_claim_" + nonce, default=None)


def emit_pending_login_cookie() -> None:
    """Ask the browser to redeem a pending login or logout nonce.

    The same nonce is sent again on later reruns until it expires. Redeeming
    it is one-time, so a rerun that tears down the iframe before the request
    finishes can still set or clear the cookie. A spent nonce gets a 404.
    """
    session = _session_state()
    nonce = session.get(SET_NONCE_KEY)
    if nonce:
        _emit_claim(str(nonce))
    clear_nonce = session.get(CLEAR_NONCE_KEY)
    if clear_nonce:
        _emit_claim(str(clear_nonce))


def _is_streamlit_app(app: Any) -> bool:
    routes = getattr(app, "routes", None)
    if not isinstance(routes, (list, tuple)):
        return False
    for route in routes:
        path = getattr(route, "path", "") or ""
        if isinstance(path, str) and "_stcore/health" in path:
            return True
    return False


def _app_from_object(obj: Any) -> Any:
    """Return a Starlette app, ignoring objects whose attrs are dynamic."""
    from starlette.applications import Starlette

    if isinstance(obj, Starlette):
        return obj
    if obj.__class__.__name__ != "UvicornServer":
        return None
    server = getattr(obj, "_server", None)
    config = getattr(server, "config", None)
    app = getattr(config, "app", None)
    if isinstance(app, Starlette):
        return app
    return None


def find_streamlit_apps() -> list[Any]:
    """Return live Starlette apps that serve Streamlit's health route."""
    import gc

    found: list[Any] = []
    seen: set[int] = set()
    for obj in gc.get_objects():
        app = _app_from_object(obj)
        if app is None:
            continue
        identity = id(app)
        if identity in seen or not _is_streamlit_app(app):
            continue
        seen.add(identity)
        found.append(app)
    return found


def mount_login_cookie_route(app: Any) -> bool:
    """Insert POST /ffis/login-cookie ahead of any catch-all route."""
    from starlette.routing import Route

    for route in app.routes:
        if getattr(route, "path", None) == LOGIN_COOKIE_PATH:
            return True
    app.routes.insert(
        0,
        Route(LOGIN_COOKIE_PATH, login_cookie_endpoint, methods=["POST"], name="ffis-login-cookie"),
    )
    return True


def mount_on_running_server() -> bool:
    """Mount the cookie route on the running Streamlit 1.59 server, once."""
    global _route_mounted, _mount_warned
    if _route_mounted:
        return True
    apps = [app for app in find_streamlit_apps() if _is_streamlit_app(app)]
    if not apps:
        if not _mount_warned:
            _mount_warned = True
            _LOG.warning(
                "FFIS login cookie route is not mounted; reload will not keep the session."
            )
        return False
    for app in apps:
        mount_login_cookie_route(app)
    _route_mounted = True
    return True


def _apply_cookie(response: Any, claim: Mapping[str, Any]) -> None:
    name = claim["name"]
    if claim["action"] == "set":
        response.set_cookie(
            key=name,
            value=claim["value"],
            max_age=int(claim["max_age"]),
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return
    response.delete_cookie(
        key=name,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


async def login_cookie_endpoint(request: Any) -> Any:
    """Redeem a nonce. The client does not get to choose the cookie value."""
    from starlette.responses import Response

    if request.method != "POST":
        return Response(status_code=405)
    body = await request.body()
    if len(body) > 512:
        return Response(status_code=400)
    try:
        payload = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeError, json.JSONDecodeError):
        return Response(status_code=400)
    if not isinstance(payload, dict):
        return Response(status_code=400)
    claim = redeem_claim(payload.get("nonce"))
    if claim is None:
        return Response(status_code=404)
    response = Response(status_code=204)
    _apply_cookie(response, claim)
    return response
