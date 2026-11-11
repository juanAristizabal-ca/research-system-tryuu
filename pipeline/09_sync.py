#!/usr/bin/env python3
"""Step 9 - Sync sends, bounces and replies from Instantly into the database.

The agent pulls from Instantly with its connector (list_emails / list_leads), which saves each page to a JSON file;
this script ingests those files. Instantly only sends; truth lives here. Idempotent (rows keyed by Instantly id).

  09_sync.py emails <page.json> ...   outputs of list_emails (email_type sent or received, any campaign)
  09_sync.py leads  <page.json> ...   outputs of list_leads (lead status: -1 bounced, -2 unsubscribed ...)
  09_sync.py status                   promote loaded -> sent -> replied from what was ingested
How the agent fetches (per campaign that has a batch): list_emails campaign_id=<id> email_type=sent|received limit=100,
following next_starting_after; list_leads campaign=<id> filter=FILTER_VAL_BOUNCED (and FILTER_VAL_UNSUBSCRIBED)."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

AUTO_RE = re.compile(r"out of office|automatic reply|auto[- ]?reply|autoreply|undeliverable|delivery status|mailer-daemon|"
                     r"ticket|case #|we have received your|thank you for contacting|do not reply", re.I)


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


def batch_for(conn, campaign_id: str | None):
    if not campaign_id:
        return None
    row = conn.execute("SELECT id FROM batches WHERE instantly_campaign_id=? ORDER BY id DESC LIMIT 1", (campaign_id,)).fetchone()
    return row["id"] if row else None


def ts(s: str | None) -> str:
    return (s or "")[:19].replace("T", " ")


def ingest_emails(conn, items: list[dict]) -> tuple[int, int]:
    sends = replies = 0
    for e in items:
        em = lib.norm_email(e.get("lead") or e.get("to_address_email_list"))
        if e.get("ue_type") == 1:
            cur = conn.execute("INSERT OR IGNORE INTO sends(business_id, batch_id, campaign_name, email, step, sent_at, instantly_email_id) VALUES (?,?,?,?,?,?,?)",
                               (business_for(conn, em), batch_for(conn, e.get("campaign_id")), e.get("campaign_id"), em,
                                step_of(e.get("step")), ts(e.get("timestamp_email")), e["id"]))
            sends += cur.rowcount
        elif e.get("ue_type") == 2:
            txt = body_text(e)
            auto = bool(e.get("is_auto_reply")) or bool(AUTO_RE.search(f"{e.get('subject', '')} {txt[:400]}"))
            cur = conn.execute("INSERT OR IGNORE INTO replies(business_id, instantly_email_id, thread_id, from_email, eaccount, subject, text, received_at, is_auto) "
                               "VALUES (?,?,?,?,?,?,?,?,?)", (business_for(conn, e.get("from_address_email")), e["id"], e.get("thread_id"),
                                                              lib.norm_email(e.get("from_address_email")), e.get("eaccount"), e.get("subject"),
                                                              txt[:4000], ts(e.get("timestamp_email")), int(auto)))
            replies += cur.rowcount
    conn.commit()
    return sends, replies


def ingest_leads(conn, items: list[dict]) -> int:
    n = 0
    for lead in items:
        em = lib.norm_email(lead.get("email"))
        bid = batch_for(conn, lead.get("campaign"))
        st = lead.get("status")
        if st == -1:  # bounced
            if not conn.execute("UPDATE sends SET bounced=1 WHERE email=? AND batch_id IS ?", (em, bid)).rowcount:
                conn.execute("INSERT INTO sends(business_id, batch_id, campaign_name, email, bounced, sent_at) VALUES (?,?,?,?,1,?)",
                             (business_for(conn, em), bid, lead.get("campaign"), em, ts(lead.get("timestamp_last_contact"))))
            n += 1
        elif st == -2:  # unsubscribed
            conn.execute("INSERT OR IGNORE INTO suppression(kind, value, reason) VALUES ('email', ?, 'unsubscribed (Instantly)')", (em,))
            d = lib.email_domain(em)
            if d and not lib.is_free_mail(d):
                conn.execute("INSERT OR IGNORE INTO suppression(kind, value, reason) VALUES ('domain', ?, 'unsubscribed (Instantly)')", (d,))
            n += 1
    conn.commit()
    return n


def update_statuses(conn):
    conn.execute("UPDATE businesses SET status='sent', updated_at=? WHERE status='loaded' AND id IN "
                 "(SELECT business_id FROM sends WHERE business_id IS NOT NULL AND batch_id IS NOT NULL AND bounced=0)", (lib.now(),))
    conn.execute("UPDATE businesses SET status='replied', updated_at=? WHERE status IN ('loaded','sent') AND id IN "
                 "(SELECT business_id FROM replies WHERE business_id IS NOT NULL AND is_auto=0)", (lib.now(),))
    conn.commit()


def items_of(path: str) -> list[dict]:
    j = json.loads(Path(path).read_text())
    return j["items"] if isinstance(j, dict) else j


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("emails", "leads", "status"):
        sys.exit(__doc__)
    conn = lib.connect()
    with lib.run(conn, f"09_sync:{sys.argv[1]}") as r:
        if sys.argv[1] == "status":
            update_statuses(conn)
            return
        for f in sys.argv[2:]:
            items = items_of(f)
            r.n_in += len(items)
            if sys.argv[1] == "emails":
                s, rp = ingest_emails(conn, items); r.n_out += s + rp
            else:
                r.n_out += ingest_leads(conn, items)
        update_statuses(conn)


if __name__ == "__main__":
    main()
