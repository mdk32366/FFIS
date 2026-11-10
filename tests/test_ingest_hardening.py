"""
tests/test_ingest_hardening.py — SPEC ffis-ingest-hardening@0.0 section 8 (T1–T13, T15;
T14 wraps the existing test_pipeline.py / test_export.py; T7 is tests/docker_t7.sh).

Localhost fakes only (tests/fake_imap.py, tests/fake_smtp.py), fake credentials only.
Env:
  FFIS_TEST_MODE      watermark (default) | legacy_unseen   (rollback proof for T2–T6, T14)
  FFIS_EVIDENCE_DIR   where per-test transcripts are written (default tests/_evidence)
Run:  python -m pytest -v tests/test_ingest_hardening.py
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ffis_test_util import (BASE_BLOB, FAKE_ALERT_TO, FAKE_SMTP_PASSWORD,  # noqa: E402
                            FAKE_SMTP_USER, REPO, base_module_dir, clean_env)
from fake_imap import FakeIMAP, csv_attachment, make_message, make_selfsigned_context  # noqa: E402
from fake_smtp import FakeSMTP  # noqa: E402

MODE = os.environ.get("FFIS_TEST_MODE", "watermark")
EVID = os.environ.get("FFIS_EVIDENCE_DIR", os.path.join(REPO, "tests", "_evidence"))
os.makedirs(EVID, exist_ok=True)
RUNNER = os.path.join(REPO, "tests", "_runner.py")
legacy_only_skip = pytest.mark.skipif(MODE != "watermark",
                                      reason="watermark-only test (SPEC: legacy runs T2–T7, T14)")


def ev(name: str, text: str) -> None:
    path = os.path.join(EVID, f"{name}-{MODE}.txt")
    with open(path, "a") as f:
        f.write(text.rstrip("\n") + "\n")


def ev_header(name: str, title: str) -> None:
    path = os.path.join(EVID, f"{name}-{MODE}.txt")
    with open(path, "w") as f:
        f.write(f"# {name}: {title}\n# mode={MODE} started={time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                f" python={sys.version.split()[0]}\n")


@pytest.fixture(scope="session")
def tls_ctx():
    return make_selfsigned_context(tempfile.mkdtemp(prefix="ffis-tls-"))


@pytest.fixture
def imap(tls_ctx):
    f = FakeIMAP(ssl_context=tls_ctx).start()
    yield f
    f.stop()


@pytest.fixture
def smtp(tls_ctx):
    s = FakeSMTP(FAKE_SMTP_USER, FAKE_SMTP_PASSWORD, ssl_context=tls_ctx).start()
    yield s
    s.stop()


@pytest.fixture
def state_dir():
    return tempfile.mkdtemp(prefix="ffis-state-")


def env_for(imap, smtp=None, sdir=None, **extra):
    kw = dict(IMAP_PORT=imap.port, FFIS_INGEST_MODE=MODE)
    if smtp is not None:
        kw["SMTP_PORT"] = smtp.port
    if sdir:
        kw["FFIS_STATE_DIR"] = sdir
    kw.update(extra)
    return clean_env(**kw)


def run_cli(moddir, env, cli, opts=(), timeout=120, name=None):
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, RUNNER, moddir, *opts, "--", *cli], env=env,
                       capture_output=True, text=True, timeout=timeout)
    dt = time.monotonic() - t0
    out = r.stdout + r.stderr
    if name:
        ev(name, f"$ runner {os.path.basename(moddir) if moddir != REPO else 'BRANCH'} "
                 f"{' '.join(opts)} -- {' '.join(cli)}  -> rc={r.returncode} in {dt:.2f}s\n{out}")
    return r.returncode, out, dt


def add_att(imap, tag, seen=False, mid=None, internaldate=None):
    return imap.add_message(make_message(subject=f"data {tag}", message_id=mid or f"<{tag}@example.invalid>",
                                         attachments=[csv_attachment(f"{tag}.csv")]),
                            seen=seen, internaldate=internaldate)


def ledger(sdir):
    c = sqlite3.connect(os.path.join(sdir, "ingest.db"))
    rows = c.execute("SELECT key, uidvalidity, uid, status, attempts, reply_sent, done_at "
                     "FROM ledger ORDER BY uid").fetchall()
    wm = c.execute("SELECT folder, uidvalidity, last_uid FROM watermark").fetchall()
    c.close()
    return rows, wm


def reply_ids(smtp):
    return [m["in_reply_to"] for m in smtp.replies()]


# ── in-process helpers (T2/T3/T15 timing) ────────────────────────────────
@pytest.fixture
def P(monkeypatch, state_dir):
    sys.path.insert(0, REPO)
    import ffis_email_pipeline as mod
    mod._HEARTBEAT = None
    for k in list(os.environ):
        if k.startswith(("IMAP_", "SMTP_", "FFIS_", "ANTHROPIC")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FFIS_STATE_DIR", state_dir)
    yield mod
    mod._HEARTBEAT = None


def set_env(monkeypatch, env):
    for k, v in env.items():
        if k.startswith(("IMAP_", "SMTP_", "FFIS_")):
            monkeypatch.setenv(k, v)


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.buf = io.StringIO()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record):
        self.buf.write(self.format(record) + "\n")


@pytest.fixture
def caplines():
    h = _Cap()
    root = logging.getLogger()
    root.addHandler(h)
    old = root.level
    root.setLevel(logging.INFO)
    yield h
    root.removeHandler(h)
    root.setLevel(old)


# ══════════════════════════════════════════════════════════════════════════
def test_T1_blackhole_hangs_unmodified_base(imap):
    """T1: unmodified 08c83b14 still blocked at deadline+10 s (hang reproduced)."""
    ev_header("T1", f"blackhole vs unmodified base blob {BASE_BLOB}")
    imap.blackhole()
    d = base_module_dir()
    deadline = 5
    p = subprocess.Popen([sys.executable, RUNNER, d, "--", "--once"],
                         env=env_for(imap, FFIS_POLL_DEADLINE_S=deadline, FFIS_IMAP_TIMEOUT_S=2),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(deadline + 10)
    alive = p.poll() is None
    p.kill()
    out, _ = p.communicate()
    ev("T1", f"alive at deadline+10s = {alive}; fake connections={imap.connections}\n{out}")
    assert alive, "base returned — hang NOT reproduced"


def test_T2_blackhole_times_out(P, imap, monkeypatch, state_dir, caplines):
    ev_header("T2", "blackhole with FFIS_IMAP_TIMEOUT_S=2")
    imap.blackhole()
    set_env(monkeypatch, env_for(imap, sdir=state_dir, FFIS_IMAP_TIMEOUT_S=2))
    t0 = time.monotonic()
    with pytest.raises(RuntimeError) as ei:
        P.run_once(send=False)
    dt = time.monotonic() - t0
    ev("T2", f"in-process run_once raised {type(ei.value).__name__}: {ei.value} after {dt:.2f}s")
    assert dt <= 3.0 and "timed out" in str(ei.value)
    # loop continues; heartbeat last_result=error
    env = env_for(imap, sdir=state_dir, FFIS_IMAP_TIMEOUT_S=2)
    p = subprocess.Popen([sys.executable, RUNNER, REPO, "--", "--watch", "--interval", "15"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(21)
    alive = p.poll() is None
    p.kill()
    out, _ = p.communicate()
    hb = json.load(open(os.path.join(state_dir, "heartbeat.json")))
    polls = out.count("Polling 127.0.0.1")
    fails = out.count("Poll cycle failed")
    ev("T2", f"watch loop: alive_after_21s={alive} polls={polls} failures={fails}\nheartbeat={hb}\n{out}")
    assert alive and polls >= 2 and fails >= 2 and hb["last_result"] == "error"


@pytest.mark.parametrize("cmd", ["SEARCH", "FETCH"])
def test_T3_hang_after_cmd(P, imap, monkeypatch, state_dir, caplines, cmd):
    ev_header(f"T3-{cmd}", f"hang_after({cmd})")
    add_att(imap, "t3a")
    imap.hang_after(cmd)
    set_env(monkeypatch, env_for(imap, sdir=state_dir, FFIS_IMAP_TIMEOUT_S=2))
    t0 = time.monotonic()
    ok = P.run_poll(send=False)
    dt = time.monotonic() - t0
    log_txt = caplines.buf.getvalue()
    hb = json.load(open(os.path.join(state_dir, "heartbeat.json")))
    ev(f"T3-{cmd}", f"run_poll ok={ok} in {dt:.2f}s; heartbeat={hb}\n{log_txt}")
    want = "stage=search" if cmd == "SEARCH" else "stage=fetch:"
    assert not ok and dt <= 3.5 and want in log_txt and "timed out" in log_txt
    assert hb["last_result"] == "error"


def test_T4_drip_watchdog_exit_70(imap, state_dir):
    ev_header("T4", "drip 1 byte/1 s, timeout 2 s, POLL_DEADLINE_S=5")
    imap.drip(1.0)
    env = env_for(imap, sdir=state_dir, FFIS_IMAP_TIMEOUT_S=2, FFIS_POLL_DEADLINE_S=5)
    t0 = time.monotonic()
    p = subprocess.Popen([sys.executable, RUNNER, REPO, "--", "--watch", "--interval", "120"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    first_poll = None
    lines = []
    for line in p.stdout:
        lines.append(line)
        if first_poll is None and "Polling" in line:
            first_poll = time.monotonic()
    rc = p.wait(timeout=60)
    end = time.monotonic()
    stall = json.load(open(os.path.join(state_dir, "stall.json")))
    since_poll = end - (first_poll or t0)
    ev("T4", f"rc={rc} total={end - t0:.2f}s since_first_poll={since_poll:.2f}s stall.json={stall}\n"
             + "".join(lines))
    assert rc == 70 and since_poll <= 15.5 and stall["exit_code"] == 70 and stall["stage"] == "connect"


def test_T5_stale_heartbeat_exit_71(imap, state_dir):
    ev_header("T5", "stale last_ok_at pre-seeded, poll stub always raises")
    interval, deadline, cycles = 15, 5, 1
    cycle = interval + deadline
    with open(os.path.join(state_dir, "heartbeat.json"), "w") as f:
        stale = time.time() - 10 * cycle
        json.dump({"last_ok_at": time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(stale)),
                   "last_result": "ok", "poll_started_at": None, "poll_finished_at": None,
                   "found": 0, "pid": 1, "version": "seed"}, f)
    env = env_for(imap, sdir=state_dir, FFIS_POLL_DEADLINE_S=deadline, FFIS_STALL_CYCLES=cycles)
    t0 = time.monotonic()
    rc, out, dt = run_cli(REPO, env, ["--watch", "--interval", str(interval)], opts=["--poll-raises"],
                          timeout=120, name="T5")
    limit = cycles * cycle + 10
    stall = json.load(open(os.path.join(state_dir, "stall.json")))
    ev("T5", f"rc={rc} elapsed={dt:.2f}s threshold={cycles * cycle}s tick=10s limit(threshold+tick)={limit}s "
             f"stall.json={stall}")
    assert rc == 71 and dt <= limit + 2.5 and out.count("Poll cycle failed") >= 1


def test_T6_health_script(state_dir):
    ev_header("T6", "ffis_ingest_health.py fresh/stale/missing/in-flight")
    import ffis_ingest_state as st
    env = clean_env(FFIS_STATE_DIR=state_dir, FFIS_POLL_DEADLINE_S=300, FFIS_WATCH_INTERVAL_S=120)
    now = time.time()
    hbp = os.path.join(state_dir, "heartbeat.json")

    def write(started, finished):
        json.dump({"poll_started_at": st.utcnow_iso(started) if started else None,
                   "poll_finished_at": st.utcnow_iso(finished) if finished else None,
                   "last_ok_at": st.utcnow_iso(finished) if finished else None, "last_result": "ok",
                   "found": 0, "pid": 1, "version": "t6"}, open(hbp, "w"))

    def health():
        r = subprocess.run([sys.executable, os.path.join(REPO, "ffis_ingest_health.py")], env=env,
                           capture_output=True, text=True, cwd=REPO, timeout=30)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    cases = []
    write(now - 30, now - 10)
    cases.append(("fresh", 0, *health()))
    write(now - 1000, now - 900)
    cases.append(("stale", 1, *health()))
    os.unlink(hbp)
    cases.append(("missing", 1, *health()))
    write(now - 400, now - 500)
    cases.append(("in-flight-over-deadline", 1, *health()))
    open(hbp, "w").write("{not json")
    cases.append(("unreadable", 1, *health()))
    for name, want, rc, out, err in cases:
        ev("T6", f"{name}: want={want} rc={rc} stdout={out} stderr={err!r}")
        assert rc == want, name
        assert len(out.splitlines()) == 1 and json.loads(out)["check"] == "ffis-ingest-health"
        assert FAKE_SMTP_PASSWORD not in out + err


# ── watermark-only tests ─────────────────────────────────────────────────
@legacy_only_skip
def test_T8_catch_up_after_outage_and_human_read(imap, smtp, state_dir):
    ev_header("T8", "catch-up: 2 ingested, outage, +5 (2 read by a human), restart")

    def scenario(moddir, sdir, label):
        f = FakeIMAP(ssl_context=imap.ssl_context).start()
        s = FakeSMTP(FAKE_SMTP_USER, FAKE_SMTP_PASSWORD, ssl_context=smtp.ssl_context).start()
        env = env_for(f, s, sdir, FFIS_IMAP_STARTTLS="true")
        for i in range(2):
            add_att(f, f"{label}-pre{i}")
        run_cli(moddir, env, ["--once"], name="T8")
        f.stop()                                                    # outage
        rc_out, _, _ = run_cli(moddir, env, ["--once"], name="T8")
        new = [add_att(f, f"{label}-out{i}") for i in range(5)]     # arrive during outage
        f.mark_seen_external(new[1])
        f.mark_seen_external(new[3])                                # a human read 2 of them
        f.start()                                                   # same port
        run_cli(moddir, env, ["--once"], name="T8")                 # client restart
        ids = reply_ids(s)
        outage_ids = [f"<{label}-out{i}@example.invalid>" for i in range(5)]
        got = [i for i in outage_ids if i in ids]
        dupes = {i: ids.count(i) for i in ids if ids.count(i) > 1}
        f.stop()
        s.stop()
        return got, ids, dupes, f.uids()

    base_got, base_ids, base_d, _ = scenario(base_module_dir(), tempfile.mkdtemp(), "base")
    new_got, new_ids, new_d, uids = scenario(REPO, state_dir, "new")
    rows, wm = ledger(state_dir)
    done = [r for r in rows if r[3] == "done"]
    ev("T8", f"RESULT base(08c83b14): outage msgs ingested {len(base_got)}/5 {base_got}; all replies={base_ids}")
    ev("T8", f"RESULT branch: outage msgs ingested {len(new_got)}/5; duplicates={new_d}; "
             f"ledger done={len(done)}; watermark={wm}; max uid={max(uids)}\nledger={rows}")
    assert len(base_got) == 3
    assert len(new_got) == 5 and not new_d and len(new_ids) == 7
    assert len(done) == 7 and wm[0][2] == max(uids)


@legacy_only_skip
@pytest.mark.parametrize("phase", ["bootstrap", "steady"])
def test_T9_crash_mid_batch(imap, smtp, state_dir, phase):
    ev_header("T9", "2nd of 3 messages raises in route_records") if phase == "bootstrap" else None
    ev("T9", f"===== phase={phase} (bootstrap = first-ever poll/resync path; steady = UID n:* path) =====")
    # base: msg3 is marked \Seen and lost
    bf = FakeIMAP(ssl_context=imap.ssl_context).start()
    bs = FakeSMTP(FAKE_SMTP_USER, FAKE_SMTP_PASSWORD, ssl_context=smtp.ssl_context).start()
    bu = [add_att(bf, f"b{i}") for i in (1, 2, 3)]
    benv = env_for(bf, bs, tempfile.mkdtemp(), FFIS_IMAP_STARTTLS="true")
    bd = base_module_dir()
    run_cli(bd, benv, ["--once"], opts=["--fail-on", "b2.csv"], name="T9")
    run_cli(bd, benv, ["--once"], opts=["--fail-on", "b2.csv"], name="T9")
    base_seen3 = bf.is_seen(bu[2])
    base_replied3 = "<b3@example.invalid>" in reply_ids(bs)
    ev("T9", f"RESULT base(08c83b14) after 2 polls: msg3 \\Seen={base_seen3} msg3 replied={base_replied3} "
             f"replies={reply_ids(bs)}")
    bf.stop()
    bs.stop()

    env = env_for(imap, smtp, state_dir, FFIS_IMAP_STARTTLS="true", FFIS_ALERT_TO=FAKE_ALERT_TO)
    if phase == "steady":
        add_att(imap, "m0")
        run_cli(REPO, env, ["--once"], name="T9")
        smtp.messages.clear()
    u = [add_att(imap, f"m{i}") for i in (1, 2, 3)]
    snaps = []
    for poll in (1, 2, 3):
        run_cli(REPO, env, ["--once"], opts=["--fail-on", "m2.csv"], name="T9")
        rows, wm = ledger(state_dir)
        st = {r[0]: (r[3], r[4], r[5]) for r in rows}
        snaps.append((poll, st, wm, [imap.is_seen(x) for x in u], reply_ids(smtp),
                      len(smtp.replies("alert"))))
        ev("T9", f"after poll {poll}: ledger={st} watermark={wm} seen(m1,m2,m3)={snaps[-1][3]} "
                 f"replies={snaps[-1][4]} alerts={snaps[-1][5]}")
    # \Seen only after commit: every STORE event for a uid is later than that uid's ledger done_at
    rows, _ = ledger(state_dir)
    import ffis_ingest_state as ist
    done_at = {r[2]: ist.parse_iso(r[6]) for r in rows if r[6]}
    stores = [e for e in imap.events if e["kind"] == "store"]
    early = [e for e in stores if e["uid"] not in done_at or e["t"] < done_at[e["uid"]]]
    implicit = [e for e in imap.events if e["kind"] == "implicit_seen"]
    ev("T9", f"STORE events={[(e['uid'], round(e['t'], 4)) for e in stores]} "
             f"done_at={ {k: round(v, 4) for k, v in done_at.items()} } stores_before_commit={len(early)} "
             f"implicit_seen_fetches={len(implicit)}")
    m1, m2, m3 = "<m1@example.invalid>", "<m2@example.invalid>", "<m3@example.invalid>"
    p1, p2, p3 = snaps
    assert base_seen3 and not base_replied3
    assert p1[1][m1][0] == "done" and p1[1][m2][:2] == ("failed", 1) and m3 not in p1[1]
    assert p1[3] == [True, False, False] and m3 not in p1[4]
    if phase == "steady":   # watermark advanced past m1 only, never past the failed m2
        assert p1[2][0][2] == u[0] and p2[2][0][2] == u[0] and p3[2][0][2] == u[2]
    assert p2[1][m2][:2] == ("failed", 2) and m3 not in p2[1] and p2[3][2] is False
    assert p3[1][m2][0] == "poison" and p3[1][m3][0] == "done" and m3 in p3[4] and p3[5] >= 1
    assert not early and not implicit


@legacy_only_skip
def test_T10_dedup_rerun_after_watermark_reset(imap, smtp, state_dir):
    ev_header("T10", "ingest 7, reset watermark to 0, poll again")
    env = env_for(imap, smtp, state_dir)
    for i in range(7):
        add_att(imap, f"d{i}")
    run_cli(REPO, env, ["--once"], name="T10")
    before, wm_b = ledger(state_dir)
    n_before = len(smtp.replies())
    c = sqlite3.connect(os.path.join(state_dir, "ingest.db"))
    c.execute("UPDATE watermark SET last_uid=0")
    c.commit()
    c.close()
    rc, out, _ = run_cli(REPO, env, ["--once"], name="T10")
    after, wm_a = ledger(state_dir)
    ev("T10", f"replies before={n_before} after={len(smtp.replies())}; ledger unchanged={before == after}; "
              f"watermark before={wm_b} after={wm_a}")
    assert n_before == 7 and len(smtp.replies()) == 7 and before == after and wm_a == wm_b


@legacy_only_skip
def test_T11_same_message_id_two_uids(imap, smtp, state_dir):
    ev_header("T11", "same Message-ID at 2 UIDs")
    env = env_for(imap, smtp, state_dir)
    add_att(imap, "dup", mid="<same@example.invalid>")
    add_att(imap, "dup", mid="<same@example.invalid>")
    rc, out, _ = run_cli(REPO, env, ["--once"], name="T11")
    rows, wm = ledger(state_dir)
    ev("T11", f"replies={reply_ids(smtp)} ledger={rows} watermark={wm}")
    assert reply_ids(smtp) == ["<same@example.invalid>"]
    assert len(rows) == 1 and rows[0][3] == "done" and "already done in ledger" in out


@legacy_only_skip
def test_T12_uidvalidity_change(imap, smtp, state_dir):
    ev_header("T12", "UIDVALIDITY change renumbers every UID")
    env = env_for(imap, smtp, state_dir)
    for i in range(3):
        add_att(imap, f"v{i}")
    run_cli(REPO, env, ["--once"], name="T12")
    imap.set_uidvalidity(2000)
    add_att(imap, "v-new")
    rc, out, _ = run_cli(REPO, env, ["--once"], name="T12")
    rows, wm = ledger(state_dir)
    ids = reply_ids(smtp)
    dupes = [i for i in set(ids) if ids.count(i) > 1]
    ev("T12", f"uids now={imap.uids()} replies={ids} duplicates={dupes} watermark={wm}\nledger={rows}")
    assert "UID SEARCH SINCE" in out and not dupes and len(ids) == 4
    assert wm[0][1] == 2000 and wm[0][2] == max(imap.uids())


@legacy_only_skip
def test_T13_gmail_star_quirk(imap, smtp, state_dir):
    ev_header("T13", "Gmail n:* quirk, no new mail")
    env = env_for(imap, smtp, state_dir)
    for i in range(3):
        add_att(imap, f"q{i}")
    run_cli(REPO, env, ["--once"], name="T13")
    n1 = len(smtp.replies())
    rc, out, _ = run_cli(REPO, env, ["--once"], name="T13")
    searches = [e for e in imap.events if e["kind"] == "search"]
    ev("T13", f"replies after poll1={n1} after poll2={len(smtp.replies())}; last search={searches[-1]}")
    assert n1 == 3 and len(smtp.replies()) == 3
    assert searches[-1]["result"] == [max(imap.uids())]          # quirk exercised
    assert "Found 0 message(s) with attachments" in out


def test_T14_existing_regression_tests():
    ev_header("T14", "existing test_pipeline.py and test_export.py")
    for t in ("test_pipeline.py", "test_export.py"):
        env = clean_env(FFIS_INGEST_MODE=MODE)
        r = subprocess.run([sys.executable, os.path.join(REPO, t)], env=env, cwd=REPO,
                           capture_output=True, text=True, timeout=120)
        ev("T14", f"$ python {t} -> rc={r.returncode}\n{r.stdout}{r.stderr}")
        assert r.returncode == 0, t


@legacy_only_skip
def test_T15_smtp_blackhole_reply(imap, smtp, state_dir):
    ev_header("T15", "fake SMTP blackhole on reply, FFIS_SMTP_TIMEOUT_S=2")
    smtp.blackhole()
    add_att(imap, "s1")
    env = env_for(imap, smtp, state_dir, FFIS_SMTP_TIMEOUT_S=2)
    rc, out, dt = run_cli(REPO, env, ["--once"], name="T15")
    ts = {}
    for line in out.splitlines():
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) ", line)
        if not m:
            continue
        t = time.mktime(time.strptime(m.group(1)[:19], "%Y-%m-%d %H:%M:%S")) + int(m.group(1)[20:]) / 1000
        if "Processed s1.csv" in line:
            ts["processed"] = t
        if "Failed to send reply" in line:
            ts["failed"] = t
    rows, _ = ledger(state_dir)
    gap = ts.get("failed", 1e9) - ts.get("processed", 0)
    ev("T15", f"rc={rc} total={dt:.2f}s processed->reply-failed={gap:.3f}s ledger={rows}")
    assert gap <= 3.0 and rows[0][3] == "done" and rows[0][5] == 0 and rc == 0


# ── extra checks (not in the SPEC T-table; cover SPEC 4 "in-memory fallback" and 5 alerts) ──
@legacy_only_skip
def test_X1_state_dir_unwritable_in_memory_fallback(imap, smtp):
    ev_header("X1", "FFIS_STATE_DIR not writable -> in-memory state with WARNING, poll still works")
    add_att(imap, "x1")
    ro = tempfile.mkdtemp(prefix="ffis-ro-")
    os.chmod(ro, 0o500)
    env = env_for(imap, smtp, os.path.join(ro, "nested"))
    rc, out, _ = run_cli(REPO, env, ["--once"], name="X1")
    os.chmod(ro, 0o700)
    assert rc == 0 and "IN MEMORY ONLY" in out and "<x1@example.invalid>" in reply_ids(smtp)


@legacy_only_skip
def test_X2_alert_rate_limit_and_recovered(imap, smtp, state_dir):
    ev_header("X2", "stall.json present -> one 'recovered' alert on next good poll; alert rate limit")
    import ffis_ingest_state as ist
    stalled = time.time() - 600
    json.dump({"stalled_at": ist.utcnow_iso(stalled), "exit_code": 70, "stage": "search",
               "recovered_at": None, "last_alert_at": ist.utcnow_iso(time.time() - 60)},
              open(os.path.join(state_dir, "stall.json"), "w"))
    env = env_for(imap, smtp, state_dir, FFIS_ALERT_TO=FAKE_ALERT_TO)
    run_cli(REPO, env, ["--once"], name="X2")
    run_cli(REPO, env, ["--once"], name="X2")
    alerts = smtp.replies("alert")
    st = json.load(open(os.path.join(state_dir, "stall.json")))
    # poison alert inside the 30-min window is rate limited
    sys.path.insert(0, REPO)
    os.environ["FFIS_ALERT_TO"] = FAKE_ALERT_TO
    try:
        r = ist.send_alert("rate-limit probe", {"x": 1}, sdir=state_dir)
    finally:
        del os.environ["FFIS_ALERT_TO"]
    ev("X2", f"alerts={[(a['subject'], a['rcpts']) for a in alerts]} stall.json={st} rate_limited_probe={r}")
    assert len(alerts) == 1 and "recovered" in alerts[0]["subject"] and st["recovered_at"]
    assert r == "rate_limited"
