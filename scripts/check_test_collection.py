"""Verify pytest collection against an exact, reviewed TOML inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


SCHEMA_VERSION = 1
DEFAULT_MANIFEST = "tests/test_collection_manifest.toml"
NON_PLUGIN_GROUPS = frozenset({"core", "integration", "tooling"})
PLUGIN_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]*\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class CollectionGuardError(ValueError):
    """Raised when the collection manifest or repository inventory is invalid."""


@dataclass(frozen=True)
class CountRule:
    items: int
    modules: int


@dataclass(frozen=True)
class ModuleRule:
    items: int
    group: str
    nodeids_sha256: str


@dataclass(frozen=True)
class CollectionManifest:
    tests_path: str
    plugins_path: str
    inventory: CountRule
    active_plugins: int
    groups: Mapping[str, CountRule]
    modules: Mapping[str, ModuleRule]
    plugins: Mapping[str, tuple[str, ...]]
    allowed_skips: frozenset[str]
    allowed_xfails: frozenset[str]


@dataclass(frozen=True)
class CollectedItem:
    nodeid: str
    module: str


@dataclass(frozen=True)
class CollectionSnapshot:
    items: tuple[CollectedItem, ...]
    skip_nodeids: frozenset[str] = frozenset()
    xfail_nodeids: frozenset[str] = frozenset()
    deselected_nodeids: tuple[str, ...] = ()
    path_errors: tuple[str, ...] = ()


def _exact_keys(table: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(table)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise CollectionGuardError(
            f"{label} keys are invalid: missing={missing or 'none'}, unknown={unknown or 'none'}"
        )


def _table(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CollectionGuardError(f"{label} must be a TOML table")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise CollectionGuardError(f"{label} must be a positive integer")
    return value


def _canonical_relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CollectionGuardError(f"{label} must be a non-empty string")
    if not value.isascii() or "\x00" in value or ":" in value or "\\" in value:
        raise CollectionGuardError(f"{label} must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CollectionGuardError(f"{label} must be a canonical repository-relative path")
    if path.as_posix() != value:
        raise CollectionGuardError(f"{label} must be a canonical repository-relative path")
    return value


def _casefold_unique(values: Iterable[str], label: str) -> None:
    seen: dict[str, str] = {}
    for value in values:
        folded = value.casefold()
        previous = seen.get(folded)
        if previous is not None:
            raise CollectionGuardError(
                f"{label} contains case-insensitive duplicates: {previous!r}, {value!r}"
            )
        seen[folded] = value


def _nodeid_list(value: object, label: str, modules: Mapping[str, ModuleRule]) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CollectionGuardError(f"{label} must be an array of node ID strings")
    _casefold_unique(value, label)
    nodeids = frozenset(value)
    if len(nodeids) != len(value):
        raise CollectionGuardError(f"{label} contains duplicate node IDs")
    for nodeid in nodeids:
        if "\\" in nodeid or "::" not in nodeid:
            raise CollectionGuardError(f"{label} contains a non-canonical node ID: {nodeid!r}")
        module = _canonical_relative_path(nodeid.split("::", maxsplit=1)[0], label)
        if module not in modules:
            raise CollectionGuardError(f"{label} references an unknown module: {nodeid!r}")
    return nodeids


def _parse_manifest(document: object) -> CollectionManifest:
    root = _table(document, "manifest")
    _exact_keys(
        root,
        {"schema_version", "paths", "inventory", "groups", "modules", "plugins", "allowed_outcomes"},
        "manifest",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != SCHEMA_VERSION:
        raise CollectionGuardError(f"schema_version must be exactly {SCHEMA_VERSION}")

    paths = _table(root["paths"], "paths")
    _exact_keys(paths, {"tests", "plugins"}, "paths")
    tests_path = _canonical_relative_path(paths["tests"], "paths.tests")
    plugins_path = _canonical_relative_path(paths["plugins"], "paths.plugins")
    if tests_path != "tests" or plugins_path != "plugins":
        raise CollectionGuardError("paths.tests and paths.plugins must target tests and plugins")

    inventory_table = _table(root["inventory"], "inventory")
    _exact_keys(inventory_table, {"total_items", "total_modules", "active_plugins"}, "inventory")
    inventory = CountRule(
        items=_positive_int(inventory_table["total_items"], "inventory.total_items"),
        modules=_positive_int(inventory_table["total_modules"], "inventory.total_modules"),
    )
    active_plugin_count = _positive_int(
        inventory_table["active_plugins"], "inventory.active_plugins"
    )

    plugin_table = _table(root["plugins"], "plugins")
    if not plugin_table:
        raise CollectionGuardError("plugins must not be empty")
    _casefold_unique(plugin_table, "plugins")
    plugins: dict[str, tuple[str, ...]] = {}
    for name, raw_modules in plugin_table.items():
        if not PLUGIN_NAME_PATTERN.fullmatch(name):
            raise CollectionGuardError(f"invalid plugin name in manifest: {name!r}")
        if not isinstance(raw_modules, list) or not raw_modules:
            raise CollectionGuardError(f"plugins.{name} must be a non-empty array")
        mapped_modules = tuple(
            _canonical_relative_path(module, f"plugins.{name}") for module in raw_modules
        )
        _casefold_unique(mapped_modules, f"plugins.{name}")
        if len(set(mapped_modules)) != len(mapped_modules):
            raise CollectionGuardError(f"plugins.{name} contains duplicate modules")
        plugins[name] = mapped_modules
    if len(plugins) != active_plugin_count:
        raise CollectionGuardError(
            "inventory.active_plugins does not match the number of plugin mappings"
        )

    group_table = _table(root["groups"], "groups")
    expected_groups = NON_PLUGIN_GROUPS | {f"plugin.{name}" for name in plugins}
    _exact_keys(group_table, set(expected_groups), "groups")
    _casefold_unique(group_table, "groups")
    groups: dict[str, CountRule] = {}
    for name, raw_rule in group_table.items():
        rule = _table(raw_rule, f"groups.{name}")
        _exact_keys(rule, {"items", "modules"}, f"groups.{name}")
        groups[name] = CountRule(
            items=_positive_int(rule["items"], f"groups.{name}.items"),
            modules=_positive_int(rule["modules"], f"groups.{name}.modules"),
        )

    module_table = _table(root["modules"], "modules")
    if not module_table:
        raise CollectionGuardError("modules must not be empty")
    normalized_module_names = [
        _canonical_relative_path(name, "modules key") for name in module_table
    ]
    _casefold_unique(normalized_module_names, "modules")
    modules: dict[str, ModuleRule] = {}
    for name, raw_rule in module_table.items():
        if not name.startswith(f"{tests_path}/") or not PurePosixPath(name).name.startswith("test_"):
            raise CollectionGuardError(f"module path is outside the test inventory: {name!r}")
        if PurePosixPath(name).suffix != ".py":
            raise CollectionGuardError(f"module path must end in .py: {name!r}")
        rule = _table(raw_rule, f"modules.{name}")
        _exact_keys(rule, {"items", "group", "nodeids_sha256"}, f"modules.{name}")
        group = rule["group"]
        digest = rule["nodeids_sha256"]
        if not isinstance(group, str) or group not in groups:
            raise CollectionGuardError(f"modules.{name}.group is unknown")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise CollectionGuardError(f"modules.{name}.nodeids_sha256 must be lowercase SHA-256")
        modules[name] = ModuleRule(
            items=_positive_int(rule["items"], f"modules.{name}.items"),
            group=group,
            nodeids_sha256=digest,
        )

    for plugin, mapped_modules in plugins.items():
        expected = {name for name, rule in modules.items() if rule.group == f"plugin.{plugin}"}
        actual = set(mapped_modules)
        if actual != expected:
            raise CollectionGuardError(
                f"plugins.{plugin} must exactly match modules assigned to group plugin.{plugin}"
            )

    if len(modules) != inventory.modules or sum(rule.items for rule in modules.values()) != inventory.items:
        raise CollectionGuardError("inventory totals do not match module rules")
    for group, expected in groups.items():
        members = [rule for rule in modules.values() if rule.group == group]
        actual = CountRule(items=sum(rule.items for rule in members), modules=len(members))
        if actual != expected:
            raise CollectionGuardError(f"groups.{group} totals do not match module rules")
    if sum(rule.items for rule in groups.values()) != inventory.items or sum(
        rule.modules for rule in groups.values()
    ) != inventory.modules:
        raise CollectionGuardError("group totals do not match inventory totals")

    outcomes = _table(root["allowed_outcomes"], "allowed_outcomes")
    _exact_keys(outcomes, {"skip", "xfail"}, "allowed_outcomes")
    allowed_skips = _nodeid_list(outcomes["skip"], "allowed_outcomes.skip", modules)
    allowed_xfails = _nodeid_list(outcomes["xfail"], "allowed_outcomes.xfail", modules)

    return CollectionManifest(
        tests_path=tests_path,
        plugins_path=plugins_path,
        inventory=inventory,
        active_plugins=active_plugin_count,
        groups=groups,
        modules=modules,
        plugins=plugins,
        allowed_skips=allowed_skips,
        allowed_xfails=allowed_xfails,
    )


def _is_link_like(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CollectionGuardError(f"cannot inspect repository path {path}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _resolve_repo_entry(root: Path, relative: str, *, directory: bool, label: str) -> Path:
    canonical = _canonical_relative_path(relative, label)
    candidate = root.joinpath(*PurePosixPath(canonical).parts)
    current = root
    for part in PurePosixPath(canonical).parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            raise CollectionGuardError(f"{label} does not exist: {canonical}")
        if _is_link_like(current):
            raise CollectionGuardError(f"{label} must not traverse a symlink or junction: {canonical}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CollectionGuardError(f"{label} escapes the repository: {canonical}") from exc
    expected_kind = resolved.is_dir() if directory else stat.S_ISREG(resolved.stat().st_mode)
    if not expected_kind:
        kind = "directory" if directory else "regular file"
        raise CollectionGuardError(f"{label} must be a {kind}: {canonical}")
    return resolved


def load_manifest(repository_root: Path, manifest_relative: str = DEFAULT_MANIFEST) -> CollectionManifest:
    repository_root = repository_root.resolve(strict=True)
    manifest_path = _resolve_repo_entry(
        repository_root, manifest_relative, directory=False, label="manifest path"
    )
    try:
        with manifest_path.open("rb") as source:
            document = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CollectionGuardError(f"cannot parse collection manifest: {exc}") from exc
    return _parse_manifest(document)


def discover_test_modules(repository_root: Path, tests_relative: str) -> frozenset[str]:
    tests_dir = _resolve_repo_entry(
        repository_root, tests_relative, directory=True, label="tests path"
    )
    modules: list[str] = []
    for current_root, directories, files in os.walk(tests_dir, topdown=True, followlinks=False):
        current = Path(current_root)
        for name in [*directories, *files]:
            candidate = current / name
            if _is_link_like(candidate):
                raise CollectionGuardError(
                    f"tests path contains a symlink or junction: {candidate.relative_to(repository_root)}"
                )
        for filename in files:
            if not filename.startswith("test_") or not filename.endswith(".py"):
                continue
            candidate = current / filename
            if not stat.S_ISREG(candidate.stat().st_mode):
                raise CollectionGuardError(f"test module is not a regular file: {candidate}")
            try:
                relative = candidate.resolve(strict=True).relative_to(repository_root).as_posix()
            except (OSError, ValueError) as exc:
                raise CollectionGuardError(f"test module escapes the repository: {candidate}") from exc
            modules.append(_canonical_relative_path(relative, "test module"))
    _casefold_unique(modules, "disk test modules")
    if len(set(modules)) != len(modules):
        raise CollectionGuardError("disk test modules contain duplicate paths")
    return frozenset(modules)


def discover_active_plugins(repository_root: Path, plugins_relative: str) -> frozenset[str]:
    plugins_dir = _resolve_repo_entry(
        repository_root, plugins_relative, directory=True, label="plugins path"
    )
    names: list[str] = []
    for child in plugins_dir.iterdir():
        manifest_path = child / "plugin.json"
        if not manifest_path.exists():
            continue
        if _is_link_like(child) or _is_link_like(manifest_path):
            raise CollectionGuardError(f"active plugin path must not be a symlink or junction: {child}")
        if not child.is_dir() or not stat.S_ISREG(manifest_path.stat().st_mode):
            raise CollectionGuardError(f"active plugin manifest must be a regular file: {manifest_path}")
        try:
            resolved_manifest = manifest_path.resolve(strict=True)
            resolved_manifest.relative_to(repository_root)
            document = json.loads(resolved_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise CollectionGuardError(f"cannot read active plugin manifest {manifest_path}: {exc}") from exc
        if not isinstance(document, dict) or document.get("name") != child.name:
            raise CollectionGuardError(
                f"plugin manifest name must exactly match its directory: {manifest_path}"
            )
        if not PLUGIN_NAME_PATTERN.fullmatch(child.name):
            raise CollectionGuardError(f"invalid active plugin directory name: {child.name!r}")
        names.append(child.name)
    _casefold_unique(names, "active plugins")
    if len(set(names)) != len(names):
        raise CollectionGuardError("active plugins contain duplicate names")
    return frozenset(names)


class _CollectionInventory:
    def __init__(self, repository_root: Path) -> None:
        self.repository_root = repository_root
        self.items: tuple[CollectedItem, ...] = ()
        self.skip_nodeids: frozenset[str] = frozenset()
        self.xfail_nodeids: frozenset[str] = frozenset()
        self.deselected_nodeids: list[str] = []
        self.path_errors: list[str] = []

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        self.deselected_nodeids.extend(item.nodeid for item in items)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_collection_finish(self, session: pytest.Session):
        yield
        self._record_items(session.items)

    def _record_items(self, items: Sequence[pytest.Item]) -> None:
        collected: list[CollectedItem] = []
        skipped: set[str] = set()
        xfailed: set[str] = set()
        for item in items:
            try:
                item_path = Path(str(item.path)).resolve(strict=True)
                module = item_path.relative_to(self.repository_root).as_posix()
                _resolve_repo_entry(
                    self.repository_root, module, directory=False, label=f"collected item {item.nodeid}"
                )
            except (CollectionGuardError, OSError, ValueError) as exc:
                self.path_errors.append(f"{item.nodeid}: {exc}")
                continue
            collected.append(CollectedItem(nodeid=item.nodeid, module=module))
            if any(any(item.iter_markers(name=name)) for name in ("skip", "skipif")):
                skipped.add(item.nodeid)
            if any(item.iter_markers(name="xfail")):
                xfailed.add(item.nodeid)
        self.items = tuple(collected)
        self.skip_nodeids = frozenset(skipped)
        self.xfail_nodeids = frozenset(xfailed)

    def snapshot(self) -> CollectionSnapshot:
        return CollectionSnapshot(
            items=self.items,
            skip_nodeids=self.skip_nodeids,
            xfail_nodeids=self.xfail_nodeids,
            deselected_nodeids=tuple(self.deselected_nodeids),
            path_errors=tuple(self.path_errors),
        )


def collect_snapshot(repository_root: Path, tests_relative: str) -> CollectionSnapshot:
    if os.environ.get("PYTEST_ADDOPTS", "").strip():
        raise CollectionGuardError("PYTEST_ADDOPTS must be empty for exact collection")
    tests_dir = _resolve_repo_entry(
        repository_root, tests_relative, directory=True, label="tests path"
    )
    inventory = _CollectionInventory(repository_root)
    exit_code = pytest.main(
        [
            str(tests_dir),
            "--rootdir",
            str(repository_root),
            "--collect-only",
            "--strict-markers",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:terminal",
            "-o",
            "addopts=",
        ],
        plugins=[inventory],
    )
    if exit_code != pytest.ExitCode.OK:
        raise CollectionGuardError(f"pytest collection failed with exit code {int(exit_code)}")
    return inventory.snapshot()


def nodeids_sha256(nodeids: Iterable[str]) -> str:
    payload = "\n".join(sorted(nodeids)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _set_drift(label: str, expected: set[str], actual: set[str]) -> list[str]:
    errors: list[str] = []
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        errors.append(f"{label} missing: {missing}")
    if extra:
        errors.append(f"{label} extra: {extra}")
    return errors


def verify_snapshot(
    manifest: CollectionManifest,
    snapshot: CollectionSnapshot,
    disk_modules: frozenset[str],
    active_plugins: frozenset[str],
) -> list[str]:
    errors = list(snapshot.path_errors)
    if snapshot.deselected_nodeids:
        errors.append(f"pytest deselected items: {sorted(snapshot.deselected_nodeids)}")

    nodeids = [item.nodeid for item in snapshot.items]
    duplicate_nodeids = sorted(nodeid for nodeid, count in Counter(nodeids).items() if count > 1)
    if duplicate_nodeids:
        errors.append(f"duplicate collected node IDs: {duplicate_nodeids}")
    try:
        _casefold_unique(nodeids, "collected node IDs")
    except CollectionGuardError as exc:
        errors.append(str(exc))

    actual_by_module: dict[str, list[str]] = {}
    for item in snapshot.items:
        actual_by_module.setdefault(item.module, []).append(item.nodeid)
    actual_modules = set(actual_by_module)
    expected_modules = set(manifest.modules)

    if len(snapshot.items) != manifest.inventory.items:
        errors.append(
            f"total item count mismatch: expected={manifest.inventory.items}, actual={len(snapshot.items)}"
        )
    if len(actual_modules) != manifest.inventory.modules:
        errors.append(
            f"total module count mismatch: expected={manifest.inventory.modules}, actual={len(actual_modules)}"
        )
    errors.extend(_set_drift("collected modules", expected_modules, actual_modules))
    errors.extend(_set_drift("disk test modules", expected_modules, set(disk_modules)))

    for module in sorted(expected_modules & actual_modules):
        expected = manifest.modules[module]
        actual_nodeids = actual_by_module[module]
        if len(actual_nodeids) != expected.items:
            errors.append(
                f"module item count mismatch for {module}: expected={expected.items}, "
                f"actual={len(actual_nodeids)}"
            )
        actual_digest = nodeids_sha256(actual_nodeids)
        if actual_digest != expected.nodeids_sha256:
            errors.append(
                f"module node ID digest mismatch for {module}: "
                f"expected={expected.nodeids_sha256}, actual={actual_digest}"
            )

    actual_group_items: Counter[str] = Counter()
    actual_group_modules: Counter[str] = Counter()
    for module, module_nodeids in actual_by_module.items():
        rule = manifest.modules.get(module)
        if rule is None:
            continue
        actual_group_items[rule.group] += len(module_nodeids)
        actual_group_modules[rule.group] += 1
    for group, expected in manifest.groups.items():
        actual = CountRule(
            items=actual_group_items[group],
            modules=actual_group_modules[group],
        )
        if actual != expected:
            errors.append(
                f"group count mismatch for {group}: expected={expected}, actual={actual}"
            )

    errors.extend(_set_drift("active plugins", set(manifest.plugins), set(active_plugins)))
    if len(active_plugins) != manifest.active_plugins:
        errors.append(
            f"active plugin count mismatch: expected={manifest.active_plugins}, actual={len(active_plugins)}"
        )
    errors.extend(
        _set_drift("allowed skip node IDs", set(manifest.allowed_skips), set(snapshot.skip_nodeids))
    )
    errors.extend(
        _set_drift("allowed xfail node IDs", set(manifest.allowed_xfails), set(snapshot.xfail_nodeids))
    )
    return errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    try:
        manifest_relative = _canonical_relative_path(args.manifest, "manifest path")
        manifest = load_manifest(repository_root, manifest_relative)
        disk_modules = discover_test_modules(repository_root, manifest.tests_path)
        active_plugins = discover_active_plugins(repository_root, manifest.plugins_path)
        snapshot = collect_snapshot(repository_root, manifest.tests_path)
        errors = verify_snapshot(manifest, snapshot, disk_modules, active_plugins)
    except CollectionGuardError as exc:
        print(f"test collection verification failed: {exc}")
        return 1
    if errors:
        print("test collection verification failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(
        "test collection verified: "
        f"items={manifest.inventory.items}, modules={manifest.inventory.modules}, "
        f"groups={len(manifest.groups)}, active_plugins={manifest.active_plugins}, "
        f"allowed_skips={len(manifest.allowed_skips)}, "
        f"allowed_xfails={len(manifest.allowed_xfails)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
