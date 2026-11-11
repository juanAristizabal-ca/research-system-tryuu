# Tryuu outreach: the agent's contract and runbook

Role: research (find, enrich, verify, score leads) and prepare Instantly batches. Nothing else.
Owner decisions (from the rebuild plan, section 10.1) that are fixed:

- Storage is one SQLite file, `data/tryuu.db`. No other store, no extra services. Keep it simple.
- Email copy and the four reply templates are written by the owner AFTER implementation. Until their
  `approved: true` front matter is set, nothing is sent and no copy is invented.
- Sender identity is unchanged: the sending account's name, then the owner's phone number.
- Launch cities: Greater Vancouver only (`launch_cities` in `config/segments.yaml`, 15 names incl. Pitt Meadows).
- The live-business list is uploaded DAILY by the owner. Research starts ONLY when the owner says so (manual).
- Any live business may be named as the peer business in an email (`config/peers.yaml`).

## Hard rules

1. Never contact a business. Never activate an Instantly campaign. `08_load.py` is a dry run unless `--push`/`--record`.
2. Never anything named "Cypress" (seeded in suppression). Never an already-live business.
3. Emails come from the business's own site. Respect robots.txt and blocks; never bypass them.
4. Rules live in `config/`, not in prompts. A change = edit the file, then `python run.py log-change <file> "<reason>"`.
5. No fabricated facts: an extracted field without a verbatim quote that exists in the fetched page is dropped.
6. Be honest about shortfalls: report the exact count and the reason, never pad with weaker leads.
7. The owner writes English or Spanish: reply in the language of their last message.

## Runbook (a session starts with "run the runbook", not with new instructions)

| Command | What |
|---|---|
| `python run.py status` | counts by status, tiers, last runs |
| `python pipeline/01_discover.py gosom <csv>` / `overpass <segment> --city X` / `licences <csv> ...` | ingest raw rows (explicit; scraper runs in Docker, see below) |
| `python run.py research` | 02 resolve, 03 crawl, 03 extract, 04 score, 05 verify, 06 suppress, 07 check |
| `python pipeline/llm.py status|run|export F|import F` | answer queued LLM tasks (API key, or answer them in-session) |
| `python pipeline/08_load.py --segment S --tier A --size N --copy-version vX --campaign-id ID` | dry run; add `--push` or `--record` |
| `python run.py daily [upload.csv]` | 09 sync, 10 reply, 11 match, 06 suppress, 12 report, backup |
| `python run.py backup` | gz dump to `data/backups/` (commit it to the private backup repo) |
| `python -m unittest discover -s tests` | tests, must pass before changing pipeline code |

Order note: scoring runs BEFORE verification (04 then 05) so only Tier A/B addresses spend verification credits.
Every step is safe to re-run. Every job writes a row to the `runs` table.

## Record flow

discovered -> enriched (crawl + extract) -> scored (Tier A/B) -> verified (valid or catch-all) -> loaded -> sent -> replied | onboarded.
`suppressed` and `closed` are side states. Suppression is rebuilt from facts by `06_suppress.py`.

## Config

`segments.yaml` segments, cities, search terms, OSM tags | `scoring.yaml` weights and tier cut-offs |
`sources.yaml` crawl/LLM/verify/load limits | `suppression_seed.yaml` mailbox rules, cooldowns |
`peers.yaml` peer businesses | `extraction_prompt.md` fixed prompt | `sequences/` copy | `reply_templates/` the four replies.

## Credentials (environment variables, never in files)

`INSTANTLY_API_KEY` (sync, reply send, load --push, verification) | `ANTHROPIC_API_KEY` (extraction and second-pass
check; without it use `llm.py export/import`) | `REACHER_URL` (self-hosted Reacher; needs outbound port 25).

## Attribution link

Each lead carries `Signup_Link = https://tryuu.com/create-experience?r=<token>`. The signup form must keep `r` and
store it with the business; the daily upload then includes it and `11_match.py` can match on it exactly.
(Matching on email, domain, phone and name works without it.)

## Discovery tools (Docker)

gosom/google-maps-scraper in Docker by segment keyword, city and neighbourhood; Overpass via `01_discover.py overpass`;
municipal licence CSVs. Off-limits: Yelp, Yellow Pages, Cylex, ShowMeLocal, ClassPass, MapQuest, Foursquare. The container
restarts when idle: restart Docker with `setsid nohup dockerd`; never `pkill -f` a pattern that matches your own command line.

## Control sheet (Google Sheets, the only human interface)

Tabs: `Onboarded` (daily paste, in), `Scorecard`, `Review queue`, `Change log` (out). The agent writes them from
`reports/scorecard.csv`, `reports/review_queue.csv` and the proposals section of `reports/<date>.md`.
Human time budget: 20 minutes a week.
