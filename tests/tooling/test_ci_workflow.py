# 验证 CI 环境、依赖安装和检查命令与项目契约一致。
"""Keep CI and dependency installation aligned with the simplified repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import node_esm
from tests.helpers.paths import REPOSITORY_ROOT

ROOT     = REPOSITORY_ROOT
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def _requirement_names() -> set[str]:
    names: set[str] = set()
    for raw_line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split(";", 1)[0].split("[", 1)[0]
        for separator in ("<", ">", "=", "!", "~"):
            name = name.split(separator, 1)[0]
        names.add(name.casefold())
    return names


def test_ci_uses_the_single_root_requirements_file() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "cache-dependency-path: requirements.txt" in workflow
    assert "python -m pip install -r requirements.txt" in workflow
    assert "python -m ruff check ." in workflow
    assert (
        "python -m pytest -q -n auto --cov=core --cov=plugins --cov-report=term-missing" in workflow
    )
    assert "requirements/" not in workflow
    assert "install_locked_dependencies" not in workflow
    assert not (ROOT / ".github" / "workflows" / "dependency-locks.yml").exists()


def test_ci_declares_node_windows_and_strict_skip_dependencies() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "actions/setup-node@v4" in workflow
    assert 'node-version: "22"' in workflow
    assert "runs-on: windows-latest" in workflow
    assert 'XIAOQING_CI: "1"' in workflow
    assert 'XIAOQING_REQUIRE_NODE: "1"' in workflow
    assert "tests/plugins/codex/test_codex_process_tree.py" in workflow
    assert "tests/scripts/test_run_bot_monitor_script.py" in workflow
    assert "tests/scripts/test_rotating_log_runner.py" in workflow
    assert "tests/scripts/test_bot_monitor_powershell.py" in workflow


def test_node_contract_fails_in_ci_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XIAOQING_REQUIRE_NODE", "1")
    monkeypatch.setattr(node_esm.shutil, "which", lambda _name: None)

    with pytest.raises(pytest.fail.Exception, match="Node.js is required"):
        node_esm.assert_node_esm_contract("export {};", "", cwd=tmp_path)


def test_root_requirements_is_direct_and_covers_runtime_surfaces() -> None:
    content = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = _requirement_names()

    assert not any(
        line.lstrip().startswith(("-r ", "--requirement ", "-c ", "--constraint "))
        for line in content.splitlines()
    )
    assert {
        "aiohttp",
        "websockets",
        "apscheduler",
        "pydantic",
        "fastapi",
        "starlette",
        "uvicorn",
        "pyjwt",
        "paramiko",
        "astropy",
        "pytest",
        "ruff",
    } <= names


def test_dockerfile_uses_the_same_requirements_file() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY requirements.txt ./requirements.txt" in dockerfile
    assert "python -m pip install --no-cache-dir -r requirements.txt" in dockerfile
    assert "requirements/" not in dockerfile
    assert "install_locked_dependencies" not in dockerfile


def test_only_runtime_and_maintenance_utility_scripts_remain() -> None:
    scripts = {
        path.name
        for path in (ROOT / "scripts").iterdir()
        if path.is_file() and path.name != "sync_to_remote.local.sh"
    }

    assert scripts == {
        "arxiv_inference_cli.py",
        "clean_pycache.sh",
        "format_code.py",
        "run_command_matrix.py",
        "run_core_pressure.py",
        "run_full_uat.py",
        "run_full_uat.sh",
        "run_xiaoqing_chat_quality.py",
        "run-bot.vbs",
        "stop-bot.vbs",
        "run-bot-monitor.ps1",
        "run_process_with_rotating_logs.py",
        "sync_to_remote.sh",
    }
    for removed in ("deploy", "release", "requirements"):
        assert not (ROOT / removed).exists()
    assert not (ROOT / "xiaoqing_path_security.py").exists()


def test_only_root_dependency_manifest_and_current_docs_remain() -> None:
    requirement_files = {
        path.relative_to(ROOT).as_posix() for path in ROOT.rglob("requirements.txt")
    }

    assert requirement_files == {"requirements.txt"}
    assert not (ROOT / "docs" / "plans").exists()
    assert not (ROOT / "docs" / "superpowers").exists()
