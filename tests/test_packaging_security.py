from __future__ import annotations

from pathlib import Path

from setuptools import find_namespace_packages

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def _package_discovery_config() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    return pyproject["tool"]["setuptools"]["packages"]["find"]


def test_setuptools_discovery_excludes_every_deprecated_plugin() -> None:
    config = _package_discovery_config()
    packages = find_namespace_packages(
        where=str(ROOT),
        include=list(config["include"]),
        exclude=list(config["exclude"]),
    )

    assert "core" in packages
    assert "plugins.codex" in packages
    assert not [package for package in packages if "deprecated" in package]
