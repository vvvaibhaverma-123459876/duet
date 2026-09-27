"""Reversible native integration setup (D12): `duet integrations plan |
install | uninstall`.

What can be installed, each item separately:
- claude-mcp / codex-mcp: the DUET MCP server, through the clients' own
  documented commands (`claude mcp add --scope user`, `codex mcp add`), so
  DUET never rewrites their configuration files itself;
- claude-skill: the `duet-pair` skill under ~/.claude/skills (a new
  directory; an existing one is never overwritten);
- claude-statusline (opt-in): DUET's wrapper around the user's own
  status-line command, which it runs unchanged (the original is saved);
- claude-stop-hook (opt-in): a Stop hook that asks the session to answer
  its peer's pending requests before it stops.

Rules: `plan` changes nothing. `install` applies only with `--yes`, and
records exactly what it owns in a manifest (with the original values).
`uninstall` removes only owned items, and restores a saved status line
only if the current value is still DUET's (a user edit is left alone and
reported). Nothing enables permission bypasses, preview features
(Claude Channels) or global model settings. An item that already exists
without DUET's ownership is reported, never replaced."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ..runtime.paths import ensure_private_dir, state_dir

ITEMS = ("claude-mcp", "codex-mcp", "claude-skill", "claude-statusline", "claude-stop-hook")
DEFAULT_ITEMS = ("claude-mcp", "codex-mcp", "claude-skill")
MANIFEST_SCHEMA = "duet.integrations/1"


def duet_command() -> list[str]:
    exe = shutil.which("duet")
    return [exe] if exe else [sys.executable, "-m", "duet"]


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _bin(provider: str) -> str | None:
    return shutil.which(os.environ.get("DUET_CLAUDE_BIN" if provider == "claude" else "DUET_CODEX_BIN", provider))


@dataclass
class Change:
    item: str
    action: str  # add | skip | remove | restore | keep
    detail: str

    def to_dict(self) -> dict:
        return {"item": self.item, "action": self.action, "detail": self.detail}


class Installer:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else state_dir()
        self.manifest_path = self.root / "integrations.json"

    # -- manifest ----------------------------------------------------------------------

    def manifest(self) -> dict:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if data.get("schema") == MANIFEST_SCHEMA:
                return data
        except (OSError, ValueError):
            pass
        return {"schema": MANIFEST_SCHEMA, "items": {}}

    def _save(self, manifest: dict) -> None:
        ensure_private_dir(self.root)
        tmp = self.manifest_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
        os.replace(tmp, self.manifest_path)

    # -- settings.json -----------------------------------------------------------------

    @staticmethod
    def settings_path() -> Path:
        return claude_dir() / "settings.json"

    def _settings(self) -> dict:
        path = self.settings_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_settings(self, data: dict) -> None:
        path = self.settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_name(path.name + ".duet-backup")
            if not backup.exists():  # the first backup is the user's original
                shutil.copy2(path, backup)
        tmp = path.with_suffix(".duet-tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _statusline_command() -> str:
        return " ".join(duet_command() + ["statusline"])

    @staticmethod
    def _hook_command() -> str:
        return " ".join(duet_command() + ["hook", "claude-stop"])

    # -- plan --------------------------------------------------------------------------

    def _mcp_present(self, provider: str) -> bool | None:
        binary = _bin(provider)
        if binary is None:
            return None
        proc = subprocess.run([binary, "mcp", "get", "duet"], capture_output=True, text=True, timeout=30)
        return proc.returncode == 0

    def plan(self, items: tuple[str, ...] = DEFAULT_ITEMS) -> list[Change]:
        owned = self.manifest()["items"]
        changes = []
        for item in items:
            if item not in ITEMS:
                raise ValueError(f"unknown integration {item!r}; choose from {ITEMS}")
            if item in owned:
                changes.append(Change(item, "skip", "already installed by DUET"))
                continue
            if item in ("claude-mcp", "codex-mcp"):
                provider = item.split("-")[0]
                present = self._mcp_present(provider)
                if present is None:
                    changes.append(Change(item, "skip", f"{provider} CLI not found"))
                elif present:
                    changes.append(Change(item, "skip", f"{provider} already has an MCP server named duet that DUET did not install; left alone"))
                else:
                    scope = " --scope user" if provider == "claude" else ""
                    changes.append(Change(item, "add", f"{provider} mcp add{scope} duet -- {' '.join(duet_command())} mcp serve"))
            elif item == "claude-skill":
                target = claude_dir() / "skills" / "duet-pair"
                changes.append(Change(item, "skip", f"{target} exists; left alone") if target.exists() else Change(item, "add", f"create {target}/SKILL.md"))
            elif item == "claude-statusline":
                current = self._settings().get("statusLine")
                wrapped = (current or {}).get("command") if isinstance(current, dict) else None
                changes.append(Change(item, "add", f"statusLine.command -> {self._statusline_command()} (runs your current command {wrapped!r} unchanged)"))
            elif item == "claude-stop-hook":
                changes.append(Change(item, "add", f"hooks.Stop += {self._hook_command()} (blocks a stop once while your peer waits on you)"))
        return changes

    # -- install -----------------------------------------------------------------------

    def install(self, items: tuple[str, ...] = DEFAULT_ITEMS, *, yes: bool = False) -> list[Change]:
        changes = self.plan(items)
        if not yes:
            return changes
        manifest = self.manifest()
        for change in changes:
            if change.action != "add":
                continue
            item = change.item
            if item in ("claude-mcp", "codex-mcp"):
                provider = item.split("-")[0]
                scope = ["--scope", "user"] if provider == "claude" else []
                proc = subprocess.run([_bin(provider), "mcp", "add", *scope, "duet", "--", *duet_command(), "mcp", "serve"],
                                      capture_output=True, text=True, timeout=60)
                if proc.returncode != 0:
                    change.action, change.detail = "failed", (proc.stderr or proc.stdout).strip()[:500]
                    continue
                manifest["items"][item] = {"provider": provider, "scope": "user" if provider == "claude" else "default"}
            elif item == "claude-skill":
                target = claude_dir() / "skills" / "duet-pair"
                target.mkdir(parents=True)
                text = resources.files("duet").joinpath("resources/skills/duet-pair/SKILL.md").read_text(encoding="utf-8")
                (target / "SKILL.md").write_text(text, encoding="utf-8")
                manifest["items"][item] = {"path": str(target), "content": text}
            elif item == "claude-statusline":
                settings = self._settings()
                original = settings.get("statusLine")
                settings["statusLine"] = {"type": "command", "command": self._statusline_command(),
                                          **({"padding": original["padding"]} if isinstance(original, dict) and "padding" in original else {})}
                self._write_settings(settings)
                manifest["items"][item] = {"original": original, "installed": settings["statusLine"]}
            elif item == "claude-stop-hook":
                settings = self._settings()
                entry = {"hooks": [{"type": "command", "command": self._hook_command(), "timeout": 10}]}
                settings.setdefault("hooks", {}).setdefault("Stop", []).append(entry)
                self._write_settings(settings)
                manifest["items"][item] = {"entry": entry}
            self._save(manifest)
        return changes

    # -- uninstall ---------------------------------------------------------------------

    def uninstall(self, *, yes: bool = False) -> list[Change]:
        manifest = self.manifest()
        changes = []
        for item, record in list(manifest["items"].items()):
            if item in ("claude-mcp", "codex-mcp"):
                provider = record["provider"]
                changes.append(Change(item, "remove", f"{provider} mcp remove duet"))
                if yes and _bin(provider):
                    scope = ["--scope", "user"] if provider == "claude" else []
                    subprocess.run([_bin(provider), "mcp", "remove", *scope, "duet"], capture_output=True, text=True, timeout=60)
            elif item == "claude-skill":
                target = Path(record["path"])
                skill = target / "SKILL.md"
                unchanged = skill.exists() and skill.read_text(encoding="utf-8") == record["content"] and [p.name for p in target.iterdir()] == ["SKILL.md"]
                changes.append(Change(item, "remove" if unchanged else "keep", str(target) if unchanged else f"{target} was changed after install; left alone"))
                if yes and unchanged:
                    shutil.rmtree(target)
            elif item == "claude-statusline":
                settings = self._settings()
                if settings.get("statusLine") == record["installed"]:
                    changes.append(Change(item, "restore", f"statusLine -> {record['original']!r}"))
                    if yes:
                        if record["original"] is None:
                            settings.pop("statusLine", None)
                        else:
                            settings["statusLine"] = record["original"]
                        self._write_settings(settings)
                else:
                    changes.append(Change(item, "keep", "statusLine was changed after install; left alone"))
            elif item == "claude-stop-hook":
                settings = self._settings()
                stops = settings.get("hooks", {}).get("Stop", [])
                kept = [e for e in stops if e != record["entry"]]
                changes.append(Change(item, "remove", "hooks.Stop: DUET's entry"))
                if yes and len(kept) != len(stops):
                    if kept:
                        settings["hooks"]["Stop"] = kept
                    else:
                        settings["hooks"].pop("Stop")
                        if not settings["hooks"]:
                            settings.pop("hooks")
                    self._write_settings(settings)
            if yes:
                manifest["items"].pop(item)
                self._save(manifest)
        return changes

    def status(self) -> dict:
        return {"manifest": str(self.manifest_path), "installed": sorted(self.manifest()["items"])}
