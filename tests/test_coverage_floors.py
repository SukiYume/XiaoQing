from __future__ import annotations

from scripts.check_coverage_floors import check_floors


def _report(line_covered=8, statements=10, branch_covered=3, branches=4):
    return {
        "files": {
            "plugins/risky/main.py": {
                "summary": {
                    "covered_lines": line_covered,
                    "num_statements": statements,
                    "covered_branches": branch_covered,
                    "num_branches": branches,
                }
            }
        }
    }


def test_package_floor_accepts_line_and_branch_above_threshold() -> None:
    assert check_floors(
        _report(),
        {"plugins.risky": {"line": 80, "branch": 75}},
    ) == []


def test_package_floor_reports_line_branch_and_missing_package() -> None:
    failures = check_floors(
        _report(line_covered=7, branch_covered=2),
        {
            "plugins.risky": {"line": 80, "branch": 75},
            "plugins.missing": {"line": 1, "branch": 1},
        },
    )
    assert any("line" in failure for failure in failures)
    assert any("branch" in failure for failure in failures)
    assert any("no measured files" in failure for failure in failures)
