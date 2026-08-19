"""Safety contracts for the lightweight rsync helper."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
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


def test_sync_validates_both_roots_and_excludes_runtime_data_from_transfer() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert SENTINEL.read_text(encoding="utf-8") == "xiaoqing-sync-root-v1\n"
    assert 'mode="dry-run"' in source
    assert 'remote_root="$($SSH_BIN' in source
    assert 'test -f "$target/$sentinel_name"' in source
    assert 'test "$(cat -- "$target/$sentinel_name")" = "$sentinel_value"' in source
    assert "remote directory must be a safe non-root absolute path" in source
    for protected in (
        "/.git/***",
        "/config/config.json",
        "/config/secrets.json",
        "/plugins/minecraft/config.json",
        "/logs/***",
        "/test_reports/runs/***",
        "/data/***",
        "/plugins/*/data/***",
        "/plugins/*/cache/***",
        "/plugins/*/backups/***",
        "/plugins/*/exports/***",
    ):
        assert f"--filter='- {protected}'" in source
    assert "--filter='P " not in source
    assert not any(line.strip().startswith("--delete-excluded") for line in source.splitlines())


def test_sync_target_is_kept_in_ignored_local_script_instead_of_passed_as_environment() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'readonly LOCAL_CONFIG_FILE="$SCRIPT_DIR/sync_to_remote.local.sh"' in source
    assert 'source "$LOCAL_CONFIG_FILE"' in source
    assert 'REMOTE_HOST="production-host"' in source
    assert 'REMOTE_DIR="/absolute/path/to/XiaoQing"' in source
    assert "scripts/sync_to_remote.local.sh" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "XIAOQING_SYNC_HOST" not in source
    assert "XIAOQING_SYNC_DIR" not in source


def test_sync_supports_local_only_remote_file_preservation() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "PRESERVE_REMOTE_FILES=()" in source
    assert "readonly -a PRESERVE_REMOTE_FILES" in source
    assert 'for preserved_file in "${PRESERVE_REMOTE_FILES[@]}"' in source
    assert 'rsync_args+=(--filter="- /$preserved_file")' in source
    assert "preserved remote file is missing or unsafe" in source
    assert "preserved file must be a safe repository-relative file path" in source


def test_sync_treats_the_arxiv_runtime_model_as_a_required_release_asset() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'readonly ARXIV_MODEL_DIR="plugins/arxiv_filter/best_model"' in source
    for artifact in (
        "config.json",
        "model.safetensors",
        "tokenizer.json",
        "tokenizer_config.json",
        "training_config.json",
    ):
        assert f'"$ARXIV_MODEL_DIR/{artifact}"' in source
    assert '--include="/$ARXIV_MODEL_DIR/***"' in source
    assert "--exclude='/plugins/arxiv_filter/best_model*'" not in source
    assert "required release file must be a non-empty regular file" in source
    assert "remote release file is not a non-empty regular file" in source
    assert '[[ -f "$local_file" && ! -L "$local_file" && -s "$local_file" ]]' in source
    assert 'test -L "$required_file"' in source
    assert "--checksum" in source


def test_sync_verifies_required_files_by_sha256_after_apply() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "sha256sum" in source
    assert "shasum -a 256" in source
    assert "openssl dgst -sha256" in source
    assert 'required_checksums+=("$required_file" "$checksum")' in source
    assert '"$remote_root" "${required_checksums[@]}"' in source
    assert "remote release checksum mismatch" in source
    assert "SHA-256 checks passed" in source


def test_sync_reuses_gitignore_without_dropping_its_include_exceptions() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '--exclude-from="$REPO_DIR/.gitignore"' in source
    assert "--include='/.env.example'" in source
    assert "--include='/.env.*.example'" in source


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
