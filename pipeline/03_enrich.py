#!/usr/bin/env python3
"""Step 3 - Enrich = crawl the business's own site, then extract facts with a fixed prompt.

  03_enrich.py crawl   [--limit N] [--recrawl]   polite crawl (robots respected, 1 req/s per host)
  03_enrich.py extract [--limit N]               enqueue LLM tasks, run them if ANTHROPIC_API_KEY is set,
                                                 apply any finished answers (otherwise: llm.py export/import)
Pass rules: the business name must appear on the site; every extracted field needs a verbatim quote that
is really in the fetched text, otherwise the field stays empty (plan 5.3 steps 3-4).
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
import llm  # noqa: E402


# ------------------------------------------------------------------ crawl
def decode_cfemail(hexstr: str) -> str:
    try:
        b = bytes.fromhex(hexstr)
        return bytes(c ^ b[0] for c in b[1:]).decode()
    except ValueError:
        return ""


def parse_page(html: str, base_url: str, booking_hosts: list[str]) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    emails, phones, booking, links = [], [], [], []
    jsonld = " ".join(s.get_text(" ", strip=True) for s in soup.find_all("script", type="application/ld+json"))
    for el in soup.find_all(attrs={"data-cfemail": True}):
        e = decode_cfemail(el["data-cfemail"])
        if e:
            emails.append(e)
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if h.lower().startswith("mailto:"):
            emails.append(h[7:].split("?")[0])
        elif h.lower().startswith("tel:"):
            phones.append(h[4:])
        else:
            full = urljoin(base_url, h)
            host = lib.norm_domain(full)
            if any(host == b or host.endswith("." + b) for b in booking_hosts):
                booking.append(full)
            links.append((full, a.get_text(" ", strip=True).lower()))
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    emails += lib.EMAIL_RE.findall(text) + lib.EMAIL_RE.findall(jsonld)
    return {"text": text, "jsonld": jsonld[:3000], "emails": list(dict.fromkeys(lib.norm_email(e) for e in emails)),
            "phones": phones, "booking": booking, "links": links}


def pick_links(links, domain: str, keywords: list[str], n: int) -> list[str]:
    scored = {}
    for url, txt in links:
        if lib.norm_domain(url) != domain or url.split("#")[0].lower().endswith((".pdf", ".jpg", ".png", ".zip")):
            continue
        u = url.split("#")[0].rstrip("/")
        s = sum(k in u.lower() or k in txt for k in keywords)
        if s:
            scored[u] = max(scored.get(u, 0), s)
    return [u for u, _ in sorted(scored.items(), key=lambda x: -x[1])][:n]


class Crawler:
    def __init__(self, cfg: dict, booking_hosts: list[str], keywords: list[str]):
        self.cfg, self.booking_hosts, self.keywords = cfg, booking_hosts, keywords
        self.sess = requests.Session()
        self.sess.headers["User-Agent"] = cfg["user_agent"]

    def robots(self, base: str) -> RobotFileParser | None:
        rp = RobotFileParser()
        try:
            r = self.sess.get(urljoin(base, "/robots.txt"), timeout=self.cfg["timeout_seconds"])
        except requests.RequestException:
            return rp  # unreachable robots: parse nothing -> allowed
        if r.status_code >= 500:
            return None  # server error on robots = do not crawl
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        return rp

    def get(self, url: str, rp: RobotFileParser | None):
        if rp is None or not rp.can_fetch(self.cfg["user_agent"], url):
            return 0, None  # blocked by robots: recorded, never bypassed
        time.sleep(self.cfg["delay_seconds"])
        try:
            r = self.sess.get(url, timeout=self.cfg["timeout_seconds"], allow_redirects=True)
        except requests.RequestException:
            return -1, None
        ct = r.headers.get("content-type", "")
        return r.status_code, (r.text if r.status_code == 200 and "html" in ct else None)

    def crawl_site(self, site: str) -> list[dict]:
        domain = lib.norm_domain(site)
        base = site if site.startswith("http") else f"https://{domain}"
        rp = self.robots(base)
        pages = []
        code, html = self.get(base, rp)
        pages.append({"url": base, "kind": "home", "status": code, "parsed": parse_page(html, base, self.booking_hosts) if html else None})
        if not html:
            return pages
        for u in pick_links(pages[0]["parsed"]["links"], domain, self.keywords, self.cfg["max_pages"]):
            if u.rstrip("/") == base.rstrip("/") or len(pages) >= self.cfg["max_pages"]:
                continue
            code, h = self.get(u, rp)
            pages.append({"url": u, "kind": "linked", "status": code, "parsed": parse_page(h, u, self.booking_hosts) if h else None})
        if not any(p["parsed"] and p["parsed"]["emails"] for p in pages):  # recovery pass
            for path in ("/contact", "/contact-us", "/about", "/get-a-quote"):
                u = urljoin(base, path)
                if any(p["url"].rstrip("/") == u for p in pages):
                    continue
                code, h = self.get(u, rp)
                pages.append({"url": u, "kind": "recovery", "status": code, "parsed": parse_page(h, u, self.booking_hosts) if h else None})
                if h and pages[-1]["parsed"]["emails"]:
                    break
        return pages


def store_pages(conn, bid: int, pages: list[dict]):
    for p in pages:
        parsed = p["parsed"]
        text = None
        if parsed:
            extras = [f"[mailto] {e}" for e in parsed["emails"]] + [f"[tel] {t}" for t in parsed["phones"]]
            text = "\n".join([parsed["text"][:20000], *extras] + ([f"[jsonld] {parsed['jsonld']}"] if parsed["jsonld"] else []))
        conn.execute("INSERT OR REPLACE INTO pages(business_id, url, kind, http_status, text, fetched_at) VALUES (?,?,?,?,?,?)",
                     (bid, p["url"], p["kind"], p["status"], text, lib.now()))
        if parsed:
            for e in parsed["emails"]:
                conn.execute("INSERT OR IGNORE INTO contacts(business_id, email, source_url) VALUES (?,?,?)", (bid, e, p["url"]))
            for b in parsed["booking"][:1]:
                conn.execute("INSERT OR IGNORE INTO evidence(business_id, field, value, quote, url) VALUES (?,?,?,?,?)",
                             (bid, "booking_link", lib.norm_domain(b), b, p["url"]))


def cmd_crawl(conn, a):
    src = lib.load_yaml("sources.yaml")
    cfg = src["crawl"]
    bad = tuple(src["offlimits"])
    q = ("SELECT id, website, domain FROM businesses WHERE website IS NOT NULL AND status IN ('discovered','enriched')"
         + ("" if a.recrawl else " AND id NOT IN (SELECT business_id FROM pages)") + " ORDER BY id")
    rows = [r for r in conn.execute(q).fetchall() if not r["domain"].endswith(bad)]
    if a.limit:
        rows = rows[: a.limit]
    crawler = Crawler(cfg, src["booking_hosts"], cfg["page_keywords"])
    with lib.run(conn, "03_enrich:crawl") as r:
        r.n_in = len(rows)
        def one(row):
            try:
                return row["id"], crawler.crawl_site(row["website"]), None
            except Exception as e:  # noqa: BLE001
                return row["id"], [], f"{row['domain']}: {type(e).__name__}: {e}"
        with ThreadPoolExecutor(cfg["workers"]) as ex:
            for bid, pages, err in ex.map(one, rows):
                if err:
                    r.errors.append(err)
                if pages:
                    store_pages(conn, bid, pages)
                    r.n_out += 1
                conn.commit()


# ------------------------------------------------------------------ extract
def build_prompt(conn, bid: int, segments: list[str], cap: int) -> str | None:
    pages = conn.execute("SELECT url, text FROM pages WHERE business_id=? AND text IS NOT NULL ORDER BY id", (bid,)).fetchall()
    if not pages:
        return None
    body, used = [], 0
    for p in pages:
        chunk = f"[[URL: {p['url']}]]\n{p['text']}"
        body.append(chunk[: max(0, cap - used)])
        used += len(chunk)
        if used >= cap:
            break
    biz = conn.execute("SELECT name, city FROM businesses WHERE id=?", (bid,)).fetchone()
    head = (lib.CONFIG / "extraction_prompt.md").read_text().replace("{segments}", ", ".join(segments))
    return f"{head}\n\nBusiness: {biz['name']} ({biz['city'] or 'unknown city'})\n\n" + "\n\n".join(body)


def apply_extraction(conn, bid: int, data: dict) -> dict:
    """Validate quotes against the real page text, write evidence, choose the mailbox. Returns summary."""
    seed = lib.load_yaml("suppression_seed.yaml")
    pages = conn.execute("SELECT url, text FROM pages WHERE business_id=?", (bid,)).fetchall()
    alltext = "\n".join(p["text"] or "" for p in pages)
    biz = conn.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
    segs = lib.load_yaml("segments.yaml")["segments"]
    ev = {}

    def put(field, value, quote=None, url=None):
        if value not in (None, ""):
            ev[field] = (str(value), quote, url)

    seg = data.get("segment")
    if seg in segs and lib.quote_in_text(data.get("segment_quote"), alltext):
        put("segment", seg, data.get("segment_quote"))
    off = data.get("offer") or {}
    if off.get("sentence") and lib.quote_in_text(off["sentence"], alltext) and lib.is_free_offer(off["sentence"]):
        put("offer_sentence", off["sentence"], off["sentence"], off.get("url"))
        put("offer_type", off.get("type"), off["sentence"], off.get("url"))
    ow = data.get("owner") or {}
    if ow.get("first_name") and lib.quote_in_text(ow.get("quote"), alltext) and ow["first_name"].lower() in (ow.get("quote") or "").lower():
        put("owner_first_name", ow["first_name"].strip().title(), ow["quote"], ow.get("url"))
        put("owner_role", ow.get("role"), ow["quote"], ow.get("url"))
    put("accepting_clients", data.get("accepting_clients"))
    loc = data.get("location_count")
    put("location_count", int(loc) if isinstance(loc, (int, float)) and loc >= 0 else None)
    put("franchise_wording", "true" if data.get("franchise_wording") else "false")
    if data.get("listing_title"):
        put("listing_title", str(data["listing_title"])[:60])
    if data.get("listing_description"):
        put("listing_description", str(data["listing_description"])[:300])
    postal = lib.first_postal_v(alltext)
    if postal:
        put("postal_code", postal, postal)

    for f, (v, q, u) in ev.items():
        conn.execute("INSERT OR REPLACE INTO evidence(business_id, field, value, quote, url, crawled_at) VALUES (?,?,?,?,?,?)",
                     (bid, f, v, q, u, lib.now()))
    if "segment" in ev:
        conn.execute("UPDATE businesses SET segment=? WHERE id=?", (ev["segment"][0], bid))
    if "postal_code" in ev and not biz["postal_code"]:
        conn.execute("UPDATE businesses SET postal_code=? WHERE id=?", (ev["postal_code"][0], bid))
    if "location_count" in ev:
        conn.execute("UPDATE businesses SET location_count=? WHERE id=?", (int(ev["location_count"][0]), bid))

    owner_first = ev.get("owner_first_name", (None,))[0]
    cands = [r["email"] for r in conn.execute("SELECT email FROM contacts WHERE business_id=?", (bid,))]
    email, reason, is_person = lib.choose_mailbox(cands, biz["domain"] or "", seed, owner_first)
    conn.execute("UPDATE contacts SET is_chosen=0 WHERE business_id=?", (bid,))
    if email:
        conn.execute("UPDATE contacts SET is_chosen=1, choice_reason=?, is_person=?, person=? WHERE business_id=? AND email=?",
                     (reason, int(is_person), owner_first if is_person else None, bid, email))
    lib.set_status(conn, bid, "enriched")
    return {"fields": list(ev), "email": email}


def cmd_extract(conn, a):
    segs = list(lib.load_yaml("segments.yaml")["segments"])
    cap = lib.load_yaml("sources.yaml")["llm"]["max_chars_per_business"]
    with lib.run(conn, "03_enrich:extract") as r:
        todo = conn.execute(
            "SELECT id FROM businesses WHERE status='discovered' AND id IN (SELECT business_id FROM pages WHERE text IS NOT NULL) "
            "AND id NOT IN (SELECT business_id FROM llm_tasks WHERE kind='extract') ORDER BY id" + (f" LIMIT {a.limit}" if a.limit else "")).fetchall()
        r.n_in = len(todo)
        for row in todo:
            p = build_prompt(conn, row["id"], segs, cap)
            if p:
                lib.enqueue_llm(conn, "extract", row["id"], p)
        conn.commit()
        if os.environ.get("ANTHROPIC_API_KEY"):
            llm.process(conn, ["extract"])
        # apply every finished, not-yet-applied task
        done = conn.execute("SELECT t.business_id FROM llm_tasks t JOIN businesses b ON b.id=t.business_id "
                            "WHERE t.kind='extract' AND t.status='done' AND b.status='discovered'").fetchall()
        for row in done:
            data = lib.get_llm_response(conn, "extract", row["business_id"])
            if data is None:
                r.errors.append(f"business {row['business_id']}: unparseable LLM answer")
                continue
            apply_extraction(conn, row["business_id"], data)
            r.n_out += 1
        conn.commit()
        pend = conn.execute("SELECT COUNT(*) FROM llm_tasks WHERE kind='extract' AND status='pending'").fetchone()[0]
        if pend:
            print(f"{pend} extract tasks pending. Set ANTHROPIC_API_KEY and run llm.py run, or use llm.py export/import.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["crawl", "extract"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--recrawl", action="store_true")
    a = ap.parse_args()
    conn = lib.connect()
    {"crawl": cmd_crawl, "extract": cmd_extract}[a.cmd](conn, a)


if __name__ == "__main__":
    main()
