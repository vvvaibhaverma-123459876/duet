from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PREVIEW_CHARS = 100
PREVIEW_SCAN_LINES = 40


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    modified: datetime
    size_bytes: int
    preview: str


def claude_project_dir(repo: Path, claude_home: Path | None = None) -> Path:
    """Claude Code stores each project's sessions under ~/.claude/projects/<slug>,
    where the slug is the absolute repo path with non-alphanumerics dashed."""
    home = claude_home if claude_home is not None else Path.home() / ".claude"
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve()))
    return home / "projects" / slug


def list_claude_sessions(repo: Path, claude_home: Path | None = None, limit: int = 10) -> list[SessionInfo]:
    project = claude_project_dir(repo, claude_home)
    if not project.is_dir():
        return []
    files = sorted(project.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    sessions = []
    for path in files[:limit]:
        stat = path.stat()
        sessions.append(
            SessionInfo(
                session_id=path.stem,
                modified=datetime.fromtimestamp(stat.st_mtime),
                size_bytes=stat.st_size,
                preview=_first_user_text(path),
            )
        )
    return sessions


def format_sessions(repo: Path, sessions: list[SessionInfo]) -> str:
    if not sessions:
        return f"No Claude Code sessions found for {repo.resolve()}."
    lines = [f"Claude Code sessions for {repo.resolve()} (newest first):"]
    for info in sessions:
        stamp = info.modified.strftime("%Y-%m-%d %H:%M")
        preview = info.preview or "(no user text found)"
        lines.append(f"  {info.session_id}  {stamp}  {preview}")
    lines.append("\nAttach with: duet run --repo <repo> --attach claude=<session-id> \"<task>\"")
    return "\n".join(lines)


def _first_user_text(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as handle:
            for _, line in zip(range(PREVIEW_SCAN_LINES), handle):
                text = _user_text_from_line(line)
                if text:
                    squashed = " ".join(text.split())
                    return squashed[:PREVIEW_CHARS] + ("…" if len(squashed) > PREVIEW_CHARS else "")
    except OSError:
        pass
    return ""


def _user_text_from_line(line: str) -> str:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if not isinstance(entry, dict) or entry.get("type") != "user":
        return ""
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"].strip()
                # Skip harness-injected reminders; we want the human's words.
                if text and not text.startswith("<system-reminder>"):
                    return text
    return ""
