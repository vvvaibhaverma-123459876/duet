#!/usr/bin/env python3
"""Emulator for the Claude Code CLI 2.1.283 (help text from the recorded
fixture; stream-json event shapes from the Claude Code docs for programmatic
use and cost tracking). Mode via FAKE_CLAUDE_MODE:
  success | auth | rate_limit | billing | model | flood | malformed | hang |
  crash_zero | budget | no_result | denied |
  env (the result text lists the names of the environment variables it got)"""
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

HELP = Path(__file__).resolve().parents[2] / "fixtures" / "provider_protocols" / "claude" / "2.1.283-help.txt"
MODE = os.environ.get("FAKE_CLAUDE_MODE", "success")
LOG = os.environ.get("FAKE_CLAUDE_LOG")
args = sys.argv[1:]

if args == ["--version"]:
    print("2.1.283 (Claude Code)")
    sys.exit(0)
if args == ["--help"]:
    print(HELP.read_text(), end="")
    sys.exit(0)


def opt(name):
    return args[args.index(name) + 1] if name in args else None


if LOG:
    with open(LOG, "a") as handle:
        handle.write(json.dumps({"argv": args}) + "\n")

prompt = sys.stdin.read()
resume = opt("--resume")
session = resume if resume and "--fork-session" not in args else (opt("--session-id") or str(uuid.uuid4()))
model = opt("--model") or "claude-opus-5-5"


def emit(obj):
    obj.setdefault("session_id", session)
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


emit({"type": "system", "subtype": "init", "model": model, "permissionMode": opt("--permission-mode") or "default", "tools": ["Read", "Edit"], "capabilities": ["interrupt_receipt_v1"], "cwd": os.getcwd()})

if MODE == "hang":
    def on_int(signum, frame):
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "interrupted", "total_cost_usd": 0, "usage": {}})
        sys.exit(130)
    signal.signal(signal.SIGINT, on_int)
    time.sleep(60)
    sys.exit(0)

if MODE == "flood":
    for i in range(200000):
        emit({"type": "assistant", "message": {"id": f"m{i}", "content": [{"type": "text", "text": "x" * 500}]}})
    sys.exit(0)

if MODE == "malformed":
    sys.stdout.write("warning: something printed to stdout\n")
    sys.stdout.flush()

if MODE in ("auth", "rate_limit", "billing", "model"):
    category = {"auth": "authentication_failed", "rate_limit": "rate_limit", "billing": "billing_error", "model": "model_not_found"}[MODE]
    emit({"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 1, "retry_delay_ms": 0, "error_status": 429 if MODE == "rate_limit" else 401, "error": category, "uuid": "u"})
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": f"API error: {category}", "total_cost_usd": 0.0, "usage": {}})
    sys.exit(1)

if MODE == "no_result":
    sys.exit(1)

if MODE == "env":
    names = "env: " + json.dumps(sorted(os.environ))
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "duration_ms": 1, "result": names, "total_cost_usd": 0.01, "usage": {}})
    sys.exit(0)

if MODE == "peer":
    # Act through the DUET MCP server named in --mcp-config (see agent_brain.py).
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import agent_brain

    config = json.loads(opt("--mcp-config") or "{}")
    text = agent_brain.act(prompt, (config.get("mcpServers") or {}).get("duet"))
    emit({"type": "assistant", "parent_tool_use_id": None, "message": {"id": "msg_1", "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 10, "output_tokens": 1}}})
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "duration_ms": 12, "result": text,
          "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 1}})
    sys.exit(0)

emit({"type": "assistant", "parent_tool_use_id": None, "message": {"id": "msg_1", "content": [{"type": "text", "text": f"Working on: {prompt[:30]}"}], "usage": {"input_tokens": 10, "output_tokens": 1}}})

if MODE == "crash_zero":
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "crashed", "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0}, "modelUsage": {model: {"inputTokens": 0, "outputTokens": 0, "costUSD": 0}}})
    sys.exit(1)

if MODE == "budget":
    emit({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": "", "total_cost_usd": 0.51, "usage": {}})
    sys.exit(1)

# Cost: a resumed session reports the whole session's spend (cumulative).
previous = float(os.environ.get("FAKE_CLAUDE_PRIOR_COST", "0")) if resume else 0.0
cost = round(previous + 0.25, 6)
result = {
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "duration_ms": 12,
    "result": f"done: {prompt[:30]} [[HANDOFF]]", "total_cost_usd": cost,
    "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 0},
    "modelUsage": {model: {"inputTokens": 1200, "outputTokens": 300, "cacheReadInputTokens": 50, "cacheCreationInputTokens": 0, "costUSD": cost}},
}
if MODE == "denied":
    result["permission_denials"] = [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "curl example.com"}}]
emit(result)
