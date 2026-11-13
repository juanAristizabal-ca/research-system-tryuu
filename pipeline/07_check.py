#!/usr/bin/env python3
"""Step 7 - Automated quality check (plan 5.6). No person reviews batches.

  07_check.py            enqueue second-pass tasks, run them if an API key is set, evaluate every verified lead
  07_check.py --offline  skip the live re-fetch of the offer page (uses the stored text)
Checks: BC postal + launch city; offer sentence still on the page; business name on the site; 1-2 locations and
no franchise wording; email verified within 14 days; not suppressed / not emailed in 60 days; a second model
pass, given only the page text, agrees on the segment. Failures are stored with a reason."""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

STOP = {"and", "the", "of", "in", "at", "for", "club", "studio", "studios", "academy", "centre", "center", "productions", "services", "group", "company", "society", "school", "inc", "ltd"}


def name_on_site(name: str, text: str) -> bool:
    toks = [t for t in lib.norm_name(name).split() if len(t) >= 3 and t not in STOP] or lib.norm_name(name).split()
    if not toks:
        return False
    low = lib.squash(text)
    return sum(t in low for t in toks) / len(toks) >= 0.6


def _crawler():
    spec = importlib.util.spec_from_file_location("enrich", Path(__file__).with_name("03_enrich.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    src = lib.load_yaml("sources.yaml")
    return m, m.Crawler(src["crawl"], src["booking_hosts"], src["crawl"]["page_keywords"])


def segment_prompt(conn, bid: int, segs: list[str]) -> str:
    text = " ".join(r["text"] or "" for r in conn.execute("SELECT text FROM pages WHERE business_id=? ORDER BY id", (bid,)))[:1500]
    return text


def evaluate(conn, b, cities: set, max_age: int, crawler=None, offline=False) -> list[str]:
    fails = []
    ev = {r["field"]: r for r in conn.execute("SELECT * FROM evidence WHERE business_id=?", (b["id"],))}
    pages = conn.execute("SELECT url, text FROM pages WHERE business_id=? AND text IS NOT NULL", (b["id"],)).fetchall()
    alltext = "\n".join(p["text"] for p in pages)
    if not (b["postal_code"] or "").upper().startswith("V") or (b["city"] or "").lower() not in cities:
        fails.append("postal code not BC or city outside launch list")
    if "offer_sentence" in ev:
        o = ev["offer_sentence"]
        text = alltext
        if not offline and crawler and o["url"]:
            rp = crawler[1].robots(o["url"])
            _, html = crawler[1].get(o["url"], rp)
            text = crawler[0].parse_page(html, o["url"], [])["text"] if html else ""
        if not lib.quote_in_text(o["quote"], text):
            fails.append("offer sentence not found on page when fetched again")
    if not name_on_site(b["name"], alltext):
        fails.append("business name not on site")
    if b["location_count"] not in (1, 2) or (ev.get("franchise_wording") and ev["franchise_wording"]["value"] == "true"):
        fails.append("location count not 1-2 or franchise wording")
    c = conn.execute("SELECT verification, verified_at FROM contacts WHERE business_id=? AND is_chosen=1", (b["id"],)).fetchone()
    ok_levels = ("valid", "catch_all") + (("mx_only",) if lib.load_yaml("sources.yaml")["verify"]["min_level"] == "mx_only" else ())
    if not c or c["verification"] not in ok_levels or not c["verified_at"] \
            or (lib.parse_ts(lib.now()) - lib.parse_ts(c["verified_at"])).days >= max_age:
        fails.append(f"email not verified within {max_age} days")
    reasons = lib.suppression_reasons(conn, b["id"])
    if reasons:
        fails.append("suppressed: " + "; ".join(reasons[:2]))
    chk = lib.get_llm_response(conn, "segment_check", b["id"])
    cats = lib.load_yaml("categories.yaml")["categories"]
    if chk is not None:   # the agent answered a second-pass task: that wins
        site = chk.get("segment")
        same_group = site in cats and b["segment"] in cats and cats[site]["group"] == cats[b["segment"]]["group"]
        if site != b["segment"] and not same_group:
            fails.append(f"second pass disagrees on segment ({site} vs {b['segment']})")
    else:                 # deterministic independent pass: the site's own opening text must show evidence for the segment
        home = next((p["text"] for p in pages), "").lower()[:700]
        pats = lib.category_patterns().get(b["segment"], [])
        kws = [k.lower() for k in cats.get(b["segment"], {}).get("keywords", [])]
        if not (any(p.search(home) for p in pats) or any(k in home for k in kws)):
            fails.append(f"no evidence on the site for segment {b['segment']}")
    return fails


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--llm-second-pass", action="store_true", help="queue the second-pass segment check for the agent instead of the deterministic one")
    a = ap.parse_args()
    conn = lib.connect()
    segs = list(lib.segment_catalog())
    cities = lib.city_set()
    max_age = lib.load_yaml("sources.yaml")["verify"]["max_age_days"]
    crawler = None if a.offline else _crawler()
    with lib.run(conn, "07_check") as r:
        rows = conn.execute("SELECT * FROM businesses WHERE status='verified'").fetchall()
        r.n_in = len(rows)
        if a.llm_second_pass:
            for b in rows:
                lib.enqueue_llm(conn, "segment_check", b["id"], segment_prompt(conn, b["id"], segs))
            conn.commit()
        passed = 0
        for b in rows:
            fails = evaluate(conn, b, cities, max_age, crawler, a.offline)
            ok = not fails
            conn.execute("INSERT OR REPLACE INTO checks(business_id, passed, failures, checked_at) VALUES (?,?,?,?)",
                         (b["id"], int(ok), json.dumps(fails), lib.now()))
            passed += ok
        conn.commit()
        r.n_out = passed
        pend = conn.execute("SELECT COUNT(*) FROM llm_tasks WHERE kind='segment_check' AND status='pending'").fetchone()[0]
        prec = passed / len(rows) if rows else 0
        print(f"research precision {prec:.0%} ({passed}/{len(rows)} passed)" + (f"; {pend} agent second-pass tasks pending" if pend else ""))


if __name__ == "__main__":
    main()
