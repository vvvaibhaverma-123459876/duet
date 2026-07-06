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
    path: Path | None = None
    cwd: str = ""


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
                path=path,
            )
        )
    return sessions


def list_codex_sessions(codex_home: Path | None = None, limit: int = 10) -> list[SessionInfo]:
    """Codex CLI records every session as a rollout JSONL under
    ~/.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<session-id>.jsonl."""
    home = codex_home if codex_home is not None else Path.home() / ".codex"
    root = home / "sessions"
    if not root.is_dir():
        return []
    files = sorted(root.glob("*/*/*/rollout-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    sessions = []
    for path in files[:limit]:
        stat = path.stat()
        meta = _codex_meta(path)
        sessions.append(
            SessionInfo(
                session_id=meta.get("id") or _codex_id_from_name(path),
                modified=datetime.fromtimestamp(stat.st_mtime),
                size_bytes=stat.st_size,
                preview=_squash(_first_codex_user_text(path)),
                path=path,
                cwd=str(meta.get("cwd", "")),
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


def format_codex_sessions(sessions: list[SessionInfo]) -> str:
    if not sessions:
        return "No Codex sessions found."
    lines = ["Codex sessions (newest first):"]
    for info in sessions:
        stamp = info.modified.strftime("%Y-%m-%d %H:%M")
        cwd = info.cwd or "?"
        preview = info.preview or "(no user text found)"
        lines.append(f"  {info.session_id}  {stamp}  cwd={cwd}  {preview}")
    lines.append("\nPeek read-only with: duet peek codex [<session-id>]")
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


def peek_session(path: Path, agent: str, limit: int = 30, max_chars: int = 300) -> list[str]:
    """Read-only tail of a session transcript: recent messages and tool calls,
    newest last. Never locks or mutates the session file, so it is safe to run
    against a session that is still live in another terminal."""
    parser = _codex_event_from_line if agent == "codex" else _claude_event_from_line
    events: list[str] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                event = parser(line, max_chars)
                if event:
                    events.append(event)
    except OSError as exc:
        return [f"(cannot read {path}: {exc})"]
    return events[-limit:]


def format_peek(info: SessionInfo, agent: str, events: list[str]) -> str:
    stamp = info.modified.strftime("%Y-%m-%d %H:%M:%S")
    header = f"{agent} session {info.session_id} (last modified {stamp}"
    if info.cwd:
        header += f", cwd={info.cwd}"
    header += f", {info.size_bytes} bytes) — read-only peek, newest last:"
    body = "\n".join(events) if events else "(no readable events)"
    return f"{header}\n\n{body}"


def _codex_event_from_line(line: str, max_chars: int) -> str:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return ""
    payload = entry.get("payload") if isinstance(entry, dict) else None
    if not isinstance(payload, dict):
        return ""
    kind = payload.get("type")
    if kind == "message":
        role = payload.get("role", "?")
        texts = [
            block.get("text", "")
            for block in payload.get("content", [])
            if isinstance(block, dict) and block.get("type") in ("input_text", "output_text")
        ]
        text = _squash(" ".join(texts), max_chars)
        # Harness-injected context blocks start with '<'; skip the noise.
        if not text or text.startswith("<"):
            return ""
        return f"[{role}] {text}"
    if kind == "function_call":
        args = _squash(str(payload.get("arguments", "")), max_chars)
        return f"[tool {payload.get('name', '?')}] {args}"
    return ""


def _claude_event_from_line(line: str, max_chars: int) -> str:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if not isinstance(entry, dict) or entry.get("type") not in ("user", "assistant"):
        return ""
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    role = message.get("role", entry["type"])
    content = message.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") == "tool_use":
                parts.append(f"[tool {block.get('name', '?')}] {_squash(json.dumps(block.get('input', {})), 120)}")
    text = _squash(" ".join(part for part in parts if part), max_chars)
    if not text or text.startswith("<system-reminder>"):
        return ""
    return f"[{role}] {text}"


def _codex_meta(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            first = handle.readline()
        entry = json.loads(first)
    except (OSError, json.JSONDecodeError):
        return {}
    payload = entry.get("payload", entry) if isinstance(entry, dict) else {}
    return payload if isinstance(payload, dict) else {}


def _codex_id_from_name(path: Path) -> str:
    match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", path.stem)
    return match.group(1) if match else path.stem


def _first_codex_user_text(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as handle:
            for _, line in zip(range(PREVIEW_SCAN_LINES), handle):
                event = _codex_event_from_line(line, PREVIEW_CHARS)
                if event.startswith("[user]"):
                    return event[len("[user]") :].strip()
    except OSError:
        pass
    return ""


def _squash(text: str, limit: int = PREVIEW_CHARS) -> str:
    squashed = " ".join(text.split())
    return squashed[:limit] + ("…" if len(squashed) > limit else "")


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
