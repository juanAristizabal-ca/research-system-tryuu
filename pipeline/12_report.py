#!/usr/bin/env python3
"""Step 12 - Scorecard and feedback proposals. One row per batch, newest first. Writes
reports/<date>.md and reports/scorecard.csv (paste into the Control sheet's Scorecard tab).
The agent PROPOSES; the owner accepts or rejects in the change log. Nothing changes silently.

  12_report.py --placement 0.0     record the latest inbox-placement result (share in inbox, 0-1)"""
import argparse
import csv
import json
import statistics
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

QUESTION = ("yes", "free", "how_it_works", "signed_up_cannot_find", "other")


def batch_rows(conn) -> list[dict]:
    out = []
    for b in conn.execute("SELECT * FROM batches ORDER BY loaded_at DESC, id DESC").fetchall():
        bid = b["id"]
        loaded = b["n_loaded"] or 0
        bounced = conn.execute("SELECT COUNT(DISTINCT email) FROM sends WHERE batch_id=? AND bounced=1", (bid,)).fetchone()[0]
        delivered = max(loaded - bounced, 0)
        ons = conn.execute("SELECT o.*, (SELECT MIN(sent_at) FROM sends s WHERE s.business_id=o.business_id AND s.batch_id=?) first "
                           "FROM onboarded o WHERE o.batch_id=? AND o.in_window=1", (bid, bid)).fetchall()
        days = [(lib.parse_ts(o["date_created"]) - lib.parse_ts(o["first"])).days for o in ons if o["first"] and o["date_created"]]
        by_step = {1: 0, 2: 0, 3: 0}
        for o in ons:
            s = conn.execute("SELECT MAX(step) st FROM sends WHERE business_id=? AND batch_id=? AND sent_at<=?", (o["business_id"], bid, o["date_created"] + " 23:59:59")).fetchone()["st"]
            if s in by_step:
                by_step[s] += 1
        q = conn.execute(f"SELECT COUNT(*) FROM replies r JOIN batch_leads l ON l.business_id=r.business_id AND l.batch_id=? "
                         f"WHERE r.is_auto=0 AND r.intent IN ({','.join('?' * len(QUESTION))})", (bid, *QUESTION)).fetchone()[0]
        stop = conn.execute("SELECT COUNT(*) FROM replies r JOIN batch_leads l ON l.business_id=r.business_id AND l.batch_id=? "
                            "WHERE r.intent IN ('stop','not_interested')", (bid,)).fetchone()[0]
        unk = conn.execute("SELECT COUNT(*) FROM onboarded WHERE batch_id=? AND in_window IS NULL", (bid,)).fetchone()[0]
        age = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - lib.parse_ts(b["loaded_at"])).days
        pl = conn.execute("SELECT v FROM meta WHERE k='inbox_placement'").fetchone()
        out.append({
            "batch": b["name"], "segment": b["segment"], "tier": b["tier"], "copy_version": b["copy_version"],
            "scoring_version": b["scoring_version"], "loaded": loaded, "delivered": delivered,
            "bounce_rate": round(bounced / loaded, 4) if loaded else 0, "onboards": len(ons), "window_unknown": unk,
            "onboard_rate": round(len(ons) / delivered, 4) if delivered else 0,
            "median_days": statistics.median(days) if days else "", "step1": by_step[1], "step2": by_step[2], "step3": by_step[3],
            "question_rate": round(q / delivered, 4) if delivered else 0, "stop_rate": round(stop / delivered, 4) if delivered else 0,
            "research_precision": round(b["research_precision"] or 0, 3), "inbox_placement": pl["v"] if pl else "",
            "status": "maturing" if age < WINDOW else "final",
        })
    return out


WINDOW = 21


def group(rows, key):
    g = {}
    for r in rows:
        x = g.setdefault(r[key], {"delivered": 0, "onboards": 0, "final_delivered": 0, "final_onboards": 0})
        x["delivered"] += r["delivered"]; x["onboards"] += r["onboards"]
        if r["status"] == "final":
            x["final_delivered"] += r["delivered"]; x["final_onboards"] += r["onboards"]
    return g


def rate(x, final=True):
    d, o = (x["final_delivered"], x["final_onboards"]) if final else (x["delivered"], x["onboards"])
    return o / d if d else None


def proposals(conn, rows) -> list[str]:
    p = []
    seg = group([r for r in rows if r["status"] == "final"], "segment")
    rates = {k: rate(v) for k, v in seg.items() if v["final_delivered"] >= 300}
    best = max([v for v in rates.values() if v is not None], default=None)
    for k, v in rates.items():
        if best and v is not None and v < best / 4:
            p.append(f"Pause segment {k}: {seg[k]['final_delivered']} final delivered, rate {v:.2%} is under a quarter of the best ({best:.2%}).")
    for r in rows:
        if r["bounce_rate"] > 0.02:
            p.append(f"Hold the next batch after {r['batch']}: bounce rate {r['bounce_rate']:.1%} is over 2%. Re-verify.")
        if r["question_rate"] > 0.03:
            p.append(f"Add the most common question to email 2: question rate {r['question_rate']:.1%} in {r['batch']}.")
        if r["research_precision"] and r["research_precision"] < 0.9:
            p.append(f"Stop loading: research precision {r['research_precision']:.0%} in {r['batch']}. Fix the failing check.")
    pl = conn.execute("SELECT v FROM meta WHERE k='inbox_placement'").fetchone()
    if pl and float(pl["v"]) < 0.8:
        p.append(f"Hold ALL sending: latest inbox placement {float(pl['v']):.0%} is under 80%.")
    tot = conn.execute("SELECT COUNT(*) FROM onboarded").fetchone()[0]
    un = conn.execute("SELECT COUNT(*) FROM onboarded WHERE business_id IS NULL").fetchone()[0]
    if tot and un / tot > 0.2:
        p.append(f"Review matching rules: {un / tot:.0%} of the live list is unmatched (other source or missed matches).")
    # scoring signals, final leads only, needs 500+
    sigs = {}
    for s in conn.execute("SELECT s.signals, EXISTS(SELECT 1 FROM onboarded o WHERE o.business_id=l.business_id AND o.in_window=1) won "
                          "FROM batch_leads l JOIN batches b ON b.id=l.batch_id JOIN scores s ON s.business_id=l.business_id "
                          "WHERE julianday('now') - julianday(b.loaded_at) >= ?", (WINDOW,)):
        for k, v in json.loads(s["signals"]).items():
            a = sigs.setdefault(k, {True: [0, 0], False: [0, 0]})[bool(v)]
            a[0] += 1; a[1] += s["won"]
    for k, a in sigs.items():
        n = a[True][0] + a[False][0]
        if n >= 500 and a[True][0] and a[False][0]:
            w, wo = a[True][1] / a[True][0], a[False][1] / a[False][0]
            if w <= wo:
                p.append(f"Cut signal '{k}' to weight 0: onboard rate {w:.2%} with vs {wo:.2%} without over {n} final leads.")
            elif wo and w > 2 * wo:
                p.append(f"Raise signal '{k}': onboard rate {w:.2%} with vs {wo:.2%} without over {n} final leads.")
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--placement", type=float)
    a = ap.parse_args()
    conn = lib.connect()
    if a.placement is not None:
        conn.execute("INSERT OR REPLACE INTO meta(k, v, updated) VALUES ('inbox_placement', ?, ?)", (str(a.placement), lib.now()))
        conn.commit()
    with lib.run(conn, "12_report") as r:
        rows = batch_rows(conn)
        r.n_out = len(rows)
        day = lib.now()[:10]
        d = lib.ROOT / "reports"
        if rows:
            with (d / "scorecard.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        lines = [f"# Scorecard {day}", "", "Onboard rate = onboards (0-21 days after email 1) / delivered. 'window unknown' rows have no signup date.", ""]
        if rows:
            lines += ["| Batch | Loaded | Delivered | Bounce | Onboards | Rate | Median days | Q rate | Stop rate | Precision | Status |",
                      "|---|---|---|---|---|---|---|---|---|---|---|"]
            for x in rows:
                lines.append(f"| {x['batch']} | {x['loaded']} | {x['delivered']} | {x['bounce_rate']:.1%} | {x['onboards']} (+{x['window_unknown']} unknown) | "
                             f"{x['onboard_rate']:.2%} | {x['median_days']} | {x['question_rate']:.1%} | {x['stop_rate']:.1%} | {x['research_precision']:.0%} | {x['status']} |")
            for key in ("segment", "copy_version"):
                lines += ["", f"## By {key}", "", f"| {key} | Delivered | Onboards | Rate |", "|---|---|---|---|"]
                for k, v in group(rows, key).items():
                    rr = rate(v, final=False)
                    lines.append(f"| {k} | {v['delivered']} | {v['onboards']} | {rr:.2%} |" if rr is not None else f"| {k} | 0 | 0 | |")
        else:
            lines.append("No batches loaded yet.")
        pr = proposals(conn, rows)
        lines += ["", "## Proposed changes (accept or reject in the change log)", ""] + ([f"- {x}" for x in pr] or ["- None."])
        funnel = conn.execute("SELECT status, COUNT(*) n FROM businesses GROUP BY status ORDER BY n DESC").fetchall()
        lines += ["", "## Pipeline", "", ", ".join(f"{f['status']}: {f['n']}" for f in funnel)]
        tiers = conn.execute("SELECT tier, COUNT(*) n FROM scores GROUP BY tier").fetchall()
        lines += ["", "Tier counts: " + ", ".join(f"{t['tier']}={t['n']}" for t in tiers)]
        (d / f"{day}.md").write_text("\n".join(lines) + "\n")
        print("\n".join(lines))


if __name__ == "__main__":
    main()
