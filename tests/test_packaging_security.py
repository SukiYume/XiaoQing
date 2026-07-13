from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib

from scripts.verify_python_release import EXPECTED_RUNTIME_PACKAGES

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_setuptools_uses_the_exact_runtime_package_allowlist() -> None:
    setuptools = _pyproject()["tool"]["setuptools"]
    packages = tuple(setuptools["packages"])

    assert packages == EXPECTED_RUNTIME_PACKAGES
    assert len(packages) == 53
    assert len(set(packages)) == len(packages)
    assert "find" not in packages
    assert {"plugins.apod", "plugins.pendo", "plugins.url_parser"} <= set(packages)
    assert not any("train_model" in package for package in packages)
    assert "plugins.pendo.scripts" not in packages
    assert "plugins.pendo.web.migrations" not in packages
    assert "plugins.xiaoqing_chat.experiments" not in packages

    for package in packages:
        assert (ROOT / Path(*package.split("."))).is_dir()


def test_manifest_does_not_recursively_collect_repository_python_files() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "recursive-include core *.py" not in manifest
    assert "recursive-include plugins *.py" not in manifest
    for path in (
        "plugins/arxiv_filter/train_model",
        "plugins/pendo/scripts",
        "plugins/pendo/web/migrations",
        "plugins/xiaoqing_chat/experiments",
        "tests",
        "scripts",
        "release",
    ):
        assert f"prune {path}" in manifest
