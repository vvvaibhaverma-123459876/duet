-- D09: routing decisions (what DUET chose for a participant's turn and why,
-- then what the provider accepted and observed), user pins and user profile
-- mappings. Pins and mappings are the user's; DUET never writes them into a
-- provider's own configuration.

CREATE TABLE routing_decisions (
  decision_id    TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES runs (run_id),
  participant_id TEXT NOT NULL,
  provider       TEXT NOT NULL,
  task_id        TEXT,
  task_revision  INTEGER,
  role           TEXT NOT NULL,
  purpose        TEXT NOT NULL,
  turn_index     INTEGER NOT NULL,
  action         TEXT NOT NULL,
  profile        TEXT NOT NULL,
  model          TEXT,
  effort         TEXT,
  coverage       TEXT NOT NULL,
  floor_met      INTEGER NOT NULL,
  escalated      INTEGER NOT NULL,
  requested_by   TEXT NOT NULL,
  action_id      TEXT,
  detail_json    TEXT NOT NULL,
  outcome_json   TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX routing_decisions_by_participant ON routing_decisions (participant_id, created_at);
CREATE INDEX routing_decisions_by_run ON routing_decisions (run_id, created_at);

CREATE TABLE routing_pins (
  provider    TEXT PRIMARY KEY,
  model       TEXT,
  effort      TEXT,
  min_profile TEXT,
  max_profile TEXT,
  set_by      TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE routing_maps (
  map_id     TEXT PRIMARY KEY,
  provider   TEXT NOT NULL,
  profile    TEXT NOT NULL,
  model      TEXT,
  effort     TEXT,
  set_by     TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
