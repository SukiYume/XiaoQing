"""Regression tests for optional feature dependency contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]

_REQUIRED_PLUGIN_DEPENDENCIES = {
    "adnmb": {"PIL", "aiohttp"},
    "ads_paper": {"aiohttp"},
    "apod": {"PIL", "bs4"},
    "earthquake": {"PIL", "bs4", "requests"},
    "github": {"bs4"},
    "pendo": {"dateutil", "fastapi", "jwt", "pydantic", "starlette", "uvicorn"},
    "twitter": {"PIL"},
    "url_parser": {"PIL", "bs4"},
    "voice": {"aiohttp"},
    "wolframalpha": {"aiohttp"},
    "xiaoqing_chat": {"aiohttp", "numpy", "pydantic"},
}

_OPTIONAL_PLUGIN_DEPENDENCIES = {
    "arxiv_filter": {
        "bs4",
        "joblib",
        "numpy",
        "pandas",
        "requests",
        "sentence_transformers",
        "sklearn",
        "torch",
        "transformers",
        "urllib3",
    },
    "astro_tools": {"astropy", "scipy"},
    "codex": {"PIL"},
    "color": {"matplotlib", "numpy"},
    "jupyter": {"ipykernel", "jupyter_client"},
    "qingssh": {"paramiko"},
    "xiaoqing_chat": {"PIL"},
}


def _project() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)["project"]


def _names(requirements: list[str]) -> set[str]:
    return {
        requirement.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].lower()
        for requirement in requirements
    }


def test_default_on_pendo_web_dependencies_are_in_base_install() -> None:
    base = _names(_project()["dependencies"])

    assert {"fastapi", "starlette", "uvicorn", "pyjwt", "python-dateutil"} <= base


def test_feature_extras_cover_their_runtime_imports() -> None:
    extras = _project()["optional-dependencies"]

    assert {"fastapi", "starlette", "uvicorn", "pyjwt"} <= _names(extras["web"])
    assert {"jupyter-client", "ipykernel"} <= _names(extras["jupyter"])
    assert {
        "torch",
        "transformers",
        "pandas",
        "requests",
        "beautifulsoup4",
        "urllib3",
        "numpy",
        "feedparser",
        "joblib",
        "scikit-learn",
        "sentence-transformers",
        "tqdm",
    } <= _names(extras["arxiv-ml"])

    all_target = extras["all"][0]
    assert "web" in all_target
    assert "jupyter" in all_target
    assert "arxiv-ml" in all_target


def test_general_plugin_and_astro_extras_have_no_orphaned_legacy_packages() -> None:
    extras = _project()["optional-dependencies"]

    assert _names(extras["plugins"]) == {
        "requests",
        "beautifulsoup4",
        "pillow",
        "numpy",
        "matplotlib",
        "python-dateutil",
    }
    assert _names(extras["astro"]) == {"astropy", "scipy"}
    requirement_lines = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    requirement_names = _names(requirement_lines)
    assert {"pandas", "scipy", "scikit-learn"} <= requirement_names
    assert "astroquery" not in requirement_names


def test_every_plugin_manifest_matches_its_runtime_dependency_contract() -> None:
    """逐个插件核对必需/可选 import，防止 preflight 晚于入口导入才失败。"""

    manifests = sorted((ROOT / "plugins").glob("*/plugin.json"))
    assert manifests
    for manifest_path in manifests:
        plugin_name = manifest_path.parent.name
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        dependencies = manifest.get("dependencies", [])
        assert isinstance(dependencies, list), plugin_name
        actual: dict[str, bool] = {}
        for dependency in dependencies:
            assert isinstance(dependency, dict), plugin_name
            name = dependency.get("name")
            description = dependency.get("description")
            assert isinstance(name, str) and name, plugin_name
            assert isinstance(description, str) and description.strip(), f"{plugin_name}:{name}"
            assert type(dependency.get("required")) is bool, f"{plugin_name}:{name}"
            assert name not in actual, f"{plugin_name}:{name}"
            actual[name] = dependency["required"]

        expected_required = _REQUIRED_PLUGIN_DEPENDENCIES.get(plugin_name, set())
        expected_optional = _OPTIONAL_PLUGIN_DEPENDENCIES.get(plugin_name, set())
        assert set(actual) == expected_required | expected_optional, plugin_name
        assert {name for name, required in actual.items() if required} == expected_required
        assert {name for name, required in actual.items() if not required} == expected_optional


def _text(response: list[dict[str, object]]) -> str:
    return "".join(
        str(segment.get("data", {}).get("text", ""))
        for segment in response
        if isinstance(segment, dict) and isinstance(segment.get("data"), dict)
    )


def test_optional_plugins_return_actionable_install_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.arxiv_filter import main as arxiv
    from plugins.jupyter import main as jupyter

    monkeypatch.setattr(jupyter, "_dependencies_available", lambda: False)
    jupyter_result = asyncio.run(jupyter.handle("jupyter", "print(1)", {}, SimpleNamespace()))

    monkeypatch.setattr(arxiv, "_load_inference", lambda **_kwargs: None)
    arxiv_result = asyncio.run(arxiv._run_filter(SimpleNamespace(config={})))

    assert 'pip install "xiaoqing[jupyter]"' in _text(jupyter_result)
    assert 'pip install "xiaoqing[arxiv-ml]"' in _text(arxiv_result)
