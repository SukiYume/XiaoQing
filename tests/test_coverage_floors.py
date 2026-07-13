from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_coverage_floors import check_floors, main


def _summary(
    line_covered: int = 8,
    statements: int = 10,
    branch_covered: int = 3,
    branches: int = 4,
) -> dict[str, int]:
    return {
        "covered_lines": line_covered,
        "num_statements": statements,
        "covered_branches": branch_covered,
        "num_branches": branches,
    }


def _report(
    line_covered: int = 8,
    statements: int = 10,
    branch_covered: int = 3,
    branches: int = 4,
) -> dict[str, object]:
    return {
        "meta": {"branch_coverage": True},
        "files": {
            "plugins/risky/main.py": {
                "summary": _summary(line_covered, statements, branch_covered, branches)
            }
        },
    }


def _floors() -> dict[str, dict[str, int]]:
    return {"plugins.risky": {"line": 80, "branch": 75}}


def test_package_floor_accepts_line_and_branch_above_threshold() -> None:
    assert check_floors(_report(), _floors()) == []


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


@pytest.mark.parametrize("branch_meta", [None, False, 1, "true"])
def test_nonzero_branch_floor_requires_strict_true_branch_metadata(branch_meta: object) -> None:
    report = _report()
    if branch_meta is None:
        report["meta"] = {}
    else:
        report["meta"] = {"branch_coverage": branch_meta}

    failures = check_floors(report, _floors())

    assert any("meta.branch_coverage must be true" in failure for failure in failures)


def test_missing_meta_shape_fails_closed() -> None:
    report = _report()
    report.pop("meta")

    assert "coverage report: meta must be an object" in check_floors(report, _floors())


def test_nonzero_branch_floor_rejects_zero_measured_branches() -> None:
    failures = check_floors(
        _report(branch_covered=0, branches=0),
        _floors(),
    )

    assert failures == ["plugins.risky: no measured branches for non-zero branch floor"]


def test_zero_branch_floor_allows_a_package_with_no_branches() -> None:
    assert check_floors(
        _report(branch_covered=0, branches=0),
        {"plugins.risky": {"line": 80, "branch": 0}},
    ) == []


@pytest.mark.parametrize(
    ("counter", "value"),
    [
        ("covered_lines", True),
        ("num_statements", -1),
        ("covered_branches", 1.5),
        ("num_branches", "4"),
        ("covered_lines", None),
    ],
)
def test_summary_counters_must_be_nonbool_nonnegative_integers(
    counter: str, value: object
) -> None:
    report = _report()
    report["files"]["plugins/risky/main.py"]["summary"][counter] = value  # type: ignore[index]

    failures = check_floors(report, _floors())

    assert any(f"{counter} must be a non-negative integer" in failure for failure in failures)


@pytest.mark.parametrize(
    ("summary", "message"),
    [
        (_summary(line_covered=11), "covered_lines exceeds num_statements"),
        (_summary(branch_covered=5), "covered_branches exceeds num_branches"),
    ],
)
def test_covered_counters_cannot_exceed_their_totals(
    summary: dict[str, int], message: str
) -> None:
    report = _report()
    report["files"]["plugins/risky/main.py"]["summary"] = summary  # type: ignore[index]

    assert any(message in failure for failure in check_floors(report, _floors()))


def test_arbitrarily_large_valid_integer_counters_do_not_crash_percentage_math() -> None:
    huge = 10**10_000
    report = _report(huge, huge, huge, huge)

    assert check_floors(report, {"plugins.risky": {"line": 100, "branch": 100}}) == []


def test_duplicate_normalized_logical_paths_fail_closed() -> None:
    report = _report()
    report["files"]["./plugins\\risky//main.py"] = {  # type: ignore[index]
        "summary": _summary()
    }

    failures = check_floors(report, _floors())

    assert "coverage report: duplicate logical file path: plugins/risky/main.py" in failures


@pytest.mark.parametrize("path", ["../plugins/risky.py", "/plugins/risky.py", "C:\\risky.py"])
def test_unsafe_coverage_file_paths_fail_closed(path: str) -> None:
    report = _report()
    report["files"] = {path: {"summary": _summary()}}

    assert any("invalid file path" in failure for failure in check_floors(report, _floors()))


@pytest.mark.parametrize(
    ("report", "message"),
    [
        ([], "root must be an object"),
        ({"meta": [], "files": {}}, "meta must be an object"),
        ({"meta": {}, "files": []}, "files must be an object"),
        (
            {"meta": {}, "files": {"plugins/risky/main.py": []}},
            "file report must be an object",
        ),
        (
            {"meta": {}, "files": {"plugins/risky/main.py": {"summary": []}}},
            "summary must be an object",
        ),
    ],
)
def test_report_container_shapes_fail_closed(report: object, message: str) -> None:
    assert any(message in failure for failure in check_floors(report, _floors()))


@pytest.mark.parametrize(
    ("floors", "message"),
    [
        ([], "coverage_floors must be a non-empty object"),
        ({}, "coverage_floors must be a non-empty object"),
        ({"not/a/package": {"line": 1, "branch": 1}}, "invalid package name"),
        ({"plugins.risky": []}, "floor must be an object"),
        ({"plugins.risky": {"line": 1}}, "missing floor keys"),
        (
            {"plugins.risky": {"line": 1, "branch": 1, "total": 2}},
            "unknown floor keys",
        ),
        ({"plugins.risky": {"line": True, "branch": 1}}, "finite number"),
        ({"plugins.risky": {"line": 10**10_000, "branch": 1}}, "between 0 and 100"),
        ({"plugins.risky": {"line": -1, "branch": 1}}, "between 0 and 100"),
        ({"plugins.risky": {"line": 1, "branch": 101}}, "between 0 and 100"),
    ],
)
def test_floor_config_shapes_fail_closed(floors: object, message: str) -> None:
    assert any(message in failure for failure in check_floors(_report(), floors))


def test_low_package_is_not_masked_by_high_coverage_sibling() -> None:
    report = {
        "meta": {"branch_coverage": True},
        "files": {
            "plugins/high/main.py": {"summary": _summary(100, 100, 100, 100)},
            "plugins/low/main.py": {"summary": _summary(1, 100, 1, 100)},
        },
    }
    floors = {
        "plugins.high": {"line": 90, "branch": 90},
        "plugins.low": {"line": 50, "branch": 50},
    }

    failures = check_floors(report, floors, ["plugins.low"])

    assert any(failure.startswith("plugins.low: line") for failure in failures)
    assert any(failure.startswith("plugins.low: branch") for failure in failures)


def _write_cli_inputs(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    packages = [
        "plugins.codex",
        "plugins.shell",
        "plugins.jupyter",
        "plugins.qingssh",
        "plugins.minecraft",
    ]
    report = {
        "meta": {"branch_coverage": True},
        "files": {
            f"{package.replace('.', '/')}/main.py": {"summary": _summary()}
            for package in packages
        },
    }
    coverage_path = tmp_path / "coverage-privileged.json"
    coverage_path.write_text(json.dumps(report), encoding="utf-8")
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(
        "[tool.xiaoqing.coverage_floors]\n"
        + "\n".join(
            f'"{package}" = {{ line = 80, branch = 75 }}' for package in packages
        ),
        encoding="utf-8",
    )
    return coverage_path, config_path, packages


def test_cli_checks_five_repeatable_package_selections_against_one_json(tmp_path: Path) -> None:
    coverage_path, config_path, packages = _write_cli_inputs(tmp_path)
    argv = ["--coverage-json", str(coverage_path), "--config", str(config_path)]
    for package in packages:
        argv.extend(("--package", package))

    assert main(argv) == 0


def test_cli_without_package_selection_checks_every_configured_floor(tmp_path: Path) -> None:
    coverage_path, config_path, _ = _write_cli_inputs(tmp_path)
    report = json.loads(coverage_path.read_text(encoding="utf-8"))
    report["files"]["plugins/minecraft/main.py"]["summary"]["covered_lines"] = 1
    coverage_path.write_text(json.dumps(report), encoding="utf-8")

    assert main(
        ["--coverage-json", str(coverage_path), "--config", str(config_path)]
    ) == 1


@pytest.mark.parametrize(
    "selection",
    [
        ["plugins.unknown"],
        ["plugins.codex", "plugins.codex"],
        [""],
    ],
)
def test_cli_rejects_unknown_duplicate_and_empty_package_selections(
    tmp_path: Path, selection: list[str]
) -> None:
    coverage_path, config_path, _ = _write_cli_inputs(tmp_path)
    argv = ["--coverage-json", str(coverage_path), "--config", str(config_path)]
    for package in selection:
        argv.extend(("--package", package))

    assert main(argv) == 1


def test_cli_fails_closed_on_missing_config_shape(tmp_path: Path) -> None:
    coverage_path, _, _ = _write_cli_inputs(tmp_path)
    config_path = tmp_path / "bad.toml"
    config_path.write_text("[tool]\n", encoding="utf-8")

    assert main(
        ["--coverage-json", str(coverage_path), "--config", str(config_path)]
    ) == 1


@pytest.mark.parametrize(
    "malformed_report",
    [
        '{"oversized_integer": ' + "9" * 5_000 + "}",
        "[" * 50_000 + "0" + "]" * 50_000,
    ],
    ids=["integer-digit-limit", "recursive-nesting-limit"],
)
def test_cli_bounds_json_parser_failures_without_echoing_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    malformed_report: str,
) -> None:
    coverage_path = tmp_path / "malformed.json"
    coverage_path.write_text(malformed_report, encoding="utf-8")
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(
        '[tool.xiaoqing.coverage_floors]\n"plugins.risky" = { line = 1, branch = 1 }\n',
        encoding="utf-8",
    )

    assert main(
        ["--coverage-json", str(coverage_path), "--config", str(config_path)]
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Coverage floor input error: unable to read or parse coverage JSON\n"
    )
    assert len(captured.err) < 100
