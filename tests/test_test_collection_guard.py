"""Tests for the exact pytest collection inventory gate."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import check_test_collection as guard

NODEIDS = {
    "tests/test_core.py": ("tests/test_core.py::test_core",),
    "tests/test_integration.py": ("tests/test_integration.py::test_integration",),
    "tests/test_tool.py": ("tests/test_tool.py::test_tool",),
    "tests/plugins/test_alpha.py": (
        "tests/plugins/test_alpha.py::test_alpha[one]",
        "tests/plugins/test_alpha.py::test_alpha[two]",
    ),
}


def _document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "paths": {"tests": "tests", "plugins": "plugins"},
        "inventory": {"total_items": 5, "total_modules": 4, "active_plugins": 1},
        "groups": {
            "core": {"items": 1, "modules": 1},
            "integration": {"items": 1, "modules": 1},
            "tooling": {"items": 1, "modules": 1},
            "plugin.alpha": {"items": 2, "modules": 1},
        },
        "modules": {
            module: {
                "items": len(nodeids),
                "group": {
                    "tests/test_core.py": "core",
                    "tests/test_integration.py": "integration",
                    "tests/test_tool.py": "tooling",
                    "tests/plugins/test_alpha.py": "plugin.alpha",
                }[module],
                "nodeids_sha256": guard.nodeids_sha256(nodeids),
            }
            for module, nodeids in NODEIDS.items()
        },
        "plugins": {"alpha": ["tests/plugins/test_alpha.py"]},
        "allowed_outcomes": {
            "skip": ["tests/test_core.py::test_core"],
            "xfail": ["tests/test_integration.py::test_integration"],
        },
    }


def _manifest(document: dict[str, object] | None = None) -> guard.CollectionManifest:
    return guard._parse_manifest(document or _document())


def _snapshot(
    nodeids: dict[str, tuple[str, ...]] | None = None,
    *,
    skips: frozenset[str] = frozenset({"tests/test_core.py::test_core"}),
    xfails: frozenset[str] = frozenset(
        {"tests/test_integration.py::test_integration"}
    ),
    deselected: tuple[str, ...] = (),
) -> guard.CollectionSnapshot:
    inventory = nodeids or NODEIDS
    return guard.CollectionSnapshot(
        items=tuple(
            guard.CollectedItem(nodeid=nodeid, module=module)
            for module, module_nodeids in inventory.items()
            for nodeid in module_nodeids
        ),
        skip_nodeids=skips,
        xfail_nodeids=xfails,
        deselected_nodeids=deselected,
    )


def _verify(
    snapshot: guard.CollectionSnapshot | None = None,
    *,
    disk_modules: frozenset[str] | None = None,
    active_plugins: frozenset[str] = frozenset({"alpha"}),
) -> list[str]:
    return guard.verify_snapshot(
        _manifest(),
        snapshot or _snapshot(),
        disk_modules or frozenset(NODEIDS),
        active_plugins,
    )


def test_exact_synthetic_inventory_passes() -> None:
    assert _verify() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
    ],
)
def test_schema_version_is_exact_and_rejects_bool(field: str, value: object) -> None:
    document = _document()
    document[field] = value
    with pytest.raises(guard.CollectionGuardError, match="schema_version"):
        guard._parse_manifest(document)


def test_unknown_schema_key_is_rejected() -> None:
    document = _document()
    document["unexpected"] = {}
    with pytest.raises(guard.CollectionGuardError, match="unknown"):
        guard._parse_manifest(document)


def test_bool_and_zero_item_counts_are_rejected() -> None:
    for value in (True, 0):
        document = _document()
        document["inventory"]["total_items"] = value  # type: ignore[index]
        with pytest.raises(guard.CollectionGuardError, match="positive integer"):
            guard._parse_manifest(document)


@pytest.mark.parametrize(
    "path",
    [
        "/tests/test_core.py",
        "../tests/test_core.py",
        "tests\\test_core.py",
        "C:/tests/test_core.py",
        "//server/share/test_core.py",
        "tests/test_\x00core.py",
        "tests/test_测试.py",
    ],
)
def test_manifest_paths_must_be_ascii_canonical_relative_posix(path: str) -> None:
    document = _document()
    modules = document["modules"]
    assert isinstance(modules, dict)
    rule = modules.pop("tests/test_core.py")
    modules[path] = rule
    with pytest.raises(guard.CollectionGuardError, match="path|POSIX|relative|module"):
        guard._parse_manifest(document)


def test_module_paths_are_casefold_unique() -> None:
    document = _document()
    modules = document["modules"]
    assert isinstance(modules, dict)
    modules["tests/Test_Core.py"] = copy.deepcopy(modules["tests/test_core.py"])
    with pytest.raises(guard.CollectionGuardError, match="case-insensitive duplicates"):
        guard._parse_manifest(document)


def test_same_total_with_one_module_replaced_is_rejected() -> None:
    inventory = dict(NODEIDS)
    inventory.pop("tests/test_tool.py")
    inventory["tests/test_replacement.py"] = ("tests/test_replacement.py::test_tool",)
    errors = _verify(_snapshot(inventory))
    assert any("collected modules missing" in error for error in errors)
    assert any("collected modules extra" in error for error in errors)


def test_test_rename_with_same_module_count_and_item_count_is_rejected() -> None:
    inventory = dict(NODEIDS)
    inventory["tests/test_core.py"] = ("tests/test_core.py::test_dummy",)
    errors = _verify(
        _snapshot(
            inventory,
            skips=frozenset({"tests/test_core.py::test_dummy"}),
        )
    )
    assert any("node ID digest mismatch" in error for error in errors)


def test_parameter_reduction_is_rejected() -> None:
    inventory = dict(NODEIDS)
    inventory["tests/plugins/test_alpha.py"] = inventory["tests/plugins/test_alpha.py"][:1]
    errors = _verify(_snapshot(inventory))
    assert any("total item count mismatch" in error for error in errors)
    assert any("module item count mismatch" in error for error in errors)


def test_zero_item_disk_module_is_rejected() -> None:
    disk = frozenset({*NODEIDS, "tests/test_zero.py"})
    errors = _verify(disk_modules=disk)
    assert any("disk test modules extra" in error for error in errors)


def test_duplicate_collected_nodeid_is_rejected() -> None:
    snapshot = _snapshot()
    duplicate = guard.CollectionSnapshot(
        items=(*snapshot.items, snapshot.items[0]),
        skip_nodeids=snapshot.skip_nodeids,
        xfail_nodeids=snapshot.xfail_nodeids,
    )
    assert any("duplicate collected node IDs" in error for error in _verify(duplicate))


def test_deselection_is_rejected() -> None:
    errors = _verify(
        _snapshot(deselected=("tests/test_core.py::test_core",))
    )
    assert any("deselected" in error for error in errors)


@pytest.mark.parametrize(
    ("skips", "xfails", "needle"),
    [
        (frozenset(), frozenset({"tests/test_integration.py::test_integration"}), "skip"),
        (
            frozenset({"tests/test_core.py::test_core"}),
            frozenset({"tests/test_tool.py::test_tool"}),
            "xfail",
        ),
    ],
)
def test_static_marker_drift_is_rejected(
    skips: frozenset[str], xfails: frozenset[str], needle: str
) -> None:
    errors = _verify(_snapshot(skips=skips, xfails=xfails))
    assert any(needle in error for error in errors)


@pytest.mark.parametrize("plugins", [frozenset(), frozenset({"alpha", "beta"})])
def test_missing_or_extra_active_plugin_is_rejected(plugins: frozenset[str]) -> None:
    assert any("active plugin" in error for error in _verify(active_plugins=plugins))


def test_empty_plugin_mapping_is_rejected() -> None:
    document = _document()
    document["plugins"]["alpha"] = []  # type: ignore[index]
    with pytest.raises(guard.CollectionGuardError, match="non-empty"):
        guard._parse_manifest(document)


def test_casefold_duplicate_plugin_mapping_is_rejected() -> None:
    document = _document()
    document["plugins"]["ALPHA"] = ["tests/plugins/test_alpha.py"]  # type: ignore[index]
    document["inventory"]["active_plugins"] = 2  # type: ignore[index]
    with pytest.raises(guard.CollectionGuardError, match="case-insensitive duplicates"):
        guard._parse_manifest(document)


def test_plugin_mapping_must_exactly_match_plugin_group() -> None:
    document = _document()
    document["plugins"]["alpha"] = ["tests/test_core.py"]  # type: ignore[index]
    with pytest.raises(guard.CollectionGuardError, match="exactly match"):
        guard._parse_manifest(document)


def test_plugin_manifest_name_must_match_directory(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    plugin = plugins / "alpha"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(json.dumps({"name": "beta"}), encoding="utf-8")
    with pytest.raises(guard.CollectionGuardError, match="exactly match"):
        guard.discover_active_plugins(tmp_path, "plugins")


def test_discovery_rejects_symlink_or_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    linked = tests / "test_linked.py"
    linked.write_text("def test_ok(): pass\n", encoding="utf-8")
    original = guard._is_link_like
    monkeypatch.setattr(
        guard,
        "_is_link_like",
        lambda path: path == linked or original(path),
    )
    with pytest.raises(guard.CollectionGuardError, match="symlink or junction"):
        guard.discover_test_modules(tmp_path, "tests")


def test_collection_failure_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.setattr(guard.pytest, "main", lambda *args, **kwargs: pytest.ExitCode.USAGE_ERROR)
    with pytest.raises(guard.CollectionGuardError, match="collection failed"):
        guard.collect_snapshot(tmp_path, "tests")


def test_pytest_addopts_injection_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k only_one_test")
    with pytest.raises(guard.CollectionGuardError, match="PYTEST_ADDOPTS"):
        guard.collect_snapshot(tmp_path, "tests")


def test_collection_invocation_is_explicit_and_unfiltered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    captured: list[str] = []

    def fake_pytest_main(args, *, plugins):
        captured.extend(args)
        assert len(plugins) == 1
        return pytest.ExitCode.OK

    monkeypatch.setattr(guard.pytest, "main", fake_pytest_main)
    guard.collect_snapshot(tmp_path, "tests")
    assert "--strict-markers" in captured
    assert captured.count("-p") == 2
    assert "no:cacheprovider" in captured
    assert "no:terminal" in captured
    assert not ({"-k", "-m", "--ignore", "--deselect"} & set(captured))


def test_collection_finish_records_only_items_with_static_markers(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    module = tests / "test_sample.py"
    module.write_text("def test_ok(): pass\n", encoding="utf-8")

    class FakeItem:
        def __init__(self, name: str, markers: set[str]) -> None:
            self.nodeid = f"tests/test_sample.py::{name}"
            self.path = module
            self._markers = markers

        def iter_markers(self, *, name: str):
            return iter((object(),)) if name in self._markers else iter(())

    inventory = guard._CollectionInventory(tmp_path)
    items = [
        FakeItem("test_plain", set()),
        FakeItem("test_skipif", {"skipif"}),
        FakeItem("test_xfail", {"xfail"}),
    ]
    inventory._record_items(items)
    snapshot = inventory.snapshot()

    assert snapshot.skip_nodeids == frozenset({"tests/test_sample.py::test_skipif"})
    assert snapshot.xfail_nodeids == frozenset({"tests/test_sample.py::test_xfail"})


def test_collection_wrapper_samples_after_trylast_finish_hook(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "def test_first(): pass\ndef test_second(): pass\n",
        encoding="utf-8",
    )
    (tests / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.hookimpl(trylast=True)\n"
        "def pytest_collection_finish(session):\n"
        "    session.items[:] = session.items[:1]\n",
        encoding="utf-8",
    )

    snapshot = guard.collect_snapshot(tmp_path, "tests")

    assert [item.nodeid for item in snapshot.items] == ["tests/test_sample.py::test_first"]
