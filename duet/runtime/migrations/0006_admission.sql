-- D08: completion-aware admission. Finishing reserves are HELD reservations
-- without an action (category finishing:<purpose>); this table carries what
-- they are for. Provider quota gauges and holds are provider scoped: one
-- authenticated login per provider CLI on this machine. Admissions record
-- every admit, defer and pause decision with its reasons.

CREATE TABLE finishing_reserves (
  reservation_id TEXT PRIMARY KEY REFERENCES reservations (reservation_id),
  run_id         TEXT NOT NULL REFERENCES runs (run_id),
  purpose        TEXT NOT NULL,
  provider       TEXT NOT NULL,
  pool           TEXT NOT NULL,
  metric         TEXT NOT NULL,
  units_planned  INTEGER NOT NULL,
  units_left     INTEGER NOT NULL,
  per_unit_json  TEXT,
  shortfall_json TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX finishing_reserves_by_run ON finishing_reserves (run_id);

CREATE TABLE quota_gauges (
  gauge_id              TEXT PRIMARY KEY,
  provider              TEXT NOT NULL,
  window                TEXT NOT NULL,
  used_percent_json     TEXT NOT NULL,
  previous_percent_json TEXT,
  resets_at             TEXT,
  observed_at           TEXT NOT NULL,
  source                TEXT NOT NULL,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);
CREATE INDEX quota_gauges_by_provider ON quota_gauges (provider);

CREATE TABLE quota_holds (
  provider        TEXT PRIMARY KEY,
  state           TEXT NOT NULL,
  reason          TEXT NOT NULL,
  resume_at       TEXT,
  attempts        INTEGER NOT NULL,
  probe_action_id TEXT,
  placed_at       TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE admissions (
  admission_id   TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES runs (run_id),
  participant_id TEXT,
  action_id      TEXT,
  provider       TEXT NOT NULL,
  action_class   TEXT NOT NULL,
  purpose        TEXT NOT NULL,
  verdict        TEXT NOT NULL,
  reason         TEXT NOT NULL,
  detail_json    TEXT NOT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX admissions_by_run ON admissions (run_id, created_at);

-- A HELD reservation is superseded once its action's usage is recorded in
-- the same pool (pools.pool_usage looks this up).
CREATE INDEX usage_records_by_action ON usage_records (action_id, pool_id);
