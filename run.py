#!/usr/bin/env python3
"""Fixed runbook entry point. A session starts with `python run.py <command>`, never with new instructions.

  run.py status               counts by status, tiers, last runs
  run.py research             resolve -> crawl -> extract -> score -> verify -> suppress -> check   (started manually)
  run.py daily                sync -> reply -> match <upload> -> report                              (after the daily upload)
  run.py backup               compressed SQL dump to data/backups/
  run.py log-change FILE "why"   record the reason for the current version of a config file
Discovery (01) and loading (08) are always explicit, see CLAUDE.md."""
import gzip
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "pipeline"))
import lib  # noqa: E402


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
    out = ROOT / "data" / "backups"
    out.mkdir(parents=True, exist_ok=True)
    c = lib.connect()
    p = out / f"tryuu_{lib.now()[:10]}.sql.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        for line in c.iterdump():
            f.write(line + "\n")
    print("backup:", p)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "status":
        status()
    elif cmd == "backup":
        backup()
    elif cmd == "research":
        for s, a in [("02_resolve.py", []), ("03_enrich.py", ["crawl"]), ("03_enrich.py", ["extract"]),
                     ("04_score.py", []), ("05_verify.py", []), ("06_suppress.py", []), ("07_check.py", [])]:
            if step(s, *a):
                sys.exit(f"stopped at {s}: fix the cause and re-run (every step is safe to repeat)")
    elif cmd == "daily":
        upload = sys.argv[2:3]
        for s, a in [("09_sync.py", []), ("10_reply.py", []), ("11_match.py", upload), ("06_suppress.py", []), ("12_report.py", [])]:
            if step(s, *a):
                sys.exit(f"stopped at {s}")
        backup()
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
