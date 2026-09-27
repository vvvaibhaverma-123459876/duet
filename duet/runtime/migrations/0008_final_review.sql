-- D10: baseline results of each required check on the run's base commit,
-- per contract version. A criterion is demonstrated by a check that did not
-- pass here and passes on the final snapshot, or by an explicit review.

CREATE TABLE baselines (
  baseline_id    TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES runs (run_id),
  check_id       TEXT NOT NULL,
  acceptance_hash TEXT NOT NULL,
  base_sha       TEXT NOT NULL,
  status         TEXT NOT NULL,
  exit_code      INTEGER,
  output_hash    TEXT,
  detail         TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX baselines_by_run ON baselines (run_id, acceptance_hash);
