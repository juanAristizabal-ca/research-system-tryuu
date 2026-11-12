#!/usr/bin/env python3
"""Step 1 - Discover. Ingest raw rows from a source; keeps the untouched source file under data/raw/<date>/.

  01_discover.py gosom <results.csv>                    output of gosom/google-maps-scraper (run via Docker)
  01_discover.py overpass <segment> --city Burnaby      OpenStreetMap via Overpass, tags from config/categories.yaml (osm: [...])
  01_discover.py licences <file.csv> --name-col ... --city-col ... --date-col ... --city Vancouver
Pass rule (plan 5.3): row has a name and either a website or a phone. Re-running is safe (row hash).
"""
import argparse
import csv
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

SOCIAL = {"facebook.com", "instagram.com", "linktr.ee", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
          "youtube.com", "yelp.com", "business.site", "google.com", "g.page", "wa.me"}


def _clean_site(url: str) -> tuple[str, str]:
    dom = lib.norm_domain(url)
    if not dom or any(dom == s or dom.endswith("." + s) for s in SOCIAL):
        return "", ""
    return url if url.startswith("http") else f"https://{dom}", dom


def _archive(path: Path) -> str:
    day = lib.now()[:10]
    dest = lib.ROOT / "data" / "raw" / day
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / path.name
    if not target.exists():
        shutil.copy2(path, target)
    return str(target.relative_to(lib.ROOT))


def insert_rows(conn, source: str, source_file: str, rows: list[tuple[dict, dict]]) -> tuple[int, int]:
    """rows: (raw_payload, normalised). Returns (inserted, rejected)."""
    ins = rej = 0
    for raw, n in rows:
        if not n.get("name") or not (n.get("domain") or n.get("phone")):
            rej += 1
            continue
        payload = json.dumps({"raw": raw, "n": n}, sort_keys=True, ensure_ascii=False)
        h = hashlib.sha256(payload.encode()).hexdigest()[:20]
        cur = conn.execute("INSERT OR IGNORE INTO raw_rows(source, source_file, row_hash, payload) VALUES (?,?,?,?)",
                           (source, source_file, h, payload))
        ins += cur.rowcount
    conn.commit()
    return ins, rej


def from_gosom(path: Path):
    out = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            site, dom = _clean_site(r.get("website") or r.get("Website") or "")
            city = r.get("city") or ""
            ca = r.get("complete_address") or ""
            if not city and ca.startswith("{"):
                try:
                    city = json.loads(ca).get("city", "")
                except json.JSONDecodeError:
                    pass
            addr = r.get("address") or r.get("Address") or ""
            if not city and addr:
                parts = [p.strip() for p in addr.split(",")]
                city = parts[-3] if len(parts) >= 3 else ""
            status = (r.get("status") or "").lower()
            if "closed" in status and "temporarily" not in status:
                continue
            out.append((r, {
                "name": (r.get("title") or r.get("name") or "").strip(),
                "website": site, "domain": dom,
                "place_id": (r.get("place_id") or "").strip(),
                "phone": lib.norm_phone(r.get("phone")), "city": lib.norm_city(city),
                "address": addr, "postal_code": lib.first_postal_v(addr) or "",
                "category": r.get("category") or "", "reviews": r.get("review_count") or "",
                "segment_hint": (r.get("input_id") or "").split("|")[0] if "|" in (r.get("input_id") or "") else "",
                "query_id": r.get("input_id") or "",
            }))
    return out


def from_licences(path: Path, a):
    out = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out.append((r, {"name": (r.get(a.name_col) or "").strip(), "website": "", "domain": "", "place_id": "",
                            "phone": lib.norm_phone(r.get(a.phone_col)) if a.phone_col else "",
                            "city": lib.norm_city(r.get(a.city_col) if a.city_col else a.city),
                            "address": r.get(a.addr_col, "") if a.addr_col else "",
                            "postal_code": "", "category": r.get(a.type_col, "") if a.type_col else "",
                            "licence_date": (r.get(a.date_col) or "")[:10] if a.date_col else ""}))
    return out


def from_overpass(segment: str, city: str):
    import requests

    seg = lib.segment_catalog()[segment]
    cfg = lib.load_yaml("sources.yaml")["discover"]["overpass"]
    out, raw_all = [], []
    for tag in seg.get("osm", []):
        k, v = tag.split("=", 1)
        q = (f'[out:json][timeout:120];area["name"="{city}"]["boundary"="administrative"]->.a;'
             f'nwr["{k}"="{v}"]["name"](area.a);out center tags {cfg["max_per_query"]};')
        r = requests.post(cfg["url"], data={"data": q}, timeout=150)
        r.raise_for_status()
        els = r.json().get("elements", [])
        raw_all.append({"tag": tag, "city": city, "elements": els})
        for e in els:
            t = e.get("tags", {})
            site, dom = _clean_site(t.get("website") or t.get("contact:website") or "")
            out.append((e, {
                "name": t.get("name", "").strip(), "website": site, "domain": dom, "place_id": "",
                "phone": lib.norm_phone(t.get("phone") or t.get("contact:phone")), "city": lib.norm_city(t.get("addr:city") or city),
                "address": " ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street")])),
                "postal_code": lib.first_postal_v(t.get("addr:postcode", "")) or "",
                "category": tag, "segment_hint": segment, "osm_id": f'{e.get("type")}/{e.get("id")}'}))
    return out, raw_all


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="src", required=True)
    g = sub.add_parser("gosom"); g.add_argument("file")
    o = sub.add_parser("overpass"); o.add_argument("segment"); o.add_argument("--city", required=True)
    l = sub.add_parser("licences"); l.add_argument("file")
    for k in ("name-col", "city-col", "date-col", "addr-col", "type-col", "phone-col"):
        l.add_argument(f"--{k}")
    l.add_argument("--city")
    a = ap.parse_args()

    conn = lib.connect()
    with lib.run(conn, f"01_discover:{a.src}") as r:
        if a.src == "gosom":
            p = Path(a.file); rows = from_gosom(p); sf = _archive(p)
        elif a.src == "licences":
            p = Path(a.file); rows = from_licences(p, a); sf = _archive(p)
        else:
            rows, raw = from_overpass(a.segment, a.city)
            day = lib.now()[:10]
            d = lib.ROOT / "data" / "raw" / day; d.mkdir(parents=True, exist_ok=True)
            sf = f"data/raw/{day}/overpass_{a.segment}_{a.city.replace(' ', '_')}.json"
            (lib.ROOT / sf).write_text(json.dumps(raw))
        r.n_in = len(rows)
        ins, rej = insert_rows(conn, a.src, sf, rows)
        r.n_out = ins
        print(f"{a.src}: {len(rows)} rows read, {ins} new, {rej} rejected (no name or no website/phone)")


if __name__ == "__main__":
    main()
