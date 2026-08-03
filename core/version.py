"""Resolve the runtime version from package/build metadata without duplication."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _source_tree_version() -> str | None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - exercised on CPython 3.10
        import tomli as tomllib

    with pyproject.open("rb") as stream:
        project = tomllib.load(stream).get("project")
    project_version = project.get("version") if isinstance(project, dict) else None
    if type(project_version) is not str or not project_version.strip():
        raise RuntimeError("pyproject.toml has no valid project.version")
    return project_version


def get_runtime_version() -> str:
    """Return the source-tree version, or installed wheel metadata outside a checkout."""

    source_version = _source_tree_version()
    if source_version is not None:
        return source_version
    try:
        return version("xiaoqing")
    except PackageNotFoundError as exc:
        raise RuntimeError("XiaoQing version metadata is unavailable") from exc


VERSION = get_runtime_version()


__all__ = ["VERSION", "get_runtime_version"]
