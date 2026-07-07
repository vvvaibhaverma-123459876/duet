from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

SESSION_ID_PLACEHOLDER = "{session_id}"
WORKSPACE_PLACEHOLDER = "{workspace}"


class AgentError(RuntimeError):
    """Raised when an agent CLI cannot produce a usable response."""


class QuotaError(AgentError):
    """The agent CLI failed in a way that looks like an exhausted usage limit
    or rate limit, so retrying immediately would only burn more quota."""


@dataclass(frozen=True)
class AgentResult:
    text: str
    exit_code: int
    duration_s: float
    raw_stdout: str
    raw_stderr: str
    session_id: str | None = None


class Agent(Protocol):
    name: str
    display_name: str

    def send(self, prompt: str, workspace: Path) -> AgentResult:
        ...


@dataclass
class CLIAgent:
    name: str
    display_name: str
    command: list[str]
    prompt_via: str
    workspace_flag: str
    output_format: str
    timeout_seconds: int
    result_json_path: str = ""
    session_json_path: str = ""
    model: str = ""
    stdin_sentinel: str = "-"
    resume_command: list[str] = field(default_factory=list)
    session_id: str = ""
    chain_sessions: bool = False
    last_session_id: str = ""

    def build_command(self, prompt: str, workspace: Path) -> tuple[list[str], str | None]:
        template = self.resume_command if self.session_id and self.resume_command else self.command
        # Some CLIs only accept the workspace flag before a subcommand
        # (codex exec resume), so templates may place it via {workspace}
        # instead of relying on the appended workspace_flag.
        inline_workspace = any(WORKSPACE_PLACEHOLDER in part for part in template)
        cmd = [
            part.replace(SESSION_ID_PLACEHOLDER, self.session_id).replace(WORKSPACE_PLACEHOLDER, str(workspace))
            for part in template
        ]
        if self.model:
            cmd.extend(["-m", self.model])
        if self.workspace_flag and not inline_workspace:
            cmd.extend([self.workspace_flag, str(workspace)])

        stdin_data: str | None = None
        if self.prompt_via == "stdin":
            stdin_data = prompt
        elif self.prompt_via == "stdin-sentinel":
            cmd.append(self.stdin_sentinel)
            stdin_data = prompt
        elif self.prompt_via == "arg":
            cmd.append(prompt)
        else:
            raise AgentError(f"{self.name}: unsupported prompt_via={self.prompt_via!r}")
        return cmd, stdin_data

    def send(self, prompt: str, workspace: Path) -> AgentResult:
        cmd, stdin_data = self.build_command(prompt, workspace)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if stdin_data is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=workspace,
                start_new_session=(os.name != "nt"),
            )
            try:
                stdout, stderr = proc.communicate(stdin_data, timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                _kill_process_tree(proc)
                stdout, stderr = proc.communicate()
                raise AgentError(
                    f"{self.name}: timed out after {self.timeout_seconds}s; killed child process tree. "
                    f"Command: {_redacted_cmd(cmd)}. If this was Codex approval blocking, set non-interactive approval/sandbox flags in duet.toml."
                ) from exc
            except BaseException:
                # Interrupt (SIGINT/SIGTERM) or any abnormal exit: never leave the
                # agent CLI running in its own process group. Kill, then re-raise.
                _kill_process_tree(proc)
                raise
        except FileNotFoundError as exc:
            raise AgentError(f"{self.name}: executable not found: {cmd[0]}. Command: {_redacted_cmd(cmd)}") from exc
        duration = time.monotonic() - started
        text, session_id, warning = self._parse_output(stdout)
        if proc.returncode != 0:
            detail = text or _tail(stderr) or _tail(stdout)
            if _looks_like_quota(detail + stderr):
                raise QuotaError(
                    f"{self.name}: exited {proc.returncode}. quota/rate-limit suspected. "
                    f"Command: {_redacted_cmd(cmd)}. Output: {detail}"
                )
            raise AgentError(f"{self.name}: exited {proc.returncode}. Command: {_redacted_cmd(cmd)}. Output: {detail}")
        if not text.strip():
            raise AgentError(f"{self.name}: produced empty output. Command: {_redacted_cmd(cmd)}. stderr: {_tail(stderr)}")
        if warning:
            text = f"{text.strip()}\n\n[Duet warning: {warning}]"
        if session_id:
            # Always remember the newest id so a later `duet resume` can
            # re-attach this conversation even from a non-chained run.
            self.last_session_id = session_id
            if self.chain_sessions:
                # Resumed sessions get a fresh id on each turn; adopt it so the
                # next send() continues the same conversation, not a stale fork.
                self.session_id = session_id
        return AgentResult(
            text=text.strip(),
            exit_code=proc.returncode,
            duration_s=duration,
            raw_stdout=stdout,
            raw_stderr=stderr,
            session_id=session_id,
        )

    def _parse_output(self, stdout: str) -> tuple[str, str | None, str]:
        if self.output_format == "text":
            return stdout.strip(), None, ""
        if self.output_format == "text-last-line":
            section = _extract_cli_speaker_section(stdout, self.name)
            if section:
                return section, None, ""
            if "[[DONE]]" in stdout or "[[HANDOFF]]" in stdout:
                return stdout.strip(), None, ""
            lines = [line.strip() for line in stdout.splitlines() if line.strip()]
            return (lines[-1] if lines else ""), None, ""
        if self.output_format == "json":
            try:
                payload = json.loads(stdout)
                text = _get_dotted(payload, self.result_json_path)
                session_id = _get_dotted(payload, self.session_json_path) if self.session_json_path else None
                if not isinstance(text, str):
                    raise AgentError(f"{self.name}: JSON path {self.result_json_path!r} did not resolve to text")
                return text, str(session_id) if session_id is not None else None, ""
            except (json.JSONDecodeError, AgentError) as exc:
                return stdout.strip(), None, f"expected JSON output but fell back to raw stdout: {exc}"
        raise AgentError(f"{self.name}: unsupported output_format={self.output_format!r}")


def _get_dotted(payload: object, path: str) -> object:
    cur = payload
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise AgentError(f"missing JSON path {path!r}")
    return cur


def _extract_cli_speaker_section(stdout: str, speaker: str) -> str:
    lines = stdout.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip().lower() == speaker.lower()]
    if not starts:
        return ""
    start = starts[-1] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].strip().lower() == "tokens used":
            end = index
            break
    section = "\n".join(lines[start:end]).strip()
    if section:
        return section
    return ""


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _redacted_cmd(cmd: list[str]) -> str:
    redacted = []
    secret_next = False
    for part in cmd:
        if secret_next:
            redacted.append("<redacted>")
            secret_next = False
            continue
        redacted.append(part)
        if part.lower() in {"--api-key", "--token", "--auth-token"}:
            secret_next = True
    return " ".join(redacted)


def _tail(text: str, limit: int = 1200) -> str:
    stripped = text.strip()
    return stripped[-limit:] if len(stripped) > limit else stripped


def _looks_like_quota(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in ("rate limit", "quota", "usage limit", "billing"))
