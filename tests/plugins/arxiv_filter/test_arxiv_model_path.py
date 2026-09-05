# 验证 arXiv 模型路径解析和允许的文件边界。
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from plugins.arxiv_filter.inference import shared
from scripts import arxiv_inference_cli
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


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
    fallback   = plugin_dir / "best_model"
    fallback.mkdir(parents=True)
    monkeypatch.setattr(shared, "_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(
        shared,
        "load_plugin_config",
        lambda: {"model": {}},
    )
    monkeypatch.delenv("ARXIV_MODEL_PATH", raising=False)

    assert shared.resolve_model_path() == str(fallback)


def test_missing_explicit_configured_model_path_is_not_silently_replaced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / "best_model").mkdir(parents=True)
    monkeypatch.setattr(shared, "_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(
        shared,
        "load_plugin_config",
        lambda: {"model": {"path": "missing-configured"}},
    )
    monkeypatch.delenv("ARXIV_MODEL_PATH", raising=False)

    assert shared.resolve_model_path() == str(plugin_dir / "missing-configured")


def test_obsolete_abstract_model_directory_is_not_auto_discovered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / "best_model_abs").mkdir(parents=True)
    monkeypatch.setattr(shared, "_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(
        shared,
        "load_plugin_config",
        lambda: {"model": {"path": "configured"}},
    )
    monkeypatch.delenv("ARXIV_MODEL_PATH", raising=False)

    assert shared.resolve_model_path() == str(plugin_dir / "configured")


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
        cwd            = ROOT,
        check          = False,
        capture_output = True,
        text           = True,
        encoding       = "utf-8",
        errors         = "replace",
        timeout        = 30,
    )
    assert completed.returncode == 0
    assert "--model-path" in completed.stdout
    assert "--force" in completed.stdout


@pytest.mark.parametrize(
    "arguments",
    [
        ["--threshold", "nan"],
        ["--threshold", "inf"],
        ["--batch-size", "0"],
        ["--max-len", "-1"],
        ["--model-path", ""],
        ["--test-positive", "--test-title", " "],
    ],
)
def test_repository_cli_rejects_invalid_arguments_before_model_load(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    def unexpected_load() -> None:
        raise AssertionError("model facade must not load for invalid arguments")

    monkeypatch.setattr(arxiv_inference_cli, "_load_inference", unexpected_load)

    with pytest.raises(SystemExit, match="2"):
        arxiv_inference_cli.main(arguments)


def test_repository_cli_refuses_existing_output_before_network_or_model_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "result.csv"
    output.write_text("preserve", encoding="utf-8")

    def unexpected_load() -> None:
        raise AssertionError("model facade must not load before output preflight")

    monkeypatch.setattr(arxiv_inference_cli, "_load_inference", unexpected_load)

    with pytest.raises(SystemExit, match="2"):
        arxiv_inference_cli.main(["--output", str(output)])
    assert output.read_text(encoding="utf-8") == "preserve"


def test_repository_cli_atomically_writes_and_explicitly_replaces_csv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = pd.DataFrame(
        [
            {
                "Title": "A paper",
                "Abstract": "Abstract",
                "Probability": 0.9,
                "Prediction": 1,
            }
        ]
    )
    facade = SimpleNamespace(
        run_inference_for_today = lambda **_kwargs: (data, 0.5),
        select_positives        = lambda frame: frame.loc[frame["Prediction"] == 1],
        format_positives        = lambda _frame: "formatted positive",
    )
    monkeypatch.setattr(arxiv_inference_cli, "_load_inference", lambda: facade)
    output = tmp_path / "nested" / "result.csv"

    assert arxiv_inference_cli.main(["--output", str(output)]) == 0
    assert pd.read_csv(output).iloc[0]["Title"] == "A paper"

    output.write_text("old", encoding="utf-8")
    assert arxiv_inference_cli.main(["--output", str(output), "--force"]) == 0
    assert pd.read_csv(output).iloc[0]["Prediction"] == 1
    assert not tuple(output.parent.glob(f".{output.name}.*.tmp"))


def test_repository_cli_removes_partial_temporary_csv(tmp_path: Path) -> None:
    output = tmp_path / "result.csv"

    class BrokenData:
        def to_csv(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated write failure")

    with pytest.raises(RuntimeError, match="simulated write failure"):
        arxiv_inference_cli._write_csv_atomic(BrokenData(), output)

    assert not output.exists()
    assert not tuple(tmp_path.glob(f".{output.name}.*.tmp"))
