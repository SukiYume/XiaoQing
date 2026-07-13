"""Enforce line and branch coverage floors for risk-sensitive packages."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


_COUNTERS = (
    "covered_lines",
    "num_statements",
    "covered_branches",
    "num_branches",
)
_PACKAGE_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


def _normalized_prefix(package: str) -> str:
    return package.replace(".", "/").strip("/") + "/"


def _aggregate(files: dict[str, dict[str, int]], package: str) -> dict[str, int]:
    prefix = _normalized_prefix(package)
    totals = dict.fromkeys(_COUNTERS, 0)
    for raw_path, summary in files.items():
        path = raw_path.replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if not path.startswith(prefix):
            continue
        for key in totals:
            totals[key] += summary[key]
    return totals


def _percent(covered: int, total: int) -> float:
    if total <= 0:
        raise ValueError("coverage percentage requires a positive total")
    return covered / total * 100.0


def _logical_path(raw_path: str) -> str:
    path = raw_path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path.startswith("/") or re.match(r"^[A-Za-z]:/", path):
        raise ValueError("must be relative")

    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("must not contain '..'")
        parts.append(part)
    if not parts:
        raise ValueError("must not be empty")
    return "/".join(parts)


def _validate_report(report: Any) -> tuple[dict[str, Any], dict[str, dict[str, int]], list[str]]:
    failures: list[str] = []
    if not isinstance(report, dict):
        return {}, {}, ["coverage report: root must be an object"]

    meta = report.get("meta")
    if not isinstance(meta, dict):
        failures.append("coverage report: meta must be an object")
        meta = {}

    raw_files = report.get("files")
    if not isinstance(raw_files, dict):
        failures.append("coverage report: files must be an object")
        return meta, {}, failures

    files: dict[str, dict[str, int]] = {}
    seen_paths: set[str] = set()
    for raw_path, raw_file in raw_files.items():
        if not isinstance(raw_path, str) or not raw_path:
            failures.append("coverage report: every file path must be a non-empty string")
            continue
        try:
            path = _logical_path(raw_path)
        except ValueError as exc:
            failures.append(f"coverage report: invalid file path {raw_path!r}: {exc}")
            continue
        if path in seen_paths:
            failures.append(f"coverage report: duplicate logical file path: {path}")
            continue
        seen_paths.add(path)
        if not isinstance(raw_file, dict):
            failures.append(f"{raw_path}: file report must be an object")
            continue
        raw_summary = raw_file.get("summary")
        if not isinstance(raw_summary, dict):
            failures.append(f"{raw_path}: summary must be an object")
            continue

        summary: dict[str, int] = {}
        valid = True
        for key in _COUNTERS:
            value = raw_summary.get(key)
            if type(value) is not int or value < 0:
                failures.append(f"{raw_path}: {key} must be a non-negative integer")
                valid = False
            else:
                summary[key] = value
        if not valid:
            continue
        if summary["covered_lines"] > summary["num_statements"]:
            failures.append(f"{raw_path}: covered_lines exceeds num_statements")
            continue
        if summary["covered_branches"] > summary["num_branches"]:
            failures.append(f"{raw_path}: covered_branches exceeds num_branches")
            continue
        files[path] = summary
    return meta, files, failures


def _validate_floors(floors: Any) -> tuple[dict[str, dict[str, float]], list[str]]:
    failures: list[str] = []
    if not isinstance(floors, dict) or not floors:
        return {}, ["coverage config: coverage_floors must be a non-empty object"]

    normalized: dict[str, dict[str, float]] = {}
    for package, raw_floor in floors.items():
        if not isinstance(package, str) or not _PACKAGE_RE.fullmatch(package):
            failures.append(f"coverage config: invalid package name {package!r}")
            continue
        if not isinstance(raw_floor, dict):
            failures.append(f"{package}: floor must be an object")
            continue
        missing = {"line", "branch"} - raw_floor.keys()
        unknown = raw_floor.keys() - {"line", "branch"}
        if missing:
            failures.append(f"{package}: missing floor keys: {', '.join(sorted(missing))}")
        if unknown:
            failures.append(f"{package}: unknown floor keys: {', '.join(sorted(unknown))}")
        if missing or unknown:
            continue

        values: dict[str, float] = {}
        valid = True
        for kind in ("line", "branch"):
            value = raw_floor[kind]
            if type(value) not in (int, float):
                failures.append(f"{package}: {kind} floor must be a finite number")
                valid = False
            elif isinstance(value, float) and not math.isfinite(value):
                failures.append(f"{package}: {kind} floor must be a finite number")
                valid = False
            elif not 0 <= value <= 100:
                failures.append(f"{package}: {kind} floor must be between 0 and 100")
                valid = False
            else:
                values[kind] = float(value)
        if valid:
            normalized[package] = values
    return normalized, failures


def _select_packages(
    floors: dict[str, dict[str, float]], packages: list[str] | None
) -> tuple[list[str], list[str]]:
    if packages is None:
        return list(floors), []
    if not packages:
        return [], ["package selection must not be empty"]

    failures: list[str] = []
    selected: list[str] = []
    seen: set[str] = set()
    for package in packages:
        if not isinstance(package, str) or not package:
            failures.append("selected package must be a non-empty string")
            continue
        if package in seen:
            failures.append(f"duplicate selected package: {package}")
            continue
        seen.add(package)
        if package not in floors:
            failures.append(f"unknown selected package: {package}")
            continue
        selected.append(package)
    return selected, failures


def check_floors(
    report: Any,
    floors: Any,
    packages: list[str] | None = None,
) -> list[str]:
    normalized_floors, failures = _validate_floors(floors)
    selected, selection_failures = _select_packages(normalized_floors, packages)
    failures.extend(selection_failures)
    meta, files, report_failures = _validate_report(report)
    failures.extend(report_failures)
    if failures:
        return failures

    branch_required = any(normalized_floors[package]["branch"] > 0 for package in selected)
    if branch_required and meta.get("branch_coverage") is not True:
        return [
            "coverage report: meta.branch_coverage must be true when a selected branch floor "
            "is greater than zero"
        ]

    for package in selected:
        floor = normalized_floors[package]
        totals = _aggregate(files, package)
        if totals["num_statements"] == 0:
            failures.append(f"{package}: no measured files")
            continue
        line = _percent(totals["covered_lines"], totals["num_statements"])
        line_floor = floor["line"]
        branch_floor = floor["branch"]
        if branch_floor > 0 and totals["num_branches"] == 0:
            failures.append(f"{package}: no measured branches for non-zero branch floor")
            continue
        branch = (
            _percent(totals["covered_branches"], totals["num_branches"])
            if totals["num_branches"] > 0
            else 0.0
        )
        print(
            f"{package}: line={line:.2f}% (floor {line_floor:.2f}%), "
            f"branch={branch:.2f}% (floor {branch_floor:.2f}%)"
        )
        if line + 1e-9 < line_floor:
            failures.append(f"{package}: line {line:.2f}% < {line_floor:.2f}%")
        if branch + 1e-9 < branch_floor:
            failures.append(f"{package}: branch {branch:.2f}% < {branch_floor:.2f}%")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage-json", default="coverage.json")
    parser.add_argument("--config", default="pyproject.toml")
    parser.add_argument(
        "--package",
        action="append",
        dest="packages",
        metavar="PACKAGE",
        help="check only this configured package (repeatable)",
    )
    args = parser.parse_args(argv)
    try:
        report = json.loads(Path(args.coverage_json).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        print("Coverage floor input error: unable to read or parse coverage JSON", file=sys.stderr)
        return 1
    try:
        config = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        print("Coverage floor input error: unable to read or parse TOML config", file=sys.stderr)
        return 1

    if not isinstance(config, dict):
        failures = ["coverage config: root must be an object"]
    else:
        tool = config.get("tool")
        xiaoqing = tool.get("xiaoqing") if isinstance(tool, dict) else None
        if not isinstance(xiaoqing, dict) or "coverage_floors" not in xiaoqing:
            failures = ["coverage config: tool.xiaoqing.coverage_floors must be an object"]
        else:
            failures = check_floors(report, xiaoqing["coverage_floors"], args.packages)
    if failures:
        print("Coverage floor failures:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
