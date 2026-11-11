#!/usr/bin/env python3
"""Step 6 - Suppress. Rebuilds the suppression table from facts in the database, then blocks businesses.

Sources: live businesses (onboarded), bounces, stop / not-interested replies, owner name patterns.
Re-entry: a business whose last email was more than the cooldown ago, with no reply and no active
suppression, goes back to 'enriched' so it is re-scored with the current config (plan 9.4)."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def add(conn, kind, value, reason, days=None):
    if not value:
        return
    exp = None if days is None else (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT OR IGNORE INTO suppression(kind, value, reason, expires) VALUES (?,?,?,?)", (kind, value, reason, exp))


def rebuild(conn) -> int:
    seed = lib.load_yaml("suppression_seed.yaml")
    before = conn.execute("SELECT COUNT(*) FROM suppression").fetchone()[0]
    for o in conn.execute("SELECT name, signup_email, website, is_test_email FROM onboarded"):
        add(conn, "name", lib.norm_name(o["name"]), "live on Tryuu")
        if o["signup_email"] and not o["is_test_email"]:
            add(conn, "email", lib.norm_email(o["signup_email"]), "live on Tryuu")
            d = lib.email_domain(o["signup_email"])
            if d and not lib.is_free_mail(d):
                add(conn, "domain", d, "live on Tryuu")
        d = lib.norm_domain(o["website"])
        if d:
            add(conn, "domain", d, "live on Tryuu")
    for s in conn.execute("SELECT DISTINCT email FROM sends WHERE bounced=1 AND email IS NOT NULL"):
        add(conn, "email", lib.norm_email(s["email"]), "bounced")
    for rp in conn.execute("SELECT from_email, intent FROM replies WHERE intent IN ('stop','not_interested') AND from_email IS NOT NULL"):
        e = lib.norm_email(rp["from_email"])
        if rp["intent"] == "stop":
            add(conn, "email", e, "unsubscribed")
            d = lib.email_domain(e)
            if d and not lib.is_free_mail(d):
                add(conn, "domain", d, "unsubscribed")
        else:
            add(conn, "email", e, "not interested", seed["not_interested_days"])
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] - before


def main():
    conn = lib.connect()
    seed = lib.load_yaml("suppression_seed.yaml")
    with lib.run(conn, "06_suppress") as r:
        added = rebuild(conn)
        rows = conn.execute("SELECT id, status FROM businesses WHERE status NOT IN ('onboarded','closed')").fetchall()
        r.n_in = len(rows)
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=seed["cooldown_days"])).strftime("%Y-%m-%d %H:%M:%S")
        blocked = reentered = 0
        for b in rows:
            reasons = lib.suppression_reasons(conn, b["id"])
            if reasons:
                if b["status"] in ("discovered", "enriched", "scored", "verified", "suppressed"):
                    lib.set_status(conn, b["id"], "suppressed")
                    blocked += 1
            elif b["status"] == "suppressed":
                lib.set_status(conn, b["id"], "enriched"); reentered += 1
            elif b["status"] in ("sent", "loaded"):
                last = conn.execute("SELECT MAX(sent_at) m FROM sends WHERE business_id=?", (b["id"],)).fetchone()["m"]
                replied = conn.execute("SELECT 1 FROM replies WHERE business_id=? AND is_auto=0", (b["id"],)).fetchone()
                if b["status"] == "sent" and last and last < cutoff and not replied:
                    lib.set_status(conn, b["id"], "enriched"); reentered += 1
        conn.commit()
        r.n_out = blocked
        print(f"suppression rows +{added}; blocked {blocked}; re-entered after cooldown {reentered}")


if __name__ == "__main__":
    main()
