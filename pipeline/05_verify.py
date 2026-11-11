#!/usr/bin/env python3
"""Step 5 - Verify the chosen email of Tier A/B businesses only (verification is the scarce resource).
Reacher (self-hosted, needs outbound port 25, REACHER_URL) first; Instantly verification (INSTANTLY_API_KEY)
for what Reacher cannot decide. Only valid addresses load; catch-all domains cap the business at Tier B."""
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

INSTANTLY = "https://api.instantly.ai/api/v2"


def reacher(email: str) -> str | None:
    url = os.environ.get("REACHER_URL")
    if not url:
        return None
    try:
        r = requests.post(url.rstrip("/") + "/v0/check_email", json={"to_email": email}, timeout=60)
        j = r.json()
    except (requests.RequestException, ValueError):
        return None
    if j.get("smtp", {}).get("is_catch_all"):
        return "catch_all"
    return {"safe": "valid", "risky": "risky", "invalid": "invalid"}.get(j.get("is_reachable"))  # 'unknown' -> None


def instantly(email: str) -> str | None:
    key = os.environ.get("INSTANTLY_API_KEY")
    if not key:
        return None
    h = {"Authorization": f"Bearer {key}"}
    try:
        j = requests.post(f"{INSTANTLY}/email-verification", json={"email": email}, headers=h, timeout=60).json()
        for _ in range(10):
            if j.get("verification_status") != "pending":
                break
            time.sleep(3)
            j = requests.get(f"{INSTANTLY}/email-verification/{email}", headers=h, timeout=60).json()
    except (requests.RequestException, ValueError):
        return None
    st = j.get("verification_status")
    if st == "verified":
        return "catch_all" if j.get("catch_all") else "valid"
    return {"invalid": "invalid"}.get(st)


def main():
    conn = lib.connect()
    if not (os.environ.get("REACHER_URL") or os.environ.get("INSTANTLY_API_KEY")):
        sys.exit("Set REACHER_URL and/or INSTANTLY_API_KEY: nothing can be verified without one (and unverified emails never load).")
    max_age = lib.load_yaml("sources.yaml")["verify"]["max_age_days"]
    with lib.run(conn, "05_verify") as r:
        rows = conn.execute(
            "SELECT c.id cid, c.email, c.verification, c.verified_at, b.id bid, b.status, s.tier FROM contacts c "
            "JOIN businesses b ON b.id=c.business_id JOIN scores s ON s.business_id=b.id "
            "WHERE c.is_chosen=1 AND s.tier IN ('A','B') AND b.status IN ('scored','verified')").fetchall()
        r.n_in = len(rows)
        for row in rows:
            fresh = row["verified_at"] and (lib.parse_ts(lib.now()) - lib.parse_ts(row["verified_at"])).days < max_age
            res = row["verification"] if fresh else (reacher(row["email"]) or instantly(row["email"]))
            if res is None:
                r.errors.append(f"{row['email']}: undecided")
                continue
            conn.execute("UPDATE contacts SET verification=?, verified_at=? WHERE id=?", (res, lib.now(), row["cid"]))
            if res in ("valid", "catch_all"):
                lib.set_status(conn, row["bid"], "verified")
                if res == "catch_all" and row["tier"] == "A":
                    conn.execute("UPDATE scores SET tier='B' WHERE business_id=?", (row["bid"],))
                r.n_out += 1
            else:
                lib.set_status(conn, row["bid"], "enriched")  # needs a different mailbox
            conn.commit()


if __name__ == "__main__":
    main()
