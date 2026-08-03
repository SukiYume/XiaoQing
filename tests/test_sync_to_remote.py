"""Safety contracts for the lightweight rsync helper."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_to_remote.sh"
SCRIPT_COMMAND = SCRIPT.relative_to(ROOT).as_posix()
SENTINEL = ROOT / ".xiaoqing-sync-root"


def _bash() -> str:
    if os.name == "nt":
        git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    executable = shutil.which("bash")
    if executable is None:
        pytest.skip("bash is unavailable")
    return executable


def test_sync_script_has_valid_bash_syntax_and_help() -> None:
    bash = _bash()
    syntax = subprocess.run(
        [bash, "-n", SCRIPT_COMMAND],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        cwd=ROOT,
    )
    help_result = subprocess.run(
        [bash, SCRIPT_COMMAND, "--help"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        cwd=ROOT,
    )

    assert syntax.returncode == 0, syntax.stderr
    assert help_result.returncode == 0, help_result.stderr
    assert "--apply --confirm-delete" in help_result.stdout


def test_sync_defaults_to_preview_and_requires_explicit_delete_confirmation() -> None:
    bash = _bash()
    result = subprocess.run(
        [bash, SCRIPT_COMMAND, "--apply"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        cwd=ROOT,
    )

    assert result.returncode != 0
    assert "--apply requires --confirm-delete" in result.stderr


def test_sync_validates_both_roots_and_protects_runtime_data() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert SENTINEL.read_text(encoding="utf-8") == "xiaoqing-sync-root-v1\n"
    assert 'mode="dry-run"' in source
    assert 'remote_root="$($SSH_BIN' in source
    assert 'test -f "$target/$2"' in source
    assert 'test "$(cat -- "$target/$2")" = "$3"' in source
    assert "remote directory must be a safe non-root absolute path" in source
    for protected in (
        "/config/config.json",
        "/config/secrets.json",
        "/logs/***",
        "/test_reports/runs/***",
        "/plugins/*/data/***",
        "/plugins/*/cache/***",
    ):
        assert f"--filter='P {protected}'" in source


def test_sync_has_no_release_staging_dependencies() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for removed in (
        "deploy/",
        "release/",
        "requirements/",
        "build_deploy_stage",
        "scan_workspace_secrets",
        "install_locked_dependencies",
    ):
        assert removed not in source
