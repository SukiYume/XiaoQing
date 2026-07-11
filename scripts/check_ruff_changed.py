"""Reject Ruff diagnostics whose source range intersects Git-added lines.

The repository still has historical lint debt.  Ruff therefore checks every
changed current Python file, while this gate reports only diagnostics touching
lines introduced since the requested base revision.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HUNK_HEADER = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@"
)


@dataclass(frozen=True, order=True)
class AddedRange:
    start: int
    end: int

    def intersects(self, start: int, end: int) -> bool:
        return start <= self.end and end >= self.start


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lint Git-added Python lines with Ruff")
    parser.add_argument("--base", required=True, help="Git revision to diff against")
    return parser


def _git_diff(base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--no-ext-diff",
            "--find-renames",
            "--unified=0",
            f"{base}...HEAD",
            "--",
            "*.py",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def parse_added_ranges(diff: str) -> dict[str, tuple[AddedRange, ...]]:
    """Parse current-file hunk ranges from one unified Git diff."""
    ranges: dict[str, list[AddedRange]] = {}
    current_path: str | None = None
    awaiting_new_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current_path = None
            awaiting_new_header = False
            continue
        if line.startswith("--- "):
            awaiting_new_header = True
            continue
        if awaiting_new_header and line.startswith("+++ "):
            awaiting_new_header = False
            raw_path = line[4:].split("\t", 1)[0]
            if raw_path.startswith('"'):
                try:
                    decoded = ast.literal_eval(raw_path)
                except (SyntaxError, ValueError) as exc:
                    raise ValueError("invalid quoted Git destination path") from exc
                if not isinstance(decoded, str):
                    raise ValueError("quoted Git path did not decode to text")
                raw_path = decoded
            if raw_path == "/dev/null":
                current_path = None
            elif raw_path.startswith("b/"):
                current_path = raw_path[2:]
            else:
                raise ValueError(f"unexpected Git destination path: {raw_path!r}")
            continue
        match = _HUNK_HEADER.match(line)
        if match and current_path is not None:
            start = int(match.group("start"))
            count = int(match.group("count") or "1")
            if count > 0:
                ranges.setdefault(current_path, []).append(
                    AddedRange(start, start + count - 1)
                )
    return {path: tuple(items) for path, items in ranges.items() if items}


def _run_ruff(paths: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--output-format=json",
            "--exit-zero",
            "--force-exclude",
            "--",
            *paths,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _relative_filename(raw_filename: str, root: Path) -> str:
    path = Path(raw_filename)
    absolute = path.resolve() if path.is_absolute() else (root / path).resolve()
    return absolute.relative_to(root).as_posix()


def _diagnostic_range(diagnostic: dict[str, Any]) -> tuple[int, int]:
    location = diagnostic["location"]
    end_location = diagnostic.get("end_location") or location
    start = int(location["row"])
    end = max(start, int(end_location.get("row", start)))
    return start, end


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    diff = _git_diff(args.base)
    if diff.returncode:
        sys.stderr.write(diff.stderr or "git diff failed\n")
        return diff.returncode or 2
    if not diff.stdout.strip():
        print("No committed Python changes to lint.")
        return 0
    try:
        added_ranges = parse_added_ranges(diff.stdout)
    except ValueError as exc:
        print(f"Failed to parse Git diff: {exc}", file=sys.stderr)
        return 2
    if not added_ranges:
        print("No added Python lines to lint.")
        return 0

    root = Path.cwd().resolve()
    paths = sorted(
        path for path in added_ranges if (root / Path(path)).is_file()
    )
    if not paths:
        print("No current Python files with added lines to lint.")
        return 0
    ruff = _run_ruff(paths)
    if ruff.returncode:
        sys.stderr.write(ruff.stderr or "Ruff execution failed\n")
        return ruff.returncode or 2
    try:
        diagnostics = json.loads(ruff.stdout)
        if not isinstance(diagnostics, list):
            raise TypeError("Ruff JSON root is not a list")
    except (json.JSONDecodeError, TypeError) as exc:
        print(f"Failed to parse Ruff JSON: {exc}", file=sys.stderr)
        return 2

    failures: list[tuple[str, int, int, str, str]] = []
    try:
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict):
                raise TypeError("Ruff diagnostic is not an object")
            relative = _relative_filename(str(diagnostic["filename"]), root)
            ranges = added_ranges.get(relative, ())
            start, end = _diagnostic_range(diagnostic)
            if ranges and any(item.intersects(start, end) for item in ranges):
                failures.append(
                    (
                        relative,
                        start,
                        end,
                        str(diagnostic.get("code") or "unknown"),
                        str(diagnostic.get("message") or "Ruff diagnostic"),
                    )
                )
    except (KeyError, TypeError, ValueError) as exc:
        print(f"Invalid Ruff diagnostic payload: {exc}", file=sys.stderr)
        return 2

    for path, start, end, code, message in sorted(failures):
        location = f"{path}:{start}" if start == end else f"{path}:{start}-{end}"
        print(f"{location}: {code} {message}", file=sys.stderr)
    if failures:
        print(
            f"Ruff found {len(failures)} diagnostic(s) intersecting added lines.",
            file=sys.stderr,
        )
        return 1
    print("No Ruff diagnostics intersect Git-added Python lines.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
