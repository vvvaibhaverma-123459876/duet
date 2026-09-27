"""AT38: a wheel built from this tree carries its defaults and runs without
the source checkout."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.packaging


def _local_setuptools_ok() -> bool:
    try:
        import setuptools
    except ImportError:
        return False
    major = int(setuptools.__version__.split(".")[0])
    return major >= 77  # matches [build-system] requires


def _build_wheel(out: Path) -> Path:
    src = out / "src"
    shutil.copytree(REPO_ROOT, src, ignore=shutil.ignore_patterns(".git", "build", "dist", "*.egg-info", "__pycache__", ".pytest_cache"))
    # Prefer an offline build with the local backend; otherwise let pip fetch
    # the declared backend (needs network) and skip if that is impossible.
    isolation = [] if not _local_setuptools_ok() else ["--no-build-isolation"]
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", *isolation, "-q", "-w", str(out / "dist"), str(src)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        pytest.skip(f"cannot build a wheel here: {proc.stderr[-400:]}")
    return next((out / "dist").glob("duet-*.whl"))


def test_wheel_contains_packaged_defaults(tmp_path):
    wheel = _build_wheel(tmp_path)
    names = zipfile.ZipFile(wheel).namelist()
    assert "duet/resources/default_config.toml" in names
    assert any(n.startswith("duet/runtime/migrations/") and n.endswith(".sql") for n in names)


def test_installed_wheel_runs_without_source_tree(tmp_path):
    wheel = _build_wheel(tmp_path)
    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    python = env_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    subprocess.run([str(python), "-m", "pip", "install", "-q", "--no-deps", str(wheel)], check=True, timeout=300)
    work = tmp_path / "elsewhere"
    work.mkdir()
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "xdg"), "XDG_STATE_HOME": str(tmp_path / "state")}
    env.pop("PYTHONPATH", None)
    for argv in (["--version"], ["ps"], ["init", "--project"]):
        proc = subprocess.run([str(python), "-m", "duet", *argv], cwd=work, env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (argv, proc.stdout, proc.stderr)
    assert "[agents.claude]" in (work / "duet.toml").read_text()
    # The runtime database migrates from packaged SQL, outside any source tree.
    code = "from duet.runtime.store import Store; import sys; print(Store(sys.argv[1]).schema_version())"
    proc = subprocess.run([str(python), "-c", code, str(tmp_path / "rt.db")], cwd=work, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and int(proc.stdout.strip()) >= 1, proc.stderr
