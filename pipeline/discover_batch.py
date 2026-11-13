#!/usr/bin/env python3
"""Run Google Maps discovery for one category and ingest the results.

  discover_batch.py <category> [--keywords "a,b,c"] [--depth 15] [--concurrency 4] [--cities "Burnaby,Surrey"] [--areas|--no-areas] [--dry-run]

Standing rule: queries cover every launch city AND every neighbourhood in config/segments.yaml (`<keyword> in <place>, BC, Canada`),
keywords from config/categories.yaml (neighbourhoods use the first `area_keywords`).
Queries already in search_log are skipped, so a re-run only does what is missing. Needs Docker running
(`setsid nohup dockerd`) and the gosom/google-maps-scraper image. Results are archived untouched under data/raw/<date>/
and every query is written to search_log (rows read, rows new) so the next session knows what was searched."""
import argparse
import csv
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

IMAGE = "gosom/google-maps-scraper:latest"


def _discover():
    spec = importlib.util.spec_from_file_location("discover", Path(__file__).with_name("01_discover.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def ingest_existing(conn, category: str, out: Path):
    """Re-ingest results that were scraped but not stored (e.g. the database was locked at the end of a long run)."""
    disc = _discover()
    qfile = Path(str(out).replace("_results.csv", "_queries.txt"))
    pairs = []
    for line in qfile.read_text().splitlines():
        if "#!#" in line:
            _, qid = line.rsplit("#!#", 1)
            _, k, c = qid.split("|", 2)
            pairs.append((k, c))
    rows = disc.from_gosom(out)
    by_q: dict[str, list] = {}
    for raw, n in rows:
        by_q.setdefault(n.get("query_id", ""), []).append((raw, n))
    sf = str(out.relative_to(lib.ROOT))
    new = 0
    with lib.run(conn, f"discover_batch:ingest:{category}") as r:
        r.n_in = len(pairs)
        for k, c in pairs:
            grp = by_q.get(f"{category}|{k}|{c}", [])
            ins, _ = disc.insert_rows(conn, "gosom", sf, grp)
            new += ins
            conn.execute("INSERT OR REPLACE INTO search_log(category, source, query, city, rows_read, rows_new) VALUES (?,?,?,?,?,?)",
                         (category, "gosom", k, c, len(grp), ins))
        conn.commit()
        r.n_out = new
    print(f"{category}: ingested {len(rows)} places, {new} new raw rows, {len(pairs)} queries logged")


def places_for(segcfg: dict, cities_arg: str | None, areas_only: bool, no_areas: bool) -> list[str]:
    """Search places. Rule (CLAUDE.md): EVERY research session searches the whole city AND every neighbourhood of every
    launch city, because Maps caps results per query. --cities limits it for a test; --no-areas is for a quick probe only."""
    cities = [c.strip() for c in cities_arg.split(",")] if cities_arg else list(segcfg["launch_cities"])
    areas = [f"{n}, {c}" for c, ns in segcfg.get("neighbourhoods", {}).items() if c in cities for n in ns]
    if areas_only:
        return areas
    return cities if no_areas else cities + areas


def run_chunk(conn, category: str, todo: list, depth: int, concurrency: int, chunk_no: int) -> int:
    """One scraper container for a slice of the queries. Returns new raw rows. A killed container never marks unrun
    queries as done: after a watchdog kill, queries with no rows are left for the next run."""
    disc = _discover()
    day = lib.now()[:10]
    d = lib.ROOT / "data" / "raw" / day
    d.mkdir(parents=True, exist_ok=True)
    tag = f"gosom_{category}_{int(time.time())}_{chunk_no}"
    (d / f"{tag}_queries.txt").write_text("\n".join(f"{k} in {c}, BC, Canada #!#{category}|{k}|{c}" for k, c in todo) + "\n")
    out = d / f"{tag}_results.csv"
    cmd = ["docker", "run", "--rm", f"--name=gm_{tag}", "-v", f"{d}:/work", IMAGE, "-input", f"/work/{tag}_queries.txt",
           "-results", f"/work/{tag}_results.csv", "-depth", str(depth), "-c", str(concurrency), "-lang", "en", "-exit-on-inactivity", "150s"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    last, idle, killed = -1, 0, False
    while proc.poll() is None:  # watchdog: the scraper container can hang at shutdown (known); stop it when the file stops growing
        time.sleep(30)
        size = out.stat().st_size if out.exists() else 0
        idle = idle + 30 if size == last else 0
        last = size
        if idle >= 420:
            subprocess.run(["docker", "kill", f"gm_{tag}"], capture_output=True)
            killed = True
            break
    if not out.exists():
        print(f"chunk {chunk_no}: scraper produced no file, queries left for the next run", flush=True)
        return 0
    rows = disc.from_gosom(out)
    by_q: dict[str, list] = {}
    for raw, n in rows:
        by_q.setdefault(n.get("query_id", ""), []).append((raw, n))
    sf = str(out.relative_to(lib.ROOT))
    total_new = logged = 0
    for k, c in todo:
        grp = by_q.get(f"{category}|{k}|{c}", [])
        if killed and not grp:
            continue
        ins, _ = disc.insert_rows(conn, "gosom", sf, grp)
        total_new += ins
        logged += 1
        conn.execute("INSERT OR REPLACE INTO search_log(category, source, query, city, rows_read, rows_new) VALUES (?,?,?,?,?,?)",
                     (category, "gosom", k, c, len(grp), ins))
    conn.commit()
    print(f"chunk {chunk_no}: {len(rows)} places read, {total_new} new raw rows, {logged}/{len(todo)} queries logged{' (watchdog)' if killed else ''}", flush=True)
    return total_new


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("category")
    ap.add_argument("--keywords"); ap.add_argument("--cities")
    ap.add_argument("--areas", action="store_true", help="only the neighbourhood pass (the default already includes it)")
    ap.add_argument("--no-areas", action="store_true", help="whole cities only (quick probe, not a research session)")
    ap.add_argument("--max-keywords", type=int, default=99, help="cap for whole-city queries")
    ap.add_argument("--chunk", type=int, default=120, help="queries per scraper container")
    ap.add_argument("--depth", type=int, default=15); ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ingest", help="ingest an existing results CSV (its *_queries.txt sits next to it) instead of scraping")
    a = ap.parse_args()

    cat = lib.load_yaml("categories.yaml")["categories"].get(a.category) or sys.exit(f"unknown category {a.category}")
    segcfg = lib.load_yaml("segments.yaml")
    conn = lib.connect()
    if a.ingest:
        return ingest_existing(conn, a.category, Path(a.ingest))
    kws = [k.strip() for k in a.keywords.split(",")] if a.keywords else cat["keywords"][:a.max_keywords]
    n_area_kw = int(segcfg.get("area_keywords", 4))
    city_set = {c.strip() for c in a.cities.split(",")} if a.cities else set(segcfg["launch_cities"])
    place_list = places_for(segcfg, a.cities, a.areas, a.no_areas)
    done = {(r["query"], r["city"]) for r in conn.execute("SELECT query, city FROM search_log WHERE category=? AND source='gosom'", (a.category,))}
    todo = []
    for c in place_list:
        ks = kws if c in city_set else kws[:n_area_kw]  # neighbourhoods use the category's strongest keywords (config: area_keywords)
        todo += [(k, c) for k in ks if (k, c) not in done]
    print(f"{a.category}: {len(place_list)} places, {len(todo)} queries not yet run", flush=True)
    if a.dry_run or not todo:
        return
    with lib.run(conn, f"discover_batch:{a.category}") as r:
        r.n_in = len(todo)
        total = 0
        for n, i in enumerate(range(0, len(todo), a.chunk), 1):
            total += run_chunk(conn, a.category, todo[i:i + a.chunk], a.depth, a.concurrency, n)
        r.n_out = total
        print(f"{a.category}: {total} new raw rows in total")


if __name__ == "__main__":
    main()
