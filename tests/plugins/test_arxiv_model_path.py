from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from plugins.arxiv_filter.inference import shared

ROOT = Path(__file__).resolve().parents[2]


def test_explicit_model_path_is_authoritative_over_environment_and_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "configured").mkdir()
    monkeypatch.setattr(shared, "_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(
        shared,
        "load_plugin_config",
        lambda: {"model": {"path": "configured"}},
    )
    monkeypatch.setenv("ARXIV_MODEL_PATH", str(tmp_path / "environment"))

    assert shared.resolve_model_path("explicit") == str(plugin_dir / "explicit")


def test_environment_model_path_is_authoritative_and_does_not_silently_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_dir = tmp_path / "plugin"
    configured = plugin_dir / "configured"
    configured.mkdir(parents=True)
    missing_environment = tmp_path / "missing-external-model"
    monkeypatch.setattr(shared, "_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(
        shared,
        "load_plugin_config",
        lambda: {"model": {"path": "configured"}},
    )
    monkeypatch.setenv("ARXIV_MODEL_PATH", str(missing_environment))

    assert shared.resolve_model_path() == str(missing_environment)


def test_config_can_use_legacy_fallback_only_without_an_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_dir = tmp_path / "plugin"
    fallback = plugin_dir / "best_model"
    fallback.mkdir(parents=True)
    monkeypatch.setattr(shared, "_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(
        shared,
        "load_plugin_config",
        lambda: {"model": {"path": "missing-configured"}},
    )
    monkeypatch.delenv("ARXIV_MODEL_PATH", raising=False)

    assert shared.resolve_model_path() == str(fallback)


def test_packaged_config_and_repository_cli_expose_the_external_model_contract() -> None:
    config = json.loads(
        (ROOT / "plugins" / "arxiv_filter" / "config.json").read_text(encoding="utf-8")
    )
    cli = ROOT / "scripts" / "arxiv_inference_cli.py"

    assert config["model"]["path"] == "best_model"
    assert cli.is_file()
    assert not (ROOT / "plugins" / "arxiv_filter" / "arxiv_test.py").exists()
    completed = subprocess.run(
        [sys.executable, str(cli), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0
    assert "--model-path" in completed.stdout
