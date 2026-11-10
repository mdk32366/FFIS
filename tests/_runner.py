"""Subprocess entry for the hardening tests.

    python tests/_runner.py <module_dir> [--fail-on FILENAME] [--poll-raises] -- <pipeline CLI args>

<module_dir> is the repo (branch code) or a temp dir holding the UNMODIFIED
base blob 08c83b14. Test-only patches:
  --fail-on NAME   route_records raises for attachment NAME (T9)
  --poll-raises    the IMAP leg always raises (T5 "poll stub always raises")
"""
import logging
import sys

argv = sys.argv[1:]
moddir = argv.pop(0)
sep = argv.index("--") if "--" in argv else len(argv)
opts, cli = argv[:sep], argv[sep + 1:]
sys.path.insert(0, moddir)
import ffis_email_pipeline as P  # noqa: E402

print("RUNNER module:", P.__file__, flush=True)
if "--fail-on" in opts:
    fail_on = opts[opts.index("--fail-on") + 1]
    _orig = P.route_records

    def _route(df, filename="", dedup=None):
        if filename == fail_on:
            raise RuntimeError(f"injected failure in route_records for {filename}")
        return _orig(df, filename=filename, dedup=dedup)
    P.route_records = _route
if "--poll-raises" in opts:
    def _boom(*a, **k):
        raise RuntimeError("injected: IMAP leg always fails (T5 stub)")
    P.fetch_inbound_messages = _boom
    if hasattr(P, "fetch_and_process_watermark"):
        P.fetch_and_process_watermark = _boom
rc = P.main(cli)
logging.shutdown()
print("RUNNER main returned", rc, flush=True)
sys.exit(rc or 0)
