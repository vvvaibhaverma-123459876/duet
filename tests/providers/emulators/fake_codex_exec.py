#!/usr/bin/env python3
"""Emulator for `codex exec --json` (event names from codex-rs exec_events.rs;
the network-denied failure path replays the recorded 0.157.1 output).
Mode via FAKE_CODEX_EXEC_MODE: success | network_denied | failed | hang"""
import json
import os
import sys
import time
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "provider_protocols" / "codex" / "0.157.1" / "exec-json-network-denied.jsonl"
MODE = os.environ.get("FAKE_CODEX_EXEC_MODE", "success")
LOG = os.environ.get("FAKE_CODEX_EXEC_LOG")
args = sys.argv[1:]
if LOG:
    with open(LOG, "a") as handle:
        handle.write(json.dumps({"argv": args}) + "\n")
prompt = sys.stdin.read()


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if MODE == "network_denied":
    for line in FIXTURE.read_text().splitlines():
        sys.stdout.write(line + "\n")
    sys.stdout.flush()
    sys.exit(1)

thread = args[args.index("resume") + 1] if "resume" in args else "01a0e15b-0000-7000-8000-000000000001"
emit({"type": "thread.started", "thread_id": thread})
emit({"type": "turn.started"})
if MODE == "hang":
    time.sleep(60)
if MODE == "failed":
    emit({"type": "turn.failed", "error": {"message": "Your credit balance is too low"}})
    sys.exit(1)
emit({"type": "error", "message": "Reconnecting... 1/5 (stream disconnected before completion)"})
emit({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "thinking"}})
emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": f"exec reply to: {prompt[:20]}"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 500, "cached_input_tokens": 100, "cache_write_input_tokens": 0, "output_tokens": 40, "reasoning_output_tokens": 12}})
