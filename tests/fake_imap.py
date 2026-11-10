"""
tests/fake_imap.py — localhost-only fake IMAP4rev1 subset server (SPEC section 8).

asyncio server on 127.0.0.1:<port> (0 = ephemeral), plaintext with optional
STARTTLS (self-signed cert supplied by the test). Never talks to anything
except the client that connects to it.

Commands: CAPABILITY, NOOP, STARTTLS, LOGIN, SELECT/EXAMINE (UIDVALIDITY,
UIDNEXT), SEARCH / UID SEARCH (ALL, UNSEEN, SEEN, SINCE, UID <set>, SUBJECT,
including the Gmail ``n:*`` quirk), FETCH / UID FETCH (UID, FLAGS,
INTERNALDATE, BODYSTRUCTURE, BODY.PEEK[...], BODY[...], RFC822 — the non-PEEK
forms set \\Seen), STORE / UID STORE, CLOSE, LOGOUT.

Modes / controls: blackhole, drip(seconds), hang_after(cmd), reject_login,
set_uidvalidity(v), add_message(...), mark_seen_external(uid), stop()/start().

CLI (used by the T7 docker sidecar):
    python tests/fake_imap.py --port 1143 --mode blackhole
    python tests/fake_imap.py --port 1143 --seed 3 --events /out/events.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import email
import json
import re
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Dict, List, Optional

FAKE_USER = "ffis-fake-ingest@example.invalid"
FAKE_PASSWORD = "FAKE-imap-pw-7f3c9e1d-NOT-REAL"

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imap_date(ts: float) -> str:
    t = time.gmtime(ts)
    return f"{t.tm_mday:02d}-{_MONTHS[t.tm_mon - 1]}-{t.tm_year} {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} +0000"


def make_message(subject: str = "data", sender: str = "sender@example.invalid",
                 message_id: Optional[str] = None,
                 attachments: Optional[List[tuple]] = None,
                 body: str = "see attached") -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = FAKE_USER
    m["Subject"] = subject
    m["Message-ID"] = message_id if message_id is not None else make_msgid(domain="example.invalid")
    m.set_content(body)
    for name, data in attachments or []:
        m.add_attachment(data, maintype="text", subtype="csv", filename=name)
    return m.as_bytes()


def csv_attachment(name: str, rows: int = 2) -> tuple:
    lines = ["Name,Industry"] + [f"{name.split('.')[0]}-{i},Tech" for i in range(rows)]
    return (name, ("\n".join(lines) + "\n").encode())


# ── tiny IMAP token parser (for client command args) ─────────────────────
def _tokenize(s: str) -> List[Any]:
    out: List[Any] = []
    stack = [out]
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == " ":
            i += 1
        elif c == "(":
            new: List[Any] = []
            stack[-1].append(new)
            stack.append(new)
            i += 1
        elif c == ")":
            stack.pop()
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(s[j])
                j += 1
            stack[-1].append("".join(buf))
            i = j + 1
        else:
            j = i
            depth = 0
            while j < n and (depth > 0 or s[j] not in " ()"):
                if s[j] == "[":
                    depth += 1
                elif s[j] == "]":
                    depth -= 1
                j += 1
            stack[-1].append(s[i:j])
            i = j
    return out


def _q(s: Optional[str]) -> str:
    if s is None:
        return "NIL"
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _bodystructure(part: email.message.Message) -> str:
    if part.is_multipart():
        subs = "".join(_bodystructure(p) for p in part.get_payload())
        b = part.get_boundary() or "b"
        return f'({subs} {_q(part.get_content_subtype().upper())} ("BOUNDARY" {_q(b)}) NIL NIL NIL)'
    maintype = part.get_content_maintype().upper()
    subtype = part.get_content_subtype().upper()
    params = [f"{_q(k.upper())} {_q(v)}" for k, v in part.get_params()[1:]] if part.get_params() else []
    pstr = "(" + " ".join(params) + ")" if params else "NIL"
    enc = (part.get("Content-Transfer-Encoding") or "7BIT").upper()
    payload = part.get_payload(decode=False) or ""
    size = len(payload.encode() if isinstance(payload, str) else payload)
    fname = part.get_filename()
    disp = part.get_content_disposition()
    dstr = f'({_q(disp.upper())} ("FILENAME" {_q(fname)}))' if disp and fname else (
        f"({_q(disp.upper())} NIL)" if disp else "NIL")
    lines = f" {str(payload).count(chr(10))}" if maintype == "TEXT" else ""
    return f"({_q(maintype)} {_q(subtype)} {pstr} NIL NIL {_q(enc)} {size}{lines} NIL {dstr} NIL NIL)"


class FakeIMAP:
    def __init__(self, port: int = 0, user: str = FAKE_USER, password: str = FAKE_PASSWORD,
                 ssl_context: Optional[ssl.SSLContext] = None, gmail_quirk: bool = True,
                 events_path: Optional[str] = None):
        self.host = "127.0.0.1"
        self.port = port
        self.user, self.password = user, password
        self.ssl_context = ssl_context
        self.gmail_quirk = gmail_quirk
        self.events_path = events_path
        self.lock = threading.RLock()
        self.uidvalidity = 1000
        self.next_uid = 1
        self.messages: List[Dict[str, Any]] = []
        self.mode = "normal"
        self.drip_s = 1.0
        self.hang_cmd: Optional[str] = None
        self.reject = False
        self.events: List[Dict[str, Any]] = []
        self.connections = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._writers: set = set()
        self._thread: Optional[threading.Thread] = None

    # ── controls ──
    def blackhole(self): self.mode = "blackhole"
    def drip(self, seconds: float = 1.0): self.mode, self.drip_s = "drip", seconds
    def hang_after(self, cmd: str): self.hang_cmd = cmd.upper()
    def reject_login(self, on: bool = True): self.reject = on
    def normal(self): self.mode, self.hang_cmd, self.reject = "normal", None, False

    def set_uidvalidity(self, v: int, renumber_base: Optional[int] = None):
        with self.lock:
            self.uidvalidity = v
            base = renumber_base if renumber_base is not None else self.next_uid + 100
            for i, m in enumerate(self.messages):
                m["uid"] = base + i
            self.next_uid = base + len(self.messages)
            self._event("set_uidvalidity", uidvalidity=v)

    def add_message(self, raw: bytes, seen: bool = False, internaldate: Optional[float] = None) -> int:
        with self.lock:
            uid = self.next_uid
            self.next_uid += 1
            self.messages.append({"uid": uid, "raw": raw,
                                  "flags": {"\\Seen"} if seen else set(),
                                  "internaldate": internaldate or time.time()})
            self._event("add", uid=uid)
            return uid

    def mark_seen_external(self, uid: int):
        with self.lock:
            self._by_uid(uid)["flags"].add("\\Seen")
            self._event("seen_external", uid=uid)

    def is_seen(self, uid: int) -> bool:
        with self.lock:
            return "\\Seen" in self._by_uid(uid)["flags"]

    def uids(self) -> List[int]:
        with self.lock:
            return [m["uid"] for m in self.messages]

    def _by_uid(self, uid: int) -> Dict[str, Any]:
        for m in self.messages:
            if m["uid"] == uid:
                return m
        raise KeyError(uid)

    def _event(self, kind: str, **kw):
        ev = {"t": time.time(), "kind": kind, **kw}
        self.events.append(ev)
        if self.events_path:
            with open(self.events_path, "a") as f:
                f.write(json.dumps(ev) + "\n")

    # ── lifecycle ──
    def start(self) -> "FakeIMAP":
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def boot():
                self._server = await asyncio.start_server(self._handle, self.host, self.port,
                                                          reuse_address=True)
                self.port = self._server.sockets[0].getsockname()[1]
                ready.set()
            self._loop.run_until_complete(boot())
            self._loop.run_forever()
            self._loop.close()
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        ready.wait(10)
        return self

    def stop(self):
        if not self._loop:
            return

        async def shut():
            self._server.close()
            for w in list(self._writers):
                try:
                    w.transport.abort()
                except Exception:
                    pass
            self._writers.clear()
        fut = asyncio.run_coroutine_threadsafe(shut(), self._loop)
        try:
            fut.result(5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop = None

    # ── protocol ──
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._writers.add(writer)
        self.connections += 1
        self._event("connect")
        try:
            if self.mode == "blackhole":
                await reader.read()          # accept, never speak
                return
            if self.mode == "drip":
                while True:                  # 1 byte every drip_s, never a full line
                    writer.write(b"*")
                    await writer.drain()
                    await asyncio.sleep(self.drip_s)
            writer.write(b"* OK [CAPABILITY IMAP4rev1 STARTTLS AUTH=PLAIN] fake ready\r\n")
            await writer.drain()
            selected = False
            while True:
                line = await reader.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                # handle a client literal (LOGIN with {n}) — imaplib only uses it for odd passwords
                m = re.search(r"\{(\d+)\}$", text)
                while m:
                    writer.write(b"+ go ahead\r\n")
                    await writer.drain()
                    lit = await reader.readexactly(int(m.group(1)))
                    rest = (await reader.readline()).decode().rstrip("\r\n")
                    text = text[:m.start()] + _q(lit.decode()) + rest
                    m = re.search(r"\{(\d+)\}$", text)
                parts = text.split(" ", 2)
                tag = parts[0]
                cmd = parts[1].upper() if len(parts) > 1 else ""
                args = parts[2] if len(parts) > 2 else ""
                uid_mode = False
                if cmd == "UID":
                    uid_mode = True
                    sub = args.split(" ", 1)
                    cmd, args = sub[0].upper(), (sub[1] if len(sub) > 1 else "")
                self._event("cmd", cmd=("UID " if uid_mode else "") + cmd)
                if self.hang_cmd and cmd == self.hang_cmd:
                    await reader.read()      # hang forever on this command
                    return
                out = self._dispatch(tag, cmd, args, uid_mode, selected)
                if cmd == "STARTTLS" and self.ssl_context is not None:
                    writer.write(f"{tag} OK begin TLS\r\n".encode())
                    await writer.drain()
                    await writer.start_tls(self.ssl_context)
                    continue
                if cmd in ("SELECT", "EXAMINE") and out.endswith(b"completed\r\n"):
                    selected = True
                writer.write(out)
                await writer.drain()
                if cmd == "LOGOUT":
                    return
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            return
        except asyncio.CancelledError:
            return
        finally:
            self._writers.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    def _dispatch(self, tag: str, cmd: str, args: str, uid_mode: bool, selected: bool) -> bytes:
        ok = f"{tag} OK {cmd} completed\r\n".encode()
        if cmd == "CAPABILITY":
            return b"* CAPABILITY IMAP4rev1 STARTTLS AUTH=PLAIN UIDPLUS\r\n" + ok
        if cmd == "NOOP":
            return ok
        if cmd == "STARTTLS":
            return f"{tag} NO STARTTLS unavailable\r\n".encode()
        if cmd == "LOGIN":
            toks = _tokenize(args)
            good = (len(toks) >= 2 and toks[0] == self.user and toks[1] == self.password)
            self._event("login", ok=bool(good and not self.reject))
            if self.reject or not good:
                return f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials\r\n".encode()
            return ok
        if cmd in ("SELECT", "EXAMINE"):
            with self.lock:
                n = len(self.messages)
                unseen = sum(1 for m in self.messages if "\\Seen" not in m["flags"])
                return (f"* {n} EXISTS\r\n* 0 RECENT\r\n* OK [UNSEEN {unseen}]\r\n"
                        f"* OK [UIDVALIDITY {self.uidvalidity}] UIDs valid\r\n"
                        f"* OK [UIDNEXT {self.next_uid}] Predicted next UID\r\n"
                        f"* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)\r\n"
                        f"{tag} OK [READ-WRITE] {cmd} completed\r\n").encode()
        if cmd == "SEARCH":
            with self.lock:
                hits = self._search(_tokenize(args))
                vals = [m["uid"] if uid_mode else self.messages.index(m) + 1 for m in hits]
                self._event("search", uid_mode=uid_mode, args=args, result=vals)
                return (("* SEARCH " + " ".join(map(str, vals))).rstrip() + "\r\n").encode() + ok
        if cmd == "FETCH":
            seqset, items = args.split(" ", 1)
            with self.lock:
                msgs = self._resolve_set(seqset, uid_mode)
                out = b""
                for m in msgs:
                    out += self._fetch_one(m, items, uid_mode)
                return out + ok
        if cmd == "STORE":
            seqset, rest = args.split(" ", 1)
            op, flags = rest.split(" ", 1)
            fl = set(f for f in flags.strip("()").split() if f)
            with self.lock:
                out = b""
                for m in self._resolve_set(seqset, uid_mode):
                    if op.upper().startswith("+"):
                        m["flags"] |= fl
                    elif op.upper().startswith("-"):
                        m["flags"] -= fl
                    else:
                        m["flags"] = set(fl)
                    self._event("store", uid=m["uid"], op=op, flags=sorted(fl))
                    if "SILENT" not in op.upper():
                        seq = self.messages.index(m) + 1
                        u = f"UID {m['uid']} " if uid_mode else ""
                        out += f"* {seq} FETCH ({u}FLAGS ({' '.join(sorted(m['flags']))}))\r\n".encode()
                return out + ok
        if cmd in ("CLOSE", "EXPUNGE", "CHECK"):
            return ok
        if cmd == "LOGOUT":
            return b"* BYE fake logging out\r\n" + ok
        return f"{tag} BAD unknown command\r\n".encode()

    def _resolve_set(self, s: str, uid_mode: bool) -> List[Dict[str, Any]]:
        res: List[Dict[str, Any]] = []
        if not self.messages:
            return res
        max_key = max(m["uid"] for m in self.messages) if uid_mode else len(self.messages)
        keys = set()
        for piece in s.split(","):
            if ":" in piece:
                a, b = piece.split(":")
                lo = int(a) if a != "*" else max_key
                hi = int(b) if b != "*" else max_key
                if b == "*" and lo > hi and self.gmail_quirk:
                    lo = hi = max_key        # Gmail quirk: n:* with n>max returns max
                lo, hi = min(lo, hi), max(lo, hi)
                keys.update(range(lo, hi + 1))
            else:
                keys.add(int(piece) if piece != "*" else max_key)
        for i, m in enumerate(self.messages):
            k = m["uid"] if uid_mode else i + 1
            if k in keys:
                res.append(m)
        return res

    def _search(self, toks: List[Any]) -> List[Dict[str, Any]]:
        def flatten(t):
            for x in t:
                if isinstance(x, list):
                    yield from flatten(x)
                else:
                    yield x
        flat = list(flatten(toks))
        hits = list(self.messages)
        i = 0
        while i < len(flat):
            k = str(flat[i]).upper()
            if k in ("ALL", "CHARSET"):
                i += 2 if k == "CHARSET" else 1
                continue
            if k == "UNSEEN":
                hits = [m for m in hits if "\\Seen" not in m["flags"]]
            elif k == "SEEN":
                hits = [m for m in hits if "\\Seen" in m["flags"]]
            elif k == "SINCE":
                d = time.mktime(time.strptime(flat[i + 1], "%d-%b-%Y")) - time.timezone
                hits = [m for m in hits if m["internaldate"] >= d]
                i += 1
            elif k == "SUBJECT":
                needle = str(flat[i + 1]).lower()
                hits = [m for m in hits if needle in (email.message_from_bytes(m["raw"]).get("Subject", "").lower())]
                i += 1
            elif k == "UID":
                allowed = {id(m) for m in self._resolve_set(flat[i + 1], True)}
                hits = [m for m in hits if id(m) in allowed]
                i += 1
            i += 1
        return hits

    def _fetch_one(self, m: Dict[str, Any], items: str, uid_mode: bool) -> bytes:
        seq = self.messages.index(m) + 1
        up = items.upper()
        parts: List[bytes] = []
        literals: List[tuple] = []
        msg = email.message_from_bytes(m["raw"])
        if uid_mode or re.search(r"\bUID\b", up):
            parts.append(f"UID {m['uid']}".encode())
        sets_seen = False
        if "INTERNALDATE" in up:
            parts.append(f'INTERNALDATE "{imap_date(m["internaldate"])}"'.encode())
        if "BODYSTRUCTURE" in up:
            parts.append(b"BODYSTRUCTURE " + _bodystructure(msg).encode())
        hm = re.search(r"BODY(\.PEEK)?\[HEADER\.FIELDS \(([^)]*)\)\]", up)
        if hm:
            names = hm.group(2).split()
            hdr = "".join(f"{h}: {msg.get(h)}\r\n" for h in
                          [n.title() if n != "MESSAGE-ID" else "Message-ID" for n in names]
                          if msg.get(h) is not None) + "\r\n"
            literals.append((f"BODY[HEADER.FIELDS ({hm.group(2)})]", hdr.encode()))
            if not hm.group(1):
                sets_seen = True
        if re.search(r"BODY\.PEEK\[\]", up):
            literals.append(("BODY[]", m["raw"]))
        elif re.search(r"(?<!\.PEEK)BODY\[\]", up):
            literals.append(("BODY[]", m["raw"]))
            sets_seen = True
        if re.search(r"\bRFC822\b(?!\.)", up):
            literals.append(("RFC822", m["raw"]))
            sets_seen = True
        if sets_seen and "\\Seen" not in m["flags"]:
            m["flags"].add("\\Seen")
            self._event("implicit_seen", uid=m["uid"])
        if "FLAGS" in re.sub(r"HEADER\.FIELDS", "", up):
            parts.append(f"FLAGS ({' '.join(sorted(m['flags']))})".encode())
        self._event("fetch", uid=m["uid"], items=items)
        head = f"* {seq} FETCH (".encode() + b" ".join(parts)
        if not literals:
            return head + b")\r\n"
        out = head
        for i, (name, data) in enumerate(literals):
            out += (b" " if (parts or i) else b"") + f"{name} {{{len(data)}}}\r\n".encode() + data
        return out + b")\r\n"


def make_selfsigned_context(tmpdir: str) -> ssl.SSLContext:
    """Self-signed server cert for the fakes' STARTTLS (test-only, localhost)."""
    import datetime
    import os
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .sign(key, hashes.SHA256()))
    cp, kp = os.path.join(tmpdir, "fake.crt"), os.path.join(tmpdir, "fake.key")
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cp, kp)
    return ctx


def _cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1143)
    ap.add_argument("--mode", default="normal", choices=["normal", "blackhole", "drip"])
    ap.add_argument("--seed", type=int, default=0, help="seed N messages with CSV attachments")
    ap.add_argument("--events", default=None)
    a = ap.parse_args()
    f = FakeIMAP(port=a.port, events_path=a.events)
    f.host = a.host
    for i in range(a.seed):
        f.add_message(make_message(subject=f"seed {i}", message_id=f"<seed-{i}@example.invalid>",
                                   attachments=[csv_attachment(f"seed{i}.csv")]))
    if a.mode == "blackhole":
        f.blackhole()
    elif a.mode == "drip":
        f.drip(1.0)
    f.start()
    print(f"fake_imap listening {f.host}:{f.port} mode={a.mode} seed={a.seed}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        f.stop()


if __name__ == "__main__":
    _cli()
