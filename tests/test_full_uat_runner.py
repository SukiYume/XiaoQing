"""全量 UAT 单入口的计划、隔离和恢复契约。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import run_full_uat

BASH_ENTRYPOINT = Path(__file__).resolve().parents[1] / "scripts" / "run_full_uat.sh"


def _arguments(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "endpoint": "http://127.0.0.1:12000/event",
        "ws_endpoint": "http://127.0.0.1:12000/ws",
        "include_external": False,
        "include_chat_quality": False,
        "scenario_fixtures": None,
        "matrix_plugins": None,
        "matrix_codes": None,
        "matrix_kinds": None,
        "phases": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_default_plan_covers_both_transports_pressure_and_ci_gates(tmp_path: Path) -> None:
    specs = run_full_uat._build_phase_specs(_arguments(), tmp_path / "run")

    assert [spec.name for spec in specs] == [
        "websocket-matrix",
        "http-matrix",
        "core-pressure",
        "compileall",
        "ruff",
        "mypy",
        "pytest",
        "diff-unstaged",
        "diff-staged",
    ]
    assert all(spec.command[0] == "python" for spec in specs[:-2])
    websocket = specs[0].command
    http = specs[1].command
    assert websocket[websocket.index("--transport") + 1] == "websocket"
    assert http[http.index("--transport") + 1] == "http"
    assert "read_only,isolated_state,privileged" in websocket
    assert "--allow-stateful" in websocket
    assert "--allow-privileged" in websocket
    assert "local" in websocket


def test_external_and_paid_quality_remain_explicit_opt_ins(tmp_path: Path) -> None:
    fixture = tmp_path / "fixtures.json"
    fixture.write_text('{"ssh_test_host":"127.0.0.1"}', encoding="utf-8")
    specs = run_full_uat._build_phase_specs(
        _arguments(
            include_external=True,
            include_chat_quality=True,
            scenario_fixtures=fixture,
        ),
        tmp_path / "run",
    )

    assert any(spec.name == "chat-quality" for spec in specs)
    websocket = next(spec for spec in specs if spec.name == "websocket-matrix")
    assert "local,external" in websocket.command
    assert str(fixture) in websocket.command


def test_matrix_filters_are_forwarded_for_fast_targeted_reruns(tmp_path: Path) -> None:
    specs = run_full_uat._build_phase_specs(
        _arguments(
            phases="ws-matrix",
            matrix_plugins="pendo,qingpet",
            matrix_codes="pendo.pendo.todo,qingpet.qingpet.feed",
            matrix_kinds="invalid,permission_denied",
        ),
        tmp_path / "run",
    )

    command = specs[0].command
    assert command[command.index("--plugins") + 1] == "pendo,qingpet"
    assert command[command.index("--codes") + 1] == (
        "pendo.pendo.todo,qingpet.qingpet.feed"
    )
    assert command[command.index("--kinds") + 1] == "invalid,permission_denied"


def test_runtime_isolation_restores_config_byte_for_byte_after_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    lock_path = tmp_path / "reports" / ".full-uat.lock"
    original = b'{\n\t"bot_name": "format-must-survive",\n\t"log_to_file": true\n}\n'
    config_path.write_bytes(original)
    monkeypatch.setattr(run_full_uat, "CONFIG_PATH", config_path)
    monkeypatch.setattr(run_full_uat, "LOCK_PATH", lock_path)
    output = tmp_path / "run"

    with pytest.raises(RuntimeError, match="simulated"):
        with run_full_uat.RuntimeIsolation(output):
            temporary = json.loads(config_path.read_text(encoding="utf-8"))
            assert Path(temporary["data_root"]) == (output / "data").resolve()
            assert temporary["log_to_file"] is False
            assert lock_path.is_file()
            raise RuntimeError("simulated")

    assert config_path.read_bytes() == original
    assert not lock_path.exists()
    assert (output / "config.original.json").read_bytes() == original


def test_plan_only_does_not_create_output_or_require_named_environment(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output = tmp_path / "planned-run"

    exit_code = run_full_uat.main(
        [
            "--plan-only",
            "--output",
            str(output),
            "--phases",
            "ws-matrix,pytest",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["python_version_gate"] is False
    assert payload["environment_policy"] == "use-current-python"
    assert [phase["name"] for phase in payload["phases"]] == [
        "websocket-matrix",
        "pytest",
    ]
    assert not output.exists()


def test_phase_selection_rejects_unknown_or_duplicate_names() -> None:
    with pytest.raises(run_full_uat.UATError, match="未知值"):
        run_full_uat._selected_phases("pytest,imaginary", include_chat_quality=False)
    with pytest.raises(run_full_uat.UATError, match="重复"):
        run_full_uat._selected_phases("pytest,pytest", include_chat_quality=False)


def test_bash_is_the_documented_cross_platform_entrypoint() -> None:
    source = BASH_ENTRYPOINT.read_text(encoding="utf-8")

    assert source.startswith("#!/usr/bin/env bash\n")
    assert "set -Eeuo pipefail" in source
    assert "conda" not in source.casefold()
    assert 'exec python "$script_dir/run_full_uat.py" "$@"' in source
    assert "powershell" not in source.casefold()
    if sys.platform == "win32":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        git_bash = program_files / "Git" / "bin" / "bash.exe"
        bash = str(git_bash) if git_bash.is_file() else None
    else:
        bash = shutil.which("bash")
    if bash is not None:
        completed = subprocess.run(
            [bash, "-n", str(BASH_ENTRYPOINT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
