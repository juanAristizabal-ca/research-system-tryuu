#!/usr/bin/env python3
"""Step 3 - Enrich = crawl the business's own site, then extract facts with a fixed prompt.

  03_enrich.py crawl   [--limit N] [--recrawl]   polite crawl (robots respected, 1 req/s per host)
  03_enrich.py extract [--limit N]               screen, queue tasks for the agent, apply answers already imported
Pass rules: the business name must appear on the site; every extracted field needs a verbatim quote that
is really in the fetched text, otherwise the field stays empty (plan 5.3 steps 3-4).
"""
import argparse
import json
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
import rules_extract  # noqa: E402


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
    emails += lib.EMAIL_RE.findall(text) + lib.EMAIL_RE.findall(jsonld) + lib.deobfuscate_emails(text)
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
         + (f" AND origin='{a.origin}'" if a.origin else "")
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
OWNER_RE = re.compile(r"\b(owner|founder|co-founder|director|principal|instructor|teacher|coach|chef|practitioner|therapist|counsellor|"
                      r"counselor|registered|led by|meet |my name is|i am|i'm|about (me|us))\b", re.I)
FREE_RE = re.compile(r"\b(free|complimentary|no[- ]cost|no[- ]charge|trial|drop-?in|intro|first (class|lesson|visit|session)|consultation|assessment|tour|tasting)\b", re.I)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\s{2,}| \| ", text) if 25 <= len(s.strip()) <= 320]


def build_prompt(conn, bid: int, cap: int) -> str | None:
    """Per-business body only (instructions are added once per worksheet by llm.py). Snippet mode: the agent sees the
    page titles, the sentences that mention an offer, the sentences that name an owner/teacher, and address text,
    instead of whole pages. Every quote the agent returns is still verified against the full stored text by code."""
    pages = conn.execute("SELECT url, text FROM pages WHERE business_id=? AND text IS NOT NULL ORDER BY id", (bid,)).fetchall()
    if not pages:
        return None
    texts = lib.dedupe_pages([p["text"] for p in pages])
    biz = conn.execute("SELECT name, city FROM businesses WHERE id=?", (bid,)).fetchone()
    out, used, seen = [f"Business: {biz['name']} ({biz['city'] or 'unknown city'})"], 0, set()
    for p, t in zip(pages, texts):
        sents = _sentences(t)
        pick = [t[:260]] + [s for s in sents if FREE_RE.search(s)][:4] + [s for s in sents if OWNER_RE.search(s)][:3]
        pick = [s for s in dict.fromkeys(pick) if s and s not in seen]
        seen.update(pick)
        if not pick:
            continue
        chunk = f"[[URL: {p['url']}]]\n" + "\n".join(f"- {s}" for s in pick)
        if used + len(chunk) > cap:
            chunk = chunk[: max(0, cap - used)]
        out.append(chunk)
        used += len(chunk)
        if used >= cap:
            break
    return "\n".join(out)


def skip_reason(conn, b, seed: dict, cities: set) -> str | None:
    """Cheap code-level screen so the agent never spends effort on a lead that cannot load."""
    if b["city"] and b["city"].lower() not in cities:
        return "outside launch cities"
    cands = [r["email"] for r in conn.execute("SELECT email FROM contacts WHERE business_id=?", (b["id"],))]
    email, _, _ = lib.choose_mailbox(cands, b["domain"] or "", seed, None, b["name"])
    return None if email else "no usable mailbox on site"


def apply_extraction(conn, bid: int, data: dict) -> dict:
    """Validate quotes against the real page text, write evidence, choose the mailbox. Returns summary."""
    seed = lib.load_yaml("suppression_seed.yaml")
    pages = conn.execute("SELECT url, text FROM pages WHERE business_id=?", (bid,)).fetchall()
    alltext = "\n".join(p["text"] or "" for p in pages)
    biz = conn.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
    segs = lib.segment_catalog()
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
    email, reason, is_person = lib.choose_mailbox(cands, biz["domain"] or "", seed, owner_first, biz["name"])
    conn.execute("UPDATE contacts SET is_chosen=0 WHERE business_id=?", (bid,))
    if email:
        conn.execute("UPDATE contacts SET is_chosen=1, choice_reason=?, is_person=?, person=? WHERE business_id=? AND email=?",
                     (reason, int(is_person), owner_first if is_person else None, bid, email))
    lib.set_status(conn, bid, "enriched")
    return {"fields": list(ev), "email": email}


def _todo(conn, a):
    return conn.execute(
        "SELECT * FROM businesses WHERE status='discovered' AND id IN (SELECT business_id FROM pages WHERE text IS NOT NULL) "
        "AND id NOT IN (SELECT business_id FROM llm_tasks WHERE kind='extract' AND status='pending') "
        "AND id NOT IN (SELECT business_id FROM evidence WHERE field='skip_reason')"
        + (f" AND origin='{a.origin}'" if a.origin else "") + " ORDER BY id" + (f" LIMIT {a.limit}" if a.limit else "")).fetchall()


def cmd_extract(conn, a):
    """Default = rules (deterministic, free). --mode llm queues the same businesses for the agent instead."""
    cap = lib.load_yaml("sources.yaml")["llm"]["max_chars_per_business"]
    seed, cities = lib.load_yaml("suppression_seed.yaml"), lib.city_set()
    with lib.run(conn, f"03_enrich:extract:{a.mode}") as r:
        todo = _todo(conn, a)
        r.n_in = len(todo)
        skipped = done = queued = 0
        for b in todo:
            why = skip_reason(conn, b, seed, cities)
            if why:
                conn.execute("INSERT OR REPLACE INTO evidence(business_id, field, value) VALUES (?,?,?)", (b["id"], "skip_reason", why))
                skipped += 1
            elif a.mode == "rules":
                apply_extraction(conn, b["id"], rules_extract.extract(conn, b["id"]))
                done += 1
            else:
                p = build_prompt(conn, b["id"], cap)
                if p and lib.enqueue_llm(conn, "extract", b["id"], p):
                    queued += 1
        conn.commit()
        for row in conn.execute("SELECT t.business_id FROM llm_tasks t JOIN businesses b ON b.id=t.business_id "
                                "WHERE t.kind='extract' AND t.status='done' AND b.status='discovered'").fetchall():
            data = lib.get_llm_response(conn, "extract", row["business_id"])
            if data is None:
                r.errors.append(f"business {row['business_id']}: unparseable answer")
                continue
            apply_extraction(conn, row["business_id"], data)
            done += 1
        conn.commit()
        r.n_out = done
        print(f"mode {a.mode}: extracted {done}, queued for the agent {queued}, skipped {skipped} (no mailbox / outside launch cities)")


EXTRA_PATHS = ["/contact", "/contact-us", "/contactus", "/contact.html", "/contact-us.html", "/about-us", "/about", "/get-in-touch", "/reach-us",
               "/connect", "/enquiries", "/info", "/register", "/faq", "/about.html", "/studio"]


def cmd_recover(conn, a):
    """Second chance for sites where no email was found: (1) de-obfuscate emails in the stored page text, (2) try more contact paths."""
    src = lib.load_yaml("sources.yaml")
    crawler = Crawler(src["crawl"], src["booking_hosts"], src["crawl"]["page_keywords"])
    with lib.run(conn, "03_enrich:recover") as r:
        rows = conn.execute("SELECT b.id, b.website, b.domain FROM businesses b WHERE b.origin=? AND b.status='discovered' AND b.website IS NOT NULL "
                            "AND NOT EXISTS (SELECT 1 FROM contacts k WHERE k.business_id=b.id) AND EXISTS (SELECT 1 FROM pages p WHERE p.business_id=b.id AND p.http_status=200)",
                            (a.origin or "gosom",)).fetchall()
        r.n_in = len(rows)
        found = 0
        for b in rows:  # (1) stored text, no network
            texts = [p["text"] or "" for p in conn.execute("SELECT text FROM pages WHERE business_id=?", (b["id"],))]
            for e in lib.deobfuscate_emails("\n".join(texts)):
                conn.execute("INSERT OR IGNORE INTO contacts(business_id, email, source_url) VALUES (?,?,?)", (b["id"], e, "deobfuscated"))
                found += 1
        conn.commit()
        todo = [b for b in rows if not conn.execute("SELECT 1 FROM contacts WHERE business_id=?", (b["id"],)).fetchone()]

        haves = {b["id"]: {p["url"].rstrip("/") for p in conn.execute("SELECT url FROM pages WHERE business_id=?", (b["id"],))} for b in todo}

        def one(b):   # worker thread: no database access here (sqlite connections are per-thread)
            base = b["website"] if b["website"].startswith("http") else f"https://{b['domain']}"
            rp = crawler.robots(base)
            have = haves[b["id"]]
            out = []
            for path in EXTRA_PATHS:
                u = urljoin(base, path)
                if u.rstrip("/") in have:
                    continue
                code, h = crawler.get(u, rp)
                if h:
                    parsed = parse_page(h, u, src["booking_hosts"])
                    out.append({"url": u, "kind": "recovery", "status": code, "parsed": parsed})
                    if parsed["emails"]:
                        break
            return b["id"], out
        with ThreadPoolExecutor(src["crawl"]["workers"]) as ex:
            for bid, pages in ex.map(one, todo):
                if pages:
                    store_pages(conn, bid, pages)
                conn.commit()
        for b in rows:
            if conn.execute("SELECT 1 FROM contacts WHERE business_id=?", (b["id"],)).fetchone():
                conn.execute("DELETE FROM evidence WHERE business_id=? AND field='skip_reason'", (b["id"],))
                r.n_out += 1
        conn.commit()
        print(f"recovered an email for {r.n_out} of {len(rows)} sites with none ({found} by de-obfuscation)")


def cmd_audit(conn, a):
    """Compare the rules extractor with the agent's answers (a JSONL worksheet answer file) on the same businesses. Nothing is written."""
    tot = {"n": 0, "segment": 0, "offer_both": 0, "offer_agree": 0, "owner_both": 0, "owner_agree": 0, "rules_offer_only": 0, "rules_owner_only": 0}
    for line in Path(a.file).read_text().splitlines():
        if not line.strip():
            continue
        g = json.loads(line)
        row = conn.execute("SELECT business_id FROM llm_tasks WHERE id=?", (g["id"],)).fetchone()
        if not row:
            continue
        gold, mine = g["response"], rules_extract.extract(conn, row["business_id"])
        tot["n"] += 1
        tot["segment"] += (gold.get("segment") == mine["segment"])
        go, mo = bool((gold.get("offer") or {}).get("sentence")), bool(mine["offer"]["sentence"])
        tot["offer_agree"] += (go == mo)
        tot["rules_offer_only"] += (mo and not go)
        gw, mw = (gold.get("owner") or {}).get("first_name"), mine["owner"]["first_name"]
        tot["owner_agree"] += ((gw or "").lower() == (mw or "").lower())
        tot["rules_owner_only"] += (bool(mw) and not gw)
        if gold.get("segment") != mine["segment"]:
            print("  segment differs:", conn.execute("SELECT name FROM businesses WHERE id=?", (row["business_id"],)).fetchone()[0], "| agent:", gold.get("segment"), "| rules:", mine["segment"])
    n = max(tot["n"], 1)
    print(f"audit on {tot['n']} businesses: segment agree {tot['segment'] / n:.0%}, offer present/absent agree {tot['offer_agree'] / n:.0%} "
          f"(rules found an offer the agent did not: {tot['rules_offer_only']}), owner first name agree {tot['owner_agree'] / n:.0%} "
          f"(rules found an owner the agent did not: {tot['rules_owner_only']})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["crawl", "extract", "audit", "recover"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--mode", choices=["rules", "llm"], default="rules")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--recrawl", action="store_true")
    ap.add_argument("--origin", help="only businesses found by this source (e.g. gosom = this research session)")
    a = ap.parse_args()
    conn = lib.connect()
    {"crawl": cmd_crawl, "extract": cmd_extract, "audit": cmd_audit, "recover": cmd_recover}[a.cmd](conn, a)


if __name__ == "__main__":
    main()
