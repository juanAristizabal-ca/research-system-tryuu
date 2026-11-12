#!/usr/bin/env python3
"""Step 8 - Load a batch into an Instantly campaign. DRY-RUN BY DEFAULT. Never activates a campaign.

  08_load.py --segment classes_studios --tier A --size 200 --copy-version v1 --campaign-id <uuid>
        dry run: shows the selection and writes data/loads/<batch>.json (nothing recorded)
  ... --record   after the agent pushed data/loads/<batch>.leads.json with the Instantly connector
                 (add_leads_to_campaign_or_list_bulk, skip_if_in_workspace), record the batch and flip statuses
Refuses to load when research precision is under the configured minimum, or --size exceeds max_batch."""
import argparse
import hashlib
import html
import json
import re
import sys
import datetime as dt
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

SIGNUP = "https://tryuu.com/create-experience"


def clean_name(n: str) -> str:
    n = html.unescape(n or "")
    n = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", n)
    n = re.sub(r"[,]?\s+\b(inc|ltd|llc|corp|corporation|incorporated|limited)\b\.?$", "", n.strip(), flags=re.I)
    return re.sub(r"\s+", " ", n).strip(" ,.-")


def token(bid: int) -> str:
    return f"{bid:x}{hashlib.sha256(f'tryuu{bid}'.encode()).hexdigest()[:3]}"


def offer_phrase(sentence: str | None) -> str:
    if not sentence:
        return "first-visit offer"
    m = lib.OFFER_RE.search(sentence)
    return m.group(0).strip().lower() if m else "free offer"


def lead_vars(conn, b, peers: dict, segs: dict) -> dict:
    ev = {r["field"]: r["value"] for r in conn.execute("SELECT field, value FROM evidence WHERE business_id=?", (b["id"],))}
    c = conn.execute("SELECT email FROM contacts WHERE business_id=? AND is_chosen=1", (b["id"],)).fetchone()
    peer = peers.get(b["segment"], {})
    has_offer = "offer_sentence" in ev
    return {
        "email": c["email"],
        "first_name": ev.get("owner_first_name", ""),
        "company_name": clean_name(b["name"]),
        "custom_variables": {
            "Business_Name": clean_name(b["name"]),
            "First_Name": ev.get("owner_first_name") or f"{clean_name(b['name'])} team",
            "Offer_Phrase": offer_phrase(ev.get("offer_sentence")),
            "Listing_Title": ev.get("listing_title", "") if has_offer else "",
            "Listing_Description": ev.get("listing_description", "") if has_offer else "",
            "City": b["city"] or "",
            "Segment_Noun": segs.get(b["segment"], {}).get("noun", ""),
            "Peer_Business": peer.get("business", ""), "Peer_Offer": peer.get("offer", ""), "Peer_Listing_URL": peer.get("url", ""),
            "Signup_Link": f"{SIGNUP}?r={token(b['id'])}",
            "Tier": "",
        },
    }


def select(conn, segment, tier, size):
    q = ("SELECT b.* FROM businesses b JOIN scores s ON s.business_id=b.id JOIN checks k ON k.business_id=b.id "
         "WHERE b.status='verified' AND s.tier=? AND k.passed=1 AND b.segment=? ORDER BY s.score DESC, b.id LIMIT ?")
    return conn.execute(q, (tier, segment, size)).fetchall()


def precision(conn, segment, tier) -> tuple[float, int]:
    r = conn.execute("SELECT SUM(k.passed) p, COUNT(*) n FROM checks k JOIN businesses b ON b.id=k.business_id "
                     "JOIN scores s ON s.business_id=b.id WHERE b.segment=? AND s.tier=?", (segment, tier)).fetchone()
    return ((r["p"] or 0) / r["n"] if r["n"] else 0.0), r["n"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--segment", required=True); ap.add_argument("--tier", required=True, choices=["A", "B"])
    ap.add_argument("--size", type=int, required=True); ap.add_argument("--copy-version", required=True)
    ap.add_argument("--campaign-id"); ap.add_argument("--record", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the precision gate (document why in the change log)")
    a = ap.parse_args()

    conn = lib.connect()
    lim = lib.load_yaml("sources.yaml")["load"]
    if a.size > lim["max_batch"] and not a.force:
        sys.exit(f"--size {a.size} exceeds load.max_batch={lim['max_batch']} in config/sources.yaml (raise it only after a gate is met).")
    prec, n = precision(conn, a.segment, a.tier)
    if prec < lim["min_precision"] and not a.force:
        sys.exit(f"research precision {prec:.0%} over {n} checked leads is under {lim['min_precision']:.0%}; fix the failing checks first.")
    rows = select(conn, a.segment, a.tier, a.size)
    peers, segs = lib.load_yaml("peers.yaml"), lib.segment_catalog()
    scoring_v = lib.config_version(conn, "scoring.yaml")
    week = dt.date.today().strftime("%Y-W%W")
    name = f"{a.segment}_{a.tier}_{a.copy_version}_{week}"
    leads = [lead_vars(conn, b, peers, segs) for b in rows]
    for l in leads:
        l["custom_variables"]["Tier"] = a.tier
    out = lib.ROOT / "data" / "loads"; out.mkdir(parents=True, exist_ok=True)
    payload = {"batch": name, "campaign_id": a.campaign_id, "skip_if_in_workspace": True, "verify_leads_on_import": False,
               "leads": leads, "business_ids": [b["id"] for b in rows]}
    (out / f"{name}.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    slim = [{**l, "custom_variables": {k: v for k, v in l["custom_variables"].items() if v}} for l in leads]
    (out / f"{name}.leads.json").write_text(json.dumps(slim, ensure_ascii=False))  # exactly what the connector gets
    print(f"batch {name}: selected {len(rows)} of {a.size} requested (precision {prec:.0%}); file data/loads/{name}.json")
    if not a.record:
        print("dry run: nothing recorded, nothing pushed")
        return
    if not rows:
        sys.exit("nothing to load")
    if not a.campaign_id:
        sys.exit("--campaign-id is required to record")
    with lib.run(conn, "08_load") as r:
        r.n_in = len(rows)
        cur = conn.execute("INSERT INTO batches(name, segment, tier, copy_version, scoring_version, instantly_campaign_id, n_loaded, research_precision) "
                           "VALUES (?,?,?,?,?,?,?,?)", (name, a.segment, a.tier, a.copy_version, scoring_v, a.campaign_id, len(rows), prec))
        for b, l in zip(rows, leads):
            conn.execute("INSERT INTO batch_leads(batch_id, business_id, email, pushed) VALUES (?,?,?,1)", (cur.lastrowid, b["id"], l["email"]))
            lib.set_status(conn, b["id"], "loaded")
        conn.commit()
        r.n_out = len(rows)
        print("recorded. The campaign is NOT activated; activate it manually in Instantly.")


if __name__ == "__main__":
    main()
