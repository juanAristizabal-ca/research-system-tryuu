-- Tryuu outreach: single source of truth. All statements are idempotent.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS businesses (
  id           INTEGER PRIMARY KEY,
  key          TEXT NOT NULL UNIQUE,          -- 'd:<domain>' | 'p:<place_id>' | 'e:<email>'
  domain       TEXT UNIQUE,
  place_id     TEXT UNIQUE,
  name         TEXT NOT NULL,
  segment      TEXT,
  city         TEXT,
  postal_code  TEXT,
  address      TEXT,
  phone        TEXT,
  website      TEXT,
  location_count INTEGER,
  status       TEXT NOT NULL DEFAULT 'discovered'
    CHECK (status IN ('discovered','enriched','verified','scored','suppressed',
                      'loaded','sent','replied','onboarded','closed')),
  origin       TEXT,                          -- discovery source or legacy import name
  first_seen   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS raw_rows (
  id           INTEGER PRIMARY KEY,
  source       TEXT NOT NULL,
  source_file  TEXT NOT NULL,
  row_hash     TEXT NOT NULL,
  retrieved_at TEXT NOT NULL DEFAULT (datetime('now')),
  payload      TEXT NOT NULL,                 -- JSON, untouched
  business_id  INTEGER REFERENCES businesses(id),
  UNIQUE (source, source_file, row_hash)
);

CREATE TABLE IF NOT EXISTS merges (
  id INTEGER PRIMARY KEY, kept_id INTEGER, raw_row_id INTEGER, reason TEXT,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY,
  business_id INTEGER NOT NULL REFERENCES businesses(id),
  url TEXT NOT NULL, kind TEXT, http_status INTEGER,
  text TEXT, fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (business_id, url)
);

CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  business_id INTEGER NOT NULL REFERENCES businesses(id),
  person TEXT, role TEXT, email TEXT, phone TEXT,
  is_person INTEGER NOT NULL DEFAULT 0,
  is_chosen INTEGER NOT NULL DEFAULT 0,
  choice_reason TEXT,
  verification TEXT,                          -- valid | risky | invalid | catch_all | NULL
  verified_at TEXT, source_url TEXT,
  UNIQUE (business_id, email)
);

CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY,
  business_id INTEGER NOT NULL REFERENCES businesses(id),
  field TEXT NOT NULL, value TEXT, quote TEXT, url TEXT,
  crawled_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (business_id, field)
);

CREATE TABLE IF NOT EXISTS scores (
  business_id INTEGER PRIMARY KEY REFERENCES businesses(id),
  score INTEGER NOT NULL, tier TEXT NOT NULL,  -- A | B | X
  signals TEXT NOT NULL, scoring_version TEXT NOT NULL,
  scored_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS checks (
  business_id INTEGER PRIMARY KEY REFERENCES businesses(id),
  passed INTEGER NOT NULL, failures TEXT, checked_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,                  -- segment_tier_copyversion_week
  segment TEXT, tier TEXT, copy_version TEXT, scoring_version TEXT,
  instantly_campaign_id TEXT, n_loaded INTEGER, research_precision REAL,
  loaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batch_leads (
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  business_id INTEGER NOT NULL REFERENCES businesses(id),
  email TEXT NOT NULL, pushed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (batch_id, business_id)
);

CREATE TABLE IF NOT EXISTS sends (
  id INTEGER PRIMARY KEY,
  business_id INTEGER REFERENCES businesses(id),
  batch_id INTEGER REFERENCES batches(id),
  campaign_name TEXT, email TEXT, step INTEGER,
  sent_at TEXT, bounced INTEGER NOT NULL DEFAULT 0,
  instantly_email_id TEXT UNIQUE,
  legacy INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sends_business ON sends(business_id);

CREATE TABLE IF NOT EXISTS replies (
  id INTEGER PRIMARY KEY,
  business_id INTEGER REFERENCES businesses(id),
  instantly_email_id TEXT UNIQUE, thread_id TEXT, from_email TEXT,
  eaccount TEXT, subject TEXT, text TEXT, received_at TEXT,
  is_auto INTEGER NOT NULL DEFAULT 0,
  intent TEXT, template_sent TEXT, handled_at TEXT
);

CREATE TABLE IF NOT EXISTS onboarded (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL, signup_email TEXT, website TEXT, phone TEXT, city TEXT,
  date_created TEXT,
  is_test_email INTEGER NOT NULL DEFAULT 0,   -- team-created listing: email is a placeholder
  business_id INTEGER REFERENCES businesses(id),
  batch_id INTEGER REFERENCES batches(id),
  match_rule TEXT,                            -- email | domain | phone | name | NULL
  in_window INTEGER,                          -- 1 within 21 days of email 1, 0 not, NULL unknown
  first_seen TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (name, signup_email)
);

CREATE TABLE IF NOT EXISTS suppression (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('email','domain','name')),
  value TEXT NOT NULL, reason TEXT NOT NULL,
  created TEXT NOT NULL DEFAULT (datetime('now')),
  expires TEXT,                               -- NULL = permanent
  UNIQUE (kind, value, reason)
);

CREATE TABLE IF NOT EXISTS config_versions (
  file TEXT NOT NULL, version TEXT NOT NULL, sha256 TEXT NOT NULL,
  change TEXT, reason TEXT, created TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (file, version)
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, job TEXT NOT NULL,
  started TEXT NOT NULL, ended TEXT, n_in INTEGER, n_out INTEGER, errors TEXT
);

-- LLM work is queued so it can run via API or be answered by the agent in-session.
CREATE TABLE IF NOT EXISTS llm_tasks (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL, business_id INTEGER REFERENCES businesses(id),
  prompt TEXT NOT NULL, prompt_hash TEXT NOT NULL,
  response TEXT, status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','error')),
  error TEXT, created TEXT NOT NULL DEFAULT (datetime('now')), done TEXT,
  UNIQUE (kind, business_id, prompt_hash)
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT, updated TEXT NOT NULL DEFAULT (datetime('now')));

-- Every discovery query that was run, so another session knows what has already been searched.
CREATE TABLE IF NOT EXISTS search_log (
  id INTEGER PRIMARY KEY,
  category TEXT NOT NULL, source TEXT NOT NULL, query TEXT NOT NULL, city TEXT,
  ran_at TEXT NOT NULL DEFAULT (datetime('now')),
  rows_read INTEGER, rows_new INTEGER,
  UNIQUE (category, source, query, city)
);
