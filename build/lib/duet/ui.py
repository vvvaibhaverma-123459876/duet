from __future__ import annotations

import os
import sys


class UI:
    def __init__(self, no_color: bool = False, stream=None) -> None:
        self.stream = stream or sys.stdout
        self.color = self.stream.isatty() and not no_color and not os.environ.get("NO_COLOR") and not os.environ.get("DUET_NO_COLOR")

    def c(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def banner(self, version: str, workspace: str, agents: dict[str, bool], solo: str | None = None) -> None:
        status = "  ".join(f"{name.title()} {'✓' if ok else '✗'}" for name, ok in agents.items())
        print(self.c(f"Duet {version}", "1;36"), file=self.stream)
        print(f"workspace: {workspace}", file=self.stream)
        print(status, file=self.stream)
        if solo:
            print(self.c(f"{solo.title()} only — solo mode; install and authenticate the other agent to enable collaboration.", "33"), file=self.stream)

    def turn(self, text: str) -> None:
        print(text, file=self.stream, flush=True)

    def status(self, workspace: str, turn: int, max_turns: int, next_agent: str) -> str:
        return f"{workspace} · turn {turn}/{max_turns} · next: {next_agent}"


def truncate_message(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated; use /transcript for full log]..."
