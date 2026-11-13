"""Shared helpers: database, config versions, run log, normalisers, LLM client.

Everything here is deterministic and side-effect free except connect()/run()/LLM calls.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DB_PATH = Path(os.environ.get("TRYUU_DB", ROOT / "data" / "tryuu.db"))

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.ca", "hotmail.com", "hotmail.ca", "outlook.com",
    "outlook.ca", "live.com", "live.ca", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "shaw.ca", "telus.net", "telus.ca", "rogers.com", "bell.net", "sympatico.ca", "protonmail.com",
    "proton.me", "gmx.com", "mail.com", "yandex.com", "qq.com", "126.com", "163.com",
}
STATUSES = ["discovered", "enriched", "verified", "scored", "suppressed", "loaded", "sent", "replied",
            "onboarded", "closed"]


# ---------------------------------------------------------------- time
def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s[:26] if "%f" in fmt else s[:19], fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- db
def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 120000")
    conn.execute("PRAGMA journal_mode = WAL")   # concurrent scraper ingests and pipeline steps must not lock each other out
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript((ROOT / "db" / "schema.sql").read_text())
    return conn


@contextlib.contextmanager
def run(conn: sqlite3.Connection, job: str):
    """Writes a row to the run log. Yield object has .n_in/.n_out counters and .errors list."""
    class R:
        n_in = 0
        n_out = 0
        errors: list[str] = []
    r = R()
    r.errors = []
    cur = conn.execute("INSERT INTO runs(job, started) VALUES (?, ?)", (job, now()))
    run_id = cur.lastrowid
    conn.commit()
    try:
        yield r
    except Exception as e:  # noqa: BLE001
        r.errors.append(f"{type(e).__name__}: {e}")
        raise
    finally:
        conn.execute("UPDATE runs SET ended=?, n_in=?, n_out=?, errors=? WHERE id=?",
                     (now(), r.n_in, r.n_out, "\n".join(r.errors[:50]) or None, run_id))
        conn.commit()
        print(f"[{job}] in={r.n_in} out={r.n_out} errors={len(r.errors)}")


# ---------------------------------------------------------------- config
def load_yaml(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text()) or {}


def config_version(conn: sqlite3.Connection, name: str) -> str:
    """Version = first 8 hex chars of the file hash. Registered on first use; reason is filled in
    later by `run.py log-change`. Same content -> same version, so re-runs never create noise."""
    path = CONFIG / name
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    ver = sha[:8]
    conn.execute("INSERT OR IGNORE INTO config_versions(file, version, sha256, change) VALUES (?,?,?,?)",
                 (name, ver, sha, "auto-registered"))
    conn.commit()
    return ver


def segment_catalog() -> dict:
    """Targetable categories from config/categories.yaml (the old segments.yaml `segments` block is gone)."""
    return {k: v for k, v in load_yaml("categories.yaml")["categories"].items() if not v.get("excluded")}


_CAT_PATS: dict | None = None


def category_patterns() -> dict:
    """Compiled `types` tokens per category slug, in catalogue order (first match wins). '=tok' = whole word, plural allowed."""
    global _CAT_PATS
    if _CAT_PATS is None:
        _CAT_PATS = {}
        for slug, c in load_yaml("categories.yaml")["categories"].items():
            _CAT_PATS[slug] = [re.compile(r"\b" + re.escape(str(t).lower()[1:]) + r"(s|es)?\b") if str(t).startswith("=")
                               else re.compile(r"\b" + re.escape(str(t).lower())) for t in c.get("types", [])]
    return _CAT_PATS


def classify_category(text: str | None, skip_excluded: bool = True) -> str:
    t = (text or "").lower()
    cats = load_yaml("categories.yaml")["categories"]
    for slug, pats in category_patterns().items():
        if skip_excluded and cats[slug].get("excluded"):
            continue
        if any(p.search(t) for p in pats):
            return slug
    return "other_unclassified"


def city_set() -> set[str]:
    return {c.lower() for c in load_yaml("segments.yaml")["launch_cities"]}


def norm_city(city: str | None) -> str:
    if not city:
        return ""
    city = re.sub(r"\s+", " ", city).strip()
    aliases = {k.lower(): v for k, v in load_yaml("segments.yaml").get("city_aliases", {}).items()}
    return aliases.get(city.lower(), city)


# ---------------------------------------------------------------- normalisers
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
POSTAL_RE = re.compile(r"\b([ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z])[ -]?(\d[ABCEGHJ-NPRSTV-Z]\d)\b", re.I)


_OBF = re.compile(r"([A-Za-z0-9._%+\-]+)\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+|@)\s*([A-Za-z0-9\-]+(?:\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+|\.)\s*[A-Za-z0-9\-]+)+)", re.I)


def deobfuscate_emails(text: str) -> list[str]:
    """'info [at] studio [dot] ca', 'info(at)studio.ca', 'info at studio dot ca' -> info@studio.ca (only when the TLD looks real)."""
    out = []
    for m in _OBF.finditer(text or ""):
        user = m.group(1)
        dom = re.sub(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+)\s*", ".", m.group(2), flags=re.I)
        dom = re.sub(r"\s+", "", dom).lower()
        if re.search(r"\.(com|ca|net|org|info|biz|co|studio|dance|art|school|academy)$", dom) and "@" not in m.group(0).replace("(at)", "").replace("[at]", "") or "@" in m.group(0):
            e = f"{user}@{dom}".lower()
            if EMAIL_RE.fullmatch(e):
                out.append(e)
    return list(dict.fromkeys(out))


def norm_email(e: str | None) -> str:
    return (e or "").strip().lower().strip("<>;,. ")


def email_domain(e: str | None) -> str:
    e = norm_email(e)
    return e.split("@", 1)[1] if "@" in e else ""


def norm_domain(url_or_domain: str | None) -> str:
    s = (url_or_domain or "").strip().lower()
    if not s:
        return ""
    if "//" not in s:
        s = "//" + s
    host = urlparse(s).netloc.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def is_free_mail(domain: str) -> bool:
    return domain.lower() in FREE_MAIL


def norm_phone(p: str | None) -> str:
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


_SUFFIX = re.compile(r"\b(inc|ltd|llc|corp|corporation|co|company|incorporated|limited|the)\b\.?")


def norm_name(n: str | None) -> str:
    n = (n or "").lower().replace("&", " and ").replace("’", "'")
    n = re.sub(r"\([^)]*\)", " ", n)
    n = _SUFFIX.sub(" ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def name_similarity(a: str, b: str) -> float:
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def business_key(domain: str, place_id: str, email: str) -> str:
    if domain:
        return f"d:{domain}"
    if place_id:
        return f"p:{place_id}"
    return f"e:{norm_email(email)}"


def first_postal_v(text: str) -> str | None:
    """First BC postal code (starts with V) found in text, formatted 'V6B 1A1'."""
    for m in POSTAL_RE.finditer(text or ""):
        if m.group(1).upper().startswith("V"):
            return f"{m.group(1).upper()} {m.group(2).upper()}"
    return None


# ---------------------------------------------------------------- offers and mailboxes
_OFFER_NOUNS = r"(consultation|consult|trial|class|lesson|assessment|tour|tasting|estimate|quote|intro(?:ductory)? (?:visit|session|class|lesson)|first (?:class|lesson|visit|session)|discovery call|evaluation)"
_FREE = r"(free|complimentary|no[- ]cost|no[- ]charge|on us)"
OFFER_RE = re.compile(rf"(\b{_FREE}\b[^.!?\n]{{0,40}}\b{_OFFER_NOUNS}\b)|(\b{_OFFER_NOUNS}\b[^.!?\n]{{0,25}}\b{_FREE}\b)", re.I)
_NOT_OFFER = re.compile(r"\bfree (shipping|delivery|parking|returns?|wifi|wi-fi|gift wrap)\b|\d+\s?% off", re.I)


def is_free_offer(sentence: str | None) -> bool:
    """Plan 5.4: free/complimentary/no cost next to a consultation, trial, class... Not shipping etc."""
    if not sentence:
        return False
    s = _NOT_OFFER.sub(" ", sentence)
    return bool(OFFER_RE.search(s))


def squash(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("’", "'").replace("“", '"').replace("”", '"')).strip().lower()


def quote_in_text(quote: str | None, text: str) -> bool:
    q = squash(quote)
    return len(q) >= 6 and q in squash(text)


def mailbox_kind(email: str, seed: dict, owner_first: str | None = None) -> str:
    """'rejected' | 'generic' | 'person'."""
    local = norm_email(email).split("@")[0]
    base = re.split(r"[+]", local)[0]
    if base in set(seed["reject_mailbox_prefixes"]) or any(base.startswith(p + ".") or base.startswith(p + "-")
                                                           for p in seed["reject_mailbox_prefixes"]):
        return "rejected"
    if base in set(seed["generic_mailbox_prefixes"]):
        return "generic"
    if owner_first and owner_first.lower() in base:
        return "person"
    if re.fullmatch(r"[a-z]{2,}[._-][a-z]{2,}", base) or re.fullmatch(r"[a-z]{3,12}", base):
        # first.last or a single short word: treat as person unless it is a role-like word
        return "person"
    return "generic"


JUNK_DOMAINS = {"example.com", "domain.com", "email.com", "yourdomain.com", "sentry.io", "wixpress.com", "wix.com", "godaddy.com",
                "squarespace.com", "weebly.com", "wordpress.com", "shopify.com", "mailchimp.com", "sentry-next.wixpress.com", "w3.org", "schema.org"}


def _clean_email(e: str) -> str | None:
    e = norm_email(e)
    if not EMAIL_RE.fullmatch(e) or "%" in e or len(e) > 70 or e.endswith((".png", ".jpg", ".gif", ".webp", ".svg", ".js", ".css")):
        return None
    return None if email_domain(e) in JUNK_DOMAINS else e


def choose_mailbox(candidates: list[str], business_domain: str, seed: dict, owner_first: str | None = None, business_name: str | None = None):
    """Public mailboxes found on the business's own site, best first (plan 5.4, widened after measuring that half of the
    rejected leads had a perfectly good public address):
      1 named person at the business domain   2 named person on free-mail   3 generic mailbox at the business domain
      4 generic free-mail shown on the site (flagged, lower quality)   5 other-domain mailbox that shares a word with the business name.
    Returns (email, reason, is_person) or (None, 'no usable mailbox', False)."""
    cands = []
    for raw in dict.fromkeys(candidates):
        e = _clean_email(raw)
        if not e:
            continue
        kind = mailbox_kind(e, seed, owner_first)
        if kind != "rejected":
            cands.append((e, kind, email_domain(e)))
    for e, kind, d in cands:
        if kind == "person" and d == business_domain:
            return e, "named person at business domain", True
    for e, kind, d in cands:
        if kind == "person" and is_free_mail(d):
            return e, "named free-mail address shown on site", True
    for e, kind, d in cands:
        if d == business_domain:
            return e, "mailbox at business domain", False
    for e, kind, d in cands:
        if is_free_mail(d):
            return e, "free-mail address shown on site (flagged: could be personal)", False
    words = {w for w in norm_name(business_name).split() if len(w) >= 4} if business_name else set()
    for e, kind, d in cands:
        if words and any(w in d.replace("-", "") or w in e.split("@")[0] for w in words):
            return e, "other-domain mailbox related to the business name", False
    return None, "no usable mailbox", False


# ---------------------------------------------------------------- LLM
def extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def enqueue_llm(conn, kind: str, business_id: int, prompt: str) -> bool:
    h = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    cur = conn.execute("INSERT OR IGNORE INTO llm_tasks(kind, business_id, prompt, prompt_hash) VALUES (?,?,?,?)",
                       (kind, business_id, prompt, h))
    return cur.rowcount > 0


def get_llm_response(conn, kind: str, business_id: int) -> dict | None:
    row = conn.execute("SELECT response FROM llm_tasks WHERE kind=? AND business_id=? AND status='done' "
                       "ORDER BY id DESC LIMIT 1", (kind, business_id)).fetchone()
    return extract_json(row["response"]) if row else None


def set_status(conn, business_id: int, status: str):
    assert status in STATUSES
    conn.execute("UPDATE businesses SET status=?, updated_at=? WHERE id=?", (status, now(), business_id))


# ---------------------------------------------------------------- suppression
def suppression_reasons(conn, business_id: int, cooldown_days: int | None = None) -> list[str]:
    """Every active reason this business must not be emailed. Empty list = clear."""
    seed = load_yaml("suppression_seed.yaml")
    cooldown = cooldown_days if cooldown_days is not None else seed["cooldown_days"]
    b = conn.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone()
    reasons = []
    low = (b["name"] or "").lower()
    for pat in seed["name_patterns"]:
        if pat.lower() in low:
            reasons.append(f"name pattern '{pat}'")
    emails = [r["email"] for r in conn.execute("SELECT email FROM contacts WHERE business_id=?", (business_id,))]
    domains = {d for d in [b["domain"], *[email_domain(e) for e in emails]] if d and not is_free_mail(d)}
    now_s = now()
    for kind, vals in (("email", emails), ("domain", sorted(domains)), ("name", [norm_name(b["name"])])):
        for v in vals:
            for s in conn.execute("SELECT reason FROM suppression WHERE kind=? AND value=? AND (expires IS NULL OR expires > ?)",
                                  (kind, v, now_s)):
                reasons.append(f"{kind} {v}: {s['reason']}")
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=cooldown)).strftime("%Y-%m-%d %H:%M:%S")
    n = conn.execute("SELECT COUNT(*) FROM sends WHERE business_id=? AND sent_at > ?", (business_id, cutoff)).fetchone()[0]
    if n:
        reasons.append(f"emailed within {cooldown} days")
    if conn.execute("SELECT 1 FROM onboarded WHERE business_id=?", (business_id,)).fetchone():
        reasons.append("already live on Tryuu")
    return reasons


# ---------------------------------------------------------------- DNS (free soft verification)
_MX_CACHE: dict[str, bool | None] = {}


def mx_ok(domain: str) -> bool | None:
    """True if the domain can receive mail (has a real MX), False if it cannot (NXDOMAIN, no MX, null MX),
    None if we could not tell. Uses DNS-over-HTTPS because outbound port 25 is blocked here."""
    import requests

    domain = domain.lower().strip()
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]
    res = None
    try:
        j = requests.get("https://cloudflare-dns.com/dns-query", params={"name": domain, "type": "MX"},
                         headers={"accept": "application/dns-json"}, timeout=10).json()
        if j.get("Status") == 3:
            res = False
        elif j.get("Status") == 0:
            ans = [a["data"].split()[-1] for a in j.get("Answer", []) if a.get("type") == 15]
            res = bool(ans) and not all(x == "." for x in ans)
    except (requests.RequestException, ValueError, KeyError):
        res = None
    _MX_CACHE[domain] = res
    return res


def dedupe_pages(texts: list[str], n: int = 8) -> list[str]:
    """Drop runs of n+ words already seen on an earlier page (navigation, footers) to keep prompts short."""
    seen: set[tuple] = set()
    out = []
    for t in texts:
        toks = t.split()
        keep = [True] * len(toks)
        wins = [tuple(toks[i:i + n]) for i in range(max(len(toks) - n + 1, 0))]
        for i, w in enumerate(wins):
            if w in seen:
                for j in range(i, i + n):
                    keep[j] = False
        seen.update(wins)
        out.append(" ".join(w for w, k in zip(toks, keep) if k))
    return out
