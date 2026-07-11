"""Enforce line and branch coverage floors for risk-sensitive packages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


def _normalized_prefix(package: str) -> str:
    return package.replace(".", "/").strip("/") + "/"


def _aggregate(files: dict[str, Any], package: str) -> dict[str, int]:
    prefix = _normalized_prefix(package)
    totals = {
        "covered_lines": 0,
        "num_statements": 0,
        "covered_branches": 0,
        "num_branches": 0,
    }
    for raw_path, report in files.items():
        path = str(raw_path).replace("\\", "/").lstrip("./")
        if not path.startswith(prefix):
            continue
        summary = report.get("summary", {})
        for key in totals:
            totals[key] += int(summary.get(key, 0) or 0)
    return totals


def _percent(covered: int, total: int) -> float:
    return 100.0 if total == 0 else covered * 100.0 / total


def check_floors(report: dict[str, Any], floors: dict[str, Any]) -> list[str]:
    files = report.get("files", {})
    failures: list[str] = []
    for package, raw_floor in floors.items():
        floor = raw_floor if isinstance(raw_floor, dict) else {}
        totals = _aggregate(files, package)
        if totals["num_statements"] == 0:
            failures.append(f"{package}: no measured files")
            continue
        line = _percent(totals["covered_lines"], totals["num_statements"])
        branch = _percent(totals["covered_branches"], totals["num_branches"])
        line_floor = float(floor.get("line", 0))
        branch_floor = float(floor.get("branch", 0))
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
    args = parser.parse_args(argv)
    report = json.loads(Path(args.coverage_json).read_text(encoding="utf-8"))
    config = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    floors = config["tool"]["xiaoqing"]["coverage_floors"]
    failures = check_floors(report, floors)
    if failures:
        print("Coverage floor failures:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
