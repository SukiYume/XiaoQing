"""Regression tests for release-time plugin resources."""

from __future__ import annotations

from pathlib import Path

from tests.helpers.git import run_git
from tests.helpers.paths import REPOSITORY_ROOT

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib

ROOT             = REPOSITORY_ROOT
_SOURCE_SUFFIXES = {".py", ".pyi"}


def _pyproject() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)


def _is_plugin_document(path: Path) -> bool:
    """插件根目录下的 Markdown 是说明文档；包里更深处的 Markdown（如人设卡）是运行时资源。"""

    return path.suffix.lower() == ".md" and len(path.parts) == 3


def _tracked_plugin_assets() -> set[str]:
    """列出已跟踪及新增的非忽略插件资源，使提交前检查覆盖新插件。"""

    return {
        path
        for path in run_git(
            ROOT,
            "-c",
            "core.quotePath=false",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "plugins",
        ).split("\0")
        if path
        and Path(path).suffix.lower() not in _SOURCE_SUFFIXES
        and not _is_plugin_document(Path(path))
    }


def _declared_plugin_assets() -> set[str]:
    """按 setuptools 的真实包目录和 glob 规则展开 package-data。"""

    setuptools         = _pyproject()["tool"]["setuptools"]
    packages           = setuptools["packages"]
    package_data       = setuptools["package-data"]
    declared: set[str] = set()

    # `*` 规则作用于每个包；包专属规则再补充其静态文件和内置数据。
    for package in packages:
        package_dir = ROOT / package.replace(".", "/")
        patterns    = [*package_data.get("*", []), *package_data.get(package, [])]
        for pattern in patterns:
            declared.update(
                path.relative_to(ROOT).as_posix()
                for path in package_dir.glob(pattern)
                if path.is_file()
            )
    return declared


def test_every_tracked_plugin_asset_is_covered_by_package_data() -> None:
    """新增运行时资源必须同步进入 wheel/sdist 的声明，不能静默漏发。"""

    assert _declared_plugin_assets() == _tracked_plugin_assets()
