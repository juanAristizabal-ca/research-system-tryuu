# Tryuu outreach: the agent's contract and runbook

**The agent (Claude, in a cloud session) is the brain and the operator.** There is no Anthropic API key and no Instantly
API key: every AI judgement is answered by the agent itself, and every Instantly action goes through the agent's
Instantly connector (MCP tools). Scripts are deterministic; the agent moves data between the connector, the scripts and
the database. Long sessions are fine. GitHub is only for keeping state and versions (see "Persistence").

Owner decisions (rebuild plan section 10.1) that are fixed:

- Storage is one SQLite file, `data/tryuu.db`. Keep it simple, no extra services.
- Email copy and the four reply templates are written by the owner AFTER implementation. Until their `approved: true`
  front matter is set nothing is sent and no copy is invented.
- Sender identity unchanged: the sending account's name, then the owner's phone number.
- Launch cities: Greater Vancouver only (`launch_cities` in `config/segments.yaml`).
- The live-business list is uploaded DAILY by the owner. Research starts ONLY when the owner says so.
- Any live business may be the peer business in an email (`config/peers.yaml`).
- Campaigns already active in Instantly (16 on 2026-11-12) are fine and their sending limits are safe (owner, 2026-11-12). Never pause or change them unless asked.

## Hard rules

1. Never contact a business. Never activate an Instantly campaign. Loading is dry-run until `--record`, and the agent
   only adds leads to a campaign the owner named.
2. Never anything named "Cypress" (seeded in suppression). Never an already-live business.
3. Emails come from the business's own site. Respect robots.txt and blocks; never bypass them.
4. Rules live in `config/`, not in prompts. A change = edit the file, then `python run.py log-change <file> "<reason>"`.
5. No fabricated facts: an extracted field without a verbatim quote that exists in the fetched page is dropped by code.
6. Report shortfalls honestly with exact counts and reasons; never pad with weaker leads.
7. The owner writes English or Spanish: reply in the language of their last message.

## Session start and end

Start: `cd tryuu-outreach && git pull` (if a remote exists) then `python run.py restore` when `data/tryuu.db` is missing
(new container), then `python run.py status`. End (and after every meaningful step): `python run.py save "<what changed>"`.

## Runbook

| Step | Who | Command |
|---|---|---|
| Discover | scripts (Docker scraper, Overpass, licences) | `01_discover.py gosom <csv>` / `overpass <segment> --city X` / `licences <csv> ...` |
| Research pipeline | scripts | `python run.py research` (02 resolve, 03 crawl, 03 extract screen+queue, 04 score, 05 mx check, 06 suppress, 07 check) |
| Extraction and second-pass answers | **the agent** | `llm.py export <w.md> --kind extract --limit 25`, read it, write `answers.jsonl`, `llm.py import answers.jsonl`, re-run `03_enrich.py extract`; same with `--kind segment_check` then `07_check.py` |
| Load | scripts + **connector** | `08_load.py --segment S --tier A --size N --copy-version vX --campaign-id ID` (dry run) -> `add_leads_to_campaign_or_list_bulk` with `data/loads/<batch>.leads.json` -> `08_load.py ... --record` |
| Sync from Instantly | **connector** + script | `list_emails` (campaign_id, email_type sent / received, limit 100, follow `next_starting_after`) and `list_leads` (campaign, filter FILTER_VAL_BOUNCED / FILTER_VAL_UNSUBSCRIBED) pages are saved to files; `09_sync.py emails|leads <files>` |
| Replies | script + **connector** | `10_reply.py` writes `reports/replies_to_send.json`; send each with `reply_to_email`; then `10_reply.py mark-sent <reply ids>` |
| Daily | all | owner's upload -> `python run.py daily <upload.csv>` (statuses, reply, match, suppress, report, save), then write the scorecard and review queue into the Control sheet |
| Tests | script | `python -m unittest discover -s tests` (must pass before changing pipeline code) |

Order note: scoring runs BEFORE the MX check (04 then 05) so only Tier A/B addresses are checked.
Every step is safe to re-run; every job writes a row to `runs`.

## Verification (owner decision 2026-11-11)

No Instantly verification: the owner accepts some bounces that cannot be prevented. `verify.min_level` is `mx_only`: the
free DNS-over-HTTPS check (`05_verify.py mx`, run by `run.py research`) confirms only that the domain can receive mail.
Outbound port 25 is blocked, so there is no SMTP check. Instantly's `verify_email` is NOT used (it costs credits).
Bounce protection in Instantly stays on; bounces are synced, suppressed, and reported in the scorecard.

## Record flow

discovered -> enriched (crawl + extract) -> scored (Tier A/B) -> verified -> loaded -> sent -> replied | onboarded.
`suppressed` and `closed` are side states. Suppression is rebuilt from facts by `06_suppress.py`.

## Categories (research is run per category)

Tryuu welcomes every kind of business, from individuals to global brands. `config/categories.yaml` is the catalogue (71
categories in 13 groups): the legacy campaign codes that already searched each one, Maps search keywords, and the `types`
tokens that classify the 26k known businesses. `python pipeline/category_report.py` writes `reports/category_coverage.*`:
known / emailed / never-contacted / live per category plus a status suggestion. `exhausted by our sweeps` means our own
lists are used up, not the market. Categories marked `proposed: true` have not been searched. Brands/chains and
municipal centres are separate tracks with different contacts (owner decision pending).

## Config

`categories.yaml` categories, keywords, legacy campaigns | `segments.yaml` launch cities | `scoring.yaml` weights, tier cut-offs |
`sources.yaml` crawl, LLM size, verify level, load limits, git | `suppression_seed.yaml` mailbox rules, cooldowns |
`peers.yaml` peers | `extraction_prompt.md` fixed instructions | `sequences/` copy | `reply_templates/` the four replies.

## Attribution link

Each lead carries `Signup_Link = https://tryuu.com/create-experience?r=<token>`. The signup form must keep `r` and store it;
the daily upload then includes it. Matching on email, domain, phone and name works without it.

## Persistence (GitHub)

`python run.py save "msg"` writes a text dump (`data/backup/tryuu.sql`, no page text) and commits and pushes. The dump
holds business emails and reply text, so data is committed ONLY when `git.private_confirmed: true` in
`config/sources.yaml`, which the owner flips once the remote repository is private. Until then only code and config are
committed. `python run.py restore` rebuilds the database from the dump in a new container (page text is re-crawled when needed).

## Discovery tools (Docker)

gosom/google-maps-scraper in Docker by segment keyword, city and neighbourhood; Overpass via `01_discover.py overpass`;
municipal licence CSVs. Off-limits: Yelp, Yellow Pages, Cylex, ShowMeLocal, ClassPass, MapQuest, Foursquare. If Docker is
down start it with `setsid nohup dockerd`; never `pkill -f` a pattern that matches your own command line.

## Control sheet (Google Sheets, the only human interface)

Tabs: `Onboarded` (daily paste, in), `Scorecard`, `Review queue`, `Change log` (out), in spreadsheet
`1toktOnXQk92Zvfb-TFkHH9pfVnlgy8KonmobIviQ1wo`. The agent writes them from `reports/scorecard.csv`,
`reports/review_queue.csv` and the proposals in `reports/<date>.md`. Human time budget: 20 minutes a week.
