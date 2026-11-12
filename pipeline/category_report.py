#!/usr/bin/env python3
"""Category coverage: what has already been searched, how deep, and what is still fresh.

  category_report.py            writes reports/category_coverage.csv and .md, prints the summary
Business types come from the Instantly export / Master sheet (evidence 'legacy_business_type') or, for newly
researched businesses, from the extracted segment. Matching: the first category in config/categories.yaml whose `types`
tokens match (word-start match; a token starting with '=' must match the whole word, plural allowed).

'status' is a SUGGESTION from our own data only. 'exhausted' means OUR sweeps are used up (few never-contacted left),
not that the market is: a real market estimate needs a probe with the Maps scraper (see CLAUDE.md)."""
import collections
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

DEEP, FRESH_MIN, UNEXPLORED_MAX = 300, 25, 20


def compile_rules(cats: dict):
    rules = []
    for slug, c in cats.items():
        pats = []
        for t in c.get("types", []):
            t = str(t).lower()
            pats.append(re.compile(r"\b" + re.escape(t[1:]) + r"(s|es)?\b") if t.startswith("=") else re.compile(r"\b" + re.escape(t)))
        rules.append((slug, pats))
    return rules


def classify(text: str, rules) -> str:
    t = (text or "").lower()
    for slug, pats in rules:
        if any(p.search(t) for p in pats):
            return slug
    return "other_unclassified"


def suggest(known: int, emailed: int, fresh: int, campaigns: int, excluded: bool, proposed: bool = False, slug: str = "") -> str:
    if slug == "other_unclassified":
        return "needs classification"
    if excluded:
        return "not a target"
    if proposed and emailed == 0:
        return "proposed, not searched yet"
    if emailed == 0 and known < UNEXPLORED_MAX and campaigns == 0:
        return "unexplored"
    if fresh < FRESH_MIN and emailed >= 100:
        return "exhausted by our sweeps"
    if emailed >= DEEP and fresh >= FRESH_MIN:
        return "explored, some fresh left"
    if emailed == 0:
        return "found, never contacted"
    return "partly explored"


def main():
    conn = lib.connect()
    cfg = lib.load_yaml("categories.yaml")
    cats, groups = cfg["categories"], cfg["groups"]
    rules = compile_rules(cats)
    types = {r["business_id"]: r["value"] for r in conn.execute("SELECT business_id, value FROM evidence WHERE field='legacy_business_type'")}
    seg = {r["business_id"]: r["value"] for r in conn.execute("SELECT business_id, value FROM evidence WHERE field='segment'")}
    onboard_ids = {r["business_id"] for r in conn.execute("SELECT business_id FROM onboarded WHERE business_id IS NOT NULL")}
    interested = {r["business_id"] for r in conn.execute("SELECT business_id FROM evidence WHERE field='instantly_interest' AND value='1'")}
    bounced = {r["business_id"] for r in conn.execute("SELECT DISTINCT business_id FROM sends WHERE bounced=1 AND business_id IS NOT NULL")}
    stat = collections.defaultdict(lambda: collections.Counter())
    unclassified = collections.Counter()
    for b in conn.execute("SELECT id, status FROM businesses"):
        text = types.get(b["id"]) or seg.get(b["id"]) or ""
        slug = classify(text, rules)
        if slug == "other_unclassified" and text:
            unclassified[text.lower()] += 1
        s = stat[slug]
        s["known"] += 1
        s[b["status"]] += 1
        if b["status"] in ("sent", "replied", "loaded", "onboarded"):
            s["emailed"] += 1
        if b["id"] in onboard_ids or b["status"] == "onboarded":
            s["live"] += 1
        s["interested"] += b["id"] in interested
        s["bounced"] += b["id"] in bounced
    rows = []
    for slug, c in cats.items():
        s = stat.get(slug, collections.Counter())
        emailed, fresh = s["emailed"], s["discovered"] + s["enriched"] + s["scored"] + s["verified"]
        rows.append({
            "category": slug, "group": c["group"], "known": s["known"], "emailed": emailed, "fresh_never_contacted": fresh,
            "replied": s["replied"], "interested": s["interested"], "live": s["live"], "bounced": s["bounced"],
            "live_per_1000_emailed": round(1000 * s["live"] / emailed, 1) if emailed else "",
            "legacy_campaigns": " ".join(map(str, c.get("campaigns", []))), "proven": bool(c.get("proven")),
            "status": suggest(s["known"], emailed, fresh, len(c.get("campaigns", [])), bool(c.get("excluded")), bool(c.get("proposed")), slug)})
    out = lib.ROOT / "reports"
    out.mkdir(exist_ok=True)
    with (out / "category_coverage.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    lines = ["# Category coverage", "", "| group | category | known | emailed | fresh | live | live/1000 | interested | status |", "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["group"], -r["known"])):
        lines.append(f"| {groups[r['group']]} | {r['category']} | {r['known']} | {r['emailed']} | {r['fresh_never_contacted']} | {r['live']} | {r['live_per_1000_emailed']} | {r['interested']} | {r['status']} |")
    lines += ["", "## Top unclassified types (add tokens to config/categories.yaml)", ""] + [f"- {t}: {n}" for t, n in unclassified.most_common(40)]
    (out / "category_coverage.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:4 + len(rows)]))
    print(f"\nunclassified businesses with a type: {sum(unclassified.values())}; without any type: {stat['other_unclassified']['known'] - sum(unclassified.values())}")
    print("top unclassified:", "; ".join(f"{t}={n}" for t, n in unclassified.most_common(30)))


if __name__ == "__main__":
    main()
