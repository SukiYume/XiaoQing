"""Compile and verify reproducible, hashed dependency locks."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS_DIR = ROOT / "requirements"
UV_VERSION = "0.11.28"
LOCK_SCHEMA = "1"
PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13")
RUNTIME_EXTRAS = (
    "plugins",
    "ml",
    "web",
    "jupyter",
    "arxiv-ml",
    "astro",
    "ssh",
)
COMPILE_CONTRACT = {
    "universal": True,
    "generate_hashes": True,
    "emit_project": False,
    "custom_compile_command": "python scripts/compile_locks.py",
}
_METADATA_KEYS = (
    "xiaoqing-lock-schema",
    "xiaoqing-lock-target",
    "xiaoqing-lock-uv",
    "xiaoqing-lock-input-sha256",
)
_METADATA_LINE = re.compile(r"^# (xiaoqing-lock-[a-z0-9-]+): (.+)$")


@dataclass(frozen=True)
class LockProfile:
    """One dependency surface compiled for every supported Python version."""

    name: str
    extras: tuple[str, ...]


@dataclass(frozen=True)
class LockTarget:
    """One immutable Python/profile lock output."""

    python_version: str
    profile: LockProfile

    @property
    def filename(self) -> str:
        return f"python-{self.python_version}-{self.profile.name}.lock"

    @property
    def path(self) -> Path:
        return REQUIREMENTS_DIR / self.filename

    @property
    def target_id(self) -> str:
        return f"python={self.python_version};profile={self.profile.name}"


LOCK_PROFILES = (
    LockProfile(name="runtime", extras=RUNTIME_EXTRAS),
    LockProfile(name="ci", extras=(*RUNTIME_EXTRAS, "dev")),
)
LOCK_TARGETS = tuple(
    LockTarget(python_version=version, profile=profile)
    for version in PYTHON_VERSIONS
    for profile in LOCK_PROFILES
)


def _uv_version() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "uv", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split()
    if len(fields) < 2:
        raise RuntimeError("unable to determine installed uv version")
    return fields[1]


def _load_pyproject(path: Path = PYPROJECT) -> dict[str, Any]:
    with path.open("rb") as source:
        parsed = tomllib.load(source)
    if not isinstance(parsed, dict):
        raise ValueError("pyproject.toml root must be a table")
    return parsed


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def dependency_input(target: LockTarget, *, pyproject_path: Path = PYPROJECT) -> dict[str, Any]:
    """Return the parsed dependency semantics that determine one lock."""
    document = _load_pyproject(pyproject_path)
    project = document.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml is missing [project]")
    optional = project.get("optional-dependencies")
    if not isinstance(optional, dict):
        raise ValueError("pyproject.toml is missing [project.optional-dependencies]")

    selected: dict[str, list[str]] = {}
    for extra in target.profile.extras:
        dependencies = optional.get(extra)
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            raise ValueError(f"selected extra is missing or invalid: {extra}")
        selected[extra] = sorted(dependencies)

    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        raise ValueError("project.dependencies must be a list of strings")
    tool = document.get("tool")
    uv_config = tool.get("uv", {}) if isinstance(tool, dict) else {}
    return {
        "schema": LOCK_SCHEMA,
        "uv_version": UV_VERSION,
        "compile_contract": COMPILE_CONTRACT,
        "target": {
            "python_version": target.python_version,
            "profile": target.profile.name,
            "extras": list(target.profile.extras),
        },
        "project": {
            "name": project.get("name"),
            "requires-python": project.get("requires-python"),
            "dependencies": sorted(dependencies),
            "selected_optional_dependencies": selected,
        },
        "tool_uv": _canonical(uv_config),
    }


def input_digest(target: LockTarget, *, pyproject_path: Path = PYPROJECT) -> str:
    payload = json.dumps(
        dependency_input(target, pyproject_path=pyproject_path),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _metadata(target: LockTarget, digest: str) -> dict[str, str]:
    return {
        "xiaoqing-lock-schema": LOCK_SCHEMA,
        "xiaoqing-lock-target": target.target_id,
        "xiaoqing-lock-uv": UV_VERSION,
        "xiaoqing-lock-input-sha256": digest,
    }


def parse_metadata(content: str) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    errors: list[str] = []
    counts: dict[str, int] = {}
    for line in content.splitlines():
        match = _METADATA_LINE.fullmatch(line)
        if match is None:
            continue
        key, value = match.groups()
        if key not in _METADATA_KEYS:
            continue
        counts[key] = counts.get(key, 0) + 1
        values[key] = value
    for key in _METADATA_KEYS:
        count = counts.get(key, 0)
        if count != 1:
            errors.append(f"{key} occurs {count} times")
    return values, errors


def metadata_problems(content: str, target: LockTarget, digest: str) -> list[str]:
    values, problems = parse_metadata(content)
    for key, expected in _metadata(target, digest).items():
        if values.get(key) != expected:
            problems.append(f"{key} does not match {expected}")
    return problems


def stamp_lock(content: str, target: LockTarget, digest: str) -> str:
    body_lines = [
        line
        for line in content.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        if _METADATA_LINE.fullmatch(line) is None
    ]
    header = [f"# {key}: {value}" for key, value in _metadata(target, digest).items()]
    return "\n".join((*header, "", *body_lines)).rstrip() + "\n"


def _compile_command(target: LockTarget, output: Path, *, upgrade: bool) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "uv",
        "pip",
        "compile",
        str(PYPROJECT),
        "--python-version",
        target.python_version,
        "--universal",
        "--generate-hashes",
        "--no-emit-package",
        "xiaoqing",
        "--custom-compile-command",
        str(COMPILE_CONTRACT["custom_compile_command"]),
        "--output-file",
        str(output),
    ]
    for extra in target.profile.extras:
        command.extend(("--extra", extra))
    if upgrade:
        command.append("--upgrade")
    return command


def _compile_to(target: LockTarget, output: Path, *, upgrade: bool) -> None:
    subprocess.run(
        _compile_command(target, output, upgrade=upgrade),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _normalized(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _generate_target(target: LockTarget, *, upgrade: bool) -> None:
    destination = target.path
    digest = input_digest(target)
    with tempfile.TemporaryDirectory(prefix="xiaoqing-lock-generate-", dir=REQUIREMENTS_DIR) as raw:
        temporary = Path(raw) / target.filename
        if destination.is_file() and not upgrade:
            shutil.copyfile(destination, temporary)
        _compile_to(target, temporary, upgrade=upgrade)
        stamped = stamp_lock(temporary.read_text(encoding="utf-8"), target, digest)
        replacement = Path(raw) / f"{target.filename}.ready"
        replacement.write_text(stamped, encoding="utf-8", newline="\n")
        os.replace(replacement, destination)


def _short_diff(committed: str, generated: str, filename: str) -> str:
    lines = list(
        difflib.unified_diff(
            _normalized(committed).splitlines(),
            _normalized(generated).splitlines(),
            fromfile=f"committed/{filename}",
            tofile=f"generated/{filename}",
            n=1,
            lineterm="",
        )
    )
    return "\n".join(lines[:30])


def check_locks(targets: Iterable[LockTarget] = LOCK_TARGETS) -> list[str]:
    """Recompile into temporary files and return every stale-lock problem."""
    problems: list[str] = []
    for target in targets:
        destination = target.path
        if not destination.is_file() or destination.is_symlink():
            problems.append(f"{target.filename}: lock is missing or not a regular file")
            continue
        committed = destination.read_text(encoding="utf-8")
        digest = input_digest(target)
        metadata_errors = metadata_problems(committed, target, digest)
        if metadata_errors:
            problems.append(f"{target.filename}: " + "; ".join(metadata_errors))
            continue
        with tempfile.TemporaryDirectory(prefix="xiaoqing-lock-check-") as raw:
            temporary = Path(raw) / target.filename
            shutil.copyfile(destination, temporary)
            _compile_to(target, temporary, upgrade=False)
            generated = stamp_lock(temporary.read_text(encoding="utf-8"), target, digest)
        if _normalized(committed) != _normalized(generated):
            diff = _short_diff(committed, generated, target.filename)
            problems.append(f"{target.filename}: compiled content is stale\n{diff}")
    return problems


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--upgrade", action="store_true")
    mode.add_argument("--check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        installed_uv = _uv_version()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.error(f"unable to run uv=={UV_VERSION}: {exc}")
    if installed_uv != UV_VERSION:
        parser.error(f"uv=={UV_VERSION} is required, found {installed_uv}")

    REQUIREMENTS_DIR.mkdir(exist_ok=True)
    if args.check:
        try:
            problems = check_locks()
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"dependency lock freshness check failed: {exc}", file=sys.stderr)
            return 1
        if problems:
            print("Dependency locks are stale:", file=sys.stderr)
            for problem in problems:
                print(f"- {problem}", file=sys.stderr)
            return 1
        print(f"All {len(LOCK_TARGETS)} dependency locks are fresh.")
        return 0

    try:
        for target in LOCK_TARGETS:
            _generate_target(target, upgrade=args.upgrade)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"dependency lock generation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
