"""
tests/fake_smtp.py — localhost-only fake SMTP server (in-process, asyncio).

Supports EHLO/HELO, STARTTLS (self-signed cert supplied by the test), AUTH
PLAIN/LOGIN, MAIL, RCPT, DATA, RSET, NOOP, QUIT. Records each accepted
message's recipients, Subject and In-Reply-To. ``blackhole()`` accepts the TCP
connection and never sends a greeting (T15). Nothing is ever relayed.
"""
from __future__ import annotations

import asyncio
import base64
import email
import ssl
import threading
import time
from typing import Any, Dict, List, Optional


class FakeSMTP:
    def __init__(self, user: str, password: str, ssl_context: Optional[ssl.SSLContext] = None,
                 port: int = 0):
        self.host, self.port = "127.0.0.1", port
        self.user, self.password = user, password
        self.ssl_context = ssl_context
        self.mode = "normal"
        self.messages: List[Dict[str, Any]] = []
        self.auth_ok = 0
        self.auth_fail = 0
        self.connections = 0
        self._loop = None
        self._server = None
        self._thread = None

    def blackhole(self): self.mode = "blackhole"
    def normal(self): self.mode = "normal"

    def start(self) -> "FakeSMTP":
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()

            async def boot():
                self._server = await asyncio.start_server(self._handle, self.host, self.port)
                self.port = self._server.sockets[0].getsockname()[1]
                ready.set()
            self._loop.run_until_complete(boot())
            self._loop.run_forever()
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        ready.wait(10)
        return self

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._server.close)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)
            self._loop = None

    def replies(self, kind: str = "result") -> List[Dict[str, Any]]:
        if kind == "alert":
            return [m for m in self.messages if m["subject"].startswith("[FFIS ingest]")]
        return [m for m in self.messages if not m["subject"].startswith("[FFIS ingest]")]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.connections += 1
        try:
            if self.mode == "blackhole":
                await reader.read()
                return

            async def send(line: str):
                writer.write((line + "\r\n").encode())
                await writer.drain()
            await send("220 fake-smtp.localhost ESMTP")
            tls = False
            mail_from, rcpts = None, []
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                verb = line.split(" ", 1)[0].upper()
                arg = line.split(" ", 1)[1] if " " in line else ""
                if verb in ("EHLO", "HELO"):
                    caps = ["250-fake-smtp.localhost"]
                    if self.ssl_context is not None and not tls:
                        caps.append("250-STARTTLS")
                    caps.append("250 AUTH PLAIN LOGIN")
                    for c in caps:
                        await send(c)
                elif verb == "STARTTLS" and self.ssl_context is not None:
                    await send("220 ready for TLS")
                    await writer.start_tls(self.ssl_context)
                    tls = True
                elif verb == "AUTH":
                    parts = arg.split()
                    mech = parts[0].upper()
                    if mech == "PLAIN":
                        blob = parts[1] if len(parts) > 1 else None
                        if blob is None:
                            await send("334 ")
                            blob = (await reader.readline()).decode().strip()
                        dec = base64.b64decode(blob).split(b"\0")
                        u, pw = dec[-2].decode(), dec[-1].decode()
                    else:  # LOGIN
                        if len(parts) > 1:
                            u = base64.b64decode(parts[1]).decode()
                        else:
                            await send("334 VXNlcm5hbWU6")
                            u = base64.b64decode((await reader.readline()).strip()).decode()
                        await send("334 UGFzc3dvcmQ6")
                        pw = base64.b64decode((await reader.readline()).strip()).decode()
                    if u == self.user and pw == self.password:
                        self.auth_ok += 1
                        await send("235 2.7.0 Authentication successful")
                    else:
                        self.auth_fail += 1
                        await send("535 5.7.8 Authentication failed")
                elif verb == "MAIL":
                    mail_from, rcpts = arg, []
                    await send("250 OK")
                elif verb == "RCPT":
                    rcpts.append(arg.split(":", 1)[1].strip().strip("<>"))
                    await send("250 OK")
                elif verb == "DATA":
                    await send("354 end with .")
                    buf = b""
                    while True:
                        l2 = await reader.readline()
                        if l2 in (b".\r\n", b".\n") or not l2:
                            break
                        buf += l2[1:] if l2.startswith(b"..") else l2
                    msg = email.message_from_bytes(buf)
                    self.messages.append({"t": time.time(), "rcpts": rcpts,
                                          "subject": str(msg.get("Subject", "")),
                                          "in_reply_to": msg.get("In-Reply-To", ""),
                                          "body_len": len(buf)})
                    await send("250 OK queued")
                elif verb == "QUIT":
                    await send("221 bye")
                    return
                else:
                    await send("250 OK")
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            return
        finally:
            try:
                writer.close()
            except Exception:
                pass
