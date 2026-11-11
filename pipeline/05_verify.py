#!/usr/bin/env python3
"""Step 5 - Verify the chosen email of Tier A/B businesses only.

Levels: valid / catch_all (verified by Instantly) > mx_only (free DNS-over-HTTPS check: the domain can receive mail)
> invalid. Outbound port 25 is blocked in this environment, so there is no SMTP check; Instantly's verifier
(0.25 credit per address, used through the connector by the agent) is the only real one.
`verify.min_level` in config/sources.yaml decides what may load: 'valid' (plan default) or 'mx_only'.

  05_verify.py mx [--limit N]                 free MX check, marks mx_only / invalid
  05_verify.py todo <out.json> [--limit N]    addresses worth spending an Instantly credit on, best Tier first
  05_verify.py ingest <results.json>          list of verify_email answers: {email, verification_status, catch_all}
  05_verify.py settle                         promote / demote businesses by the configured level (also run by the above)
Catch-all domains cap the business at Tier B."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

LEVELS = {"valid": 3, "catch_all": 3, "mx_only": 1}


def settle(conn) -> tuple[int, int]:
    need = 1 if lib.load_yaml("sources.yaml")["verify"]["min_level"] == "mx_only" else 3
    up = down = 0
    rows = conn.execute("SELECT c.verification v, b.id bid, b.status, s.tier FROM contacts c JOIN businesses b ON b.id=c.business_id "
                        "JOIN scores s ON s.business_id=b.id WHERE c.is_chosen=1 AND s.tier IN ('A','B') AND b.status IN ('scored','verified','enriched')").fetchall()
    for r in rows:
        ok = LEVELS.get(r["v"], 0) >= need
        if ok and r["status"] != "verified":
            lib.set_status(conn, r["bid"], "verified"); up += 1
        elif not ok and r["status"] == "verified":
            lib.set_status(conn, r["bid"], "scored"); down += 1
        elif r["v"] == "invalid" and r["status"] == "scored":
            lib.set_status(conn, r["bid"], "enriched")  # needs a different mailbox
        if r["v"] == "catch_all" and r["tier"] == "A":
            conn.execute("UPDATE scores SET tier='B' WHERE business_id=?", (r["bid"],))
    conn.commit()
    return up, down


def cmd_mx(conn, a):
    with lib.run(conn, "05_verify:mx") as r:
        rows = conn.execute("SELECT c.id cid, c.email FROM contacts c JOIN businesses b ON b.id=c.business_id JOIN scores s ON s.business_id=b.id "
                            "WHERE c.is_chosen=1 AND s.tier IN ('A','B') AND b.status IN ('scored','enriched') AND c.verification IS NULL"
                            + (f" LIMIT {a.limit}" if a.limit else "")).fetchall()
        r.n_in = len(rows)
        for row in rows:
            res = lib.mx_ok(lib.email_domain(row["email"]))
            if res is None:
                r.errors.append(f"{row['email']}: DNS undecided"); continue
            conn.execute("UPDATE contacts SET verification=?, verified_at=? WHERE id=?", ("mx_only" if res else "invalid", lib.now(), row["cid"]))
            r.n_out += 1
        conn.commit()
        print("settle:", settle(conn))


def cmd_todo(conn, a):
    rows = conn.execute("SELECT c.email, b.name, s.tier, s.score FROM contacts c JOIN businesses b ON b.id=c.business_id JOIN scores s ON s.business_id=b.id "
                        "WHERE c.is_chosen=1 AND s.tier IN ('A','B') AND b.status IN ('scored','verified') "
                        "AND (c.verification IS NULL OR c.verification='mx_only') ORDER BY s.tier, s.score DESC"
                        + (f" LIMIT {a.limit}" if a.limit else "")).fetchall()
    Path(a.file).write_text(json.dumps([{"email": r["email"], "name": r["name"], "tier": r["tier"]} for r in rows], indent=1))
    print(f"{len(rows)} addresses written to {a.file} (each costs 0.25 Instantly credit)")


def cmd_ingest(conn, a):
    with lib.run(conn, "05_verify:ingest") as r:
        items = json.loads(Path(a.file).read_text())
        r.n_in = len(items)
        for it in items:
            st = it.get("verification_status")
            res = "catch_all" if st == "verified" and it.get("catch_all") else "valid" if st == "verified" else "invalid" if st == "invalid" else None
            if res is None:
                r.errors.append(f"{it.get('email')}: status {st}"); continue
            conn.execute("UPDATE contacts SET verification=?, verified_at=? WHERE email=? AND is_chosen=1", (res, lib.now(), lib.norm_email(it["email"])))
            r.n_out += 1
        conn.commit()
        print("settle:", settle(conn))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mx"); m.add_argument("--limit", type=int)
    t = sub.add_parser("todo"); t.add_argument("file"); t.add_argument("--limit", type=int)
    i = sub.add_parser("ingest"); i.add_argument("file")
    sub.add_parser("settle")
    a = ap.parse_args()
    conn = lib.connect()
    if a.cmd == "settle":
        print("settle:", settle(conn))
    else:
        {"mx": cmd_mx, "todo": cmd_todo, "ingest": cmd_ingest}[a.cmd](conn, a)


if __name__ == "__main__":
    main()
