-- DUET v2 runtime schema, version 1.
-- The event log and the materialised state live in one database so that they
-- always commit together. Materialised tables are derivable by replaying
-- `events` through duet.runtime.reducer; operational tables are marked.

CREATE TABLE events (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id     TEXT NOT NULL UNIQUE,
  run_id       TEXT,
  type         TEXT NOT NULL,
  actor        TEXT NOT NULL,
  at           TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX events_by_run ON events (run_id, seq);

CREATE TABLE policies (
  policy_hash TEXT PRIMARY KEY,
  body_json   TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE runs (
  run_id                 TEXT PRIMARY KEY,
  repo_id                TEXT NOT NULL,
  objective              TEXT NOT NULL,
  scope_json             TEXT NOT NULL,
  base_sha               TEXT,
  acceptance_json        TEXT NOT NULL,
  acceptance_hash        TEXT NOT NULL,
  acceptance_version     INTEGER NOT NULL,
  policy_hash            TEXT NOT NULL REFERENCES policies (policy_hash),
  initiating_participant TEXT,
  lifecycle              TEXT NOT NULL,
  collaboration          TEXT NOT NULL,
  deadline_at            TEXT,
  state_version          INTEGER NOT NULL,
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL
);

CREATE TABLE participants (
  participant_id    TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES runs (run_id),
  provider          TEXT NOT NULL,
  origin            TEXT NOT NULL,
  native_session_id TEXT,
  connection_id     TEXT,
  capabilities_json TEXT NOT NULL,
  account_scope     TEXT,
  workspace         TEXT,
  liveness          TEXT NOT NULL,
  token_hash        TEXT NOT NULL UNIQUE,
  state_version     INTEGER NOT NULL,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  UNIQUE (run_id, provider)
);

CREATE TABLE tasks (
  task_id             TEXT PRIMARY KEY,
  run_id              TEXT NOT NULL REFERENCES runs (run_id),
  parent_id           TEXT REFERENCES tasks (task_id),
  description         TEXT NOT NULL,
  deliverables_json   TEXT NOT NULL,
  acceptance_ids_json TEXT NOT NULL,
  depends_on_json     TEXT NOT NULL,
  required            INTEGER NOT NULL,
  revision            INTEGER NOT NULL,
  owner               TEXT REFERENCES participants (participant_id),
  state               TEXT NOT NULL,
  blocked_reason      TEXT,
  next_action         TEXT,
  attempts            INTEGER NOT NULL,
  proposed_by         TEXT NOT NULL,
  state_version       INTEGER NOT NULL,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);
CREATE INDEX tasks_by_run ON tasks (run_id, state);

CREATE TABLE messages (
  message_id         TEXT PRIMARY KEY,
  run_id             TEXT NOT NULL REFERENCES runs (run_id),
  seq                INTEGER NOT NULL,
  sender             TEXT NOT NULL,
  recipient          TEXT REFERENCES participants (participant_id),
  kind               TEXT NOT NULL,
  task_id            TEXT REFERENCES tasks (task_id),
  reply_to           TEXT REFERENCES messages (message_id),
  correlation_id     TEXT,
  causation_id       TEXT,
  snapshot_ref       TEXT,
  body               TEXT NOT NULL,
  artifact_refs_json TEXT NOT NULL,
  state              TEXT NOT NULL,
  expires_at         TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (run_id, seq)
);
CREATE INDEX messages_by_recipient ON messages (recipient, seq);

CREATE TABLE inbox_cursors (
  participant_id TEXT PRIMARY KEY REFERENCES participants (participant_id),
  acked_seq      INTEGER NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE actions (
  action_id              TEXT PRIMARY KEY,
  run_id                 TEXT NOT NULL REFERENCES runs (run_id),
  type                   TEXT NOT NULL,
  task_id                TEXT REFERENCES tasks (task_id),
  task_revision          INTEGER,
  participant_id         TEXT REFERENCES participants (participant_id),
  input_digest           TEXT NOT NULL,
  provider_invocation_id TEXT,
  state                  TEXT NOT NULL,
  result_json            TEXT,
  reconciliation         TEXT,
  state_version          INTEGER NOT NULL,
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL
);
CREATE INDEX actions_by_state ON actions (state);

CREATE TABLE reservations (
  reservation_id TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES runs (run_id),
  action_id      TEXT REFERENCES actions (action_id),
  provider       TEXT NOT NULL,
  pool           TEXT NOT NULL,
  metric         TEXT NOT NULL,
  quantity_json  TEXT NOT NULL,
  category       TEXT NOT NULL,
  state          TEXT NOT NULL,
  expires_at     TEXT,
  actual_json    TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE outbox (
  outbox_id     TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES runs (run_id),
  action_id     TEXT NOT NULL UNIQUE REFERENCES actions (action_id),
  state         TEXT NOT NULL,
  claimed_by    TEXT,
  fencing_token INTEGER,
  attempts      INTEGER NOT NULL,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX outbox_by_state ON outbox (state);

CREATE TABLE approvals (
  approval_id      TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES runs (run_id),
  scope            TEXT NOT NULL,
  action           TEXT NOT NULL,
  constraints_json TEXT NOT NULL,
  policy_hash      TEXT NOT NULL,
  granted_by       TEXT NOT NULL,
  granted_at       TEXT NOT NULL,
  expires_at       TEXT,
  revoked_at       TEXT
);

CREATE TABLE leases (
  resource      TEXT PRIMARY KEY,
  owner         TEXT,
  fencing_token INTEGER NOT NULL,
  acquired_at   TEXT,
  expires_at    TEXT,
  released_at   TEXT
);

-- Operational (not replayed): cached responses for idempotent commands.
CREATE TABLE idempotency_keys (
  principal     TEXT NOT NULL,
  key           TEXT NOT NULL,
  command       TEXT NOT NULL,
  request_hash  TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (principal, key)
);
