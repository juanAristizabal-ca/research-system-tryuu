"""Deterministic extraction (no AI, no tokens): same output schema as the agent's extraction answers.

Every field still passes through apply_extraction(), which only keeps a field when its quote exists verbatim in the stored
page text. Quality is measured against the agent's own answers on a sample (`03_enrich.py audit`), see CATEGORY_STATE.md.
  segment   Maps category label first, then the site's title/first words, both classified with config/categories.yaml
  offer     sentence matching the plan's rule (free/complimentary next to trial, class, consultation...), best phrasing first
  owner     'Founder X', 'X, Owner', 'My name is X ... owner' patterns on the site
  franchise 'franchise', 'chain of' wording
  locations number of distinct BC postal codes (at least 1)"""
from __future__ import annotations

import json
import re

import lib

NAME_STOP = {"photo", "phone", "email", "contact", "studio", "dance", "our", "the", "team", "about", "instructor", "director", "artistic",
             "founder", "owner", "principal", "head", "executive", "chef", "manager", "teacher", "coach", "and", "home", "meet", "welcome",
             "academy", "school", "centre", "center", "classes", "class", "lessons", "ballet", "yoga", "music", "services", "co", "inc",
             "ltd", "vancouver", "burnaby", "surrey", "richmond", "canada", "british", "columbia", "monday", "tuesday", "wednesday",
             "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april", "june", "july", "august", "september",
             "october", "november", "december", "new", "all", "this", "that", "his", "her", "its", "who", "what", "why", "how", "join",
             "book", "call", "read", "more", "info", "see", "get", "free", "kids", "adult", "adults", "teen", "teens", "studios", "fitness"}
ROLE_WORDS = r"(?:Co-?[Ff]ounder|[Ff]ounder|[Oo]wner|[Dd]irector|[Pp]rincipal|[Pp]roprietor|[Hh]ead [Ii]nstructor|[Hh]ead [Cc]oach|[Hh]ead [Cc]hef|[Cc]hef/[Oo]wner|[Cc]hef and [Oo]wner)"
NAME2 = r"([A-Z][a-z]{2,15})\s+([A-Z][a-z]{2,20})"
CITY_WORDS = {"north", "south", "west", "east", "langley", "delta", "coquitlam", "port", "maple", "ridge", "white", "rock", "pitt", "meadows", "moody",
              "westminster", "lonsdale", "steveston", "yaletown", "gastown", "kitsilano", "fleetwood", "newton", "guildford", "cloverdale"}
# (pattern, kind) kind: 'two' = needs a two-word personal name, 'first' = first-person sentence, single name allowed
OWNER_PATTERNS = [
    (re.compile(rf"{NAME2}\s*[,–—\-|:(]?\s*(?:is\s+(?:the|our)\s+|the\s+)?(?:Artistic |Studio |Executive |Managing )?({ROLE_WORDS})"), "two"),
    (re.compile(rf"({ROLE_WORDS})s?\s*[:\-–—,]?\s*(?:is\s+)?{NAME2}"), "two"),
    (re.compile(rf"(?:Founded|Owned|Led|Run|Directed) by (?:\w+ ){{0,2}}?{NAME2}"), "two"),
    (re.compile(r"(?:[Mm]y name is|I am|I’m|I'm)\s+([A-Z][a-z]{2,15})(?:\s+[A-Z][a-z]{2,20})?[,.]?\s*(?:and I am\s+)?(?:the\s+)?(?:a |an )?(?:founder|owner|director|chef|instructor|teacher|coach)"), "first"),
]
BAD_OFFER = re.compile(r"shipping|parking|delivery|returns?\b|gift card|cancell|% off|\bdiscount|subscribe|newsletter|wifi|wi-fi|recipe", re.I)
GOOD_OFFER = re.compile(r"free (first |intro(?:ductory)? |trial |sample |discovery )?(trial|class|lesson|consultation|assessment|session|tour|tasting|estimate|quote|visit)|"
                        r"first (class|lesson|session|visit) (is )?free|complimentary|try (a|your first|the) class (for )?free|on us", re.I)
FRANCHISE_RE = re.compile(r"\bfranchis(e|ed|ee|es|or)\b|\ba chain of\b|\blocations (across|throughout) (canada|north america)\b", re.I)
NON_TARGET = re.compile(r"salvation army|school district|secondary school|elementary school|\bcity of\b|food bank|\bchurch\b|community college|public library|"
                        r"\bmunicipal|government of|\bsd ?\d{2}\b|\bsociety for\b|hospital", re.I)
NON_TARGET_DOMAIN = re.compile(r"(\.gov(\.|$)|\.edu(\.|$)|\.gc\.ca$|\.bc\.ca$|deltasd|sd\d+\.|csf\.bc|newwestcity|\.gov\.bc)")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\s{2,}| \| ")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text) if 20 <= len(s.strip()) <= 300]


def offer_type(s: str) -> str:
    t = s.lower()
    for k, v in (("consult", "consultation"), ("assess", "assessment"), ("tour", "tour"), ("tasting", "tasting"), ("quote", "quote"),
                 ("estimate", "quote"), ("trial", "trial"), ("lesson", "class"), ("class", "class"), ("session", "class"), ("visit", "tour")):
        if k in t:
            return v
    return "trial"


def title_for(kind: str | None) -> str | None:
    return {"trial": "Free trial class", "class": "Free first class", "consultation": "Free consultation", "assessment": "Free assessment",
            "tour": "Free tour", "tasting": "Free tasting", "quote": "Free quote"}.get(kind or "")


def find_owner(pages: list[tuple[str, str]], business_name: str = ""):
    """Precision first: a wrong first name in an email greeting is worse than no name. Two-word personal names only
    (unless the site says 'my name is X ... owner'), and never a word of the business name, a city or a generic word."""
    bwords = {w for w in lib.norm_name(business_name).split() if len(w) >= 3}
    for url, text in sorted(pages, key=lambda p: (0 if re.search(r"about|team|story|meet|contact|our-", p[0]) else 1)):
        for pat, kind in OWNER_PATTERNS:
            for m in pat.finditer(text):
                groups = [g for g in m.groups() if g]
                role = next((g for g in groups if re.fullmatch(ROLE_WORDS, g)), None) or "owner"
                words = [g for g in groups if re.fullmatch(r"[A-Z][a-z]{2,20}", g) and g != role]
                if not words:
                    continue
                first = words[0]
                bad = {w.lower() for w in words} & (NAME_STOP | CITY_WORDS | (bwords if kind == "two" else set()))
                if bad or (kind == "two" and len(words) < 2):
                    continue
                quote = re.sub(r"\s+", " ", text[max(0, m.start() - 10): m.end() + 10].strip())
                if first.lower() in quote.lower() and lib.quote_in_text(quote, text):
                    rl = role.lower()
                    rl = "founder" if "found" in rl else "owner" if ("owner" in rl or "proprietor" in rl) else "director" if ("direct" in rl or "principal" in rl) else "practitioner"
                    return first, rl, quote, url
    return None


def extract(conn, bid: int) -> dict:
    biz = conn.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
    pages = [(r["url"], r["text"]) for r in conn.execute("SELECT url, text FROM pages WHERE business_id=? AND text IS NOT NULL ORDER BY id", (bid,))]
    alltext = "\n".join(t for _, t in pages)
    home = pages[0][1] if pages else ""
    maps_cat = ""
    for r in conn.execute("SELECT payload FROM raw_rows WHERE business_id=? AND source='gosom'", (bid,)):
        maps_cat = json.loads(r["payload"])["n"].get("category", "") or maps_cat
    segs = lib.segment_catalog()
    if NON_TARGET.search(f"{biz['name']} {home[:300]}") or NON_TARGET_DOMAIN.search(biz["domain"] or ""):
        return {"segment": "other", "segment_quote": None, "offer": {"sentence": None, "url": None, "type": None},
                "owner": {"first_name": None, "role": None, "quote": None, "url": None}, "accepting_clients": "unknown",
                "location_count": 1, "franchise_wording": False, "listing_title": None, "listing_description": None}
    seg = lib.classify_category(maps_cat)
    site = lib.classify_category(home[:500])
    if seg not in segs:
        seg = site if site in segs else None
    seg_quote = None
    if seg:
        pats = lib.category_patterns().get(seg, [])
        for s in [home[:160]] + sentences(home):
            if any(p.search(s.lower()) for p in pats):
                seg_quote = s[:200]
                break
        seg_quote = seg_quote or home[:100]
    offer = {"sentence": None, "url": None, "type": None}
    best = None
    for url, text in pages:
        for s in sentences(text):
            if lib.is_free_offer(s) and not BAD_OFFER.search(s) and len(s) <= 220:
                sc = (2 if GOOD_OFFER.search(s) else 0) - (len(s) / 1000)
                if best is None or sc > best[0]:
                    best = (sc, s, url)
    if best and best[0] >= 1:  # only phrasings that clearly describe a free first-time offer
        offer = {"sentence": best[1], "url": best[2], "type": offer_type(best[1])}
    ow = find_owner(pages, biz['name'])
    postals = {m.group(0).upper().replace(" ", "") for m in lib.POSTAL_RE.finditer(alltext) if m.group(1).upper().startswith("V")}
    desc = next((s for s in sentences(home) if 60 <= len(s) <= 280 and not re.search(r"cookie|©|copyright|skip to|menu|log in|cart", s, re.I)), None)
    return {
        "segment": seg or "other", "segment_quote": seg_quote,
        "offer": offer,
        "owner": {"first_name": ow[0], "role": ow[1], "quote": ow[2], "url": ow[3]} if ow else {"first_name": None, "role": None, "quote": None, "url": None},
        "accepting_clients": "unknown", "location_count": max(1, min(len(postals), 5)),
        "franchise_wording": bool(FRANCHISE_RE.search(alltext)),
        "listing_title": title_for(offer["type"]) or (f"{biz['name']}"[:60]), "listing_description": desc[:300] if desc else None,
    }
