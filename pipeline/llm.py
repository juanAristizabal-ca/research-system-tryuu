#!/usr/bin/env python3
"""LLM tasks are answered by the agent (Claude) inside the session. No API key, no external AI.

  llm.py export <worksheet.md> [--kind extract|segment_check] [--limit N]   write pending tasks as a worksheet
  llm.py import <answers.jsonl>      lines of {"id": <task id>, "response": {...json...}}
  llm.py status                      pending / done / error by kind
The worksheet holds the instructions once, then one block per task. The agent reads it, writes the answers file,
and imports it. Prompts are blanked after answering to keep the database small."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

SEGMENT_CHECK = ("For each task decide, from ONLY the website text, which single category best describes the business. "
                 "Choose one of: {segments}. Response per task: {{\"segment\": \"<choice>\"}}")


def instructions(kind: str) -> str:
    segs = ", ".join(lib.segment_catalog())
    if kind == "extract":
        return (lib.CONFIG / "extraction_prompt.md").read_text().replace("{segments}", segs)
    return SEGMENT_CHECK.format(segments=segs)


def export(conn, path: Path, kind: str, limit: int | None) -> int:
    rows = conn.execute("SELECT id, business_id, prompt FROM llm_tasks WHERE status='pending' AND kind=? ORDER BY id"
                        + (f" LIMIT {int(limit)}" if limit else ""), (kind,)).fetchall()
    out = [f"# WORKSHEET kind={kind} tasks={len(rows)}", "", instructions(kind), "",
           'Write one JSON line per task to the answers file: {"id": <task id>, "response": <the JSON object>}', ""]
    for r in rows:
        out += [f"### TASK id={r['id']}", r["prompt"], ""]
    path.write_text("\n".join(out))
    return len(rows)


def import_answers(conn, path: Path) -> tuple[int, int]:
    ok = bad = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        resp = d["response"] if isinstance(d["response"], str) else json.dumps(d["response"], ensure_ascii=False)
        if lib.extract_json(resp) is None:
            conn.execute("UPDATE llm_tasks SET status='error', error='unparseable answer' WHERE id=?", (d["id"],)); bad += 1
            continue
        conn.execute("UPDATE llm_tasks SET status='done', response=?, prompt='', done=? WHERE id=?", (resp, lib.now(), d["id"]))
        ok += 1
    conn.commit()
    return ok, bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["export", "import", "status"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--kind", default="extract")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    conn = lib.connect()
    if a.cmd == "status":
        for r in conn.execute("SELECT kind, status, COUNT(*) n FROM llm_tasks GROUP BY kind, status"):
            print(f"{r['kind']:15} {r['status']:8} {r['n']}")
    elif a.cmd == "export":
        print(f"exported {export(conn, Path(a.file), a.kind, a.limit)} {a.kind} tasks to {a.file}")
    else:
        ok, bad = import_answers(conn, Path(a.file))
        print(f"imported {ok} answers, {bad} unparseable (left as error; re-export to redo)")


if __name__ == "__main__":
    main()
