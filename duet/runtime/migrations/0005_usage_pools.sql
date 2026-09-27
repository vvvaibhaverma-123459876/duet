-- D07: local usage pools with user-authorised allowances, and deduplicated
-- usage records. Reservations (0001) draw on a pool; the capacity check runs
-- inside the reserving transaction, so two runs (or two processes) can never
-- both take the last of an allowance.

CREATE TABLE usage_pools (
  pool_id        TEXT PRIMARY KEY,
  provider       TEXT NOT NULL,
  metric         TEXT NOT NULL,
  unit           TEXT NOT NULL,
  allowance_json TEXT,
  window_seconds INTEGER,
  enforcement    TEXT NOT NULL,
  defined_by     TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

-- record_id is the dedupe identity (provider/source event), so replayed or
-- duplicated reports of the same work are stored once.
CREATE TABLE usage_records (
  record_id      TEXT PRIMARY KEY,
  pool_id        TEXT NOT NULL REFERENCES usage_pools (pool_id),
  run_id         TEXT,
  action_id      TEXT,
  participant_id TEXT,
  metric         TEXT NOT NULL,
  quantity_json  TEXT NOT NULL,
  quality        TEXT NOT NULL,
  source         TEXT NOT NULL,
  observed_at    TEXT NOT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX usage_records_by_pool ON usage_records (pool_id, observed_at);
CREATE INDEX reservations_by_pool ON reservations (pool, state);
