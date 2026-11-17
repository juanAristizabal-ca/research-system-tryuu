#!/usr/bin/env python3
"""Bring businesses that were emailed ONLY in cooldown-exempt campaigns (config/suppression_seed.yaml, owner decision 2026-11-17: campaigns
before series C) back into the research pipeline so they can be crawled, extracted, scored and checked like new businesses.
Still excluded: bounced, onboarded, interest-flagged (warm or negative), anything in a later campaign, anything named Cypress.

  reenter_exempt.py --segments martial_arts,dance,... [--out ids.txt]     sets status 'discovered' and a website from an own-domain email"""
import argparse
import re
import sys
import collections
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

FREE = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "shaw.ca", "telus.net", "icloud.com", "live.com", "me.com", "protonmail.com", "aol.com"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--segments", required=True)
    ap.add_argument("--out", default="/tmp/reenter_ids.txt")
    a = ap.parse_args()
    want = set(a.segments.split(","))
    conn = lib.connect()
    seed = lib.load_yaml("suppression_seed.yaml")
    exempt = re.compile(seed["cooldown_exempt_campaigns"])
    sends = collections.defaultdict(list)
    for r in conn.execute("SELECT business_id, campaign_name, bounced, email FROM sends"):
        sends[r["business_id"]].append(r)
    onb = {r[0] for r in conn.execute("SELECT business_id FROM onboarded WHERE business_id IS NOT NULL")}
    flagged = {r[0] for r in conn.execute("SELECT business_id FROM evidence WHERE field='instantly_interest'")}
    btype = {r[0]: r[1] for r in conn.execute("SELECT business_id, value FROM evidence WHERE field='legacy_business_type'")}
    ids = []
    with lib.run(conn, "reenter_exempt") as run:
        for b in conn.execute("SELECT id, name, website, domain, status, origin FROM businesses WHERE origin!='gosom' AND status IN ('sent','loaded','enriched')"):
            ss = sends.get(b["id"])
            if not ss or any(not (x["campaign_name"] and exempt.search(x["campaign_name"])) for x in ss) or any(x["bounced"] for x in ss):
                continue
            if b["id"] in onb or b["id"] in flagged or re.search("cypress", b["name"], re.I):
                continue
            if lib.classify_category(f"{btype.get(b['id'], '')} {b['name']}") not in want:
                continue
            dom = lib.email_domain(ss[0]["email"] or "")
            site = b["website"] or (f"https://{dom}" if dom and dom not in FREE else None)
            conn.execute("UPDATE businesses SET status='discovered', website=? WHERE id=?", (site, b["id"]))
            if site and not b["domain"] and not conn.execute("SELECT 1 FROM businesses WHERE domain=?", (dom,)).fetchone():
                conn.execute("UPDATE businesses SET domain=? WHERE id=?", (dom, b["id"]))  # domain is UNIQUE: keep it NULL for duplicates
            ids.append(b["id"])
        conn.commit()
        run.n_in = run.n_out = len(ids)
    Path(a.out).write_text("\n".join(map(str, ids)))
    print(f"{len(ids)} businesses re-entered as 'discovered'; ids in {a.out}")


if __name__ == "__main__":
    main()
