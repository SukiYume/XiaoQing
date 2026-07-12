"""Build and verify isolated XiaoQing wheel and sdist installations."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PLUGIN_COUNT = 29
MAX_ARCHIVE_MEMBERS = 50_000
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024


class ReleaseVerificationError(RuntimeError):
    """Raised when a Python release artifact fails a fail-closed gate."""


@dataclass(frozen=True)
class RuntimeInventory:
    files: tuple[str, ...]
    plugins: tuple[tuple[str, str], ...]
    resources: tuple[str, ...]

    @property
    def plugin_map(self) -> dict[str, str]:
        return dict(self.plugins)


@dataclass(frozen=True)
class ReleaseMetadata:
    schema_version: int
    project_name: str
    project_version: str
    plugin_count: int
    runtime_file_count: int
    resource_count: int
    wheel_filename: str
    sdist_filename: str


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _ordinary_file(path: Path, description: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseVerificationError(f"cannot stat {description}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseVerificationError(f"{description} must be an ordinary file")
    return path


def _project_document(repo: Path) -> dict[str, Any]:
    pyproject = _ordinary_file(repo / "pyproject.toml", "pyproject.toml")
    try:
        with pyproject.open("rb") as source:
            document = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseVerificationError("cannot parse pyproject.toml") from exc
    if not isinstance(document, dict):
        raise ReleaseVerificationError("pyproject.toml root must be a table")
    return document


def _project_identity(document: dict[str, Any]) -> tuple[str, str]:
    project = document.get("project")
    if not isinstance(project, dict):
        raise ReleaseVerificationError("pyproject.toml is missing [project]")
    name = str(project.get("name", "")).strip()
    version = str(project.get("version", "")).strip()
    if not name or not version:
        raise ReleaseVerificationError("project name/version must be non-empty")
    return name, version


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseVerificationError(f"command could not run: {command[0]}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-4000:]
        raise ReleaseVerificationError(
            f"command failed ({completed.returncode}): {' '.join(command[:4])}\n{detail}"
        )
    return completed


def _git_tracked_files(repo: Path) -> set[str]:
    completed = _run(
        ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--", "main.py", "core", "plugins"],
        cwd=repo.parent,
        timeout=60,
    )
    tracked = {raw for raw in completed.stdout.split("\0") if raw}
    if "main.py" not in tracked:
        raise ReleaseVerificationError("Git runtime inventory is missing main.py")
    return tracked


def _safe_relative_path(raw: str, *, description: str) -> str:
    if not raw or "\\" in raw or "\x00" in raw:
        raise ReleaseVerificationError(f"{description} contains an invalid path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseVerificationError(f"{description} contains an unsafe path: {raw!r}")
    if ":" in path.parts[0]:
        raise ReleaseVerificationError(f"{description} contains a drive path: {raw!r}")
    return path.as_posix()


def _manifest_plugins(repo: Path, tracked: set[str]) -> dict[str, str]:
    plugins: dict[str, str] = {}
    manifests = sorted(
        relative
        for relative in tracked
        if len(PurePosixPath(relative).parts) == 3
        and relative.startswith("plugins/")
        and relative.endswith("/plugin.json")
    )
    if len(manifests) != EXPECTED_PLUGIN_COUNT:
        raise ReleaseVerificationError(
            f"expected {EXPECTED_PLUGIN_COUNT} tracked plugin manifests, found {len(manifests)}"
        )
    for relative in manifests:
        path = _ordinary_file(repo / Path(*PurePosixPath(relative).parts), relative)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseVerificationError(f"cannot parse {relative}") from exc
        if not isinstance(payload, dict):
            raise ReleaseVerificationError(f"{relative} must contain a JSON object")
        plugin_name = PurePosixPath(relative).parts[1]
        if payload.get("name") != plugin_name:
            raise ReleaseVerificationError(f"{relative} name does not match its directory")
        entry = _safe_relative_path(str(payload.get("entry", "main.py")), description=relative)
        if not entry.endswith(".py"):
            raise ReleaseVerificationError(f"{relative} entry must be a Python file")
        entry_path = f"plugins/{plugin_name}/{entry}"
        if entry_path not in tracked:
            raise ReleaseVerificationError(f"{relative} entry is not Git-tracked: {entry}")
        plugins[plugin_name] = entry
    return plugins


def _package_directories(tracked: set[str]) -> set[PurePosixPath]:
    directories: set[PurePosixPath] = set()
    for relative in tracked:
        path = PurePosixPath(relative)
        if path.suffix == ".py" and path.parts[0] in {"core", "plugins"}:
            directories.add(path.parent)
    return directories


def _package_data_resources(
    repo: Path,
    document: dict[str, Any],
    tracked: set[str],
) -> set[str]:
    tool = document.get("tool")
    setuptools = tool.get("setuptools") if isinstance(tool, dict) else None
    package_data = setuptools.get("package-data") if isinstance(setuptools, dict) else None
    if not isinstance(package_data, dict):
        raise ReleaseVerificationError("pyproject.toml is missing [tool.setuptools.package-data]")

    package_directories = _package_directories(tracked)
    resources: set[str] = set()
    for raw_package, raw_patterns in package_data.items():
        if not isinstance(raw_package, str) or not isinstance(raw_patterns, list):
            raise ReleaseVerificationError("setuptools package-data entries must be arrays")
        if not all(isinstance(pattern, str) and pattern for pattern in raw_patterns):
            raise ReleaseVerificationError(f"invalid package-data patterns for {raw_package}")
        bases = (
            package_directories if raw_package == "*" else {PurePosixPath(*raw_package.split("."))}
        )
        for pattern in raw_patterns:
            matches: set[str] = set()
            for base in bases:
                base_path = repo / Path(*base.parts)
                if not base_path.is_dir():
                    continue
                for candidate in base_path.glob(pattern):
                    if not candidate.is_file() or candidate.is_symlink():
                        continue
                    relative = candidate.relative_to(repo).as_posix()
                    if relative in tracked:
                        matches.add(relative)
            if not matches:
                raise ReleaseVerificationError(
                    f"package-data pattern matched no tracked files: {raw_package}:{pattern}"
                )
            resources.update(matches)
    return resources


def build_runtime_inventory(repo: Path) -> RuntimeInventory:
    """Return the exact Git-backed runtime file contract for built artifacts."""
    repo = repo.resolve()
    document = _project_document(repo)
    tracked = _git_tracked_files(repo)
    plugins = _manifest_plugins(repo, tracked)
    python_files = {
        relative
        for relative in tracked
        if relative == "main.py"
        or (
            relative.endswith(".py")
            and (relative.startswith("core/") or relative.startswith("plugins/"))
        )
    }
    manifests = {f"plugins/{name}/plugin.json" for name in plugins}
    resources = _package_data_resources(repo, document, tracked) - manifests
    files = python_files | manifests | resources
    for relative in files:
        _ordinary_file(repo / Path(*PurePosixPath(relative).parts), relative)
    return RuntimeInventory(
        files=tuple(sorted(files)),
        plugins=tuple(sorted(plugins.items())),
        resources=tuple(sorted(resources)),
    )


def _archive_member_name(raw: str, *, directory: bool) -> str:
    value = raw.rstrip("/") if directory else raw
    return _safe_relative_path(value, description="release archive")


def _metadata_identity(payload: bytes, description: str) -> tuple[str, str]:
    if len(payload) > MAX_METADATA_BYTES:
        raise ReleaseVerificationError(f"{description} exceeds the metadata byte limit")
    message = BytesParser(policy=compat32).parsebytes(payload)
    names = message.get_all("Name", [])
    versions = message.get_all("Version", [])
    if len(names) != 1 or len(versions) != 1:
        raise ReleaseVerificationError(f"{description} must contain one Name and Version")
    return str(names[0]).strip(), str(versions[0]).strip()


def inspect_wheel(
    wheel: Path,
    *,
    project_name: str,
    project_version: str,
    inventory: RuntimeInventory,
) -> None:
    """Validate wheel identity, members, types, budgets, and exact app inventory."""
    _ordinary_file(wheel, "wheel")
    expected_distribution = _canonical_name(project_name).replace("-", "_")
    parts = wheel.name.removesuffix(".whl").split("-") if wheel.name.endswith(".whl") else []
    if len(parts) not in {5, 6} or parts[0] != expected_distribution or parts[1] != project_version:
        raise ReleaseVerificationError("wheel filename does not match pyproject.toml")
    seen: set[str] = set()
    files: set[str] = set()
    metadata_payloads: list[bytes] = []
    total_bytes = 0
    try:
        archive = zipfile.ZipFile(wheel, mode="r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseVerificationError("wheel is not a readable ZIP archive") from exc
    with archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ReleaseVerificationError("wheel exceeds the archive member limit")
        for member in members:
            directory = member.is_dir()
            name = _archive_member_name(member.filename, directory=directory)
            if name in seen:
                raise ReleaseVerificationError(f"wheel contains a duplicate member: {name}")
            seen.add(name)
            if member.flag_bits & 0x1:
                raise ReleaseVerificationError(f"wheel contains an encrypted member: {name}")
            mode = member.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode):
                raise ReleaseVerificationError(f"wheel contains a symbolic link: {name}")
            if directory:
                if file_type not in {0, stat.S_IFDIR}:
                    raise ReleaseVerificationError(f"wheel contains a special directory: {name}")
                continue
            if file_type not in {0, stat.S_IFREG}:
                raise ReleaseVerificationError(f"wheel contains a non-regular member: {name}")
            total_bytes += member.file_size
            if total_bytes > MAX_ARCHIVE_BYTES:
                raise ReleaseVerificationError("wheel exceeds the uncompressed byte limit")
            files.add(name)
            if name.endswith(".dist-info/METADATA"):
                metadata_payloads.append(archive.read(member))
    if len(metadata_payloads) != 1:
        raise ReleaseVerificationError("wheel must contain exactly one METADATA file")
    metadata_name, metadata_version = _metadata_identity(metadata_payloads[0], "wheel METADATA")
    if _canonical_name(metadata_name) != _canonical_name(project_name):
        raise ReleaseVerificationError("wheel METADATA Name does not match pyproject.toml")
    if metadata_version != project_version:
        raise ReleaseVerificationError("wheel METADATA Version does not match pyproject.toml")
    app_files = {
        name
        for name in files
        if name == "main.py" or name.startswith("core/") or name.startswith("plugins/")
    }
    expected = set(inventory.files)
    if app_files != expected:
        missing = sorted(expected - app_files)[:10]
        extra = sorted(app_files - expected)[:10]
        raise ReleaseVerificationError(
            f"wheel runtime inventory mismatch: missing={missing!r} extra={extra!r}"
        )


def inspect_sdist(
    sdist: Path,
    *,
    project_name: str,
    project_version: str,
    inventory: RuntimeInventory,
) -> None:
    """Validate sdist identity, safe members, and required runtime source inventory."""
    _ordinary_file(sdist, "sdist")
    expected_distribution = _canonical_name(project_name).replace("-", "_")
    if sdist.name != f"{expected_distribution}-{project_version}.tar.gz":
        raise ReleaseVerificationError("sdist filename does not match pyproject.toml")
    seen: set[str] = set()
    files: set[str] = set()
    metadata_payloads: list[bytes] = []
    roots: set[str] = set()
    total_bytes = 0
    try:
        archive = tarfile.open(sdist, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseVerificationError("sdist is not a readable tar.gz archive") from exc
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ReleaseVerificationError("sdist exceeds the archive member limit")
        for member in members:
            name = _archive_member_name(member.name, directory=member.isdir())
            if name in seen:
                raise ReleaseVerificationError(f"sdist contains a duplicate member: {name}")
            seen.add(name)
            roots.add(PurePosixPath(name).parts[0])
            if not (member.isdir() or member.isreg()):
                raise ReleaseVerificationError(f"sdist contains a non-regular member: {name}")
            if member.isdir():
                continue
            total_bytes += member.size
            if total_bytes > MAX_ARCHIVE_BYTES:
                raise ReleaseVerificationError("sdist exceeds the uncompressed byte limit")
            files.add(name)
            if PurePosixPath(name).name == "PKG-INFO" and len(PurePosixPath(name).parts) == 2:
                source = archive.extractfile(member)
                if source is None:
                    raise ReleaseVerificationError("sdist PKG-INFO has no payload")
                metadata_payloads.append(source.read(MAX_METADATA_BYTES + 1))
    if len(roots) != 1:
        raise ReleaseVerificationError("sdist must contain one top-level directory")
    if len(metadata_payloads) != 1:
        raise ReleaseVerificationError("sdist must contain exactly one root PKG-INFO")
    metadata_name, metadata_version = _metadata_identity(metadata_payloads[0], "sdist PKG-INFO")
    if _canonical_name(metadata_name) != _canonical_name(project_name):
        raise ReleaseVerificationError("sdist PKG-INFO Name does not match pyproject.toml")
    if metadata_version != project_version:
        raise ReleaseVerificationError("sdist PKG-INFO Version does not match pyproject.toml")
    stripped = {
        PurePosixPath(*PurePosixPath(name).parts[1:]).as_posix()
        for name in files
        if len(PurePosixPath(name).parts) > 1
    }
    missing = sorted(set(inventory.files) - stripped)
    if missing:
        raise ReleaseVerificationError(f"sdist is missing runtime files: {missing[:10]!r}")


def _find_artifacts(dist_dir: Path, project_name: str) -> tuple[Path, Path]:
    expected = _canonical_name(project_name).replace("-", "_")
    wheels = sorted(dist_dir.glob(f"{expected}-*.whl"))
    sdists = sorted(dist_dir.glob(f"{expected}-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseVerificationError(
            f"build must produce one wheel and one sdist, found {len(wheels)} and {len(sdists)}"
        )
    return _ordinary_file(wheels[0], "wheel"), _ordinary_file(sdists[0], "sdist")


def build_artifacts(repo: Path, work_dir: Path, project_name: str) -> tuple[Path, Path]:
    """Run PyPA build from a cwd outside the repository to avoid build/ shadowing."""
    dist_dir = work_dir / "dist"
    dist_dir.mkdir()
    build_cwd = work_dir / "build-cwd"
    build_cwd.mkdir()
    _run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(dist_dir),
            str(repo),
        ],
        cwd=build_cwd,
        timeout=900,
    )
    return _find_artifacts(dist_dir, project_name)


def _sanitized_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in list(environment):
        if key.upper() in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}:
            environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    return environment


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_console(venv: Path) -> Path:
    return venv / ("Scripts/xiaoqing.exe" if os.name == "nt" else "bin/xiaoqing")


_PROBE_SOURCE = r"""from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import sys
import sysconfig
import zipfile
from pathlib import Path


def fail(message: str) -> None:
    raise RuntimeError(message)


spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
source_root = Path(spec["source_root"]).resolve()
purelib = Path(sysconfig.get_path("purelib")).resolve()
cwd = Path.cwd().resolve()
try:
    cwd.relative_to(source_root)
except ValueError:
    pass
else:
    fail("probe cwd is inside the source repository")
for raw in sys.path:
    if not raw:
        continue
    candidate = Path(raw).resolve()
    if candidate == source_root:
        fail("source repository is present on sys.path")


def require_installed(path: Path, description: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(purelib)
    except ValueError as exc:
        raise RuntimeError(f"{description} was not loaded from venv site-packages: {resolved}") from exc
    return resolved


distribution = importlib.metadata.distribution("xiaoqing")
if distribution.version != spec["project_version"]:
    fail("installed distribution version mismatch")
installed_files = {str(item).replace("\\", "/") for item in distribution.files or ()}
generated_bytecode = {
    path
    for path in installed_files
    if "/__pycache__/" in path and path.endswith((".pyc", ".pyo"))
}
installed_runtime = {
    path
    for path in installed_files
    if path == "main.py" or path.startswith("core/") or path.startswith("plugins/")
} - generated_bytecode
expected_runtime = set(spec["runtime_files"])
if installed_runtime != expected_runtime:
    fail(
        f"installed runtime inventory mismatch: "
        f"missing={sorted(expected_runtime - installed_runtime)[:10]} "
        f"extra={sorted(installed_runtime - expected_runtime)[:10]}"
    )

for package_name in ("main", "core", "plugins"):
    module = importlib.import_module(package_name)
    module_file = getattr(module, "__file__", None)
    if not module_file:
        fail(f"{package_name} has no module file")
    require_installed(Path(module_file), package_name)

plugins_root = require_installed(purelib / "plugins", "plugins root")
manifest_paths = sorted(plugins_root.glob("*/plugin.json"))
if len(manifest_paths) != spec["expected_plugin_count"]:
    fail(f"installed plugin count mismatch: {len(manifest_paths)}")
expected_plugins = dict(spec["plugins"])
seen_plugins: dict[str, str] = {}
for manifest_path in manifest_paths:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    name = manifest_path.parent.name
    if payload.get("name") != name:
        fail(f"manifest name mismatch: {name}")
    entry = str(payload.get("entry", "main.py"))
    if expected_plugins.get(name) != entry:
        fail(f"manifest entry mismatch: {name}")
    entry_path = manifest_path.parent / Path(*entry.replace("\\", "/").split("/"))
    if not entry_path.is_file():
        fail(f"manifest entry missing: {name}/{entry}")
    module_suffix = entry.removesuffix(".py").replace("/", ".").replace("\\", ".")
    module = importlib.import_module(f"plugins.{name}.{module_suffix}")
    module_file = getattr(module, "__file__", None)
    if not module_file:
        fail(f"plugin entry has no module file: {name}")
    require_installed(Path(module_file), f"plugin {name}")
    seen_plugins[name] = entry
if seen_plugins != expected_plugins:
    fail("installed plugin names do not match source manifests")

for module_name, module in list(sys.modules.items()):
    if not (module_name == "main" or module_name == "core" or module_name.startswith("core.") or module_name == "plugins" or module_name.startswith("plugins.")):
        continue
    module_file = getattr(module, "__file__", None)
    if module_file:
        require_installed(Path(module_file), module_name)

for relative in spec["resources"]:
    path = require_installed(purelib / Path(*relative.split("/")), relative)
    payload = path.read_bytes()
    if not payload:
        fail(f"packaged resource is empty: {relative}")

for relative in (
    "plugins/arxiv_filter/config.json",
    "plugins/color/color.json",
    "plugins/dict/assets/manifest.json",
    "plugins/xiaoqing_chat/config/xiaoqing_config.json",
    "plugins/xiaoqing_chat/media/qq_face_builtin_catalog.json",
):
    value = json.loads((purelib / Path(*relative.split("/"))).read_text(encoding="utf-8"))
    if not isinstance(value, (dict, list)):
        fail(f"JSON resource has invalid root: {relative}")

stellar = (purelib / "plugins/color/stellar_colors.txt").read_text(encoding="utf-8")
if not stellar.strip():
    fail("stellar color resource is empty")

dict_root = purelib / "plugins/dict/assets"
dictionary_manifest = json.loads((dict_root / "manifest.json").read_text(encoding="utf-8"))
for record in dictionary_manifest.get("files", {}).values():
    filename = record.get("filename")
    expected_hash = record.get("sha256")
    if not isinstance(filename, str) or not isinstance(expected_hash, str):
        fail("dictionary manifest entry is invalid")
    actual_hash = hashlib.sha256((dict_root / filename).read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        fail(f"dictionary resource digest mismatch: {filename}")

pendo_root = purelib / "plugins/pendo/web"
if "<html" not in (pendo_root / "static/index.html").read_text(encoding="utf-8").lower():
    fail("Pendo index.html is not HTML")
if "<svg" not in (pendo_root / "static/favicon.svg").read_text(encoding="utf-8").lower():
    fail("Pendo favicon is not SVG")
with zipfile.ZipFile(pendo_root / "services/assets/demo_bundle.pendo.zip") as bundle:
    if bundle.testzip() is not None:
        fail("Pendo demo bundle failed CRC validation")

print(json.dumps({
    "plugin_count": len(seen_plugins),
    "runtime_file_count": len(installed_runtime),
    "resource_count": len(spec["resources"]),
}, sort_keys=True))
"""


def _write_probe_inputs(
    work_dir: Path,
    *,
    repo: Path,
    project_version: str,
    inventory: RuntimeInventory,
) -> tuple[Path, Path, Path]:
    probe_dir = work_dir / "probe"
    probe_dir.mkdir()
    probe_script = probe_dir / "artifact_probe.py"
    probe_script.write_text(_PROBE_SOURCE, encoding="utf-8", newline="\n")
    specification = {
        "source_root": str(repo.resolve()),
        "project_version": project_version,
        "expected_plugin_count": EXPECTED_PLUGIN_COUNT,
        "runtime_files": list(inventory.files),
        "plugins": [list(item) for item in inventory.plugins],
        "resources": list(inventory.resources),
    }
    spec_path = probe_dir / "spec.json"
    spec_path.write_text(
        json.dumps(specification, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
        newline="\n",
    )
    probe_cwd = probe_dir / "cwd"
    probe_cwd.mkdir()
    return probe_script, spec_path, probe_cwd


def verify_installed_artifact(
    artifact: Path,
    *,
    kind: str,
    work_dir: Path,
    probe_script: Path,
    spec_path: Path,
    probe_cwd: Path,
) -> dict[str, Any]:
    """Install one artifact in its own venv and run an isolated import/resource probe."""
    venv = work_dir / f"{kind}-venv"
    _run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
        cwd=work_dir,
        timeout=180,
    )
    python = _ordinary_file(_venv_python(venv), f"{kind} venv Python")
    install = [str(python), "-I", "-m", "pip", "install", "--no-deps"]
    if kind == "sdist":
        install.append("--no-build-isolation")
    install.append(str(artifact))
    environment = _sanitized_environment()
    _run(install, cwd=probe_cwd, env=environment, timeout=600)
    result = _run(
        [str(python), "-I", str(probe_script), str(spec_path)],
        cwd=probe_cwd,
        env=environment,
        timeout=600,
    )
    console = _ordinary_file(_venv_console(venv), f"{kind} console script")
    help_result = _run(
        [str(console), "--help"],
        cwd=probe_cwd,
        env=environment,
        timeout=120,
    )
    if "XiaoQing QQ Bot framework" not in help_result.stdout:
        raise ReleaseVerificationError(f"{kind} console help output is invalid")
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError(f"{kind} probe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ReleaseVerificationError(f"{kind} probe returned a non-object")
    return payload


def verify_release(repo: Path) -> ReleaseMetadata:
    """Build, inspect, install, and import both release artifact formats."""
    repo = repo.resolve()
    document = _project_document(repo)
    project_name, project_version = _project_identity(document)
    inventory = build_runtime_inventory(repo)
    with tempfile.TemporaryDirectory(prefix="xiaoqing-python-release-") as raw:
        work_dir = Path(raw)
        wheel, sdist = build_artifacts(repo, work_dir, project_name)
        inspect_wheel(
            wheel,
            project_name=project_name,
            project_version=project_version,
            inventory=inventory,
        )
        inspect_sdist(
            sdist,
            project_name=project_name,
            project_version=project_version,
            inventory=inventory,
        )
        probe_script, spec_path, probe_cwd = _write_probe_inputs(
            work_dir,
            repo=repo,
            project_version=project_version,
            inventory=inventory,
        )
        wheel_probe = verify_installed_artifact(
            wheel,
            kind="wheel",
            work_dir=work_dir,
            probe_script=probe_script,
            spec_path=spec_path,
            probe_cwd=probe_cwd,
        )
        sdist_probe = verify_installed_artifact(
            sdist,
            kind="sdist",
            work_dir=work_dir,
            probe_script=probe_script,
            spec_path=spec_path,
            probe_cwd=probe_cwd,
        )
        if wheel_probe != sdist_probe:
            raise ReleaseVerificationError("wheel and sdist probes reported different results")
        return ReleaseMetadata(
            schema_version=1,
            project_name=project_name,
            project_version=project_version,
            plugin_count=len(inventory.plugins),
            runtime_file_count=len(inventory.files),
            resource_count=len(inventory.resources),
            wheel_filename=wheel.name,
            sdist_filename=sdist.name,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        metadata = verify_release(args.repo)
    except (ReleaseVerificationError, OSError, ValueError) as exc:
        print(f"verify_python_release.py: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(metadata), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
