#!/usr/bin/env python3
"""Step 11 - Match the daily live-business upload to leads.

  11_match.py <file.csv|tsv>    upload (paste export). Headers recognised: name, email, website, phone, date, city.
                                A headerless 2-column file is read as name, email.
  11_match.py                   re-match what is already stored

Rules, first hit wins (plan 8.2): 1 signup email = lead email; 2 email/website domain = lead domain (free-mail skipped);
3 phone; 4 normalised name + city above a fixed similarity; 5 no match -> 'other source'.
Rows whose signup email is a team placeholder (testcompany+N@tryuu.com, juan+N@tryuu.com) skip rules 1-2.
An onboard counts for its batch only when date created is 0-21 days after the first email; with no date the
window is left unknown (in_window NULL) and reported separately."""
import csv
import re
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

THRESH = 0.90
WINDOW_DAYS = 21
PLACEHOLDER = re.compile(r"^(testcompany|juan)\+\d+@tryuu\.com$", re.I)
HEAD = {"name": ["business on tryuu.com", "business name", "business", "name"], "email": ["signup email", "email"],
        "website": ["website", "url", "site"], "phone": ["phone", "telephone"], "date": ["date created", "date", "created"],
        "city": ["city"]}


def read_upload(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    delim = "\t" if text.count("\t") >= text.count(",") else ","
    rows = list(csv.reader(text.splitlines(), delimiter=delim))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]
    idx = {k: next((head.index(c) for c in cands if c in head), None) for k, cands in HEAD.items()}
    body = rows[1:] if idx["name"] is not None else rows
    if idx["name"] is None:
        idx = {"name": 0, "email": 1, "website": None, "phone": None, "date": None, "city": None}
    out = []
    for r in body:
        get = lambda k: (r[idx[k]].strip() if idx[k] is not None and idx[k] < len(r) else "")
        if get("name"):
            out.append({k: get(k) for k in HEAD})
    return out


def store(conn, rows: list[dict]) -> int:
    n = 0
    for r in rows:
        email = re.sub(r"[^\w.+@\-]", "", r["email"].replace("⚠️", "")).lower() if r["email"] else ""
        test = bool(PLACEHOLDER.match(email)) or "⚠" in r["email"]
        d = lib.parse_ts(r["date"])
        cur = conn.execute("INSERT OR IGNORE INTO onboarded(name, signup_email, website, phone, city, date_created, is_test_email) VALUES (?,?,?,?,?,?,?)",
                           (r["name"], email, r["website"] or None, lib.norm_phone(r["phone"]) or None, r["city"] or None,
                            d.strftime("%Y-%m-%d") if d else None, int(test)))
        n += cur.rowcount
    conn.commit()
    return n


def find_business(conn, o) -> tuple[int | None, str | None]:
    email, test = lib.norm_email(o["signup_email"]), o["is_test_email"]
    if email and not test:
        r = conn.execute("SELECT business_id FROM contacts WHERE email=? LIMIT 1", (email,)).fetchone()
        if r:
            return r["business_id"], "email"
        dom = lib.email_domain(email)
        if dom and not lib.is_free_mail(dom):
            r = conn.execute("SELECT id FROM businesses WHERE domain=?", (dom,)).fetchone()
            if r:
                return r["id"], "domain"
    wdom = lib.norm_domain(o["website"])
    if wdom:
        r = conn.execute("SELECT id FROM businesses WHERE domain=?", (wdom,)).fetchone()
        if r:
            return r["id"], "domain"
    if o["phone"]:
        r = conn.execute("SELECT id FROM businesses WHERE phone=?", (o["phone"],)).fetchone()
        if r:
            return r["id"], "phone"
    best, best_id = 0.0, None
    for b in conn.execute("SELECT id, name, city FROM businesses WHERE status!='suppressed' OR 1=1"):
        if o["city"] and b["city"] and lib.norm_city(o["city"]).lower() != b["city"].lower():
            continue
        s = lib.name_similarity(o["name"], b["name"])
        if s > best:
            best, best_id = s, b["id"]
    return (best_id, "name") if best >= THRESH else (None, None)


def window(conn, bid: int, date_created: str | None):
    first = conn.execute("SELECT MIN(sent_at) m FROM sends WHERE business_id=? AND sent_at IS NOT NULL", (bid,)).fetchone()["m"]
    batch = conn.execute("SELECT batch_id FROM batch_leads WHERE business_id=? ORDER BY batch_id DESC LIMIT 1", (bid,)).fetchone()
    inw = None
    if date_created and first:
        days = (lib.parse_ts(date_created) - lib.parse_ts(first)).days
        inw = int(0 <= days <= WINDOW_DAYS)
    return inw, (batch["batch_id"] if batch else None)


def match_all(conn) -> dict:
    stats = {"matched": 0, "other_source": 0, "in_window": 0, "window_unknown": 0}
    for o in conn.execute("SELECT * FROM onboarded WHERE business_id IS NULL").fetchall():
        bid, rule = find_business(conn, o)
        if bid is None:
            stats["other_source"] += 1
            continue
        inw, batch_id = window(conn, bid, o["date_created"])
        conn.execute("UPDATE onboarded SET business_id=?, match_rule=?, in_window=?, batch_id=? WHERE id=?", (bid, rule, inw, batch_id, o["id"]))
        if inw != 0:  # earlier than email 1 means already on Tryuu: do not flip status
            lib.set_status(conn, bid, "onboarded")
        stats["matched"] += 1
        stats["in_window" if inw == 1 else "window_unknown" if inw is None else "other_source"] += 1
    conn.commit()
    return stats


def main():
    conn = lib.connect()
    with lib.run(conn, "11_match") as r:
        if len(sys.argv) > 1:
            r.n_in = store(conn, read_upload(Path(sys.argv[1])))
        s = match_all(conn)
        r.n_out = s["matched"]
        tot = conn.execute("SELECT COUNT(*) FROM onboarded").fetchone()[0]
        un = conn.execute("SELECT COUNT(*) FROM onboarded WHERE business_id IS NULL").fetchone()[0]
        print(f"uploaded rows stored: {tot}; unmatched ('other source'): {un} ({un / tot:.0%} of live list)" if tot else "no uploads yet")
        if tot and un / tot > 0.2:
            print("note: more than 20% unmatched. The feedback rule says review the matching rules.")
        print(f"this run: {s}")


if __name__ == "__main__":
    main()
