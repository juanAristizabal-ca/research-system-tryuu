#!/usr/bin/env python3
"""Step 2 - Resolve. Merge raw rows into one business each, keyed by website domain and Maps place id.
Every merge is logged. Safe to re-run: only rows with business_id NULL are processed."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

FILL = ("name", "city", "postal_code", "address", "phone", "website")


def resolve_row(conn, raw_id: int, n: dict, origin: str) -> int:
    dom, pid = n.get("domain", ""), n.get("place_id", "")
    row = None
    if dom:
        row = conn.execute("SELECT * FROM businesses WHERE domain=?", (dom,)).fetchone()
    if row is None and pid:
        row = conn.execute("SELECT * FROM businesses WHERE place_id=?", (pid,)).fetchone()
    if row:
        sets, vals = [], []
        for f in FILL:
            if not row[f] and n.get(f):
                sets.append(f"{f}=?"); vals.append(n[f])
        if dom and not row["domain"]:
            sets.append("domain=?"); vals.append(dom)
        if pid and not row["place_id"]:
            sets.append("place_id=?"); vals.append(pid)
        if sets:
            conn.execute(f"UPDATE businesses SET {', '.join(sets)}, updated_at=? WHERE id=?", (*vals, lib.now(), row["id"]))
        conn.execute("INSERT INTO merges(kept_id, raw_row_id, reason) VALUES (?,?,?)",
                     (row["id"], raw_id, "domain" if dom and row["domain"] == dom else "place_id"))
        return row["id"]
    key = lib.business_key(dom, pid, "")
    cur = conn.execute(
        "INSERT INTO businesses(key, domain, place_id, name, city, postal_code, address, phone, website, origin) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (key, dom or None, pid or None, n["name"], n.get("city"), n.get("postal_code") or None, n.get("address"),
         n.get("phone") or None, n.get("website") or None, origin))
    return cur.lastrowid


def main():
    conn = lib.connect()
    with lib.run(conn, "02_resolve") as r:
        rows = conn.execute("SELECT id, source, payload FROM raw_rows WHERE business_id IS NULL ORDER BY id").fetchall()
        r.n_in = len(rows)
        created = 0
        before = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        for row in rows:
            n = json.loads(row["payload"])["n"]
            bid = resolve_row(conn, row["id"], n, row["source"])
            conn.execute("UPDATE raw_rows SET business_id=? WHERE id=?", (bid, row["id"]))
            hint = n.get("segment_hint")
            if hint:
                conn.execute("UPDATE businesses SET segment=COALESCE(segment, ?) WHERE id=?", (hint, bid))
        conn.commit()
        created = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] - before
        r.n_out = created
        print(f"resolved {len(rows)} raw rows -> {created} new businesses, {len(rows) - created} merged")


if __name__ == "__main__":
    main()
