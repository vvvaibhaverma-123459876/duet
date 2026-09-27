"""A scripted stand-in for a model, used by the emulators in `peer` mode.

It reads the prompt DUET built for a managed turn, connects to the DUET MCP
server named in the provider's MCP config (exactly as the real CLI would),
and acts through the tools. State that must survive between turns (Claude
starts a new process per turn) lives in FAKE_BRAIN_STATE.

Policy:
- writer, first turn: ask the peer a question and end the turn;
- reviewer, first question: ask a clarifying question back before answering;
- any answer to the writer's question: claim, implement mul(), submit;
- review request: read the read-only snapshot; approve only a correct mul();
- blocker from the controller: claim, fix, submit."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

MUL = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
MESSAGE_RE = re.compile(r"^\[seq (\d+)\] (\w+) from (\w+) \(message_id (msg_\w+)(?:, snapshot (snap_\w+))?\):\n", re.M)


def parse_messages(prompt: str) -> list[dict]:
    out = []
    matches = list(MESSAGE_RE.finditer(prompt))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(prompt)
        out.append({"seq": int(match[1]), "kind": match[2], "from": match[3], "id": match[4], "snapshot": match[5], "body": prompt[match.end():end].strip()})
    return out


class State:
    def __init__(self, role: str) -> None:
        root = Path(os.environ.get("FAKE_BRAIN_STATE", "/tmp"))
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{role}.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data))


async def _act(prompt: str, server: dict) -> str:
    from mcp import Client, StdioServerParameters

    writer = "Role: writer" in prompt
    role = "writer" if writer else "reviewer"
    state = State(role)
    messages = parse_messages(prompt)
    params = StdioServerParameters(command=server["command"], args=list(server.get("args", [])), env=dict(server.get("env") or {}))
    done: list[str] = []
    async with Client(params, mode="legacy") as tools:

        async def call(name, **args):
            result = await tools.call_tool(name, args)
            if result.is_error:
                raise RuntimeError(result.content[0].text)
            return result.structured_content

        async def implement(note: str) -> None:
            claimed = await call("duet_claim")
            Path(claimed["workspace"], "calc.py").write_text(MUL)
            submitted = await call("duet_submit", summary=note)
            done.append(f"submitted {submitted['snapshot_id']}")

        if os.environ.get("FAKE_BRAIN_MODE") == "plan":
            return await _plan_mode(call, implement, writer, prompt, messages, state, done)
        if writer and "No messages yet" in prompt:
            asked = await call("duet_send", kind="QUESTION", body="Should mul(a, b) live in calc.py next to add()?")
            state.data["my_question"] = asked["message_id"]
            state.save()
            return "Asked my peer where mul() belongs; waiting for the answer."
        for m in messages:
            if m["kind"] == "QUESTION":
                if not writer and not state.data.get("asked_back"):
                    back = await call("duet_send", kind="QUESTION", body="Before I answer: do callers ever pass floats?")
                    state.data.update(asked_back=back["message_id"], pending=m["id"])
                    state.save()
                    done.append("asked back")
                else:
                    await call("duet_send", kind="ANSWER", body="Only integers today.", reply_to=m["id"])
                    done.append("answered")
            elif m["kind"] == "ANSWER":
                if writer and m["body"] and state.data.get("my_question") and not state.data.get("implemented"):
                    await implement("added mul() as agreed with my peer")
                    state.data["implemented"] = True
                    state.save()
                elif not writer and state.data.get("pending"):
                    await call("duet_send", kind="ANSWER", body="Yes, put it in calc.py; integers only.", reply_to=state.data.pop("pending"))
                    state.save()
                    done.append("answered the original question")
            elif m["kind"] == "REVIEW_REQUEST":
                path = next(line.split(": ", 1)[1] for line in m["body"].splitlines() if line.startswith("Read-only copy"))
                code = Path(path, "calc.py").read_text()
                if "return a * b" in code:
                    review = {"disposition": "approve"}
                else:
                    review = {"disposition": "changes_requested", "findings": [{"severity": "blocking", "summary": "mul does not multiply", "location": "calc.py"}]}
                await call("duet_send", kind="REVIEW_RESULT", body=f"Reviewed {m['snapshot']}.", reply_to=m["id"], review=review)
                done.append(f"reviewed: {review['disposition']}")
            elif m["kind"] == "BLOCKER" and writer:
                await implement("fixed after feedback")
    return "; ".join(done) or "nothing to do"


def act(prompt: str, server: dict | None) -> str:
    if not server:
        return "no DUET tools configured"
    return asyncio.run(_act(prompt, server))


async def _plan_mode(call, implement, writer, prompt, messages, state, done) -> str:
    """D06: the writer proposes a plan that gives the reviewer an
    investigation task, accepts the reviewer's result, then implements."""
    if writer and "No messages yet" in prompt:
        plan = await call("duet_propose_plan", rationale="codex investigates callers while I prepare the change", tasks=[
            {"key": "callers", "description": "Find all callers of add() and whether any pass floats", "kind": "investigate"},
        ])
        state.data["plan"] = plan["plan_id"]
        state.save()
        return f"proposed {plan['plan_id']}"
    for m in messages:
        if m["kind"] == "PLAN_PROPOSAL" and not writer:
            plan_id = re.search(r"Plan (pln_\w+)", m["body"])[1]
            await call("duet_decide_plan", plan_id=plan_id, decision="accept", reason="sensible split")
            done.append(f"accepted {plan_id}")
        elif m["kind"] == "TASK_PROPOSAL" and not writer:
            task_id = re.search(r"(tsk_\w+)", m["body"])[1]
            await call("duet_claim", task_id=task_id)
            await call("duet_complete_task", task_id=task_id, summary="add() has no float callers", artifact="callers: none outside calc.py")
            done.append(f"completed {task_id}")
        elif m["kind"] == "REVIEW_REQUEST" and writer and not m["snapshot"]:
            task_id = re.search(r"Task (tsk_\w+)", m["body"])[1]
            await call("duet_decide_task", task_id=task_id, decision="accept", reason="thanks, that settles it")
            await implement("added mul() after codex's investigation")
            done.append(f"accepted {task_id} and submitted")
        elif m["kind"] == "REVIEW_REQUEST" and not writer:
            path = next(line.split(": ", 1)[1] for line in m["body"].splitlines() if line.startswith("Read-only copy"))
            ok = "return a * b" in Path(path, "calc.py").read_text()
            review = {"disposition": "approve"} if ok else {"disposition": "changes_requested", "findings": [{"severity": "blocking", "summary": "mul does not multiply", "location": "calc.py"}]}
            await call("duet_send", kind="REVIEW_RESULT", body="reviewed", reply_to=m["id"], review=review)
            done.append(f"reviewed: {review['disposition']}")
    return "; ".join(done) or "nothing to do yet"
