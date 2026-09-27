"""Process identity and participant credentials.

A PID alone does not identify a process: PIDs are reused and reset on reboot.
A ProcessIdentity pairs the PID with the kernel boot id and the process start
time, so an old lease can be attributed to a dead owner reliably.

Participant tokens are random secrets returned once at registration and
stored only as SHA-256 hashes. They are passed to MCP proxies through the
environment or a 0600 file, never on the command line."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TOKEN_BYTES = 32


def new_token() -> str:
    return "duet_pt_" + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash)


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
            return out.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            pass
    return "unknown"


def process_start(pid: int) -> str | None:
    """Kernel start time of `pid`, or None if the process does not exist."""
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            raw = stat_path.read_text(encoding="utf-8")
        except OSError:
            return None
        # comm (field 2) may contain spaces and parentheses; split after the last ')'.
        fields = raw[raw.rfind(")") + 2 :].split()
        # fields[0] is state (field 3); starttime is field 22 -> index 19.
        if len(fields) > 19:
            if fields[0] == "Z":
                return None  # zombie: exited
            return fields[19]
        return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    return text or None


@dataclass(frozen=True)
class ProcessIdentity:
    host: str
    boot: str
    pid: int
    start: str

    def __str__(self) -> str:
        return f"{self.host}|{self.boot}|{self.pid}|{self.start}"

    @classmethod
    def parse(cls, text: str) -> "ProcessIdentity | None":
        parts = (text or "").split("|")
        if len(parts) != 4:
            return None
        try:
            return cls(parts[0], parts[1], int(parts[2]), parts[3])
        except ValueError:
            return None

    @classmethod
    def current(cls) -> "ProcessIdentity":
        pid = os.getpid()
        return cls(socket.gethostname(), boot_id(), pid, process_start(pid) or "unknown")

    def is_alive(self) -> bool:
        """True only if this exact process (same host, boot, pid and start
        time) still runs. Unknown hosts are treated as alive: another machine's
        process cannot be judged dead from here."""
        if self.host != socket.gethostname():
            return True
        if self.boot != boot_id():
            return False
        start = process_start(self.pid)
        return start is not None and start == self.start


def owner_is_dead(owner: str | None) -> bool:
    if not owner:
        return True
    identity = ProcessIdentity.parse(owner)
    if identity is None:
        return False  # not a process identity (e.g. a principal id): cannot judge
    return not identity.is_alive()
