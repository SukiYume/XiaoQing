"""Build a minimal, secret-scanned Docker context from one XiaoQing wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path, PurePosixPath

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.scan_workspace_secrets import scan_report  # noqa: E402

MAX_WHEEL_FILES = 50_000
MAX_WHEEL_BYTES = 2 * 1024 * 1024 * 1024
MAX_WHEEL_ARCHIVE_BYTES = 512 * 1024 * 1024

CONTEXT_SOURCES = {
    ".dockerignore": ".dockerignore",
    "Dockerfile": "Dockerfile",
    "config/config.json.example": "config/config.json.example",
    "config/secrets.json.example": "config/secrets.json.example",
    "requirements/python-3.13-runtime.lock": "requirements/python-3.13-runtime.lock",
}
WHEEL_CONTEXT_PATH = "artifacts/xiaoqing.whl"


class DockerContextError(RuntimeError):
    """Raised when a Docker context cannot be proven minimal and safe."""


@dataclass(frozen=True)
class ContextMetadata:
    schema_version: int
    project_name: str
    project_version: str
    wheel_filename: str
    wheel_sha256: str
    file_count: int


def _project_identity(repo: Path) -> tuple[str, str]:
    pyproject = _ordinary_file(repo / "pyproject.toml", description="pyproject.toml")
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        name = str(project["name"]).strip()
        version = str(project["version"]).strip()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise DockerContextError("cannot read project name/version from pyproject.toml") from exc
    if not name or not version:
        raise DockerContextError("pyproject.toml project name/version must be non-empty")
    return name, version


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _ordinary_file(path: Path, *, description: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DockerContextError(f"cannot stat {description}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DockerContextError(f"{description} must be one ordinary file")
    return path


def _find_wheel(dist_dir: Path, project_name: str) -> Path:
    try:
        info = dist_dir.lstat()
    except OSError as exc:
        raise DockerContextError("cannot stat dist directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DockerContextError("dist directory must be one ordinary directory")

    prefix = _canonical_name(project_name).replace("-", "_") + "-"
    try:
        with os.scandir(dist_dir) as entries:
            candidates = sorted(
                (
                    entry
                    for entry in entries
                    if entry.name.startswith(prefix) and entry.name.endswith(".whl")
                ),
                key=lambda entry: entry.name,
            )
    except OSError as exc:
        raise DockerContextError("cannot enumerate dist directory") from exc
    if len(candidates) != 1:
        raise DockerContextError(f"dist directory must contain exactly one {prefix}*.whl candidate")
    candidate = candidates[0]
    try:
        if candidate.is_symlink() or not candidate.is_file(follow_symlinks=False):
            raise DockerContextError("XiaoQing wheel candidate must be one ordinary file")
        size = candidate.stat(follow_symlinks=False).st_size
    except OSError as exc:
        raise DockerContextError("cannot stat XiaoQing wheel candidate") from exc
    if size > MAX_WHEEL_ARCHIVE_BYTES:
        raise DockerContextError(f"wheel archive exceeds the {MAX_WHEEL_ARCHIVE_BYTES} byte limit")
    return Path(candidate.path)


def _filename_version(wheel: Path, project_name: str) -> str:
    if not wheel.name.endswith(".whl"):
        raise DockerContextError("wheel filename must end in .whl")
    parts = wheel.name[:-4].split("-")
    if len(parts) not in {5, 6}:
        raise DockerContextError("wheel filename does not use the supported wheel tag format")
    distribution, version = parts[0], parts[1]
    expected_distribution = _canonical_name(project_name).replace("-", "_")
    if distribution != expected_distribution or not version:
        raise DockerContextError("wheel filename distribution does not match pyproject.toml")
    if any(not value for value in parts[-3:]):
        raise DockerContextError("wheel filename contains an empty compatibility tag")
    return version


def _safe_member_name(raw_name: str) -> tuple[str, bool]:
    if not raw_name or "\\" in raw_name or "\x00" in raw_name:
        raise DockerContextError("wheel contains an invalid member name")
    directory = raw_name.endswith("/")
    stripped = raw_name.rstrip("/")
    if not stripped:
        raise DockerContextError("wheel contains an invalid root member")
    path = PurePosixPath(stripped)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DockerContextError(f"wheel member escapes extraction root: {raw_name!r}")
    if ":" in path.parts[0]:
        raise DockerContextError(f"wheel member uses an absolute drive path: {raw_name!r}")
    return path.as_posix(), directory


def _validate_member_type(member: zipfile.ZipInfo, *, directory: bool, name: str) -> None:
    if member.flag_bits & 0x1:
        raise DockerContextError(f"wheel contains an encrypted member: {name}")
    mode = member.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if stat.S_ISLNK(mode):
        raise DockerContextError(f"wheel contains a symbolic link: {name}")
    if directory:
        if file_type not in {0, stat.S_IFDIR}:
            raise DockerContextError(f"wheel directory has a non-directory type: {name}")
    elif file_type not in {0, stat.S_IFREG}:
        raise DockerContextError(f"wheel contains a non-regular member: {name}")


def _check_member_collision(
    seen: dict[str, bool],
    *,
    name: str,
    directory: bool,
) -> None:
    if name in seen:
        raise DockerContextError(f"wheel contains a duplicate member: {name}")
    parts = PurePosixPath(name).parts
    for index in range(1, len(parts)):
        ancestor = PurePosixPath(*parts[:index]).as_posix()
        if ancestor in seen and not seen[ancestor]:
            raise DockerContextError(f"wheel member has a file as its parent: {name}")
    if not directory and any(existing.startswith(f"{name}/") for existing in seen):
        raise DockerContextError(f"wheel file collides with an existing directory: {name}")
    seen[name] = directory


def _extract_wheel(wheel: Path, destination: Path) -> Path:
    destination_root = destination.resolve()
    seen: dict[str, bool] = {}
    member_count = 0
    declared_bytes = 0
    copied_bytes = 0
    metadata_paths: list[Path] = []
    try:
        archive = zipfile.ZipFile(wheel, mode="r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise DockerContextError("wheel is not a readable ZIP archive") from exc
    with archive:
        for member in archive.infolist():
            member_count += 1
            if member_count > MAX_WHEEL_FILES:
                raise DockerContextError(f"wheel exceeds the {MAX_WHEEL_FILES} file limit")
            name, directory = _safe_member_name(member.filename)
            _validate_member_type(member, directory=directory, name=name)
            _check_member_collision(seen, name=name, directory=directory)
            target = (destination / Path(*PurePosixPath(name).parts)).resolve()
            try:
                target.relative_to(destination_root)
            except ValueError as exc:
                raise DockerContextError(f"wheel member escapes extraction root: {name}") from exc
            if directory:
                target.mkdir(parents=True, exist_ok=True)
                continue

            declared_bytes += member.file_size
            if declared_bytes > MAX_WHEEL_BYTES:
                raise DockerContextError(f"wheel exceeds the {MAX_WHEEL_BYTES} byte limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                source = archive.open(member, mode="r")
                with source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        copied_bytes += len(chunk)
                        if copied_bytes > MAX_WHEEL_BYTES:
                            raise DockerContextError(
                                f"wheel exceeds the {MAX_WHEEL_BYTES} byte limit"
                            )
                        output.write(chunk)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise DockerContextError(f"cannot safely extract wheel member: {name}") from exc
            if name.endswith(".dist-info/METADATA"):
                metadata_paths.append(target)
    if copied_bytes != declared_bytes:
        raise DockerContextError("wheel extracted byte count does not match its directory")
    if len(metadata_paths) != 1:
        raise DockerContextError("wheel must contain exactly one .dist-info/METADATA file")
    return metadata_paths[0]


def _metadata_identity(metadata_path: Path) -> tuple[str, str]:
    try:
        message = BytesParser(policy=compat32).parsebytes(metadata_path.read_bytes())
    except OSError as exc:
        raise DockerContextError("cannot read wheel METADATA") from exc
    names = message.get_all("Name", [])
    versions = message.get_all("Version", [])
    if len(names) != 1 or len(versions) != 1:
        raise DockerContextError("wheel METADATA must contain exactly one Name and Version")
    return str(names[0]).strip(), str(versions[0]).strip()


def _scan_or_raise(root: Path, *, description: str) -> None:
    # The artifact being inspected is untrusted. In particular, never let a
    # wheel-provided .secret-scan-allowlist.json suppress its own findings.
    report = scan_report(root, mode="workspace", use_default_allowlist=False)
    if not report.ok:
        detail = "; ".join(report.rendered_problems()[:10])
        raise DockerContextError(f"{description} failed secret scan: {detail}")


def _copy_context_source(repo: Path, relative: str, context_dir: Path) -> None:
    source = _ordinary_file(repo / Path(*PurePosixPath(relative).parts), description=relative)
    target = context_dir / Path(*PurePosixPath(relative).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_context(repo: Path, dist_dir: Path, context_dir: Path) -> ContextMetadata:
    """Validate one wheel and create an exact Docker build context."""
    repo = repo.resolve()
    # Preserve the final path components so lstat checks below cannot be
    # bypassed with a symlink that Path.resolve() would silently follow.
    dist_dir = dist_dir.absolute()
    context_dir = context_dir.absolute()
    try:
        context_dir.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise DockerContextError("cannot stat context directory") from exc
    else:
        raise DockerContextError("context directory must not already exist")
    project_name, project_version = _project_identity(repo)
    wheel = _find_wheel(dist_dir, project_name)
    filename_version = _filename_version(wheel, project_name)
    if filename_version != project_version:
        raise DockerContextError("wheel filename version does not match pyproject.toml")

    with tempfile.TemporaryDirectory(prefix="xiaoqing-wheel-inspect-") as temporary:
        unpacked = Path(temporary)
        metadata_path = _extract_wheel(wheel, unpacked)
        metadata_name, metadata_version = _metadata_identity(metadata_path)
        if _canonical_name(metadata_name) != _canonical_name(project_name):
            raise DockerContextError("wheel METADATA Name does not match pyproject.toml")
        if metadata_version != project_version:
            raise DockerContextError("wheel METADATA Version does not match pyproject.toml")
        _scan_or_raise(unpacked, description="unpacked wheel")

    try:
        context_dir.mkdir(parents=False)
    except OSError as exc:
        raise DockerContextError("cannot create context directory") from exc
    try:
        for relative in CONTEXT_SOURCES:
            _copy_context_source(repo, relative, context_dir)
        wheel_target = context_dir / Path(*PurePosixPath(WHEEL_CONTEXT_PATH).parts)
        wheel_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(wheel, wheel_target)
        _scan_or_raise(context_dir, description="Docker context")
        file_count = sum(1 for path in context_dir.rglob("*") if path.is_file())
        return ContextMetadata(
            schema_version=1,
            project_name=project_name,
            project_version=project_version,
            wheel_filename=wheel.name,
            wheel_sha256=_sha256(wheel_target),
            file_count=file_count,
        )
    except Exception:
        shutil.rmtree(context_dir, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--context-dir", type=Path, required=True)
    parser.add_argument("--metadata-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        metadata = build_context(args.repo, args.dist_dir, args.context_dir)
    except DockerContextError as exc:
        print(f"build_docker_context.py: {exc}", file=sys.stderr)
        return 1
    payload = json.dumps(asdict(metadata), sort_keys=True, separators=(",", ":"))
    if args.metadata_out:
        args.metadata_out.write_text(f"{payload}\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
