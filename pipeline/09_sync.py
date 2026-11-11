#!/usr/bin/env python3
"""Step 9 - Sync sends, bounces and replies from Instantly into the database (needs INSTANTLY_API_KEY).
Instantly only sends; truth lives here. Idempotent: rows are keyed by the Instantly email id.

  09_sync.py                    every campaign that has a recorded batch
  09_sync.py --campaign <id>    one campaign (repeatable)"""
import argparse
import os
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

API = "https://api.instantly.ai/api/v2"
AUTO_RE = re.compile(r"out of office|automatic reply|auto[- ]?reply|autoreply|undeliverable|delivery status|mailer-daemon|"
                     r"ticket|case #|we have received your|thank you for contacting|do not reply", re.I)


def pages(method, path, key, params=None, body=None):
    cursor = None
    while True:
        p = dict(params or {}, limit=100)
        b = dict(body or {}, limit=100)
        if cursor:
            p["starting_after"] = cursor; b["starting_after"] = cursor
        r = requests.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {key}"},
                             params=p if method == "GET" else None, json=b if method == "POST" else None, timeout=90)
        r.raise_for_status()
        j = r.json()
        yield from j.get("items", [])
        cursor = j.get("next_starting_after")
        if not cursor or not j.get("items"):
            return


def step_of(step: str | None) -> int | None:
    m = re.match(r"\d+_(\d+)_\d+", step or "")
    return int(m.group(1)) + 1 if m else None


def body_text(e: dict) -> str:
    b = e.get("body") or {}
    t = b.get("text") or re.sub(r"<[^>]+>", " ", b.get("html") or "")
    return re.sub(r"\s+", " ", t).strip()


def business_for(conn, email: str):
    row = conn.execute("SELECT business_id FROM contacts WHERE email=? LIMIT 1", (lib.norm_email(email),)).fetchone()
    if row:
        return row["business_id"]
    d = lib.email_domain(email)
    if d and not lib.is_free_mail(d):
        row = conn.execute("SELECT id FROM businesses WHERE domain=?", (d,)).fetchone()
        return row["id"] if row else None
    return None


def sync_campaign(conn, key: str, cid: str, batch_id: int | None, r) -> None:
    for e in pages("GET", "/emails", key, {"campaign_id": cid}):
        em = lib.norm_email(e.get("lead") or e.get("to_address_email_list"))
        bid = business_for(conn, em)
        if e.get("ue_type") == 1:
            cur = conn.execute("INSERT OR IGNORE INTO sends(business_id, batch_id, email, step, sent_at, instantly_email_id) VALUES (?,?,?,?,?,?)",
                               (bid, batch_id, em, step_of(e.get("step")), (e.get("timestamp_email") or "")[:19].replace("T", " "), e["id"]))
            r.n_out += cur.rowcount
        elif e.get("ue_type") == 2:
            txt = body_text(e)
            auto = bool(e.get("is_auto_reply")) or bool(AUTO_RE.search(f"{e.get('subject', '')} {txt[:400]}"))
            conn.execute("INSERT OR IGNORE INTO replies(business_id, instantly_email_id, thread_id, from_email, eaccount, subject, text, received_at, is_auto) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (bid, e["id"], e.get("thread_id"), lib.norm_email(e.get("from_address_email")),
                                                         e.get("eaccount"), e.get("subject"), txt[:4000],
                                                         (e.get("timestamp_email") or "")[:19].replace("T", " "), int(auto)))
    for lead in pages("POST", "/leads/list", key, body={"campaign": cid}):
        if lead.get("status") == -1:  # bounced
            em = lib.norm_email(lead.get("email"))
            if not conn.execute("UPDATE sends SET bounced=1 WHERE email=? AND batch_id IS ?", (em, batch_id)).rowcount:
                conn.execute("INSERT INTO sends(business_id, batch_id, email, bounced, sent_at) VALUES (?,?,?,1,?)",
                             (business_for(conn, em), batch_id, em, (lead.get("timestamp_last_contact") or "")[:19].replace("T", " ")))
    conn.commit()


def update_statuses(conn):
    conn.execute("UPDATE businesses SET status='sent', updated_at=? WHERE status='loaded' AND id IN "
                 "(SELECT business_id FROM sends WHERE business_id IS NOT NULL AND batch_id IS NOT NULL AND bounced=0)", (lib.now(),))
    conn.execute("UPDATE businesses SET status='replied', updated_at=? WHERE status IN ('loaded','sent') AND id IN "
                 "(SELECT business_id FROM replies WHERE business_id IS NOT NULL AND is_auto=0)", (lib.now(),))
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaign", action="append")
    a = ap.parse_args()
    key = os.environ.get("INSTANTLY_API_KEY") or sys.exit("INSTANTLY_API_KEY not set")
    conn = lib.connect()
    with lib.run(conn, "09_sync") as r:
        if a.campaign:
            targets = [(c, (conn.execute("SELECT id FROM batches WHERE instantly_campaign_id=? ORDER BY id DESC", (c,)).fetchone() or [None])[0]) for c in a.campaign]
        else:
            targets = [(row["instantly_campaign_id"], row["id"]) for row in conn.execute(
                "SELECT id, instantly_campaign_id FROM batches WHERE instantly_campaign_id IS NOT NULL")]
        r.n_in = len(targets)
        for cid, bid in targets:
            try:
                sync_campaign(conn, key, cid, bid, r)
            except requests.RequestException as e:
                r.errors.append(f"{cid}: {e}")
        update_statuses(conn)


if __name__ == "__main__":
    main()
