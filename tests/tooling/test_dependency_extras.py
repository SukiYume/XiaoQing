"""Plugin dependency declarations match the packaging extras and install hints."""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from types import SimpleNamespace

import pytest

from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


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

    assert {"miservice", "qrcode"} <= _names(extras["xiaoai"])

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


def test_every_plugin_manifest_declares_well_formed_dependencies() -> None:
    """每个插件的 dependencies：描述非空、required 是布尔、同一插件内不重名。"""

    manifests = sorted((ROOT / "plugins").glob("*/plugin.json"))
    assert manifests
    for manifest_path in manifests:
        plugin_name = manifest_path.parent.name
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        dependencies = manifest.get("dependencies", [])
        assert isinstance(dependencies, list), plugin_name
        seen: set[str] = set()
        for dependency in dependencies:
            assert isinstance(dependency, dict), plugin_name
            name        = dependency.get("name")
            description = dependency.get("description")
            assert isinstance(name, str) and name, plugin_name
            assert isinstance(description, str) and description.strip(), f"{plugin_name}:{name}"
            assert type(dependency.get("required")) is bool, f"{plugin_name}:{name}"
            assert name not in seen, f"{plugin_name}:{name}"
            seen.add(name)


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
    from plugins.jupyter import jupyter_manager
    from plugins.jupyter import main as jupyter

    # 可选依赖真的导入失败：jupyter_client 与推理模块（torch 等）都不可用。
    monkeypatch.setitem(sys.modules, "jupyter_client", None)
    monkeypatch.setattr(jupyter_manager, "JUPYTER_AVAILABLE", False)
    monkeypatch.setattr(jupyter_manager, "IMPORT_ERROR", None)
    monkeypatch.setattr(jupyter_manager, "KernelManager", None)
    monkeypatch.setattr(jupyter, "_DEPENDENCY_PROBED", False)
    monkeypatch.setitem(sys.modules, "plugins.arxiv_filter.inference.runner", None)
    monkeypatch.setattr(arxiv, "_inference_func", None)

    jupyter_result = asyncio.run(jupyter.handle("jupyter", "print(1)", {}, SimpleNamespace()))
    arxiv_result = asyncio.run(arxiv._run_filter(SimpleNamespace(config={})))

    assert 'pip install "xiaoqing[jupyter]"' in _text(jupyter_result)
    assert 'pip install "xiaoqing[arxiv-ml]"' in _text(arxiv_result)
