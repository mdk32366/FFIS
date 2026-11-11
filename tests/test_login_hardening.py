"""Login cap and session-cookie flags.

Manual browser reload (not run here; no deploy, no production login):

1. Open https://mdkappsvr.tailea9f4f.ts.net:8443 over HTTPS. Do not use plain HTTP.
2. Sign in with the normal account.
3. DevTools → Application → Cookies: the login cookie (normally ffis_auth)
   shows Secure, HttpOnly, and SameSite=Lax.
4. Reload. The sidebar still shows the signed-in user, not the login form.
5. Log out, reload, and confirm the login form is back.
6. Submit a wrong password 5 times. The form is replaced by the lockout
   message. Restart the app process to clear it; do not restart ingest.

The automated reload test checks the same contract the authenticator uses on
the next request: the Set-Cookie value is the JWT CookieModel.get_cookie
accepts (username + exp_date, HS256).
"""

from __future__ import annotations

import time
from http.cookies import SimpleCookie

import bcrypt
import jwt
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import ffis_login
from ffis_login import (
    CLEAR_NONCE_KEY,
    DEFAULT_MAX_FAILURES,
    ENV_MAX_FAILURES,
    LOGIN_COOKIE_PATH,
    LOGIN_LOCKED_KEY,
    SET_NONCE_KEY,
    AttemptTracker,
    apply_login_attempt,
    claim_set,
    configured_max_failures,
    find_streamlit_apps,
    get_tracker,
    install_login_hardening,
    issue_clear_cookie,
    issue_set_cookie,
    mount_login_cookie_route,
)


@pytest.fixture(autouse=True)
def _reset_login_state():
    ffis_login.reset_state_for_tests()
    ffis_login._route_mounted = False
    ffis_login._mount_warned = False
    yield
    ffis_login.reset_state_for_tests()


def _password_checker(password: str, hashed: str):
    session_box = {}

    def original(username, given, *args, **kwargs):
        session_box["calls"] = session_box.get("calls", 0) + 1
        session_box["last_password"] = given
        ok = (
            username == "alice"
            and given is not None
            and bcrypt.checkpw(given.encode(), hashed.encode())
        )
        return ok

    session_box["original"] = original
    return session_box


def _cookie_app() -> Starlette:
    def health(_request):
        return PlainTextResponse("ok")

    def catchall(_request):
        return PlainTextResponse("catchall")

    return Starlette(
        routes=[
            Route("/_stcore/health", health),
            Route("/{path:path}", catchall),
        ]
    )


def _flags(set_cookie: str, name: str = "ffis_auth") -> dict:
    jar = SimpleCookie()
    jar.load(set_cookie)
    morsel = jar[name]
    return {
        "value": morsel.value,
        "httponly": bool(morsel["httponly"]),
        "secure": bool(morsel["secure"]),
        "samesite": str(morsel["samesite"]),
        "path": morsel["path"],
        "max-age": morsel["max-age"],
    }


def test_cap_defaults_to_five_and_rejects_disable(monkeypatch):
    monkeypatch.delenv(ENV_MAX_FAILURES, raising=False)
    assert configured_max_failures() == DEFAULT_MAX_FAILURES == 5
    monkeypatch.setenv(ENV_MAX_FAILURES, "nope")
    assert configured_max_failures() == 5
    monkeypatch.setenv(ENV_MAX_FAILURES, "0")
    assert configured_max_failures() == 5
    monkeypatch.setenv(ENV_MAX_FAILURES, "3")
    assert configured_max_failures() == 3


def test_lockout_refuses_further_password_checks(monkeypatch):
    monkeypatch.setenv(ENV_MAX_FAILURES, "5")
    hashed = bcrypt.hashpw(b"right-password", bcrypt.gensalt()).decode()
    box = _password_checker("right-password", hashed)
    tracker = AttemptTracker()
    session = {}
    for _ in range(5):
        result = apply_login_attempt(
            tracker, "alice", "wrong-password", box["original"], session, (), {}
        )
        assert result is False
    assert box["calls"] == 5
    assert tracker.is_locked("alice")
    assert session[LOGIN_LOCKED_KEY] is True

    result = apply_login_attempt(
        tracker, "alice", "right-password", box["original"], session, (), {}
    )
    assert result is False
    assert box["calls"] == 5
    assert box["last_password"] == "wrong-password"


def test_successful_login_resets_the_cap(monkeypatch):
    monkeypatch.setenv(ENV_MAX_FAILURES, "5")
    hashed = bcrypt.hashpw(b"right-password", bcrypt.gensalt()).decode()
    box = _password_checker("right-password", hashed)
    tracker = AttemptTracker()
    session = {}
    for _ in range(4):
        assert (
            apply_login_attempt(
                tracker, "alice", "wrong-password", box["original"], session, (), {}
            )
            is False
        )
    assert (
        apply_login_attempt(
            tracker, "alice", "right-password", box["original"], session, (), {}
        )
        is True
    )
    assert tracker.failure_count("alice") == 0
    assert session.get(LOGIN_LOCKED_KEY) is False
    for _ in range(4):
        apply_login_attempt(
            tracker, "alice", "wrong-password", box["original"], session, (), {}
        )
    assert tracker.is_locked("alice") is False


def test_lockout_is_per_username(monkeypatch):
    monkeypatch.setenv(ENV_MAX_FAILURES, "2")
    tracker = AttemptTracker()
    session = {}
    calls = {"bob": 0}

    def original(username, password, *args, **kwargs):
        if username == "bob":
            calls["bob"] += 1
        return False

    apply_login_attempt(tracker, "alice", "x", original, session, (), {})
    apply_login_attempt(tracker, "alice", "x", original, session, (), {})
    assert tracker.is_locked("alice")
    assert apply_login_attempt(tracker, "alice", "again", original, session, (), {}) is False
    assert apply_login_attempt(tracker, "bob", "y", original, session, (), {}) is False
    assert calls["bob"] == 1
    assert tracker.is_locked("bob") is False


def test_reload_reauth_is_not_counted_or_blocked(monkeypatch):
    monkeypatch.setenv(ENV_MAX_FAILURES, "1")
    tracker = AttemptTracker()
    tracker.record_failure("alice")
    assert tracker.is_locked("alice")
    seen = {}

    def original(username, password, *args, **kwargs):
        seen["username"] = username
        seen["token"] = kwargs.get("token")
        return None

    session = {}
    apply_login_attempt(
        tracker,
        None,
        None,
        original,
        session,
        (),
        {"token": {"username": "alice"}},
    )
    assert seen["token"]["username"] == "alice"
    assert LOGIN_LOCKED_KEY not in session
    assert tracker.failure_count("alice") == 1


def test_failures_do_not_touch_the_credential_dict(monkeypatch):
    monkeypatch.setenv(ENV_MAX_FAILURES, "5")
    credentials = {"usernames": {"alice": {"name": "Alice", "password": "hash"}}}
    snapshot = {"usernames": {"alice": {"name": "Alice", "password": "hash"}}}
    tracker = AttemptTracker()
    session = {}

    def original(username, password, *args, **kwargs):
        return False

    for _ in range(5):
        apply_login_attempt(tracker, "alice", "nope", original, session, (), {})
    assert credentials == snapshot


def test_install_counts_a_failure_once_and_skips_js_cookie_write(monkeypatch):
    monkeypatch.setenv(ENV_MAX_FAILURES, "5")
    session = {}
    monkeypatch.setattr(ffis_login, "_session_state", lambda: session)
    monkeypatch.setattr(ffis_login, "mount_on_running_server", lambda: True)
    emitted = []
    monkeypatch.setattr(ffis_login, "_emit_claim", lambda nonce: emitted.append(nonce))
    js_writes = []

    class Manager:
        def set(self, *args, **kwargs):
            js_writes.append("set")

        def delete(self, *args, **kwargs):
            js_writes.append("delete")

    class AuthModel:
        def login(self, username=None, password=None, *args, **kwargs):
            return False

    class CookieModel:
        def __init__(self):
            self.cookie_manager = Manager()
            self.cookie_name = "ffis_auth"
            self.cookie_expiry_days = 7
            self.exp_date = None

        def _set_exp_date(self):
            return time.time() + 3600

        def _token_encode(self):
            return jwt.encode(
                {"username": "alice", "exp_date": self.exp_date},
                "test-cookie-key",
                algorithm="HS256",
            )

    class Authenticator:
        def __init__(self):
            self.authentication_controller = type("C", (), {})()
            self.authentication_controller.authentication_model = AuthModel()
            self.cookie_controller = type("K", (), {})()
            self.cookie_controller.cookie_model = CookieModel()

    auth = Authenticator()
    install_login_hardening(auth)
    install_login_hardening(auth)
    model = auth.authentication_controller.authentication_model
    assert model.login("alice", "bad") is False
    assert model.login("alice", "bad") is False
    assert get_tracker().failure_count("alice") == 2

    auth.cookie_controller.cookie_model.set_cookie()
    assert js_writes == []
    assert session.get(SET_NONCE_KEY)

    auth.cookie_controller.cookie_model.delete_cookie()
    assert js_writes == []
    assert session.get(SET_NONCE_KEY) is None
    assert session.get(CLEAR_NONCE_KEY) in emitted


def test_cookie_flags_and_reload_restores_user():
    """The cookie the server sets is the one a reload would present."""
    exp = time.time() + 7 * 86400

    class CookieModel:
        cookie_name = "ffis_auth"
        cookie_expiry_days = 7
        exp_date = None

        def _set_exp_date(self):
            return exp

        def _token_encode(self):
            return jwt.encode(
                {"username": "alice", "exp_date": self.exp_date},
                "test-cookie-key",
                algorithm="HS256",
            )

    session = {}
    issue_set_cookie(CookieModel(), session)
    app = _cookie_app()
    assert mount_login_cookie_route(app)
    assert app.routes[0].path == LOGIN_COOKIE_PATH

    with TestClient(app) as client:
        stolen = client.post(
            LOGIN_COOKIE_PATH,
            json={"nonce": session[SET_NONCE_KEY], "value": "evil", "name": "other"},
        )
        assert stolen.status_code == 204
        assert stolen.text == ""
        flags = _flags(stolen.headers["set-cookie"])
        replay = client.post(
            LOGIN_COOKIE_PATH, json={"nonce": session[SET_NONCE_KEY]}
        )
        missing = client.post(LOGIN_COOKIE_PATH, json={})

    assert stolen.text != "catchall"
    assert flags["httponly"] is True
    assert flags["secure"] is True
    assert flags["samesite"].lower() == "lax"
    assert flags["path"] == "/"
    assert int(flags["max-age"]) == 7 * 86400
    assert flags["value"] != "evil"
    restored = jwt.decode(flags["value"], "test-cookie-key", algorithms=["HS256"])
    assert restored["username"] == "alice"
    assert restored["exp_date"] > time.time()
    assert replay.status_code == 404
    assert "set-cookie" not in {key.lower() for key in replay.headers.keys()}
    assert missing.status_code == 404


def test_logout_cookie_is_cleared_with_the_same_flags():
    class CookieModel:
        cookie_name = "ffis_auth"

    session = {}
    nonce = issue_clear_cookie(CookieModel(), session)
    app = _cookie_app()
    mount_login_cookie_route(app)
    with TestClient(app) as client:
        resp = client.post(LOGIN_COOKIE_PATH, json={"nonce": nonce})
    flags = _flags(resp.headers["set-cookie"])
    assert resp.status_code == 204
    assert flags["httponly"] is True
    assert flags["secure"] is True
    assert flags["samesite"].lower() == "lax"
    assert flags["max-age"] in {"0", 0}


def test_route_mounts_on_a_live_streamlit_shaped_app():
    app = _cookie_app()
    found = find_streamlit_apps()
    assert app in found
    nonce = claim_set("ffis_auth", "aaa.bbb.ccc", 60)
    ffis_login.mount_on_running_server()
    with TestClient(app) as client:
        resp = client.post(LOGIN_COOKIE_PATH, json={"nonce": nonce})
    assert resp.status_code == 204
    assert _flags(resp.headers["set-cookie"])["value"] == "aaa.bbb.ccc"


def test_app_wires_cap_and_cookie_claim():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "flat_file_scrubber.py").read_text(
        encoding="utf-8"
    )
    assert "install_login_hardening(authenticator)" in source
    assert "emit_pending_login_cookie()" in source
    assert "LOGIN_LOCKED_KEY" in source


def test_claim_component_posts_nonce_and_does_not_write_document_cookie():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "ffis_login_claim" / "frontend"
    script = (root / "main.js").read_text(encoding="utf-8")
    page = (root / "index.html").read_text(encoding="utf-8")
    assert LOGIN_COOKIE_PATH in script
    assert "document.cookie" not in script
    assert "document.cookie" not in page
    assert "nonce" not in page
