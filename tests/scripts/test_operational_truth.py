"""Operational scripts and health metadata must reflect the real repository."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
CLEAN_SCRIPT = ROOT / "scripts" / "clean_pycache.sh"


def _bash() -> str | None:
    if os.name == "nt":
        git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    return shutil.which("bash")


def _copy_cleaner(repository: Path) -> Path:
    scripts_directory = repository / "scripts"
    scripts_directory.mkdir(exist_ok=True)
    target = scripts_directory / CLEAN_SCRIPT.name
    shutil.copyfile(CLEAN_SCRIPT, target)
    target.chmod(0o755)
    return target


def test_clean_pycache_is_anchored_to_script_repository(tmp_path: Path) -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("POSIX bash is unavailable")

    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    (repository / "core").mkdir(parents=True)
    outside.mkdir()
    (repository / ".xiaoqing-sync-root").write_text(
        "xiaoqing-sync-root-v1\n",
        encoding="utf-8",
    )
    (repository / "pyproject.toml").write_text(
        "[project]\nname='xiaoqing'\n",
        encoding="utf-8",
    )
    (repository / "main.py").write_text("", encoding="utf-8")
    cleaner = _copy_cleaner(repository)
    repository_cache = repository / "pkg" / "__pycache__"
    repository_cache.mkdir(parents=True)
    (repository_cache / "module.pyc").write_bytes(b"cache")
    protected_markers = []
    for protected_root in (
        ".local_archive",
        ".venv",
        "venv",
        "data",
        "logs",
        "plugins/demo/data",
        "plugins/demo/cache",
    ):
        protected_cache = repository / protected_root / "snapshot" / "__pycache__"
        protected_cache.mkdir(parents=True)
        protected_marker = protected_cache / "keep.pyc"
        protected_marker.write_bytes(b"protected")
        protected_markers.append(protected_marker)
    report_cache = repository / "test_reports" / "runs" / "run-1" / "__pycache__"
    report_cache.mkdir(parents=True)
    (report_cache / "generated.pyc").write_bytes(b"cache")
    outside_cache = outside / "__pycache__"
    outside_cache.mkdir()
    outside_marker = outside_cache / "keep.pyc"
    outside_marker.write_bytes(b"outside")

    result = subprocess.run(
        [bash, str(cleaner)],
        cwd=outside,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert not repository_cache.exists()
    assert not report_cache.exists()
    assert all(marker.read_bytes() == b"protected" for marker in protected_markers)
    assert outside_marker.read_bytes() == b"outside"


def test_clean_pycache_refuses_a_directory_without_all_sentinels(tmp_path: Path) -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("POSIX bash is unavailable")

    fake_repository = tmp_path / "not-a-repository"
    outside = tmp_path / "outside"
    fake_repository.mkdir()
    outside.mkdir()
    cleaner = _copy_cleaner(fake_repository)
    marker = outside / "keep.pyc"
    marker.write_bytes(b"outside")

    result = subprocess.run(
        [bash, str(cleaner)],
        cwd=outside,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )

    assert result.returncode == 2
    assert "repository sentinels are missing" in result.stderr
    assert marker.read_bytes() == b"outside"


def test_python_functions_contain_no_misplaced_string_expressions() -> None:
    failures: list[str] = []
    for source_root in (ROOT / "core", ROOT / "plugins", ROOT / "scripts", ROOT / "tests"):
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_bytes(), filename=str(path))
            allowed_docstrings: set[int] = set()
            for owner in ast.walk(tree):
                if (
                    not isinstance(
                        owner,
                        (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                    )
                    or not owner.body
                ):
                    continue
                first = owner.body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    allowed_docstrings.add(id(first))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and id(node) not in allowed_docstrings
                ):
                    failures.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert failures == []


def test_message_flow_documents_the_strict_bounded_queue_contract() -> None:
    documentation = (ROOT / "docs" / "08-message-flow.md").read_text(encoding="utf-8")

    assert "`inbound_ws_max_workers` 配置范围为 `1..128`" in documentation
    assert "`ws_queue_size` 配置范围为 `1..10000`" in documentation
    assert "总接纳容量为 worker 与 backlog 之和" in documentation
    assert "有界调度器" in documentation
    assert "inbound_queue_maxsize" not in documentation
    assert "0 表示无限" not in documentation
    assert "0 means unlimited" not in documentation.lower()
