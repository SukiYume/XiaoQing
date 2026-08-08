"""Regression tests for stable, bounded plugin manifest snapshots."""

import json
import logging
import os
from pathlib import Path

import pytest

from core.plugin_manager import PluginManager
from core.plugin_watcher import _MAX_PLUGIN_MANIFEST_BYTES, _ManifestRejection
from core.router import CommandRouter

_MAX_MANIFEST_BYTES = _MAX_PLUGIN_MANIFEST_BYTES


def _build_manager(tmp_path: Path) -> PluginManager:
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "__init__.py").write_text("", encoding="utf-8")
    return PluginManager(
        plugins_dir=plugins_dir,
        router=CommandRouter(),
        context_factory=lambda *_args, **_kwargs: None,
    )


def _manifest_payload(name: str, version: str = "1.0.0") -> bytes:
    return json.dumps(
        {
            "name": name,
            "version": version,
            "entry": "main.py",
            "commands": [],
            "schedule": [],
            "concurrency": "parallel",
            "enabled": True,
        },
        separators=(",", ":"),
    ).encode()


class _ReadAudit:
    def __init__(self, handle, read_limits: list[int]) -> None:  # type: ignore[no-untyped-def]
        self._handle = handle
        self._read_limits = read_limits

    def __enter__(self):  # type: ignore[no-untyped-def]
        self._handle.__enter__()
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        return self._handle.__exit__(*args)

    def fileno(self) -> int:
        return self._handle.fileno()

    def read(self, size: int = -1) -> bytes:
        self._read_limits.append(size)
        return self._handle.read(size)


def _audit_manifest_open(
    monkeypatch: pytest.MonkeyPatch,
    manifest_path: Path,
) -> tuple[list[str], list[int]]:
    real_open = Path.open
    modes: list[str] = []
    read_limits: list[int] = []

    def audited_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        mode = args[0] if args else kwargs.get("mode", "r")
        handle = real_open(path, *args, **kwargs)
        if path == manifest_path and mode == "rb":
            modes.append(mode)
            return _ReadAudit(handle, read_limits)
        return handle

    monkeypatch.setattr(Path, "open", audited_open)
    return modes, read_limits


def _redirect_manifest_open(
    monkeypatch: pytest.MonkeyPatch,
    manifest_path: Path,
    replacement_path: Path,
) -> None:
    """Model the A -> B -> A resolver/open ABA window deterministically."""

    real_open = Path.open

    def raced_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == manifest_path and mode == "rb":
            return real_open(replacement_path, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", raced_open)


def _write_race_pair(tmp_path: Path) -> tuple[PluginManager, Path, Path, Path]:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    manifest_path = plugin_dir / "plugin.json"
    replacement_path = tmp_path / "replacement-plugin.json"
    manifest_path.write_bytes(_manifest_payload("demo", "1.0.0"))
    replacement_path.write_bytes(_manifest_payload("demo", "2.0.0"))

    # Keep size and mtime equal so the regression proves descriptor/path file
    # identity, rather than superficial metadata, detects the replacement.
    original = manifest_path.stat()
    os.utime(
        replacement_path,
        ns=(original.st_atime_ns, original.st_mtime_ns),
    )
    replacement = replacement_path.stat()
    assert replacement.st_size == original.st_size
    assert replacement.st_mtime_ns == original.st_mtime_ns
    assert not os.path.samestat(original, replacement)
    return manager, plugin_dir, manifest_path, replacement_path


def test_manifest_snapshot_uses_binary_bounded_read_and_two_fstats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    manifest_path = plugin_dir / "plugin.json"
    manifest_path.write_bytes(_manifest_payload("demo"))
    modes, read_limits = _audit_manifest_open(monkeypatch, manifest_path)

    real_fstat = os.fstat
    fstat_fds: list[int] = []

    def audited_fstat(fd: int):  # type: ignore[no-untyped-def]
        fstat_fds.append(fd)
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", audited_fstat)

    definition = manager._load_definition(plugin_dir)

    assert definition is not None
    assert definition.version == "1.0.0"
    assert modes == ["rb"]
    assert read_limits == [_MAX_MANIFEST_BYTES + 1]
    assert len(fstat_fds) == 2
    assert fstat_fds[0] == fstat_fds[1]


def test_oversized_manifest_is_rejected_after_at_most_one_mib_plus_one_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    manifest_path = plugin_dir / "plugin.json"
    manifest_path.write_bytes(b"{" + b" " * _MAX_MANIFEST_BYTES)
    modes, read_limits = _audit_manifest_open(monkeypatch, manifest_path)

    with caplog.at_level(logging.ERROR, logger="core.plugin_manager"):
        definition = manager._load_definition(plugin_dir)

    assert isinstance(definition, _ManifestRejection)
    assert definition.bucket == "invalid"
    assert modes == ["rb"]
    assert read_limits == [_MAX_MANIFEST_BYTES + 1]
    assert f"manifest exceeds {_MAX_MANIFEST_BYTES} bytes" in caplog.text


def test_manifest_descriptor_identity_rejects_resolver_open_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager, plugin_dir, manifest_path, replacement_path = _write_race_pair(tmp_path)
    _redirect_manifest_open(monkeypatch, manifest_path, replacement_path)

    with caplog.at_level(logging.ERROR, logger="core.plugin_manager"):
        definition = manager._load_definition(plugin_dir)

    assert isinstance(definition, _ManifestRejection)
    assert definition.bucket == "read"
    assert "Cannot read a stable plugin.json" in caplog.text
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["version"] == "1.0.0"


def test_manifest_aba_keeps_watcher_rate_limit_and_manual_logging_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager, plugin_dir, manifest_path, replacement_path = _write_race_pair(tmp_path)
    _redirect_manifest_open(monkeypatch, manifest_path, replacement_path)

    with caplog.at_level(logging.ERROR, logger="core.plugin_manager"):
        manager._load_definition_for_watch(plugin_dir)
        manager._load_definition_for_watch(plugin_dir)
        manager._load_definition(plugin_dir)
        manager._load_definition(plugin_dir)

    stable_errors = [
        record
        for record in caplog.records
        if "Cannot read a stable plugin.json" in record.getMessage()
    ]
    assert len(stable_errors) == 3
