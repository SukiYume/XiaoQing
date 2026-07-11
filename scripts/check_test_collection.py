"""Fail CI when the full pytest inventory unexpectedly shrinks."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import pytest


class _CollectionInventory:
    def __init__(self) -> None:
        self.node_ids: list[str] = []

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        self.node_ids = [item.nodeid for item in items]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimum", type=int, required=True)
    parser.add_argument(
        "--tests-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    inventory = _CollectionInventory()
    exit_code = pytest.main(
        [str(args.tests_dir), "--collect-only", "-p", "no:terminal", "-o", "addopts="],
        plugins=[inventory],
    )
    if exit_code != pytest.ExitCode.OK:
        print(f"pytest collection failed with exit code {int(exit_code)}")
        return int(exit_code)

    count = len(inventory.node_ids)
    if count < args.minimum:
        print(f"test collection too small: collected={count}, minimum={args.minimum}")
        return 1

    if len(set(inventory.node_ids)) != count:
        print("test collection contains duplicate node IDs")
        return 1

    print(f"test collection verified: collected={count}, minimum={args.minimum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
