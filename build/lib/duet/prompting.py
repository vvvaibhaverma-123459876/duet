from __future__ import annotations

from .transcript import Transcript


MAX_PROMPT_CHARS = 24000
MAX_SECTION_CHARS = 8000


def build_prompt(
    task: str,
    transcript: Transcript,
    partner_last: str,
    workspace_state: str,
    role: str,
) -> str:
    earlier = transcript.messages[:-2] if len(transcript.messages) > 2 else []
    summary = _rolling_summary(earlier)
    prompt = f"""You are participating in Duet, a sequential two-agent coding session.

Role:
{role}

Original task:
{task}

Control protocol:
- Emit [[HANDOFF]] when your turn is complete and the partner should continue.
- Emit [[DONE]] only when the task is complete and verified by the available checks.
- Do not wait for interactive approval; make concrete file edits when your role calls for it.

Partner's latest message:
{_clip(partner_last or "(none yet)", MAX_SECTION_CHARS)}

Rolling summary of earlier turns:
{summary}

Current workspace state:
{_clip(workspace_state, MAX_SECTION_CHARS)}

Now take exactly one useful turn. Keep the response concise, mention changed files and test results when relevant, and include one control token.
"""
    return _clip(prompt, MAX_PROMPT_CHARS)


def _rolling_summary(messages) -> str:
    if not messages:
        return "(no earlier turns)"
    chunks = []
    for msg in messages[-6:]:
        one_line = " ".join(msg.content.split())
        chunks.append(f"- Turn {msg.turn_index} {msg.agent}: {_clip(one_line, 500)}")
    return "\n".join(chunks)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 80].rstrip() + "\n...[truncated by Duet prompt budget]..."
