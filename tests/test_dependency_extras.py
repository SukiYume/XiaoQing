"""Regression tests for optional feature dependency contracts."""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


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

    assert {"fastapi", "uvicorn", "pyjwt", "python-dateutil"} <= base


def test_feature_extras_cover_their_runtime_imports() -> None:
    extras = _project()["optional-dependencies"]

    assert {"fastapi", "uvicorn", "pyjwt"} <= _names(extras["web"])
    assert {"jupyter-client", "ipykernel"} <= _names(extras["jupyter"])
    assert {
        "torch",
        "transformers",
        "pandas",
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


def test_optional_plugins_publish_actionable_install_hints() -> None:
    jupyter = (ROOT / "plugins" / "jupyter" / "main.py").read_text(encoding="utf-8")
    arxiv = (ROOT / "plugins" / "arxiv_filter" / "main.py").read_text(encoding="utf-8")

    assert 'xiaoqing[jupyter]' in jupyter
    assert 'xiaoqing[arxiv-ml]' in arxiv
