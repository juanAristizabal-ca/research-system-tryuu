#!/usr/bin/env python3
"""Collect list_leads pages that the Instantly connector saved to the session's tool-results folder.

  collect_pages.py            copy new pages into data/raw/<date>/instantly/ and print the next cursor per campaign
  collect_pages.py --state    print the state only
A page with fewer than 100 leads is the last one (the connector still returns a cursor); a campaign is 'done' then.
Cursor state lives in data/raw/<date>/instantly/_state.json so re-running is safe."""
import glob
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "raw" / "2026-11-12" / "instantly"
SRC = glob.glob("/root/.claude/projects/*/*/tool-results/mcp-instantly-list_leads-*.txt")
PAGE = 100


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sp = OUT / "_state.json"
    st = json.loads(sp.read_text()) if sp.exists() else {"seen": [], "campaigns": {}}
    if "--state" not in sys.argv:
        for f in sorted(SRC, key=lambda p: Path(p).stat().st_mtime):
            if f in st["seen"]:
                continue
            try:
                j = json.loads(Path(f).read_text())
            except json.JSONDecodeError:
                continue
            items = j.get("items", [])
            if not items:
                st["seen"].append(f); continue
            camps = {i.get("campaign") for i in items}
            cid = camps.pop() if len(camps) == 1 else "mixed"
            c = st["campaigns"].setdefault(cid, {"pages": 0, "leads": 0, "cursor": None, "done": False})
            if len(items) and "FILTER" not in f and cid != "mixed":
                shutil.copy2(f, OUT / f"leads_{cid}_{c['pages']:03d}.json")
                c["pages"] += 1; c["leads"] += len(items)
                c["cursor"] = j.get("next_starting_after")
                c["done"] = len(items) < PAGE
            st["seen"].append(f)
        sp.write_text(json.dumps(st))
    todo = {k: v for k, v in st["campaigns"].items() if not v["done"]}
    print(f"campaigns seen {len(st['campaigns'])}, leads collected {sum(v['leads'] for v in st['campaigns'].values())}, not done {len(todo)}")
    for k, v in todo.items():
        print(f"{k} {v['cursor']}")


if __name__ == "__main__":
    main()
