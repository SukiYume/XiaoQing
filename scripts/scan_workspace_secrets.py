"""Stream files through auditable secret rules without printing secret values."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}
DEFAULT_ALLOWLIST = ".secret-scan-allowlist.json"
MIN_SECRET_LENGTH = 12

PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
    re.IGNORECASE,
)
ASSIGNMENT = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9_-])["']?
    (?P<key>
        client[_-]?secret|secret[_-]?key|api[_-]?key|
        access[_-]?token|refresh[_-]?token|auth[_-]?token|bearer[_-]?token|token|
        password|passwd|pwd|authorization|cookie|sid
    )
    ["']?(?![A-Za-z0-9_-])\s*[:=]\s*
    (?:
        "(?P<double>[^"\r\n]+)" |
        '(?P<single>[^'\r\n]+)' |
        (?P<bare>[A-Za-z0-9][A-Za-z0-9._~+/=:%@-]{11,})(?=$|[\s,;#\]\}])
    )
    """,
)

_EXACT_PLACEHOLDERS = {
    "changeme",
    "dummy-secret",
    "bearer-canary",
    "example-secret",
    "invalid_token",
    "my_password",
    "new-token",
    "old-token",
    "password123",
    "placeholder",
    "replace-me",
    "secret-pass",
    "secret_token_123",
    "sk-1234567890",
    "sk-xxx",
    "test_access_token",
    "test_csrf",
    "test_key",
    "test_pass",
    "test_sid_12345",
    "test_token",
    "test_token_123",
    "top-secret",
    "your-api-key-placeholder",
    "your-session-id",
    "xxx",
}
_PLACEHOLDER_PATTERNS = (
    re.compile(r"<[-A-Za-z0-9_.]+>"),
    re.compile(r"\$\{[-A-Za-z0-9_.]+\}"),
    re.compile(r"\{\{[-A-Za-z0-9_.]+\}\}"),
    re.compile(
        r"(?:[A-Za-z0-9]+[-_])?your[-_][A-Za-z0-9_-]*"
        r"(?:key|token|secret|password)(?:[-_]placeholder)?",
        re.I,
    ),
    re.compile(r"(?:example|sample|dummy|test)[-_](?:api[-_]?key|token|secret|password)", re.I),
    re.compile(r"[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)"),
    re.compile(r"(?:PASTE|INSERT|REPLACE)_[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)_HERE"),
)


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line: int
    rule_id: str
    fingerprint: str

    @property
    def short_fingerprint(self) -> str:
        return self.fingerprint[:12]

    def render(self) -> str:
        return (
            f"{self.path}:{self.line}: {self.rule_id} "
            f"fingerprint=sha256:{self.short_fingerprint}"
        )


@dataclass(frozen=True)
class AllowlistEntry:
    path: str
    rule_id: str
    fingerprint: str
    reason: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.rule_id, self.fingerprint)


@dataclass(frozen=True)
class ScanReport:
    findings: tuple[SecretFinding, ...]
    allowed: tuple[SecretFinding, ...] = ()
    stale_allowlist: tuple[AllowlistEntry, ...] = ()
    allowlist_errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.findings and not self.stale_allowlist and not self.allowlist_errors

    def rendered_problems(self) -> list[str]:
        rendered = [finding.render() for finding in self.findings]
        rendered.extend(f"allowlist: {error}" for error in self.allowlist_errors)
        rendered.extend(
            "allowlist: stale entry "
            f"path={entry.path} rule={entry.rule_id} "
            f"fingerprint=sha256:{entry.fingerprint[:12]}"
            for entry in self.stale_allowlist
        )
        return rendered


def _is_placeholder(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return True
    if re.fullmatch(r"https?://[^\s]+", normalized, re.I):
        return True
    bearer = re.fullmatch(r"(?i)Bearer\s+(.+)", normalized)
    if bearer:
        return _is_placeholder(bearer.group(1))
    if normalized.lower() in _EXACT_PLACEHOLDERS:
        return True
    return any(pattern.fullmatch(normalized) for pattern in _PLACEHOLDER_PATTERNS)


def _rule_for_key(key: str) -> str:
    normalized = key.lower().replace("-", "_")
    if normalized in {"password", "passwd", "pwd"}:
        return "credential.assignment.password.v1"
    if normalized in {"api_key", "secret_key", "client_secret"}:
        return f"credential.assignment.{normalized.replace('_', '-')}.v1"
    if normalized in {"authorization", "cookie", "sid"}:
        return f"credential.assignment.{normalized}.v1"
    return "credential.assignment.token.v1"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _workspace_files(root: Path) -> Iterator[Path]:
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(directory for directory in dirs if directory not in SKIP_DIRS)
        for filename in sorted(files):
            path = Path(current) / filename
            if not path.is_symlink() and path.is_file():
                yield path


def _tracked_files(root: Path) -> Iterator[Path]:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    repo_root = Path(completed.stdout.strip()).resolve()
    listed = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--cached"],
        check=True,
        capture_output=True,
    ).stdout
    for raw_path in sorted(part for part in listed.split(b"\0") if part):
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        path = (repo_root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if not path.is_symlink() and path.is_file():
            yield path


def iter_files(root: Path, *, mode: str = "workspace") -> Iterator[Path]:
    """Yield workspace or Git-tracked files without a file-size cutoff."""
    root = root.resolve()
    if mode == "workspace":
        yield from _workspace_files(root)
        return
    if mode == "tracked":
        yield from _tracked_files(root)
        return
    raise ValueError(f"unsupported scan mode: {mode}")


def _scan_file(root: Path, path: Path) -> Iterator[SecretFinding]:
    relative = path.relative_to(root).as_posix()
    try:
        with path.open("rb") as handle:
            probe = handle.read(8192)
            if b"\0" in probe:
                return
            handle.seek(0)
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.decode("utf-8", errors="ignore")
                for marker in PRIVATE_KEY.finditer(line):
                    value = marker.group(0)
                    yield SecretFinding(
                        relative,
                        line_number,
                        "private-key.pem.v1",
                        _fingerprint(value),
                    )
                for match in ASSIGNMENT.finditer(line):
                    value = next(
                        group
                        for group in (
                            match.group("double"),
                            match.group("single"),
                            match.group("bare"),
                        )
                        if group is not None
                    ).strip()
                    if len(value) < MIN_SECRET_LENGTH or _is_placeholder(value):
                        continue
                    yield SecretFinding(
                        relative,
                        line_number,
                        _rule_for_key(match.group("key")),
                        _fingerprint(value),
                    )
    except OSError:
        return


def _load_allowlist(path: Path | None) -> tuple[list[AllowlistEntry], list[str]]:
    if path is None or not path.exists():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"cannot read {path.name}: {type(exc).__name__}"]
    raw_entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, list):
        return [], [f"{path.name} must contain an entries array"]
    entries: list[AllowlistEntry] = []
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            errors.append(f"entry {index} must be an object")
            continue
        relative = str(raw.get("path", "")).replace("\\", "/").strip()
        rule_id = str(raw.get("rule_id", "")).strip()
        fingerprint = str(raw.get("fingerprint", "")).removeprefix("sha256:").strip().lower()
        reason = str(raw.get("reason", "")).strip()
        if (
            not relative
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or not rule_id
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            or not reason
        ):
            errors.append(
                f"entry {index} requires relative path, rule_id, full SHA-256 fingerprint, and reason"
            )
            continue
        entry = AllowlistEntry(relative, rule_id, fingerprint, reason)
        if entry.key in seen:
            errors.append(f"entry {index} duplicates an earlier allowlist tuple")
            continue
        seen.add(entry.key)
        entries.append(entry)
    return entries, errors


def scan_report(
    root: Path,
    *,
    mode: str = "workspace",
    allowlist_path: Path | None = None,
) -> ScanReport:
    root = root.resolve()
    default_allowlist = root / DEFAULT_ALLOWLIST
    effective_allowlist = allowlist_path.resolve() if allowlist_path else default_allowlist
    entries, errors = _load_allowlist(effective_allowlist)
    if allowlist_path is not None and not effective_allowlist.exists():
        errors.append(f"explicit allowlist does not exist: {effective_allowlist.name}")
    findings = sorted(
        (
            finding
            for path in iter_files(root, mode=mode)
            for finding in _scan_file(root, path)
        ),
        key=lambda item: (item.path, item.line, item.rule_id, item.fingerprint),
    )
    allowed_keys = {entry.key for entry in entries}
    allowed = tuple(
        finding
        for finding in findings
        if (finding.path, finding.rule_id, finding.fingerprint) in allowed_keys
    )
    used_keys = {(finding.path, finding.rule_id, finding.fingerprint) for finding in allowed}
    remaining = tuple(
        finding
        for finding in findings
        if (finding.path, finding.rule_id, finding.fingerprint) not in allowed_keys
    )
    stale = tuple(entry for entry in entries if entry.key not in used_keys)
    return ScanReport(remaining, allowed, stale, tuple(errors))


def scan(
    root: Path,
    *,
    mode: str = "workspace",
    allowlist_path: Path | None = None,
) -> list[str]:
    """Compatibility wrapper returning redacted, human-readable problems."""
    return scan_report(
        Path(root),
        mode=mode,
        allowlist_path=allowlist_path,
    ).rendered_problems()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--tracked", action="store_true", help="scan only Git-tracked files")
    mode.add_argument(
        "--workspace",
        action="store_true",
        help="scan the full workspace, including ignored files (default)",
    )
    parser.add_argument("--allowlist", type=Path)
    args = parser.parse_args(argv)
    selected_mode = "tracked" if args.tracked else "workspace"
    try:
        report = scan_report(
            Path(args.root),
            mode=selected_mode,
            allowlist_path=args.allowlist,
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"secret scan failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    for problem in report.rendered_problems():
        print(problem, file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
