"""同步前的定时任务目标群比对：按生产现有文件和本机文件算出的推送目标不同时报出来。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import check_schedule_targets as targets
from tests.helpers.paths import REPOSITORY_ROOT

SCRIPT = REPOSITORY_ROOT / "scripts" / "check_schedule_targets.py"


def _manifest(*schedules: dict[str, Any], enabled: bool = True) -> str:
    return json.dumps({"name": "demo", "enabled": enabled, "schedule": list(schedules)})


def _schedule(schedule_id: str = "poll", **fields: Any) -> dict[str, Any]:
    return {"id": schedule_id, "handler": schedule_id, "cron": {"minute": "*"}, **fields}


def _config(*, default: list[int] | None = None, overrides: dict[str, list[int]] | None = None):
    config: dict[str, Any] = {"default_group_ids": default or []}
    if overrides:
        config["plugins"] = {
            "demo": {"schedules": {key: {"group_ids": value} for key, value in overrides.items()}}
        }
    return json.dumps(config)


def _files(config: str, manifest: str) -> dict[str, str]:
    return {"config/config.json": config, "plugins/demo/plugin.json": manifest}


def _snapshot(files: dict[str, str]) -> bytes:
    return b"".join(
        path.encode() + b"\0" + text.encode("utf-8") + b"\0" for path, text in files.items()
    )


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _run(tmp_path: Path, remote: dict[str, str], local: dict[str, str]):
    snapshot = tmp_path / "remote.bin"
    snapshot.write_bytes(_snapshot(remote))
    root = tmp_path / "local"
    _write_tree(root, local)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--remote-snapshot", str(snapshot), "--root", str(root)],
        capture_output = True,
        text           = True,
        encoding       = "utf-8",
        check          = False,
    )


def test_identical_targets_pass(tmp_path: Path) -> None:
    files = _files(_config(overrides={"poll": [101, 102]}), _manifest(_schedule()))

    result = _run(tmp_path, files, files)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_dropped_deployment_override_that_falls_back_to_default_groups_blocks(tmp_path: Path):
    remote = _files(_config(default=[900], overrides={"poll": [101, 102]}), _manifest(_schedule()))
    local = _files(_config(default=[900]), _manifest(_schedule()))

    result = _run(tmp_path, remote, local)

    assert result.returncode == 1
    assert "demo/poll" in result.stdout
    assert "default_group_ids" in result.stdout


@pytest.mark.parametrize(
    ("remote", "local", "change"),
    [
        pytest.param(
            _files(_config(overrides={"poll": [101]}), _manifest(_schedule())),
            _files(_config(), _manifest(_schedule(group_ids=[101]))),
            ("plugins.demo.schedules.poll.group_ids", "demo plugin.json schedule poll group_ids"),
            id="same-groups-from-another-field",
        ),
        pytest.param(
            _files(_config(default=[101]), _manifest(_schedule())),
            _files(_config(default=[101]), _manifest(_schedule(enabled=False))),
            None,
            id="schedule-disabled",
        ),
        pytest.param(
            _files(_config(default=[101]), _manifest(_schedule())),
            _files(_config(default=[101]), _manifest(_schedule(), _schedule("digest"))),
            None,
            id="schedule-added",
        ),
    ],
)
def test_any_change_to_who_receives_a_schedule_is_reported(remote, local, change):
    before = targets.schedule_targets(remote)
    after  = targets.schedule_targets(local)

    changes = targets.compare(before, after)

    assert len(changes) == 1
    if change is not None:
        assert change[0] in changes[0] and change[1] in changes[0]


@pytest.mark.parametrize(
    "files",
    [
        pytest.param(
            _files(_config(default=[101]), _manifest(_schedule(delivery="silent"))), id="silent"
        ),
        pytest.param(
            _files(_config(default=[101]), _manifest(_schedule(), enabled=False)),
            id="plugin-disabled",
        ),
        pytest.param(
            {
                "config/config.json"      : json.dumps({"enable_plugin_schedules": False}),
                "plugins/demo/plugin.json": _manifest(_schedule()),
            },
            id="plugin-schedules-off",
        ),
    ],
)
def test_schedules_that_do_not_run_or_deliver_are_not_compared(files):
    assert targets.schedule_targets(files) == {}


def test_invalid_group_ids_are_reported_as_a_skipped_schedule():
    files = _files(json.dumps({"default_group_ids": ["abc"]}), _manifest(_schedule()))

    (target,) = targets.schedule_targets(files).values()

    assert target.error and target.groups == ()


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param(b"config/config.json\0{}\0plugins/demo/plugin.json", id="truncated"),
        pytest.param(b"config/config.json\0{not json\0", id="bad-json"),
    ],
)
def test_unreadable_remote_snapshot_fails_closed(tmp_path: Path, snapshot: bytes) -> None:
    path = tmp_path / "remote.bin"
    path.write_bytes(snapshot)
    (tmp_path / "local" / "config").mkdir(parents=True)
    (tmp_path / "local" / "config" / "config.json").write_text("{}", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--remote-snapshot",
            str(path),
            "--root",
            str(tmp_path / "local"),
        ],
        capture_output = True,
        text           = True,
        encoding       = "utf-8",
        check          = False,
    )

    assert result.returncode == 2
