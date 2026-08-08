"""全量 UAT 单入口的计划、隔离和恢复契约。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path
from typing import cast

import pytest

from scripts import (
    run_command_matrix,
    run_core_pressure,
    run_full_uat,
    run_xiaoqing_chat_quality,
)
from tests.helpers.paths import REPOSITORY_ROOT

BASH_ENTRYPOINT = REPOSITORY_ROOT / "scripts" / "run_full_uat.sh"


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
    quality = next(spec for spec in specs if spec.name == "chat-quality")
    assert quality.command[quality.command.index("--chat-data-dir") + 1] == str(
        tmp_path / "run" / "data" / "xiaoqing_chat"
    )
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
    assert command[command.index("--codes") + 1] == ("pendo.pendo.todo,qingpet.qingpet.feed")
    assert command[command.index("--kinds") + 1] == "invalid,permission_denied"


def test_runtime_isolation_restores_config_byte_for_byte_after_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    legacy_data = project_root / "plugins" / "demo" / "data"
    legacy_data.mkdir(parents=True)
    (legacy_data.parent / "plugin.json").write_text("{}", encoding="utf-8")
    (legacy_data / "state.json").write_text('{"preserve":true}', encoding="utf-8")
    config_path = tmp_path / "config.json"
    lock_path = tmp_path / "reports" / ".full-uat.lock"
    original = b'{\n\t"bot_name": "format-must-survive",\n\t"log_to_file": true\n}\n'
    config_path.write_bytes(original)
    monkeypatch.setattr(run_full_uat, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(run_full_uat, "CONFIG_PATH", config_path)
    monkeypatch.setattr(run_full_uat, "LOCK_PATH", lock_path)
    output = tmp_path / "run"

    with (
        pytest.raises(RuntimeError, match="simulated"),
        run_full_uat.RuntimeIsolation(output),
    ):
        temporary = json.loads(config_path.read_text(encoding="utf-8"))
        assert Path(temporary["data_root"]) == (output / "data").resolve()
        assert temporary["log_to_file"] is False
        assert temporary["enable_inbound_server"] is True
        assert temporary["enable_ws_client"] is False
        assert temporary["enable_plugin_watcher"] is False
        assert lock_path.is_file()
        assert not legacy_data.exists()
        hidden = tuple(legacy_data.parent.glob(".uat-hidden-data-*"))
        assert len(hidden) == 1
        assert (hidden[0] / "state.json").read_text(encoding="utf-8") == '{"preserve":true}'
        raise RuntimeError("simulated")

    assert config_path.read_bytes() == original
    assert not lock_path.exists()
    assert (output / "config.original.json").read_bytes() == original
    assert (legacy_data / "state.json").read_text(encoding="utf-8") == '{"preserve":true}'
    assert not tuple(legacy_data.parent.glob(".uat-hidden-data-*"))


def test_runtime_recovery_restores_hidden_legacy_data_before_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    legacy_data = project_root / "plugins" / "demo" / "data"
    legacy_data.mkdir(parents=True)
    (legacy_data.parent / "plugin.json").write_text("{}", encoding="utf-8")
    (legacy_data / "state.json").write_text("legacy", encoding="utf-8")
    config_path = project_root / "config" / "config.json"
    config_path.parent.mkdir()
    original = b'{"log_to_file":true}\n'
    config_path.write_bytes(original)
    lock_path = tmp_path / "reports" / ".full-uat.lock"

    monkeypatch.setattr(run_full_uat, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(run_full_uat, "CONFIG_PATH", config_path)
    monkeypatch.setattr(run_full_uat, "LOCK_PATH", lock_path)
    monkeypatch.setattr(run_full_uat, "_port_is_open", lambda _host, _port: False)

    isolation = run_full_uat.RuntimeIsolation(tmp_path / "run")
    isolation.__enter__()
    assert not legacy_data.exists()
    assert json.loads(config_path.read_text(encoding="utf-8"))["log_to_file"] is False

    result = run_full_uat._recover_interrupted_run("http://127.0.0.1:12000/event")

    assert result == 0
    assert config_path.read_bytes() == original
    assert (legacy_data / "state.json").read_text(encoding="utf-8") == "legacy"
    assert not lock_path.exists()


def test_runtime_recovery_lock_cannot_move_paths_outside_plugins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "plugins").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(run_full_uat, "PROJECT_ROOT", project_root)

    with pytest.raises(run_full_uat.UATError, match="意外旧数据路径"):
        run_full_uat._legacy_moves_from_lock(
            {
                "legacy_data_moves": [
                    {
                        "source": str(outside / "data"),
                        "hidden": str(outside / ".uat-hidden-data-tampered"),
                    }
                ]
            }
        )


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


def test_plan_reports_explicit_chat_quality_phase_as_included(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    result = run_full_uat.main(
        [
            "--plan-only",
            "--output",
            str(tmp_path / "plan"),
            "--phases",
            "chat-quality",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["include_chat_quality"] is True
    assert [phase["name"] for phase in payload["phases"]] == ["chat-quality"]


def test_phase_selection_rejects_unknown_or_duplicate_names() -> None:
    with pytest.raises(run_full_uat.UATError, match="未知值"):
        run_full_uat._selected_phases("pytest,imaginary", include_chat_quality=False)
    with pytest.raises(run_full_uat.UATError, match="重复"):
        run_full_uat._selected_phases("pytest,pytest", include_chat_quality=False)


def test_static_phase_runner_marks_internal_exception_as_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = run_full_uat.PhaseSpec("ruff", ("python", "-m", "ruff", "check", "."), False, "lint")

    def fail_phase(*_args: object, **_kwargs: object) -> run_full_uat.PhaseResult:
        raise RuntimeError("simulated runner failure")

    monkeypatch.setattr(run_full_uat, "_run_logged_phase", fail_phase)

    result, detail = run_full_uat._run_static_phase(spec, tmp_path, timeout=1.0)

    assert result.status == "failed"
    assert result.detail == "阶段 runner 异常"
    assert detail == "ruff: RuntimeError: simulated runner failure"


def test_core_pressure_event_identity_is_explicit_and_stage_local() -> None:
    first = run_core_pressure._event(100, 7, same_session=True)
    second = run_core_pressure._event(101, 7, same_session=True)
    next_session = run_core_pressure._event(102, 8, same_session=True)
    unique = run_core_pressure._event(103, 9, same_session=False)

    assert first["user_id"] == second["user_id"]
    assert first["group_id"] == second["group_id"]
    assert next_session["group_id"] != first["group_id"]
    assert unique["group_id"] != next_session["group_id"]


def test_core_pressure_token_reader_rejects_duplicate_keys(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.json"
    secrets.write_text(
        '{"inbound_token":"first","inbound_token":"second"}',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="重复 JSON 键: inbound_token"):
        run_core_pressure._load_token(secrets)


@pytest.mark.parametrize("timeout", ["0", "nan", "inf", "-inf"])
def test_core_pressure_cli_rejects_invalid_timeout_before_network(
    tmp_path: Path,
    timeout: str,
) -> None:
    with pytest.raises(SystemExit, match="2"):
        run_core_pressure.main(["--output", str(tmp_path / "pressure.json"), "--timeout", timeout])


@pytest.mark.parametrize(
    "stage",
    [
        ":1:1:unique",
        "two words:1:1:unique",
        "name.with.dot:1:1:unique",
        f"{'x' * 65}:1:1:unique",
    ],
)
def test_core_pressure_stage_parser_rejects_ambiguous_name(stage: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="阶段名必须"):
        run_core_pressure._parse_stage(stage)


def test_core_pressure_cli_rejects_duplicate_stage_names(tmp_path: Path) -> None:
    output = tmp_path / "pressure.json"

    with pytest.raises(SystemExit, match="2"):
        run_core_pressure.main(
            [
                "--output",
                str(output),
                "--stage",
                "repeat:1:1:unique",
                "--stage",
                "repeat:2:1:same",
            ]
        )


def test_core_pressure_cli_rejects_non_positive_message_seed(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="2"):
        run_core_pressure.main(
            [
                "--output",
                str(tmp_path / "pressure.json"),
                "--message-id-seed",
                "0",
            ]
        )


def test_core_pressure_cli_writes_report_with_explicit_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "reports" / "pressure.json"
    captured: dict[str, object] = {}

    async def fake_run(args: argparse.Namespace) -> dict[str, object]:
        captured["seed"] = args.message_id_seed
        captured["stages"] = args.stages
        return {"schema_version": 2, "gate_passed": True}

    monkeypatch.setattr(run_core_pressure, "run", fake_run)

    result = run_core_pressure.main(["--output", str(output), "--message-id-seed", "98765"])

    assert result == 0
    assert captured["seed"] == 98765
    assert len(cast(list[run_core_pressure.Stage], captured["stages"])) == 4
    assert json.loads(output.read_text(encoding="utf-8"))["gate_passed"] is True


@pytest.mark.asyncio
async def test_core_pressure_health_preflight_failure_produces_failed_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "secrets.json"
    secrets.write_text('{"inbound_token":"token"}', encoding="utf-8")

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def failed_health(
        _session: object,
        _endpoint: str,
        _token: str,
    ) -> dict[str, object]:
        return {
            "status": None,
            "payload": None,
            "latency_ms": 1.0,
            "error": "ClientConnectionError: offline",
        }

    monkeypatch.setattr(run_core_pressure.aiohttp, "TCPConnector", lambda **_kwargs: object())
    monkeypatch.setattr(
        run_core_pressure.aiohttp,
        "ClientSession",
        lambda **_kwargs: FakeSession(),
    )
    monkeypatch.setattr(run_core_pressure, "_get_health", failed_health)
    args = argparse.Namespace(
        endpoint="http://127.0.0.1:12000/event",
        secrets=secrets,
        timeout=1.0,
        message_id_seed=100,
        stages=[run_core_pressure.Stage("one", 1, 1, False)],
    )

    report = await run_core_pressure.run(args)

    assert report["abort_reason"] == "health preflight failed"
    assert report["stages"] == []
    assert report["gate_passed"] is False
    assert report["health_before"]["error"] == "ClientConnectionError: offline"


def test_quality_auth_reader_rejects_duplicate_keys(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.json"
    secrets.write_text(
        '{"inbound_token":"first","inbound_token":"second","admin_user_ids":[1]}',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="重复 JSON 键: inbound_token"):
        run_xiaoqing_chat_quality._load_auth(secrets)


def test_quality_probe_converts_network_failure_but_propagates_programming_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = run_xiaoqing_chat_quality.EventProbe(
        endpoint="http://127.0.0.1:12000/event",
        token="token",
        admin_id=1,
        timeout=1.0,
        message_id_seed=100,
    )

    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(run_xiaoqing_chat_quality.urllib.request, "urlopen", fail_network)
    result = probe.send("hello", user_id=2, group_id=3)

    assert result["status"] is None
    assert result["error"] == "URLError: <urlopen error offline>"

    def fail_programming(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("programming bug")

    monkeypatch.setattr(run_xiaoqing_chat_quality.urllib.request, "urlopen", fail_programming)
    with pytest.raises(RuntimeError, match="programming bug"):
        probe.send("hello", user_id=2, group_id=3)


def test_quality_stale_topic_does_not_treat_transport_failure_as_non_reply(
    tmp_path: Path,
) -> None:
    class FailingProbe:
        def send(self, *_args: object, **_kwargs: object) -> run_xiaoqing_chat_quality.ProbeResult:
            return {
                "status": None,
                "latency_ms": 1.0,
                "error": "URLError: offline",
                "payload": {},
            }

    result = run_xiaoqing_chat_quality._run_stale_topic_case(
        cast(run_xiaoqing_chat_quality.EventProbe, FailingProbe()),
        972_234_000,
        tmp_path,
    )

    assert result["attempt"] is None
    assert result["checks"] == {"found_non_reply_seed": False}
    assert result["all_requests_ok"] is False
    assert result["all_cleanups_ok"] is False


@pytest.mark.parametrize("timeout", ["0", "nan", "inf", "-inf"])
def test_quality_cli_rejects_invalid_timeout_before_network(
    tmp_path: Path,
    timeout: str,
) -> None:
    with pytest.raises(SystemExit, match="2"):
        run_xiaoqing_chat_quality.main(
            ["--output", str(tmp_path / "quality.json"), "--timeout", timeout]
        )


def test_quality_cli_writes_exclusive_report_with_explicit_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data" / "xiaoqing_chat"
    data_dir.mkdir(parents=True)
    output = tmp_path / "quality.json"
    captured: dict[str, object] = {}

    monkeypatch.setattr(run_xiaoqing_chat_quality, "_load_auth", lambda _path: ("token", 1))

    def fake_suite(
        probe: run_xiaoqing_chat_quality.EventProbe,
        actual_data_dir: Path,
    ) -> dict[str, object]:
        captured["seed"] = probe.message_id_seed
        captured["data_dir"] = actual_data_dir
        return {"schema_version": 2, "gate_passed": True}

    monkeypatch.setattr(run_xiaoqing_chat_quality, "_run_quality_suite", fake_suite)

    result = run_xiaoqing_chat_quality.main(
        [
            "--chat-data-dir",
            str(data_dir),
            "--output",
            str(output),
            "--message-id-seed",
            "12345",
        ]
    )

    assert result == 0
    assert captured == {"seed": 12345, "data_dir": data_dir.resolve()}
    assert json.loads(output.read_text(encoding="utf-8"))["gate_passed"] is True
    with pytest.raises(SystemExit, match="2"):
        run_xiaoqing_chat_quality.main(["--chat-data-dir", str(data_dir), "--output", str(output)])


def test_quality_cli_rejects_invalid_seed_and_missing_data_dir(tmp_path: Path) -> None:
    output = tmp_path / "quality.json"
    with pytest.raises(SystemExit, match="2"):
        run_xiaoqing_chat_quality.main(["--output", str(output), "--message-id-seed", "0"])
    with pytest.raises(SystemExit, match="2"):
        run_xiaoqing_chat_quality.main(
            [
                "--chat-data-dir",
                str(tmp_path / "missing"),
                "--output",
                str(output),
            ]
        )


def test_command_matrix_rejects_non_finite_timeout_before_loading_auth() -> None:
    args = run_command_matrix.build_parser().parse_args(["--plan-only", "--timeout", "nan"])

    with pytest.raises(run_command_matrix.MatrixError, match="参数非法"):
        run_command_matrix.run(args)


def test_full_uat_rejects_non_finite_timeout_before_planning() -> None:
    with pytest.raises(SystemExit, match="2"):
        run_full_uat.main(["--plan-only", "--phase-timeout", "nan"])


def test_bash_is_the_documented_cross_platform_entrypoint() -> None:
    source = BASH_ENTRYPOINT.read_text(encoding="utf-8")

    assert source.startswith("#!/usr/bin/env bash\n")
    assert "set -Eeuo pipefail" in source
    assert "conda" not in source.casefold()
    assert 'exec python "$script_dir/run_full_uat.py" "$@"' in source
    assert "powershell" not in source.casefold()
    if sys.platform == "win32":
        program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
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
