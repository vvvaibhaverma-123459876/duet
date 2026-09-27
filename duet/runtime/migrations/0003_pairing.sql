-- D05: invitations for a second native participant to join a run.
-- Only the hash of the invite code is stored; the code is shown once to the
-- initiating session, which passes it on through the user.

CREATE TABLE invites (
  invite_id   TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES runs (run_id),
  provider    TEXT NOT NULL,
  code_hash   TEXT NOT NULL UNIQUE,
  created_by  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  used_by     TEXT,
  used_at     TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX invites_by_run ON invites (run_id);
