"""Regression tests for reproducible dependency installation."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ("3.10", "3.11", "3.12", "3.13")


def _lock(version: str) -> Path:
    return ROOT / "requirements" / f"python-{version}.lock"


def test_every_supported_python_has_a_hashed_lock() -> None:
    for version in VERSIONS:
        content = _lock(version).read_text(encoding="utf-8")
        requirement_lines = [
            line
            for line in content.splitlines()
            if line and not line[0].isspace() and not line.startswith("#")
        ]

        assert len(requirement_lines) >= 60
        assert all("==" in line for line in requirement_lines)
        assert content.count("--hash=sha256:") >= len(requirement_lines)
        assert "python scripts/compile_locks.py" in content


def test_ci_and_docker_enforce_hash_checking() -> None:
    ci = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "requirements/python-${{ matrix.python-version }}.lock" in ci
    assert 'pip install --require-hashes -r "$LOCK_FILE"' in ci
    assert "pip install --no-cache-dir --require-hashes" in dockerfile
    assert "requirements/python-3.13.lock" in dockerfile


def test_lock_refresh_workflow_uses_pinned_compiler_and_pull_request() -> None:
    workflow = (ROOT / ".github" / "workflows" / "dependency-locks.yml").read_text(
        encoding="utf-8"
    )

    assert "python -m pip install uv==0.11.28" in workflow
    assert "python scripts/compile_locks.py --upgrade" in workflow
    assert re.search(r"gh pr create\b", workflow)
