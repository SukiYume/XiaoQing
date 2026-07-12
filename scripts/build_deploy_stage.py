"""Build a secret-scanned deployment stage from one immutable Git commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.scan_workspace_secrets import scan_report  # noqa: E402

MANIFEST_VERSION = "# xiaoqing-deploy-manifest-v1"
DEFAULT_MANIFEST = "deploy/runtime-paths.txt"
MAX_STAGE_FILES = 50_000
MAX_STAGE_BYTES = 2 * 1024 * 1024 * 1024
_HEX_COMMIT = re.compile(r"[0-9a-f]{40}")


class StagingError(RuntimeError):
    """Raised when a deployment stage cannot be proven safe and reproducible."""


@dataclass(frozen=True)
class StageMetadata:
    schema_version: int
    commit: str
    manifest_path: str
    manifest_sha256: str
    tree_sha256: str
    file_count: int


def _git(repo: Path, *args: str, text: bool = False) -> bytes | str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=text,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise StagingError(f"git command failed: {args[0] if args else 'unknown'}") from exc
    return result.stdout


def resolve_commit(repo: Path, ref: str) -> str:
    """Resolve a caller-provided ref once and return its immutable commit ID."""
    value = str(
        _git(
            repo,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{ref}^{{commit}}",
            text=True,
        )
    ).strip()
    if _HEX_COMMIT.fullmatch(value) is None:
        raise StagingError("git ref did not resolve to one full commit ID")
    return value


def _safe_repo_path(raw: str) -> str:
    if not raw or "\\" in raw:
        raise StagingError("manifest paths must be non-empty POSIX paths")
    if re.fullmatch(r"[A-Za-z0-9._/][A-Za-z0-9._/-]*", raw) is None or raw.startswith("-"):
        raise StagingError(f"manifest path contains unsafe characters: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise StagingError(f"unsafe manifest path: {raw!r}")
    normalized = path.as_posix()
    if normalized == ".git" or normalized.startswith(".git/"):
        raise StagingError("the Git metadata directory cannot be deployed")
    return normalized


def parse_manifest(payload: bytes) -> tuple[str, ...]:
    """Parse the versioned manifest and reject ambiguous or unsafe entries."""
    try:
        lines = payload.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise StagingError("deployment manifest must be UTF-8") from exc
    if not lines or lines[0] != MANIFEST_VERSION:
        raise StagingError(f"deployment manifest must start with {MANIFEST_VERSION!r}")
    entries = tuple(
        _safe_repo_path(line.strip())
        for line in lines[1:]
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not entries:
        raise StagingError("deployment manifest contains no runtime paths")
    if len(entries) != len(set(entries)):
        raise StagingError("deployment manifest contains duplicate paths")
    if entries != tuple(sorted(entries)):
        raise StagingError("deployment manifest paths must be sorted")
    return entries


def _member_is_covered(name: str, entries: tuple[str, ...], *, directory: bool) -> bool:
    if any(name == entry or name.startswith(f"{entry}/") for entry in entries):
        return True
    if directory:
        prefix = f"{name.rstrip('/')}/"
        return any(entry.startswith(prefix) for entry in entries)
    return False


def _safe_member_name(raw_name: str) -> str:
    if not raw_name or "\\" in raw_name:
        raise StagingError("archive contains an invalid member name")
    path = PurePosixPath(raw_name.rstrip("/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise StagingError(f"archive member escapes the stage: {raw_name!r}")
    return path.as_posix()


def extract_archive(
    archive_path: Path,
    stage_dir: Path,
    entries: tuple[str, ...],
) -> None:
    """Extract only ordinary files/directories without using tarfile.extract()."""
    seen: set[str] = set()
    file_count = 0
    total_bytes = 0
    stage_root = stage_dir.resolve()
    with tarfile.open(archive_path, mode="r:") as archive:
        for member in archive:
            name = _safe_member_name(member.name)
            if name in seen:
                raise StagingError(f"archive contains duplicate member: {name}")
            seen.add(name)
            if not (member.isdir() or member.isreg()):
                raise StagingError(f"archive contains a non-regular member: {name}")
            if not _member_is_covered(name, entries, directory=member.isdir()):
                raise StagingError(f"archive returned a path outside the manifest: {name}")
            target = (stage_dir / Path(*PurePosixPath(name).parts)).resolve()
            try:
                target.relative_to(stage_root)
            except ValueError as exc:
                raise StagingError(f"archive member escapes the stage: {name}") from exc
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            file_count += 1
            total_bytes += int(member.size)
            if file_count > MAX_STAGE_FILES:
                raise StagingError(f"archive exceeds the {MAX_STAGE_FILES} file limit")
            if total_bytes > MAX_STAGE_BYTES:
                raise StagingError(f"archive exceeds the {MAX_STAGE_BYTES} byte limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise StagingError(f"archive file has no payload: {name}")
            with source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            os.chmod(target, member.mode & 0o777)


def _tree_digest(stage_dir: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    files = sorted(path for path in stage_dir.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(stage_dir).as_posix().encode("utf-8")
        file_digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                file_digest.update(chunk)
        mode = stat.S_IMODE(path.stat().st_mode)
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(mode.to_bytes(4, "big"))
        digest.update(size.to_bytes(8, "big"))
        digest.update(file_digest.digest())
    return digest.hexdigest(), len(files)


def build_stage(
    repo: Path,
    ref: str,
    stage_dir: Path,
    *,
    manifest_path: str = DEFAULT_MANIFEST,
) -> StageMetadata:
    """Create and validate an immutable deployment stage."""
    repo = repo.resolve()
    stage_dir = stage_dir.resolve()
    if stage_dir.exists():
        raise StagingError("stage directory must not already exist")
    commit = resolve_commit(repo, ref)
    safe_manifest_path = _safe_repo_path(manifest_path)
    manifest_payload = _git(repo, "show", f"{commit}:{safe_manifest_path}")
    if not isinstance(manifest_payload, bytes):  # pragma: no cover - defensive typing guard
        raise StagingError("git returned non-binary manifest data")
    entries = parse_manifest(manifest_payload)
    for entry in entries:
        _git(repo, "cat-file", "-e", f"{commit}:{entry}")

    stage_dir.mkdir(parents=True)
    try:
        with tempfile.NamedTemporaryFile(prefix="xiaoqing-deploy-", suffix=".tar", delete=False) as tmp:
            archive_path = Path(tmp.name)
        try:
            with archive_path.open("wb") as archive_output:
                try:
                    subprocess.run(
                        ["git", "-C", str(repo), "archive", "--format=tar", commit, "--", *entries],
                        check=True,
                        stdout=archive_output,
                        stderr=subprocess.PIPE,
                    )
                except (OSError, subprocess.CalledProcessError) as exc:
                    raise StagingError("git archive failed") from exc
            extract_archive(archive_path, stage_dir, entries)
        finally:
            archive_path.unlink(missing_ok=True)

        report = scan_report(stage_dir, mode="workspace")
        if not report.ok:
            detail = "; ".join(report.rendered_problems()[:10])
            raise StagingError(f"deployment stage failed secret scan: {detail}")
        tree_sha256, file_count = _tree_digest(stage_dir)
        return StageMetadata(
            schema_version=1,
            commit=commit,
            manifest_path=safe_manifest_path,
            manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
            tree_sha256=tree_sha256,
            file_count=file_count,
        )
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--metadata-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        metadata = build_stage(
            args.repo,
            args.ref,
            args.stage_dir,
            manifest_path=args.manifest,
        )
    except StagingError as exc:
        print(f"build_deploy_stage.py: {exc}", file=sys.stderr)
        return 1
    payload = json.dumps(asdict(metadata), sort_keys=True, separators=(",", ":"))
    if args.metadata_out:
        args.metadata_out.write_text(f"{payload}\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
