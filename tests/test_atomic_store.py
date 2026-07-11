from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core import atomic_store
from core.atomic_store import AtomicJsonStore, MISSING_ETAG, active_keyed_lock_count


def test_atomic_json_store_recovers_last_valid_backup(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path / "state.json")
    store.write({"version": 1})
    store.write({"version": 2})
    store.path.write_text('{"truncated":', encoding="utf-8")

    assert store.read({}) == {"version": 1}
    assert json.loads(store.path.read_text(encoding="utf-8")) == {"version": 1}


def test_atomic_json_store_concurrent_mutations_do_not_lose_updates(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path / "counter.json")
    store.write({"count": 0})

    def increment(_index: int) -> None:
        store.mutate({"count": 0}, lambda value: {"count": value["count"] + 1})

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(increment, range(200)))

    assert store.read({}) == {"count": 200}
    assert active_keyed_lock_count() == 0


def test_atomic_json_store_compare_and_swap_rejects_stale_writer(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path / "state.json")
    value, initial_etag = store.read_versioned({})
    assert value == {} and initial_etag == MISSING_ETAG
    swapped, current_etag = store.compare_and_swap(initial_etag, {"owner": "first"})
    assert swapped is True
    stale, observed_etag = store.compare_and_swap(initial_etag, {"owner": "stale"})
    assert stale is False and observed_etag == current_etag
    assert store.read({}) == {"owner": "first"}


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
