-- D06: task kinds, shared plan proposals, task results with peer decisions,
-- substantive contribution records and loop interventions.

ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'code';

CREATE TABLE plans (
  plan_id               TEXT PRIMARY KEY,
  run_id                TEXT NOT NULL REFERENCES runs (run_id),
  proposer              TEXT NOT NULL,
  rationale             TEXT NOT NULL,
  tasks_json            TEXT NOT NULL,
  state                 TEXT NOT NULL,
  decided_by            TEXT,
  decision_reason       TEXT,
  created_task_ids_json TEXT NOT NULL,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);
CREATE INDEX plans_by_run ON plans (run_id, created_at);

CREATE TABLE task_results (
  result_id       TEXT PRIMARY KEY,
  task_id         TEXT NOT NULL REFERENCES tasks (task_id),
  run_id          TEXT NOT NULL REFERENCES runs (run_id),
  author          TEXT NOT NULL,
  summary         TEXT NOT NULL,
  artifact_ref    TEXT,
  snapshot_id     TEXT,
  decision        TEXT,
  decided_by      TEXT,
  decision_reason TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX task_results_by_task ON task_results (task_id, created_at);

-- Contribution ids are derived from (run, participant, kind, ref), so the
-- same contribution can never be counted twice.
CREATE TABLE contributions (
  contribution_id TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES runs (run_id),
  participant_id  TEXT NOT NULL,
  provider        TEXT NOT NULL,
  kind            TEXT NOT NULL,
  ref             TEXT NOT NULL,
  summary         TEXT NOT NULL,
  created_at      TEXT NOT NULL
);
CREATE INDEX contributions_by_run ON contributions (run_id, provider);

CREATE TABLE interventions (
  intervention_id TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES runs (run_id),
  status          TEXT NOT NULL,
  task_id         TEXT,
  reason          TEXT NOT NULL,
  evidence_json   TEXT NOT NULL,
  fingerprint     TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX interventions_by_run ON interventions (run_id, created_at);
