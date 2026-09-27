#!/usr/bin/env python3
"""Emulator for `codex app-server` (stdio, newline-delimited JSON-RPC).

initialize, model/list and account/read are answered with the *recorded*
responses from codex-cli 0.157.1 (fixtures/.../app-server-handshake-
unauthenticated.jsonl), including the unsolicited notifications and extra
fields the real server sends. Turn events follow the generated v2 schema.

Behaviour is selected with FAKE_CODEX_MODE:
  success | fail | hang | approval | malformed | die | ignore_interrupt |
  ratelimited (account/rateLimits/read succeeds) |
  early_fail (the turn fails with a usage-limit error *before* the turn/start
  response is sent) | env (the reply lists the server's environment names)"""
import json
import os
import sys
import time
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "provider_protocols" / "codex" / "0.157.1" / "app-server-handshake-unauthenticated.jsonl"
MODE = os.environ.get("FAKE_CODEX_MODE", "success")
LOG = os.environ.get("FAKE_CODEX_LOG")

recorded = {}
unsolicited = []
for line in FIXTURE.read_text().splitlines():
    record = json.loads(line)
    msg = record["msg"]
    if record["dir"] == "server->client":
        if "id" in msg:
            recorded[msg["id"]] = msg
        else:
            unsolicited.append(msg)
by_method = {}
for line in FIXTURE.read_text().splitlines():
    record = json.loads(line)
    if record["dir"] == "client->server" and "id" in record["msg"]:
        by_method[record["msg"]["method"]] = recorded.get(record["msg"]["id"])


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def log(obj):
    if LOG:
        with open(LOG, "a") as handle:
            handle.write(json.dumps(obj) + "\n")


threads = {}
turn_counter = [0]
pending_interrupt = {}


def usage(n):
    return {
        "last": {"inputTokens": 100 * n, "cachedInputTokens": 10, "cacheWriteInputTokens": 0, "outputTokens": 20, "reasoningOutputTokens": 5, "totalTokens": 100 * n + 20},
        "total": {"inputTokens": 300 * n, "cachedInputTokens": 30, "cacheWriteInputTokens": 0, "outputTokens": 60, "reasoningOutputTokens": 15, "totalTokens": 300 * n + 60},
        "modelContextWindow": 400000,
    }


def complete_turn(thread_id, turn_id, status="completed", error=None):
    turn = {"id": turn_id, "items": [], "status": status, "error": error}
    send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}, "emittedAtMs": int(time.time() * 1000)})


def handle(msg):
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    log({"recv": msg})
    if rid is None:
        return  # notifications (initialized) and responses to our requests
    if method == "initialize":
        send({"id": rid, "result": by_method["initialize"]["result"]})
        for note in unsolicited:
            send(note)
    elif method == "account/read":
        send({"id": rid, "result": by_method["account/read"]["result"]})
    elif method == "account/rateLimits/read":
        if MODE == "ratelimited":
            send({"id": rid, "result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 42, "windowDurationMins": 300, "resetsAt": 1790500000}, "secondary": {"usedPercent": 7, "windowDurationMins": 10080, "resetsAt": 1791000000}}}})
        else:
            send({"id": rid, "error": by_method["account/rateLimits/read"]["error"]})
    elif method == "model/list":
        data = by_method["model/list"]["result"]["data"]
        cursor = params.get("cursor")
        if cursor is None:
            send({"id": rid, "result": {"data": data[:3], "nextCursor": "page2"}})
        else:
            send({"id": rid, "result": {"data": data[3:], "nextCursor": None}})
    elif method in ("thread/start", "thread/resume", "thread/fork"):
        if method == "thread/start":
            thread_id = f"thr-{len(threads) + 1}"
        elif method == "thread/fork":
            thread_id = params["threadId"] + "-fork"
        else:
            thread_id = params["threadId"]
        threads[thread_id] = params
        send({"id": rid, "result": {
            "approvalPolicy": params.get("approvalPolicy", "on-request"), "approvalsReviewer": "user", "cwd": params.get("cwd", "/"),
            "model": "gpt-6-astra", "modelProvider": "openai", "reasoningEffort": "low",
            "sandbox": {"type": params.get("sandbox", "read-only")},
            "thread": {"id": thread_id, "cliVersion": "0.157.1", "createdAt": 0, "cwd": params.get("cwd", "/"), "ephemeral": False,
                       "modelProvider": "openai", "preview": "", "projectId": None, "sessionId": thread_id, "source": "appServer",
                       "status": {"type": "idle"}, "turns": [], "updatedAt": 0},
        }})
    elif method == "turn/start":
        turn_counter[0] += 1
        n = turn_counter[0]
        thread_id = params["threadId"]
        turn_id = f"turn-{n}"
        if MODE == "early_fail":
            # A fast failure: the turn's notifications precede the response.
            send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id, "items": [], "status": "inProgress"}}})
            send({"method": "error", "params": {"threadId": thread_id, "turnId": turn_id, "willRetry": False, "error": {"message": "You've hit your usage limit."}}})
            complete_turn(thread_id, turn_id, "failed", {"message": "You've hit your usage limit."})
            send({"id": rid, "result": {"turn": {"id": turn_id, "items": [], "status": "inProgress"}}})
            return
        send({"id": rid, "result": {"turn": {"id": turn_id, "items": [], "status": "inProgress"}}})
        send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id, "items": [], "status": "inProgress"}}})
        if MODE == "malformed":
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
        if MODE == "approval":
            send({"id": 9001, "method": "item/commandExecution/requestApproval", "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "i1", "command": "rm -rf /"}})
            return  # completion happens when the client answers
        if MODE == "die":
            os._exit(3)
        if MODE in ("hang", "ignore_interrupt"):
            pending_interrupt[turn_id] = thread_id
            return
        text = params["input"][0]["text"]
        if MODE == "peer":
            # Act through the DUET MCP server from the thread's config (see agent_brain.py).
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import agent_brain

            server = ((threads.get(thread_id) or {}).get("config") or {}).get("mcp_servers", {}).get("duet")
            reply = agent_brain.act(text, server)
            send({"method": "thread/tokenUsage/updated", "params": {"threadId": thread_id, "turnId": turn_id, "tokenUsage": usage(n)}, "emittedAtMs": 1})
            send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id, "completedAtMs": 1, "item": {"type": "agentMessage", "id": "a1", "text": reply, "phase": None}}})
            complete_turn(thread_id, turn_id)
            return
        send({"method": "item/agentMessage/delta", "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "a1", "delta": "partial"}})
        send({"method": "thread/tokenUsage/updated", "params": {"threadId": thread_id, "turnId": turn_id, "tokenUsage": usage(n)}, "emittedAtMs": 1})
        send({"method": "account/rateLimits/updated", "params": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 43, "windowDurationMins": 300, "resetsAt": 1790500000}}}})
        if MODE == "fail":
            send({"method": "error", "params": {"threadId": thread_id, "turnId": turn_id, "willRetry": False, "error": {"message": "You've hit your usage limit."}}})
            complete_turn(thread_id, turn_id, "failed", {"message": "You've hit your usage limit."})
            return
        reply = f"echo: {text[:40]} model={params.get('model')} effort={params.get('effort')}"
        if MODE == "env":
            reply = "env: " + json.dumps(sorted(os.environ))
        send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id, "completedAtMs": 1, "item": {"type": "agentMessage", "id": "a1", "text": reply, "phase": None}}})
        complete_turn(thread_id, turn_id)
    elif method == "turn/interrupt":
        send({"id": rid, "result": {}})
        if MODE != "ignore_interrupt":
            thread_id = pending_interrupt.pop(params["turnId"], params["threadId"])
            complete_turn(thread_id, params["turnId"], "interrupted")
    else:
        send({"id": rid, "error": {"code": -32600, "message": f"Invalid request: unknown variant `{method}`"}})


for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    if "id" in msg and "method" not in msg:
        # Our answer to a server request (approval): record and finish the turn.
        log({"answer": msg})
        if MODE == "approval" and msg.get("id") == 9001:
            send({"method": "item/completed", "params": {"threadId": "thr-1", "turnId": "turn-1", "completedAtMs": 1, "item": {"type": "agentMessage", "id": "a1", "text": "could not run the command", "phase": None}}})
            complete_turn("thr-1", "turn-1")
        continue
    handle(msg)
