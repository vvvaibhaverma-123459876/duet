-- D03: snapshots, verification evidence, reviews, findings, checkpoints.
-- Evidence is keyed to an exact snapshot and acceptance-contract hash, so a
-- change to the code or to the contract makes earlier evidence irrelevant.

CREATE TABLE snapshots (
  snapshot_id   TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES runs (run_id),
  tree_hash     TEXT NOT NULL,
  base_sha      TEXT,
  author        TEXT,
  file_count    INTEGER NOT NULL,
  changed_json  TEXT NOT NULL,
  excluded_json TEXT NOT NULL,
  manifest_ref  TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX snapshots_by_run ON snapshots (run_id, created_at);

CREATE TABLE evidence (
  evidence_id     TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES runs (run_id),
  check_id        TEXT NOT NULL,
  snapshot_id     TEXT NOT NULL REFERENCES snapshots (snapshot_id),
  acceptance_hash TEXT NOT NULL,
  argv_json       TEXT NOT NULL,
  cwd             TEXT NOT NULL,
  env_fingerprint TEXT NOT NULL,
  status          TEXT NOT NULL,
  exit_code       INTEGER,
  started_at      TEXT NOT NULL,
  ended_at        TEXT NOT NULL,
  output_hash     TEXT NOT NULL,
  artifact_ref    TEXT,
  producer        TEXT NOT NULL,
  trust           TEXT NOT NULL,
  detail          TEXT NOT NULL
);
CREATE INDEX evidence_by_snapshot ON evidence (run_id, snapshot_id, check_id);

CREATE TABLE reviews (
  review_id         TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES runs (run_id),
  reviewer          TEXT NOT NULL REFERENCES participants (participant_id),
  reviewer_provider TEXT NOT NULL,
  snapshot_id       TEXT NOT NULL REFERENCES snapshots (snapshot_id),
  acceptance_hash   TEXT NOT NULL,
  scope_json        TEXT NOT NULL,
  disposition       TEXT NOT NULL,
  summary           TEXT NOT NULL,
  created_at        TEXT NOT NULL
);
CREATE INDEX reviews_by_snapshot ON reviews (run_id, snapshot_id);

CREATE TABLE findings (
  finding_id   TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL REFERENCES runs (run_id),
  review_id    TEXT NOT NULL REFERENCES reviews (review_id),
  severity     TEXT NOT NULL,
  summary      TEXT NOT NULL,
  location     TEXT,
  status       TEXT NOT NULL,
  resolution   TEXT,
  resolved_by  TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX findings_by_run ON findings (run_id, status);

CREATE TABLE checkpoints (
  checkpoint_id   TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES runs (run_id),
  snapshot_id     TEXT NOT NULL REFERENCES snapshots (snapshot_id),
  acceptance_hash TEXT NOT NULL,
  report_hash     TEXT NOT NULL,
  artifact_ref    TEXT NOT NULL,
  created_at      TEXT NOT NULL
);
