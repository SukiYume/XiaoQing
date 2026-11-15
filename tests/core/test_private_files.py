"""只给当前用户看的文件：目录收紧、文件原子写入。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from core import private_files


def test_write_private_json_replaces_the_target_atomically(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "token.json"
    private_files.write_private_json(path, {"token": "旧的"})
    private_files.write_private_json(path, {"token": "新的"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"token": "新的"}
    assert [child.name for child in path.parent.iterdir()] == ["token.json"], "临时文件不留下"


@pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位")
def test_private_files_are_owner_only_on_posix(tmp_path: Path) -> None:
    directory = tmp_path / "secrets"
    private_files.protect_directory(directory)
    private_files.write_private_json(directory / "token.json", {"token": "x"})
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "token.json").stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL")
def test_protect_directory_restricts_the_acl_to_the_current_user_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(private_files.subprocess, "check_output", lambda *a, **k: "host\\me\n")
    monkeypatch.setattr(private_files.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    directory = tmp_path / "secrets"
    private_files.protect_directory(directory)
    assert directory.is_dir()
    assert calls == [["icacls", str(directory), "/inheritance:r", "/grant:r", "host\\me:(OI)(CI)F"]]
