from __future__ import annotations

from .transcript import Transcript


MAX_PROMPT_CHARS = 24000
MAX_SECTION_CHARS = 8000
MAX_VERIFICATION_CHARS = 3000


def build_prompt(
    task: str,
    transcript: Transcript,
    partner_last: str,
    workspace_state: str,
    role: str,
    verification: str = "",
) -> str:
    # Everything except the partner's latest message (shown in full below),
    # so an agent also sees its own previous turn without session chaining.
    earlier = transcript.messages[:-1] if len(transcript.messages) > 1 else []
    summary = _rolling_summary(earlier)
    verification_section = (
        f"\nVerification (run by Duet on the current workspace):\n{_clip(verification, MAX_VERIFICATION_CHARS)}\n"
        if verification
        else ""
    )
    prompt = f"""You are participating in Duet, a sequential two-agent coding session.

Role:
{role}

Original task:
{task}

Control protocol:
- End your reply with exactly one control token on its own final line.
- [[HANDOFF]]: your turn is complete and the partner should continue.
- [[DONE]]: the task is complete. Duet runs the configured checks itself; a
  [[DONE]] is not accepted while those checks fail.
- Do not wait for interactive approval; make concrete file edits when your role calls for it.

Partner's latest message:
{_clip(partner_last or "(none yet)", MAX_SECTION_CHARS)}

Rolling summary of earlier turns:
{summary}
{verification_section}
Current workspace state:
{_clip(workspace_state, MAX_SECTION_CHARS)}

Now take exactly one useful turn. Keep the response concise, mention changed files and test results when relevant, and finish with one control token on its own line.
"""
    return _clip(prompt, MAX_PROMPT_CHARS)


def default_roles(start: str, agent_names: list[str]) -> dict[str, str]:
    """Task-agnostic roles: the first speaker implements, everyone else reviews."""
    roles = {}
    for name in agent_names:
        if name == start:
            roles[name] = (
                "You are the Implementer. Edit source files to implement the requested behavior, "
                "run the relevant tests, then hand off for review."
            )
        else:
            roles[name] = (
                "You are the Reviewer. Review your partner's changes against the task, add or improve tests "
                "where coverage is missing, fix or report concrete defects, and run the relevant tests."
            )
    return roles


def demo_roles(start: str, agent_names: list[str]) -> dict[str, str]:
    """Roles for the seeded roman-numeral demo only."""
    roles = default_roles(start, agent_names)
    for name in agent_names:
        if name != start:
            roles[name] = (
                "You are the Verifier. Add edge-case tests to test_roman.py and review the implementation "
                "for bugs, reporting issues back."
            )
    return roles


def format_verification(result, rejected_done: bool = False) -> str:
    """Render a VerificationResult for the next prompt."""
    if result is None:
        return ""
    lines = [f"Status: {result.status}"]
    if rejected_done:
        lines.append("The last [[DONE]] was NOT accepted because these checks failed. Fix the failures first.")
    output = (result.output or "").strip()
    if output:
        tail = output[-MAX_VERIFICATION_CHARS:]
        lines.append("Output (tail):")
        lines.append(tail)
    return "\n".join(lines)


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
