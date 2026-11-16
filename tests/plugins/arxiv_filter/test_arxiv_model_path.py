# 验证 arXiv 模型路径解析和允许的文件边界。
from __future__ import annotations

from pathlib import Path

from plugins.arxiv_filter.inference import shared
from tests.helpers.arxiv_filter_test_support import use_plugin_config

# 相对路径按插件目录解析；config.json、模型目录都在这个目录里。
_PLUGIN_DIR = Path(shared.__file__).resolve().parent.parent


def test_explicit_model_path_is_authoritative_over_environment_and_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    use_plugin_config(monkeypatch, tmp_path, {"model": {"path": "configured"}})
    monkeypatch.setenv("ARXIV_MODEL_PATH", str(tmp_path / "environment"))

    assert Path(shared.resolve_model_path("explicit")) == _PLUGIN_DIR / "explicit"


def test_environment_model_path_is_authoritative_and_does_not_silently_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    missing_environment = tmp_path / "missing-external-model"
    monkeypatch.setenv("ARXIV_MODEL_PATH", str(missing_environment))

    assert shared.resolve_model_path() == str(missing_environment)


def test_config_can_use_legacy_fallback_only_without_an_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    use_plugin_config(monkeypatch, tmp_path, {"model": {}})
    monkeypatch.delenv("ARXIV_MODEL_PATH", raising=False)

    assert Path(shared.resolve_model_path()) == _PLUGIN_DIR / "best_model"


def test_missing_explicit_configured_model_path_is_not_silently_replaced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    use_plugin_config(monkeypatch, tmp_path, {"model": {"path": "missing-configured"}})
    monkeypatch.delenv("ARXIV_MODEL_PATH", raising=False)

    assert Path(shared.resolve_model_path()) == _PLUGIN_DIR / "missing-configured"
