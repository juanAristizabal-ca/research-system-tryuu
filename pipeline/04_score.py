#!/usr/bin/env python3
"""Step 4 - Score. Code applies config/scoring.yaml; the score and its signals are stored with the
scoring version so results can be compared across versions. Tier A >= 70, Tier B 45-69, rest X."""
import json
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def compute(conn, b, cfg: dict, segs: dict, cities: set) -> tuple[int, dict]:
    ev = {r["field"]: r for r in conn.execute("SELECT * FROM evidence WHERE business_id=?", (b["id"],))}
    chosen = conn.execute("SELECT * FROM contacts WHERE business_id=? AND is_chosen=1", (b["id"],)).fetchone()
    w = cfg["signals"]
    sig = {
        "proven_segment": bool(b["segment"] and segs.get(b["segment"], {}).get("proven")),
        "free_offer_with_quote": "offer_sentence" in ev and bool(ev["offer_sentence"]["quote"]) and bool(ev["offer_sentence"]["url"]),
        "named_owner": "owner_first_name" in ev,
        "person_mailbox": bool(chosen and chosen["is_person"]),
        "one_or_two_locations": b["location_count"] in (1, 2),
        "launch_city_postal": bool(b["postal_code"] and b["postal_code"].upper().startswith("V") and (b["city"] or "").lower() in cities),
        "new_licence_12m": False,
        "online_booking_link": "booking_link" in ev,
    }
    cutoff = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    for r in conn.execute("SELECT payload FROM raw_rows WHERE business_id=?", (b["id"],)):
        d = json.loads(r["payload"])["n"].get("licence_date", "")
        if d and d >= cutoff:
            sig["new_licence_12m"] = True
    return sum(w[k] for k, v in sig.items() if v), sig


def tier_for(score: int, tiers: dict) -> str:
    return "A" if score >= tiers["A"] else "B" if score >= tiers["B"] else "X"


def main():
    conn = lib.connect()
    ver = lib.config_version(conn, "scoring.yaml")
    cfg, segs, cities = lib.load_yaml("scoring.yaml"), lib.load_yaml("segments.yaml")["segments"], lib.city_set()
    with lib.run(conn, "04_score") as r:
        rows = conn.execute("SELECT * FROM businesses WHERE status IN ('enriched','scored','verified')").fetchall()
        r.n_in = len(rows)
        counts = {"A": 0, "B": 0, "X": 0}
        for b in rows:
            score, sig = compute(conn, b, cfg, segs, cities)
            tier = tier_for(score, cfg["tiers"])
            conn.execute("INSERT OR REPLACE INTO scores(business_id, score, tier, signals, scoring_version, scored_at) VALUES (?,?,?,?,?,?)",
                         (b["id"], score, tier, json.dumps(sig), ver, lib.now()))
            if tier != "X" and b["status"] == "enriched":
                lib.set_status(conn, b["id"], "scored")
            counts[tier] += 1
            r.n_out += 1
        conn.commit()
        print(f"scoring {ver}: Tier A={counts['A']}  Tier B={counts['B']}  not contacted={counts['X']}")


if __name__ == "__main__":
    main()
