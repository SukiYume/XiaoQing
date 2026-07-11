"""Compile reproducible, hashed dependency locks from pyproject.toml."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UV_VERSION = "0.11.28"
PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13")
EXTRAS = ("plugins", "astro", "ssh", "dev")


def _uv_version() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "uv", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().split()[1]


def _compile(version: str, *, upgrade: bool) -> None:
    output = ROOT / "requirements" / f"python-{version}.lock"
    command = [
        sys.executable,
        "-m",
        "uv",
        "pip",
        "compile",
        str(ROOT / "pyproject.toml"),
        "--python-version",
        version,
        "--universal",
        "--generate-hashes",
        "--no-emit-package",
        "xiaoqing",
        "--custom-compile-command",
        "python scripts/compile_locks.py",
        "--output-file",
        str(output),
    ]
    for extra in EXTRAS:
        command.extend(("--extra", extra))
    if upgrade:
        command.append("--upgrade")
    subprocess.run(command, cwd=ROOT, check=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade", action="store_true")
    args = parser.parse_args(argv)

    installed_uv = _uv_version()
    if installed_uv != UV_VERSION:
        parser.error(f"uv=={UV_VERSION} is required, found {installed_uv}")

    (ROOT / "requirements").mkdir(exist_ok=True)
    for version in PYTHON_VERSIONS:
        _compile(version, upgrade=args.upgrade)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
