from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import scripts.check_ruff_changed as checker
from scripts.check_ruff_changed import AddedRange, parse_added_ranges

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_ruff_changed.py"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _run(repo: Path, base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "tests@example.invalid")
    _git(tmp_path, "config", "user.name", "XiaoQing Tests")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff.lint]\nselect = ["E", "F", "I"]\n',
        encoding="utf-8",
    )
    (tmp_path / "legacy.py").write_text(
        "def legacy():\n    return missing_legacy_name\n",
        encoding="utf-8",
    )
    _commit(tmp_path, "baseline")
    return tmp_path


def test_ruff_changed_script_accepts_an_empty_git_diff(git_repo: Path) -> None:
    result = _run(git_repo, "HEAD")

    assert result.returncode == 0, result.stderr
    assert "No committed Python changes to lint." in result.stdout


def test_old_diagnostic_does_not_block_a_clean_added_line(git_repo: Path) -> None:
    base = _git(git_repo, "rev-parse", "HEAD")
    with (git_repo / "legacy.py").open("a", encoding="utf-8") as handle:
        handle.write("\nCLEAN_VALUE = 1\n")
    _commit(git_repo, "clean addition")

    result = _run(git_repo, base)

    assert result.returncode == 0, result.stderr
    assert "No Ruff diagnostics intersect" in result.stdout


@pytest.mark.parametrize(
    ("line", "expected_code"),
    [
        ("BROKEN = missing_new_name\n", "F821"),
        ("import os\n", "F401"),
    ],
)
def test_new_fixable_and_unfixable_diagnostics_both_fail(
    git_repo: Path,
    line: str,
    expected_code: str,
) -> None:
    base = _git(git_repo, "rev-parse", "HEAD")
    with (git_repo / "legacy.py").open("a", encoding="utf-8") as handle:
        handle.write(line)
    _commit(git_repo, "bad addition")

    result = _run(git_repo, base)

    assert result.returncode == 1
    assert expected_code in result.stderr
    assert "legacy.py" in result.stderr


def test_pure_deletion_has_no_added_lines(git_repo: Path) -> None:
    base = _git(git_repo, "rev-parse", "HEAD")
    (git_repo / "legacy.py").unlink()
    _commit(git_repo, "delete old file")

    result = _run(git_repo, base)

    assert result.returncode == 0, result.stderr
    assert "No added Python lines" in result.stdout


def test_bad_new_python_file_fails(git_repo: Path) -> None:
    base = _git(git_repo, "rev-parse", "HEAD")
    (git_repo / "brand_new.py").write_text("VALUE = unknown_value\n", encoding="utf-8")
    _commit(git_repo, "new bad file")

    result = _run(git_repo, base)

    assert result.returncode == 1
    assert "brand_new.py" in result.stderr
    assert "F821" in result.stderr


def test_pure_rename_does_not_reclassify_legacy_debt(git_repo: Path) -> None:
    base = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "mv", "legacy.py", "renamed legacy.py")
    _commit(git_repo, "pure rename")

    result = _run(git_repo, base)

    assert result.returncode == 0, result.stderr
    assert "No added Python lines" in result.stdout


def test_rename_with_a_bad_added_line_still_fails(git_repo: Path) -> None:
    base = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "mv", "legacy.py", "renamed.py")
    with (git_repo / "renamed.py").open("a", encoding="utf-8") as handle:
        handle.write("NEW_VALUE = another_missing_name\n")
    _commit(git_repo, "rename and edit")

    result = _run(git_repo, base)

    assert result.returncode == 1
    assert "renamed.py" in result.stderr
    assert "F821" in result.stderr


def test_added_file_with_spaces_is_checked(git_repo: Path) -> None:
    base = _git(git_repo, "rev-parse", "HEAD")
    path = git_repo / "file with spaces.py"
    path.write_text(
        "\n".join(["VALUE = 1"] * 20) + "\nBROKEN = spaced_missing\n",
        encoding="utf-8",
    )
    _commit(git_repo, "space path")

    result = _run(git_repo, base)

    assert result.returncode == 1
    assert "file with spaces.py" in result.stderr
    assert "F821" in result.stderr


def test_parser_preserves_multiple_added_hunks() -> None:
    diff = """diff --git a/module.py b/module.py
--- a/module.py
+++ b/module.py
@@ -1,0 +2,2 @@
+FIRST = 1
+SECOND = 2
@@ -10,0 +13 @@
+THIRD = 3
"""

    assert parse_added_ranges(diff) == {
        "module.py": (AddedRange(2, 3), AddedRange(13, 13))
    }


@pytest.mark.parametrize(
    ("ruff_result", "expected"),
    [
        (subprocess.CompletedProcess([], 2, "", "ruff crashed"), 2),
        (subprocess.CompletedProcess([], 0, "not-json", ""), 2),
    ],
)
def test_ruff_execution_and_json_failures_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ruff_result: subprocess.CompletedProcess[str],
    expected: int,
) -> None:
    (tmp_path / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        checker,
        "_git_diff",
        lambda _base: subprocess.CompletedProcess(
            [],
            0,
            "diff --git a/changed.py b/changed.py\n"
            "--- a/changed.py\n+++ b/changed.py\n@@ -0,0 +1 @@\n+VALUE = 1\n",
            "",
        ),
    )
    monkeypatch.setattr(checker, "_run_ruff", lambda _paths: ruff_result)

    assert checker.main(["--base", "base"]) == expected


def test_multiline_diagnostic_intersecting_added_line_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "changed.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        checker,
        "_git_diff",
        lambda _base: subprocess.CompletedProcess(
            [],
            0,
            "diff --git a/changed.py b/changed.py\n"
            "--- a/changed.py\n+++ b/changed.py\n@@ -1,0 +2 @@\n+VALUE = 1\n",
            "",
        ),
    )
    payload = (
        '[{"filename": "changed.py", "code": "E999", "message": "crosses", '
        '"location": {"row": 1}, "end_location": {"row": 2}}]'
    )
    monkeypatch.setattr(
        checker,
        "_run_ruff",
        lambda _paths: subprocess.CompletedProcess([], 0, payload, ""),
    )

    assert checker.main(["--base", "base"]) == 1


def test_invalid_base_fails_closed(git_repo: Path) -> None:
    result = _run(git_repo, "definitely-not-a-revision")

    assert result.returncode != 0
    assert result.stderr
