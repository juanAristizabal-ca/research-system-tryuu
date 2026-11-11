#!/usr/bin/env python3
"""Step 10 - Reply handling. Classify each human reply into ONE intent with plain rules and act on it.
Only four approved templates are ever sent, word for word; anything unclear goes to the review queue unsent.

  10_reply.py                  classify new replies, suppress stop / not-interested, write reports/replies_to_send.json
  10_reply.py mark-sent <id>.. record replies the agent answered (via the Instantly reply_to_email connector tool)
Templates live in config/reply_templates/*.md and are inert until their front matter says approved: true.
Each item in replies_to_send.json maps 1:1 to reply_to_email(reply_to_uuid, eaccount, subject, to, body_text)."""
import argparse
import csv
import json
import re
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

RULES = [  # order matters: first match wins; more than one distinct intent matching -> 'other' (review)
    ("stop", r"\b(unsubscribe|stop (emailing|sending|contacting)|stop\b|remove (me|us)|do not (contact|email)|don'?t (contact|email))"),
    ("not_interested", r"\b(not interested|no thanks?|no thank you|not at this time|not for us|pass on this)\b"),
    ("signed_up_cannot_find", r"\b(signed up|i (have )?registered|created (a|my) (listing|account)|can'?t find|cannot find|where is (my|the) listing)\b"),
    ("free", r"\b(is it (really )?free|any (fee|cost|charge)s?|how much|what does it cost|pricing|price|credit card)\b"),
    ("how_it_works", r"\b(how (does|do) (it|this)|what (is|does) tryuu|how many (users|people|visitors|customers)|what do i get|tell me more about)\b"),
    ("yes", r"\b(yes|sure|sounds good|send (me )?(the )?(details|info|information|link)|i'?m interested|interested|happy to|go ahead|more info)\b"),
]
TEMPLATE_FOR = {"yes": "yes", "free": "free", "how_it_works": "how_it_works", "signed_up_cannot_find": "signed_up_cannot_find"}


def classify(text: str, is_auto: bool) -> str:
    if is_auto:
        return "auto"
    t = (text or "")[:600].lower()
    hits = [i for i, pat in RULES if re.search(pat, t)]
    if len(hits) == 1:
        return hits[0]
    if "stop" in hits:  # an unsubscribe always wins, even next to other words
        return "stop"
    if len(set(hits) - {"yes"}) == 1 and "yes" in hits:  # 'yes, is it free?' -> answer the question
        return (set(hits) - {"yes"}).pop()
    return "other"


def load_template(name: str):
    p = lib.CONFIG / "reply_templates" / f"{name}.md"
    if not p.exists():
        return None
    raw = p.read_text()
    m = re.match(r"---\n(.*?)\n---\n(.*)", raw, re.S)
    if not m or not re.search(r"^approved:\s*true", m.group(1), re.M):
        return None
    return m.group(2).strip()


def main():
    conn = lib.connect()
    if len(sys.argv) > 2 and sys.argv[1] == "mark-sent":
        for rid in sys.argv[2:]:
            conn.execute("UPDATE replies SET template_sent=intent, handled_at=? WHERE id=? AND template_sent IS NULL", (lib.now(), int(rid)))
        conn.commit()
        print(f"marked {len(sys.argv) - 2} replies as answered")
        return
    with lib.run(conn, "10_reply") as r:
        rows = conn.execute("SELECT * FROM replies WHERE intent IS NULL ORDER BY id").fetchall()
        r.n_in = len(rows)
        for rp in rows:
            conn.execute("UPDATE replies SET intent=? WHERE id=?", (classify(rp["text"], bool(rp["is_auto"])), rp["id"]))
        conn.commit()
        import importlib.util
        spec = importlib.util.spec_from_file_location("sup", Path(__file__).with_name("06_suppress.py"))
        sup = importlib.util.module_from_spec(spec); spec.loader.exec_module(sup)
        sup.rebuild(conn)
        out_items = []
        for rp in conn.execute("SELECT * FROM replies WHERE template_sent IS NULL AND handled_at IS NULL AND is_auto=0 "
                               "AND intent IN ('yes','free','how_it_works','signed_up_cannot_find')").fetchall():
            body = load_template(TEMPLATE_FOR[rp["intent"]])
            if body:  # unapproved template -> nothing is queued
                out_items.append({"reply_id": rp["id"], "reply_to_uuid": rp["instantly_email_id"], "eaccount": rp["eaccount"],
                                  "subject": "Re: " + re.sub(r"^(re:\s*)+", "", rp["subject"] or "", flags=re.I),
                                  "to": rp["from_email"], "body_text": body, "intent": rp["intent"]})
        (lib.ROOT / "reports" / "replies_to_send.json").write_text(json.dumps(out_items, indent=1, ensure_ascii=False))
        conn.execute("UPDATE replies SET handled_at=? WHERE handled_at IS NULL AND intent IN ('auto','stop','not_interested')", (lib.now(),))
        conn.commit()
        q = conn.execute("SELECT r.id, r.received_at, r.from_email, b.name, r.intent, substr(r.text,1,300) text FROM replies r "
                         "LEFT JOIN businesses b ON b.id=r.business_id WHERE r.intent IN ('other','signed_up_cannot_find') AND r.handled_at IS NULL "
                         "ORDER BY r.received_at").fetchall()
        with (lib.ROOT / "reports" / "review_queue.csv").open("w", newline="") as f:
            w = csv.writer(f); w.writerow(["id", "received", "from", "business", "intent", "text"])
            w.writerows([list(x) for x in q])
        r.n_out = len(rows)
        by = conn.execute("SELECT intent, COUNT(*) n FROM replies GROUP BY intent").fetchall()
        print("replies by intent:", {x["intent"]: x["n"] for x in by})
        print(f"review queue: {len(q)} (reports/review_queue.csv); approved-template replies to send: {len(out_items)} (reports/replies_to_send.json)")


if __name__ == "__main__":
    main()
