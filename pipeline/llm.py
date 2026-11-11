#!/usr/bin/env python3
"""Runs queued LLM tasks. Two ways to answer them, same table:

  llm.py run            call the Anthropic API for every pending task (needs ANTHROPIC_API_KEY)
  llm.py export F.jsonl write pending tasks so the agent in a session can answer them
  llm.py import F.jsonl read {"id":..., "response":"..."} lines back in
  llm.py status         pending / done / error counts by kind
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def process(conn, kinds=None, limit=None, workers=6) -> int:
    """Answer pending tasks through the API. Returns number done."""
    model = lib.load_yaml("sources.yaml")["llm"]["model"]
    q = "SELECT id, prompt FROM llm_tasks WHERE status='pending'"
    args = []
    if kinds:
        q += f" AND kind IN ({','.join('?' * len(kinds))})"; args += list(kinds)
    q += " ORDER BY id" + (f" LIMIT {int(limit)}" if limit else "")
    tasks = conn.execute(q, args).fetchall()

    def one(t):
        try:
            return t["id"], lib.llm_complete(t["prompt"], model), None
        except Exception as e:  # noqa: BLE001
            return t["id"], None, f"{type(e).__name__}: {e}"

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for tid, resp, err in ex.map(one, tasks):
            if err:
                conn.execute("UPDATE llm_tasks SET status='error', error=? WHERE id=?", (err[:500], tid))
            else:
                conn.execute("UPDATE llm_tasks SET status='done', response=?, done=? WHERE id=?", (resp, lib.now(), tid))
                done += 1
    conn.commit()
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "export", "import", "status"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--kind", action="append")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    conn = lib.connect()
    if a.cmd == "status":
        for r in conn.execute("SELECT kind, status, COUNT(*) n FROM llm_tasks GROUP BY kind, status"):
            print(f"{r['kind']:15} {r['status']:8} {r['n']}")
    elif a.cmd == "run":
        with lib.run(conn, "llm:run") as r:
            r.n_out = process(conn, a.kind, a.limit)
    elif a.cmd == "export":
        q = "SELECT id, kind, business_id, prompt FROM llm_tasks WHERE status='pending'"
        rows = conn.execute(q + (f" LIMIT {a.limit}" if a.limit else "")).fetchall()
        Path(a.file).write_text("\n".join(json.dumps(dict(r)) for r in rows))
        print(f"exported {len(rows)} tasks to {a.file}")
    elif a.cmd == "import":
        n = 0
        for line in Path(a.file).read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                conn.execute("UPDATE llm_tasks SET status='done', response=?, done=? WHERE id=?",
                             (d["response"], lib.now(), d["id"]))
                n += 1
        conn.commit()
        print(f"imported {n} responses")


if __name__ == "__main__":
    main()
