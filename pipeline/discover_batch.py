#!/usr/bin/env python3
"""Run Google Maps discovery for one category across the launch cities and ingest the results.

  discover_batch.py <category> [--keywords "a,b,c"] [--depth 2] [--concurrency 4] [--cities "Burnaby,Surrey"] [--dry-run]

Queries are `<keyword> in <city>, BC, Canada`, one per keyword and launch city, taken from config/categories.yaml.
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("category")
    ap.add_argument("--keywords"); ap.add_argument("--cities")
    ap.add_argument("--areas", action="store_true", help="second pass: neighbourhoods of the big cities instead of whole cities")
    ap.add_argument("--max-keywords", type=int, default=99)
    ap.add_argument("--depth", type=int, default=15); ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ingest", help="ingest an existing results CSV (its *_queries.txt sits next to it) instead of scraping")
    a = ap.parse_args()

    cat = lib.load_yaml("categories.yaml")["categories"].get(a.category) or sys.exit(f"unknown category {a.category}")
    kws = [k.strip() for k in a.keywords.split(",")] if a.keywords else cat["keywords"][:a.max_keywords]
    segcfg = lib.load_yaml("segments.yaml")
    if a.areas:  # (label, query place) pairs: "Kitsilano, Vancouver"
        cities = [f"{n}, {c}" for c, ns in segcfg.get("neighbourhoods", {}).items() for n in ns]
    else:
        cities = [c.strip() for c in a.cities.split(",")] if a.cities else segcfg["launch_cities"]
    conn = lib.connect()
    done = {(r["query"], r["city"]) for r in conn.execute("SELECT query, city FROM search_log WHERE category=? AND source='gosom'", (a.category,))}
    todo = [(k, c) for k in kws for c in cities if (k, c) not in done]
    print(f"{a.category}: {len(kws)} keywords x {len(cities)} cities = {len(kws) * len(cities)} queries, {len(todo)} not yet run")
    if a.dry_run or not todo:
        return
    if a.ingest:
        return ingest_existing(conn, a.category, Path(a.ingest))
    day = lib.now()[:10]
    d = lib.ROOT / "data" / "raw" / day
    d.mkdir(parents=True, exist_ok=True)
    tag = f"gosom_{a.category}_{int(time.time())}"
    (d / f"{tag}_queries.txt").write_text("\n".join(f"{k} in {c}, BC, Canada #!#{a.category}|{k}|{c}" for k, c in todo) + "\n")
    out = d / f"{tag}_results.csv"
    with lib.run(conn, f"discover_batch:{a.category}") as r:
        r.n_in = len(todo)
        cmd = ["docker", "run", "--rm", "-v", f"{d}:/work", IMAGE, "-input", f"/work/{tag}_queries.txt", "-results", f"/work/{tag}_results.csv",
               "-depth", str(a.depth), "-c", str(a.concurrency), "-lang", "en", "-exit-on-inactivity", "150s"]
        name = f"gm_{tag}"
        cmd.insert(2, f"--name={name}")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        last, idle = -1, 0
        while proc.poll() is None:  # watchdog: the scraper container can hang at shutdown (known); stop it when the file stops growing
            time.sleep(30)
            size = out.stat().st_size if out.exists() else 0
            idle = idle + 30 if size == last else 0
            last = size
            if idle >= 420:
                subprocess.run(["docker", "kill", name], capture_output=True)
                break
        if not out.exists():
            r.errors.append("scraper produced no file")
            raise SystemExit("scraper failed: no results file")
        disc = _discover()
        rows = disc.from_gosom(out)
        by_q: dict[str, list] = {}
        for raw, n in rows:
            by_q.setdefault(n.get("query_id", ""), []).append((raw, n))
        sf = str(out.relative_to(lib.ROOT))
        total_new = 0
        for k, c in todo:
            grp = by_q.get(f"{a.category}|{k}|{c}", [])
            ins, _ = disc.insert_rows(conn, "gosom", sf, grp)
            total_new += ins
            conn.execute("INSERT OR REPLACE INTO search_log(category, source, query, city, rows_read, rows_new) VALUES (?,?,?,?,?,?)",
                         (a.category, "gosom", k, c, len(grp), ins))
        conn.commit()
        r.n_out = total_new
        print(f"{a.category}: {len(rows)} places read, {total_new} new raw rows, {len(todo)} queries logged")


if __name__ == "__main__":
    main()
