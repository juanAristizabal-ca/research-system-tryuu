#!/usr/bin/env python3
"""One-time import of what exists today (plan 4.7). Idempotent; every source is archived untouched.

  import_legacy.py master  <values.json>    Master tab as {"values": [[...]]} (Sheets API output)
  import_legacy.py instantly <leads.csv>    Instantly leads export: gives campaign, status and last-contact dates
  import_legacy.py instantly-json <page.json> ... [--campaigns <list_campaigns.json> ...]
                                            pages saved by the Instantly connector (list_leads); names from list_campaigns
  import_legacy.py csv <file.csv> [...]     any old research CSV (Email, Business_Name, City, Website, ...)
Onboarded list: use 11_match.py <file>. Run 06_suppress.py afterwards to rebuild suppression."""
import csv
import json
import shutil
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

CONTACTED = {"contacted - no reply", "contacted (trello)", "follow-up sent", "contacted", "contacted (instagram)"}
REPLIED = {"replied", "interested", "replied - auto/out of office"}


def archive(path: Path) -> str:
    d = lib.ROOT / "data" / "raw" / lib.now()[:10]
    d.mkdir(parents=True, exist_ok=True)
    if not (d / path.name).exists():
        shutil.copy2(path, d / path.name)
    return str((d / path.name).relative_to(lib.ROOT))


def upsert(conn, email: str, name: str, city: str, website: str, origin: str, phone: str = "") -> int | None:
    email = lib.norm_email(email)
    if not lib.EMAIL_RE.fullmatch(email) or not name.strip():
        return None
    dom = lib.email_domain(email)
    dom = "" if lib.is_free_mail(dom) else dom
    wdom = lib.norm_domain(website)
    wdom = "" if lib.is_free_mail(wdom) else wdom
    dom = wdom or dom
    site = (website if website.startswith("http") else f"https://{wdom}") if wdom else (f"https://{dom}" if dom else None)
    row = conn.execute("SELECT id FROM businesses WHERE domain=?", (dom,)).fetchone() if dom else \
        conn.execute("SELECT id FROM businesses WHERE key=?", (f"e:{email}",)).fetchone()
    if row:
        bid = row["id"]
    else:
        cur = conn.execute("INSERT INTO businesses(key, domain, name, city, phone, website, origin) VALUES (?,?,?,?,?,?,?)",
                           (lib.business_key(dom, "", email), dom or None, name.strip(), lib.norm_city(city) or None,
                            lib.norm_phone(phone) or None, site, origin))
        bid = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO contacts(business_id, email, source_url) VALUES (?,?,?)", (bid, email, f"legacy:{origin}"))
    return bid


def supp(conn, kind, value, reason, days=None):
    exp = None if days is None else (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT OR IGNORE INTO suppression(kind, value, reason, expires) VALUES (?,?,?,?)", (kind, value, reason, exp))


def legacy_send(conn, bid, email, campaign, sent_at=None, bounced=0):
    if not conn.execute("SELECT 1 FROM sends WHERE legacy=1 AND email=? AND campaign_name IS ?", (email, campaign)).fetchone():
        conn.execute("INSERT INTO sends(business_id, email, campaign_name, sent_at, bounced, legacy) VALUES (?,?,?,?,?,1)",
                     (bid, email, campaign, sent_at, bounced))


def do_master(path: Path):
    vals = json.loads(path.read_text())["values"]
    archive(path)
    stats = {}
    for r in vals[1:]:
        r = (r + [""] * 8)[:8]
        email, name, typ, city, status, camp, elig = r[:7]
        if not email:
            continue
        bid = upsert(conn, email, name, city, "", "master")
        if bid is None:
            stats["skipped_invalid"] = stats.get("skipped_invalid", 0) + 1
            continue
        e = lib.norm_email(email)
        s = status.strip().lower()
        stats[s] = stats.get(s, 0) + 1
        if s in CONTACTED or s.startswith("queued"):
            legacy_send(conn, bid, e, camp or "master")
            lib.set_status(conn, bid, "loaded" if s.startswith("queued") else "sent")
        elif s in REPLIED:
            legacy_send(conn, bid, e, camp or "master")
            lib.set_status(conn, bid, "replied")
        elif s == "bounced":
            legacy_send(conn, bid, e, camp or "master", bounced=1)
            supp(conn, "email", e, "bounced (Master)")
        elif s == "not interested":
            supp(conn, "email", e, "not interested (Master)", 180)
        elif s.startswith("excluded"):
            supp(conn, "email", e, "excluded - review (Master)")
        elif s == "onboarded":
            lib.set_status(conn, bid, "sent")  # becomes 'onboarded' when 11_match finds it in the live list
    conn.commit()
    return stats


def pick(row: dict, *names):
    low = {k.lower().strip(): v for k, v in row.items() if k}
    for n in names:
        if low.get(n):
            return low[n].strip()
    return ""


def do_instantly(path: Path):
    archive(path)
    stats = {"rows": 0, "bounced": 0, "unsubscribed": 0, "no_email": 0}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            stats["rows"] += 1
            email = lib.norm_email(pick(row, "email"))
            if not email:
                stats["no_email"] += 1
                continue
            name = pick(row, "business_name", "company_name", "company") or pick(row, "first_name") or email.split("@")[0]
            bid = upsert(conn, email, name, pick(row, "city"), pick(row, "website", "company_website"), "instantly", pick(row, "phone"))
            if bid is None:
                continue
            camp = pick(row, "campaign", "campaign_name", "campaign_id")
            status = pick(row, "status", "lead_status", "email_status").lower()
            last = pick(row, "timestamp_last_contact", "last_contacted", "last_contact", "timestamp_created", "created")
            ts = lib.parse_ts(last)
            bounced = int("bounce" in status or status == "-1")
            legacy_send(conn, bid, email, camp, ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None, bounced)
            if bounced:
                supp(conn, "email", email, "bounced (Instantly)"); stats["bounced"] += 1
            if "unsub" in status or status == "-2":
                supp(conn, "email", email, "unsubscribed (Instantly)")
                d = lib.email_domain(email)
                if not lib.is_free_mail(d):
                    supp(conn, "domain", d, "unsubscribed (Instantly)")
                stats["unsubscribed"] += 1
            if conn.execute("SELECT status FROM businesses WHERE id=?", (bid,)).fetchone()["status"] in ("discovered", "enriched"):
                lib.set_status(conn, bid, "sent")
    conn.commit()
    return stats


def do_instantly_json(path: Path, names: dict):
    """list_leads connector pages. Status: 1 active, 2 paused, 3 completed, -1 bounced, -2 unsubscribed, -3 skipped."""
    j = json.loads(path.read_text())
    items = j["items"] if isinstance(j, dict) else j
    stats = {"rows": 0, "bounced": 0, "unsubscribed": 0, "replied": 0}
    for it in items:
        stats["rows"] += 1
        email = lib.norm_email(it.get("email"))
        pl = it.get("payload") or {}
        name = pl.get("Business_Name") or pl.get("company_name") or it.get("company_name") or it.get("first_name") or email.split("@")[0]
        bid = upsert(conn, email, name, pl.get("City", ""), it.get("website") or it.get("company_domain") or "", "instantly")
        if bid is None:
            continue
        if it.get("lt_interest_status") not in (None, 0):
            conn.execute("INSERT OR REPLACE INTO evidence(business_id, field, value, quote) VALUES (?,?,?,?)",
                         (bid, "instantly_interest", str(it["lt_interest_status"]), "Instantly lead interest status"))
        camp = names.get(it.get("campaign"), it.get("campaign"))
        ts = lib.parse_ts(it.get("timestamp_last_contact") or "")
        st = it.get("status")
        legacy_send(conn, bid, email, camp, ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None, int(st == -1))
        if st == -1:
            supp(conn, "email", email, "bounced (Instantly)"); stats["bounced"] += 1
        elif st == -2:
            supp(conn, "email", email, "unsubscribed (Instantly)")
            d = lib.email_domain(email)
            if not lib.is_free_mail(d):
                supp(conn, "domain", d, "unsubscribed (Instantly)")
            stats["unsubscribed"] += 1
        cur = conn.execute("SELECT status FROM businesses WHERE id=?", (bid,)).fetchone()["status"]
        if (it.get("email_reply_count") or 0) > 0 and cur in ("discovered", "enriched", "sent", "loaded"):
            lib.set_status(conn, bid, "replied"); stats["replied"] += 1
        elif it.get("timestamp_last_contact") and cur in ("discovered", "enriched"):
            lib.set_status(conn, bid, "sent")
    conn.commit()
    return stats


def campaign_names(files: list) -> dict:
    out = {}
    for f in files:
        j = json.loads(Path(f).read_text())
        for c in (j["items"] if isinstance(j, dict) else j):
            out[c["id"]] = c["name"]
    return out


def do_csv(path: Path):
    archive(path)
    n = 0
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            bid = upsert(conn, pick(row, "email"), pick(row, "business_name", "business"), pick(row, "city"),
                         pick(row, "website"), f"csv:{path.name}", pick(row, "phone"))
            n += bool(bid)
    conn.commit()
    return {"imported": n}


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("master", "instantly", "instantly-json", "csv"):
        sys.exit(__doc__)
    conn = lib.connect()
    args, names = sys.argv[2:], {}
    if "--campaigns" in args:
        i = args.index("--campaigns")
        names = campaign_names(args[i + 1:])
        args = args[:i]
    fn = {"master": do_master, "instantly": do_instantly, "csv": do_csv,
          "instantly-json": lambda x: do_instantly_json(x, names)}[sys.argv[1]]
    with lib.run(conn, f"import_legacy:{sys.argv[1]}") as r:
        for p in args:
            print(p, fn(Path(p)))
            r.n_in += 1
        r.n_out = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
