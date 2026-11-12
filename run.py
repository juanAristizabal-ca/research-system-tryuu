#!/usr/bin/env python3
"""Fixed runbook entry point. A session starts with `python run.py restore` (new container) and
`python run.py status`, never with new instructions.

  run.py status               counts by status, tiers, last runs
  run.py research             resolve -> crawl -> extract -> score -> mx check -> suppress -> check   (started manually)
  run.py daily [upload]       statuses -> reply -> match <upload> -> suppress -> report -> save        (after the daily upload)
  run.py backup | restore     text SQL dump (no page text) in data/backup/ | rebuild the db from it (new session)
  run.py save "message"       backup + commit + push
  run.py log-change FILE "why"   record the reason for the current version of a config file
Discovery (01), LLM answers (llm.py), Instantly pulls/pushes (connector) and loading (08) are explicit, see CLAUDE.md."""
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "pipeline"))
import lib  # noqa: E402

DUMP = ROOT / "data" / "backup" / "tryuu.sql"


def step(script: str, *args: str) -> int:
    print(f"\n=== {script} {' '.join(args)}")
    return subprocess.call([sys.executable, str(ROOT / "pipeline" / script), *args])


def status():
    c = lib.connect()
    print("businesses by status:")
    for r in c.execute("SELECT status, COUNT(*) n FROM businesses GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:12} {r['n']}")
    print("tiers:", {r["tier"]: r["n"] for r in c.execute("SELECT tier, COUNT(*) n FROM scores GROUP BY tier")})
    print("onboarded rows:", c.execute("SELECT COUNT(*) FROM onboarded").fetchone()[0],
          "| matched:", c.execute("SELECT COUNT(*) FROM onboarded WHERE business_id IS NOT NULL").fetchone()[0])
    print("llm tasks:", {f"{r['kind']}/{r['status']}": r["n"] for r in c.execute("SELECT kind, status, COUNT(*) n FROM llm_tasks GROUP BY kind, status")})
    print("last runs:")
    for r in c.execute("SELECT job, started, n_in, n_out, errors IS NOT NULL err FROM runs ORDER BY id DESC LIMIT 8"):
        print(f"  {r['started']}  {r['job']:24} in={r['n_in']} out={r['n_out']}{'  ERRORS' if r['err'] else ''}")


def backup():
    """Text SQL dump (git-friendly) without the bulky, re-crawlable page text and without answered LLM prompts."""
    DUMP.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        src = lib.connect()
        dst = sqlite3.connect(Path(d) / "t.db")
        src.backup(dst)
        src.close()
        dst.execute("DELETE FROM pages")
        dst.execute("UPDATE llm_tasks SET prompt='' WHERE status='done'")
        dst.commit()
        with DUMP.open("w", encoding="utf-8") as f:
            for line in dst.iterdump():
                f.write(line + "\n")
        dst.close()
    print(f"backup: {DUMP} ({DUMP.stat().st_size / 1e6:.1f} MB)")


def restore(force: bool = False):
    if lib.DB_PATH.exists() and not force:
        print("database exists; use `restore --force` to overwrite it from the dump")
        return
    if not DUMP.exists():
        sys.exit(f"no dump at {DUMP}")
    lib.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    lib.DB_PATH.unlink(missing_ok=True)
    c = sqlite3.connect(lib.DB_PATH)
    c.executescript(DUMP.read_text(encoding="utf-8"))
    c.close()
    print("restored", lib.DB_PATH, "(page text is not in the dump: re-crawl if needed)")


def git(*a):
    return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True)


def save(msg: str):
    """backup + commit + push. Data (dump, reports, raw, loads) is only committed when git.private_confirmed is true,
    because the dump holds business emails and reply text."""
    subprocess.call([sys.executable, str(ROOT / "pipeline" / "category_report.py")], stdout=subprocess.DEVNULL)  # refreshes CATEGORY_STATE.md
    backup()
    private = bool(lib.load_yaml("sources.yaml").get("git", {}).get("private_confirmed"))
    git("add", "-A", "--", ".")
    if private:  # these paths are gitignored so a public remote can never receive them by accident
        git("add", "-f", "--", "data/backup", "data/raw", "data/loads", "reports")
    r = git("commit", "-m", msg)
    out = (r.stdout or r.stderr).strip()
    print(out.splitlines()[0] if out else "nothing to commit")
    if not private:
        print("NOTE: data/ and reports/ were NOT committed. Set git.private_confirmed: true in config/sources.yaml "
              "only once the remote repository is private.")
    if not git("remote").stdout.strip():
        print("no git remote configured; commit is local only")
        return
    p = None
    for wait in (0, 2, 4, 8, 16):
        time.sleep(wait)
        p = git("push", "-u", "origin", "HEAD")
        if p.returncode == 0:
            print("pushed")
            return
    print("PUSH FAILED:", p.stderr.strip()[-300:])


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "status":
        status()
    elif cmd == "backup":
        backup()
    elif cmd == "restore":
        restore("--force" in sys.argv)
    elif cmd == "save":
        save(sys.argv[2] if len(sys.argv) > 2 else f"state {lib.now()}")
    elif cmd == "research":
        for s, a in [("02_resolve.py", []), ("03_enrich.py", ["crawl"]), ("03_enrich.py", ["extract"]),
                     ("04_score.py", []), ("05_verify.py", ["mx"]), ("06_suppress.py", []), ("07_check.py", [])]:
            if step(s, *a):
                sys.exit(f"stopped at {s}: fix the cause and re-run (every step is safe to repeat)")
    elif cmd == "daily":
        upload = sys.argv[2:3]
        # the agent has already pulled Instantly with its connector and run 09_sync.py on the saved pages
        for s, a in [("09_sync.py", ["status"]), ("10_reply.py", []), ("11_match.py", upload), ("06_suppress.py", []), ("12_report.py", [])]:
            if step(s, *a):
                sys.exit(f"stopped at {s}")
        save("daily run")
    elif cmd == "log-change" and len(sys.argv) == 4:
        c = lib.connect()
        ver = lib.config_version(c, sys.argv[2])
        c.execute("UPDATE config_versions SET change=?, reason=? WHERE file=? AND version=?", ("manual", sys.argv[3], sys.argv[2], ver))
        c.commit()
        print(f"{sys.argv[2]} {ver}: {sys.argv[3]}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
