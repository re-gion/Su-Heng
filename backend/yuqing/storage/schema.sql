PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS task (
  id TEXT PRIMARY KEY,
  event_query TEXT NOT NULL,
  user_note TEXT,
  time_range_from TEXT,
  time_range_to TEXT,
  depth TEXT NOT NULL DEFAULT 'standard' CHECK (depth IN ('quick','standard','deep')),
  status TEXT NOT NULL CHECK (status IN ('queued','running','pausing','paused','stopping','failed','done')),
  phase TEXT NOT NULL DEFAULT 'planning',
  outer_round INTEGER NOT NULL DEFAULT 0,
  config_snapshot TEXT,
  tokens_used INTEGER NOT NULL DEFAULT 0,
  cost_estimate REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
  pk TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  local_id TEXT NOT NULL,
  url TEXT NOT NULL,
  url_hash TEXT NOT NULL,
  title TEXT NOT NULL,
  source_name TEXT,
  source_domain TEXT NOT NULL,
  publisher_entity TEXT,
  origin_url TEXT,
  source_role TEXT NOT NULL DEFAULT 'unknown' CHECK (source_role IN ('authority','party','independent','syndicated','unknown')),
  source_tier INTEGER NOT NULL CHECK (source_tier BETWEEN 1 AND 5),
  published_at TEXT,
  discovered_at TEXT NOT NULL,
  fetch_status TEXT NOT NULL DEFAULT 'discovered' CHECK (fetch_status IN ('discovered','fetched','fetch_failed')),
  fetched_at TEXT,
  snippet TEXT,
  content_text TEXT,
  snapshot_path TEXT,
  content_sha256 TEXT,
  retrieval_query TEXT,
  provider TEXT,
  lang TEXT,
  extra TEXT,
  CHECK (fetch_status <> 'fetched' OR (content_text IS NOT NULL AND snapshot_path IS NOT NULL AND content_sha256 IS NOT NULL AND fetched_at IS NOT NULL)),
  CHECK (fetch_status = 'fetched' OR snippet IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_task_local ON evidence(task_id, local_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_task_url ON evidence(task_id, url_hash);
CREATE INDEX IF NOT EXISTS ix_evidence_task_time ON evidence(task_id, published_at);

CREATE TABLE IF NOT EXISTS claim (
  pk TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  local_id TEXT NOT NULL,
  text TEXT NOT NULL,
  statement_kind TEXT NOT NULL DEFAULT 'fact' CHECK (statement_kind IN ('fact','rumor')),
  rumor_text TEXT,
  correction_text TEXT,
  agent TEXT NOT NULL,
  round INTEGER NOT NULL,
  section TEXT,
  is_editorial INTEGER NOT NULL DEFAULT 0 CHECK (is_editorial IN (0,1)),
  is_key INTEGER NOT NULL DEFAULT 1 CHECK (is_key IN (0,1)),
  is_key_reason TEXT,
  badge TEXT CHECK (badge IN ('verified','unverified','disputed','refuted')),
  verdict TEXT CHECK (verdict IN ('support','partial','contradict','not_mentioned','conflict')),
  verify_reason TEXT,
  verification_state TEXT NOT NULL DEFAULT 'pending' CHECK (verification_state IN ('pending','complete','incomplete','skipped')),
  independent_sources INTEGER DEFAULT 0,
  max_source_tier INTEGER,
  verifier_model TEXT,
  verified_at TEXT,
  created_at TEXT NOT NULL,
  CHECK ((statement_kind = 'fact' AND correction_text IS NULL) OR (statement_kind = 'rumor' AND rumor_text IS NULL)),
  CHECK (is_key = 1 OR is_key_reason IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_claim_task_local ON claim(task_id, local_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_claim_task_text ON claim(task_id, text);

CREATE TABLE IF NOT EXISTS claim_evidence (
  claim_pk TEXT NOT NULL REFERENCES claim(pk) ON DELETE CASCADE,
  evidence_pk TEXT NOT NULL REFERENCES evidence(pk) ON DELETE CASCADE,
  quote TEXT,
  quote_type TEXT NOT NULL DEFAULT 'paraphrase' CHECK (quote_type IN ('verbatim','paraphrase','snippet')),
  quote_start INTEGER,
  quote_end INTEGER,
  quote_verified INTEGER NOT NULL DEFAULT 0 CHECK (quote_verified IN (0,1)),
  relation TEXT CHECK (relation IN ('support','partial','contradict','not_mentioned','conflict')),
  verify_reason TEXT,
  cited_sentence TEXT,
  cited_verified INTEGER NOT NULL DEFAULT 0 CHECK (cited_verified IN (0,1)),
  is_correction INTEGER NOT NULL DEFAULT 0 CHECK (is_correction IN (0,1)),
  superseded INTEGER NOT NULL DEFAULT 0 CHECK (superseded IN (0,1)),
  ord INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (claim_pk, evidence_pk),
  CHECK (quote_type <> 'verbatim' OR (quote_start IS NOT NULL AND quote_end IS NOT NULL AND quote_verified = 1))
);

CREATE TABLE IF NOT EXISTS event_log (
  task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('task.status','agent.status','agent.token','search.result','evidence.added','claim.added','forum.message','host.review','loop.round','verify.progress','report.section','report.done','budget.update','warning','error')),
  ts TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (task_id, seq)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS forum_message (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  round INTEGER NOT NULL,
  agent TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('finding','summary','question','review','directive','conflict','system')),
  content TEXT NOT NULL,
  refs TEXT,
  payload TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_state (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  step_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('llm_call','search','fetch','verify','round_checkpoint')),
  status TEXT NOT NULL CHECK (status IN ('intent','executing','settled','failed')),
  replay TEXT NOT NULL DEFAULT 'safe' CHECK (replay IN ('safe','never')),
  payload TEXT,
  result_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_state_step ON task_state(task_id, step_key);

CREATE TABLE IF NOT EXISTS hot_snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT REFERENCES dataset_asset(id) ON DELETE SET NULL,
  platform TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  rank INTEGER NOT NULL,
  title TEXT NOT NULL,
  heat_value REAL,
  url TEXT,
  raw TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_hot_snapshot_point
ON hot_snapshot(platform, captured_at, rank, title);
CREATE INDEX IF NOT EXISTS ix_hot_time ON hot_snapshot(captured_at, platform);
CREATE INDEX IF NOT EXISTS ix_hot_title ON hot_snapshot(title);

CREATE TABLE IF NOT EXISTS dataset_asset (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  source_url TEXT NOT NULL,
  license_label TEXT NOT NULL,
  upstream_rights_note TEXT NOT NULL,
  redistribution TEXT NOT NULL CHECK (redistribution IN ('allowed','restricted','unknown')),
  personal_fields_removed INTEGER NOT NULL DEFAULT 1 CHECK (personal_fields_removed IN (0,1)),
  content_sha256 TEXT,
  record_count INTEGER NOT NULL DEFAULT 0,
  metadata TEXT,
  imported_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS historical_event (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES dataset_asset(id) ON DELETE CASCADE,
  event_name TEXT NOT NULL,
  event_time_start TEXT,
  event_time_end TEXT,
  summary TEXT NOT NULL,
  outcome TEXT,
  nature TEXT,
  outbreak_path TEXT,
  response TEXT,
  regulatory_involvement TEXT,
  source_url TEXT NOT NULL,
  source_title TEXT,
  source_name TEXT,
  source_published_at TEXT,
  keywords TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(asset_id, event_name, event_time_start, source_url)
);
CREATE INDEX IF NOT EXISTS ix_historical_event_time
ON historical_event(event_time_start, event_time_end);
CREATE INDEX IF NOT EXISTS ix_historical_event_name ON historical_event(event_name);

CREATE TABLE IF NOT EXISTS event_alias (
  event_id TEXT NOT NULL REFERENCES historical_event(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  PRIMARY KEY(event_id, alias)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS task_history_match (
  task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  historical_event_id TEXT NOT NULL REFERENCES historical_event(id) ON DELETE CASCADE,
  evidence_pk TEXT REFERENCES evidence(pk) ON DELETE SET NULL,
  score REAL NOT NULL,
  matched_terms TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  PRIMARY KEY(task_id, historical_event_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0,1)),
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  ir_json TEXT NOT NULL,
  html_path TEXT,
  pdf_path TEXT,
  metrics TEXT,
  generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_quota (
  provider TEXT NOT NULL,
  period_key TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(provider, period_key)
);

CREATE TABLE IF NOT EXISTS takedown_request (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT NOT NULL REFERENCES report(id) ON DELETE CASCADE,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
  requested_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_takedown_report_status
ON takedown_request(report_id, status);

CREATE TABLE IF NOT EXISTS demo_task_owner (
  task_id TEXT PRIMARY KEY REFERENCES task(id) ON DELETE CASCADE,
  owner_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_demo_owner ON demo_task_owner(owner_hash, created_at);
