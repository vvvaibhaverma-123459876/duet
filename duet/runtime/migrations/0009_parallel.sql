-- D11: isolated worktrees for independent code tasks. Each belongs to one
-- task and one owner (fenced by its own lease); its accepted result is
-- integrated into the run's workspace by the writer, the integration owner.

CREATE TABLE task_workspaces (
  task_id        TEXT PRIMARY KEY REFERENCES tasks (task_id),
  run_id         TEXT NOT NULL REFERENCES runs (run_id),
  owner          TEXT NOT NULL,
  path           TEXT NOT NULL,
  branch         TEXT NOT NULL,
  base_sha       TEXT NOT NULL,
  state          TEXT NOT NULL,
  patch_ref      TEXT,
  snapshot_id    TEXT,
  detail         TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX task_workspaces_by_run ON task_workspaces (run_id);
