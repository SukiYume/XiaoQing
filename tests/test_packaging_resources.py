"""Regression tests for release-time plugin resources."""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = ROOT / "plugins"


def _pyproject() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)


def test_all_active_plugins_have_publishable_manifests() -> None:
    active_manifests = sorted(
        path.relative_to(PLUGINS_DIR).as_posix()
        for path in PLUGINS_DIR.glob("*/plugin.json")
        if not path.parent.name.endswith("_deprecated")
    )

    assert len(active_manifests) == 29
    assert _pyproject()["tool"]["setuptools"]["package-data"]["*"] == ["plugin.json"]


def test_pendo_static_and_builtin_resources_are_declared() -> None:
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]

    assert package_data["plugins.pendo.web"] == [
        "scriptable/*.js",
        "static/*.html",
        "static/*.svg",
        "static/css/*.css",
        "static/js/*.js",
        "static/js/*/*.js",
    ]
    assert package_data["plugins.pendo.web.services"] == ["assets/*.zip"]
    assert "assets/manifest.json" in package_data["plugins.dict"]
    assert package_data["plugins.color"] == ["color.json", "stellar_colors.txt"]
    assert package_data["plugins.xiaoqing_chat.config"] == ["xiaoqing_config.json"]
    assert package_data["plugins.xiaoqing_chat.media"] == ["qq_face_builtin_catalog.json"]

    assert (PLUGINS_DIR / "pendo" / "web" / "static" / "index.html").is_file()
    assert (PLUGINS_DIR / "pendo" / "web" / "static" / "js" / "app.js").is_file()
    assert (
        PLUGINS_DIR / "pendo" / "web" / "services" / "assets" / "demo_bundle.pendo.zip"
    ).is_file()


def test_runtime_and_secret_files_are_not_declared_as_package_data() -> None:
    serialized = repr(_pyproject()["tool"]["setuptools"]["package-data"])

    assert "minecraft" not in serialized
    assert "data/" not in serialized
    assert "cache" not in serialized
    assert "*.old" not in serialized
