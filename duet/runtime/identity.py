"""Process identity and participant credentials.

A PID alone does not identify a process: PIDs are reused and reset on reboot.
A ProcessIdentity pairs the PID with the kernel boot id and the process start
time, so an old lease can be attributed to a dead owner reliably.

Participant tokens are random secrets returned once at registration and
stored only as SHA-256 hashes. They are passed to MCP proxies through the
environment or a 0600 file, never on the command line.

Where /proc is unavailable (macOS), the boot id and process start times come
from `sysctl`/`ps`. The runtime store forbids waiting on a subprocess inside a
write transaction, so callers there use `is_alive(allow_subprocess=False)`,
which never spawns; the boot id is read once per process and cached."""
from __future__ import annotations

import functools
import hashlib
import hmac
import os
import re
import secrets
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import oscompat

TOKEN_BYTES = 32


def new_token() -> str:
    return "duet_pt_" + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash)


def _has_procfs() -> bool:
    """True when process information can be read from /proc without a subprocess."""
    return Path("/proc/self/stat").exists()


MAX_HOST = 40


def host_name() -> str:
    """This machine's name as stored in identities: the hostname, or a stable
    hash of it when it is long (macOS CI runners use 70-character names), so
    a serialised identity always fits the runtime's 128-character id limit."""
    name = socket.gethostname()
    if len(name) <= MAX_HOST:
        return name
    return "h-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def _darwin_boot(raw: str) -> str:
    """`sysctl -n kern.boottime` prints '{ sec = N, usec = M } <date>'; the
    seconds identify the boot compactly (and '|' never appears)."""
    match = re.search(r"sec\s*=\s*(\d+)", raw)
    return f"boottime:{match.group(1)}" if match else (raw.strip().replace("|", "/")[:40] or "unknown")


@functools.lru_cache(maxsize=1)
def boot_id() -> str:
    """The kernel boot id. It cannot change while this process runs, so it is
    computed once per process (on macOS that costs a `sysctl` subprocess)."""
    if oscompat.IS_WINDOWS:
        # Windows derives its boot time from the uptime, which jitters by a
        # second between processes, so it cannot identify a boot. Process
        # creation times are exact and never reused across boots, so the
        # start time alone distinguishes a reused pid.
        return "windows"
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
            return _darwin_boot(out)
        except (OSError, subprocess.SubprocessError):
            pass
    return "unknown"


def process_start(pid: int) -> str | None:
    """Kernel start time of `pid`, or None if the process does not exist.
    Spawns `ps` only where /proc is unavailable."""
    if pid <= 0:
        return None
    if oscompat.IS_WINDOWS:
        return oscompat.windows_process_start(pid)
    stat_path = Path(f"/proc/{pid}/stat")
    procfs = _has_procfs()
    if procfs and not stat_path.exists():
        return None  # /proc lists every process we could judge: this one is gone
    if procfs:
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
        return cls.of(os.getpid())

    @classmethod
    def of(cls, pid: int) -> "ProcessIdentity":
        """Identity of another local process (e.g. the agent CLI hosting an
        MCP server). The start time distinguishes a reused pid."""
        return cls(host_name(), boot_id(), pid, process_start(pid) or "unknown")

    def is_alive(self, allow_subprocess: bool = True) -> bool:
        """True only if this exact process (same host, boot, pid and start
        time) still runs. Unknown hosts are treated as alive: another machine's
        process cannot be judged dead from here.

        With `allow_subprocess=False` no subprocess is ever spawned (required
        inside a store transaction). Where /proc is available the answer is the
        same; elsewhere the check degrades conservatively: the process is dead
        only if its boot id (when already cached) differs or its pid does not
        exist at all; otherwise it is assumed alive."""
        if self.host != host_name():
            return True
        if not allow_subprocess and not _has_procfs() and not oscompat.IS_WINDOWS:
            if boot_id.cache_info().currsize and self.boot != boot_id():
                return False
            return _pid_exists(self.pid)
        if self.boot != boot_id():
            return False
        start = process_start(self.pid)
        return start is not None and start == self.start


def _pid_exists(pid: int) -> bool:
    return oscompat.pid_exists(pid)


def owner_is_dead(owner: str | None, *, allow_subprocess: bool = True) -> bool:
    if not owner:
        return True
    identity = ProcessIdentity.parse(owner)
    if identity is None:
        return False  # not a process identity (e.g. a principal id): cannot judge
    return not identity.is_alive(allow_subprocess=allow_subprocess)
