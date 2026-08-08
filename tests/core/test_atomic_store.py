from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core import atomic_store
from core.atomic_store import AtomicJsonStore


def test_atomic_json_store_recovers_last_valid_backup(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path / "state.json")
    store.write({"version": 1})
    store.write({"version": 2})
    store.path.write_text('{"truncated":', encoding="utf-8")

    assert store.read({}) == {"version": 1}
    assert json.loads(store.path.read_text(encoding="utf-8")) == {"version": 1}


def test_atomic_json_store_strict_read_rejects_corrupt_primary_with_backup(
    tmp_path: Path,
) -> None:
    store = AtomicJsonStore(tmp_path / "state.json")
    store.write({"version": 1})
    store.write({"version": 2})
    store.path.write_text('{"truncated":', encoding="utf-8")
    before = store.path.read_bytes()

    with pytest.raises(json.JSONDecodeError):
        store.read({}, raise_on_error=True)

    assert store.path.read_bytes() == before
    assert json.loads(store.backup_path.read_text(encoding="utf-8")) == {"version": 1}


def test_atomic_json_store_concurrent_writes_remain_complete(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path / "counter.json")

    def write_value(index: int) -> None:
        store.write({"count": index})

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write_value, range(200)))

    assert store.read({})["count"] in range(200)
    # 锁池是实现细节；回归测试直接检查私有状态，不为测试扩张生产 API。
    with atomic_store._POOL_GUARD:
        assert atomic_store._PATH_LOCKS == {}


def test_atomic_json_store_write_with_backup_publishes_same_generation(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path / "state.json")

    store.write_with_backup({"version": 2})

    assert json.loads(store.path.read_text(encoding="utf-8")) == {"version": 2}
    assert json.loads(store.backup_path.read_text(encoding="utf-8")) == {"version": 2}


def test_atomic_write_failure_preserves_previous_complete_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.bin"
    atomic_store.atomic_write_bytes(path, b"old")
    real_replace = atomic_store.os.replace

    def fail_replace(source, destination):
        if Path(destination) == path:
            raise OSError("injected replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(atomic_store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        atomic_store.atomic_write_bytes(path, b"new")

    assert path.read_bytes() == b"old"
    assert not list(tmp_path.glob(".state.bin.*"))


def test_atomic_write_retries_transient_permission_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.bin"
    real_replace = atomic_store.os.replace
    attempts = 0

    def flaky_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("target is temporarily busy")
        return real_replace(source, destination)

    monkeypatch.setattr(atomic_store.os, "replace", flaky_replace)
    monkeypatch.setattr(atomic_store.time, "sleep", lambda _delay: None)

    atomic_store.atomic_write_bytes(path, b"complete")

    assert attempts == 3
    assert path.read_bytes() == b"complete"
    assert not list(tmp_path.glob(".state.bin.*"))
