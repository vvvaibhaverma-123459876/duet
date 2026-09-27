"""Native-session helpers run by the user's own client (D12): the Claude
Code Stop hook and the status-line wrapper.

Both find the DUET session the same way the MCP proxy does: the proxy is a
child of the client and stores its session under the client's process
identity; a hook or status-line command is also started by the client
(sometimes through a shell), so walking up a few ancestors finds it. Both
must never break the user's client: any error means "do nothing"."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

from ..runtime.identity import ProcessIdentity
from ..runtime.service import ServiceClient, ServicePaths, service_running

MAX_ANCESTORS = 6


def _parent(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return int(handle.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def find_session_token(paths: ServicePaths, start_pid: int | None = None) -> str | None:
    """The DUET token of the native session this process runs under, if any."""
    pid = start_pid or os.getppid()
    for _ in range(MAX_ANCESTORS):
        if not pid or pid <= 1:
            return None
        host = str(ProcessIdentity.of(pid))
        path = paths.root / "sessions" / (hashlib.sha256(host.encode()).hexdigest()[:32] + ".json")
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))["token"]
            except (OSError, ValueError, KeyError):
                return None
        pid = _parent(pid)
    return None


def claude_stop_hook(stdin_text: str, paths: ServicePaths) -> dict | None:
    """Claude Code Stop hook. If the peer is waiting on this session (an
    unanswered question or review request), block the stop once with the
    reason, so the session answers before it ends its turn. Returns the hook
    output, or None to let the stop proceed."""
    try:
        payload = json.loads(stdin_text or "{}")
    except ValueError:
        return None
    if payload.get("stop_hook_active"):
        return None  # Claude is already continuing because of a stop hook: never loop
    if not service_running(paths):
        return None
    token = find_session_token(paths)
    if token is None:
        return None
    try:
        pending = ServiceClient(paths, token=token).call("inbox", rpc_timeout=5.0, limit=1)["pending_requests"]
    except Exception:
        return None
    if not pending:
        return None
    kinds = ", ".join(sorted({p["kind"] for p in pending}))
    return {
        "decision": "block",
        "reason": (f"DUET: your peer is waiting on {len(pending)} request(s) from you ({kinds}). Read them with duet_inbox and "
                   "answer each with duet_send(reply_to=...) before you stop; or call duet_wait to see what changed."),
    }


def hook_main(kind: str, paths: ServicePaths) -> int:
    if kind != "claude-stop":
        print(f"duet hook: unknown hook {kind!r}", file=sys.stderr)
        return 0
    try:
        out = claude_stop_hook(sys.stdin.read(), paths)
    except Exception:  # a hook must never break the client
        out = None
    if out is not None:
        print(json.dumps(out))
    return 0


def statusline_main(paths: ServicePaths, original: str | None) -> int:
    """Run the user's original status-line command unchanged, and record
    Claude's quota windows from the same input (changes only)."""
    from .claude_statusline import run_status_line

    raw = sys.stdin.buffer.read()

    def sink(observations) -> None:
        readings = []
        for o in observations:
            if getattr(o, "dimension", None) is None or o.dimension.value != "quota" or o.value is None:
                continue
            readings.append({"window": o.key, "used_percent": o.value, "resets_at_ms": o.resets_at_ms, "observed_at_ms": o.observed_at_ms,
                             "source": "claude.statusline"})
        if not readings:
            return
        from ..runtime.api import Runtime
        from ..runtime.store import Store
        from ..usage.reservations import ReservationBook

        book = ReservationBook(Runtime(Store(paths.db)))
        known = {(g["window"], str(g["used_percent"])) for g in book.status()["gauges"] if g["provider"] == "claude"}
        fresh = [r for r in readings if (r["window"], str(r["used_percent"])) not in known]
        if fresh:
            book.observe_quota("claude", fresh)

    result = run_status_line(original, raw, sink=sink, observed_at_ms=int(time.time() * 1000), timeout_s=5.0)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


def original_statusline(paths: ServicePaths) -> str | None:
    """The user's own status-line command, as saved by the installer."""
    try:
        manifest = json.loads((paths.root.parent / "integrations.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    saved = (manifest.get("items", {}).get("claude-statusline") or {}).get("original")
    return saved.get("command") if isinstance(saved, dict) else None


__all__ = ["claude_stop_hook", "find_session_token", "hook_main", "statusline_main", "original_statusline"]
