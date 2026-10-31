"""Regression tests for release-time plugin resources."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath

from tests.helpers.git import run_git
from tests.helpers.paths import REPOSITORY_ROOT

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


def _pruned_source_dirs() -> list[PurePosixPath]:
    """MANIFEST.in 明确不发布的目录（训练、迁移、实验脚本等）。"""

    return [
        PurePosixPath(line.split(None, 1)[1].strip())
        for line in (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        if line.startswith("prune ")
    ]


def _source_packages() -> set[str]:
    """列出 core 和 plugins 下所有含 Python 源码、且不在 prune 名单里的目录。"""

    pruned             = _pruned_source_dirs()
    packages: set[str] = set()
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
        "core",
        "plugins",
    ).split("\0"):
        if not path or PurePosixPath(path).suffix != ".py":
            continue
        directory = PurePosixPath(path).parent
        if any(directory == item or item in directory.parents for item in pruned):
            continue
        packages.add(".".join(directory.parts))
    return packages


def test_every_source_package_is_declared_for_the_wheel() -> None:
    """packages 是显式列表：新增子包不写进去，wheel 里就没有它，安装后导入直接失败。"""

    assert set(_pyproject()["tool"]["setuptools"]["packages"]) == _source_packages()
