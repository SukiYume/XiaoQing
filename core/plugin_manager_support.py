"""Plugin import transactions, path authorization, and shared lifecycle records."""

import asyncio
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import os
import stat
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType, ModuleType
from typing import Any, cast

from .models import (
    canonical_plugin_name,
    canonical_python_entry,
    canonical_python_module_part,
    canonical_relative_path,
)
from .plugin_execution import (
    PluginConcurrency,
    PluginExecutionGate,
)

logger = logging.getLogger(__name__)


class PluginPathError(ValueError):
    """A plugin file is outside the plugin directory or is link-backed."""


def is_link_like(metadata: object) -> bool:
    mode = getattr(metadata, "st_mode", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(mode) or bool(reparse_flag and attributes & reparse_flag)


def _reject_link_components(root: Path, relative: str, description: str) -> Path:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise PluginPathError(f"cannot inspect {description}") from exc
        if is_link_like(metadata):
            raise PluginPathError(f"{description} traverses a link or reparse point")
    return current


def _resolved_root(root: Path, description: str, *, reject_link: bool) -> Path:
    try:
        metadata = root.lstat()
        if reject_link and is_link_like(metadata):
            raise PluginPathError(f"{description} root must not be a link")
        resolved = root.resolve(strict=True)
    except PluginPathError:
        raise
    except OSError as exc:
        raise PluginPathError(f"cannot resolve {description} root") from exc
    if not resolved.is_dir():
        raise PluginPathError(f"{description} root must be a directory")
    return resolved


def resolve_contained_regular_file(
    root: Path,
    relative: str,
    *,
    description: str = "file",
    reject_root_link: bool = True,
) -> Path:
    try:
        normalized = canonical_relative_path(relative, description=description)
    except ValueError as exc:
        raise PluginPathError(str(exc)) from exc
    root_path = Path(root)
    resolved_root = _resolved_root(root_path, description, reject_link=reject_root_link)
    candidate = _reject_link_components(root_path, normalized, description)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise PluginPathError(f"{description} escapes its root") from exc
    if resolved == resolved_root or not resolved.is_file():
        raise PluginPathError(f"{description} must be an ordinary file below its root")
    return resolved


def resolve_contained_directory(
    root: Path,
    relative: str,
    *,
    description: str = "directory",
    reject_root_link: bool = True,
) -> Path:
    try:
        normalized = canonical_relative_path(relative, description=description)
    except ValueError as exc:
        raise PluginPathError(str(exc)) from exc
    root_path = Path(root)
    resolved_root = _resolved_root(root_path, description, reject_link=reject_root_link)
    candidate = _reject_link_components(root_path, normalized, description)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise PluginPathError(f"{description} escapes its root") from exc
    if resolved == resolved_root or not resolved.is_dir():
        raise PluginPathError(f"{description} must be a directory below its root")
    return resolved


def resolve_plugin_root(plugins_dir: Path, plugin_dir: Path) -> Path:
    plugins_path = Path(plugins_dir)
    candidate = Path(plugin_dir)
    try:
        name = canonical_plugin_name(candidate.name)
    except ValueError as exc:
        raise PluginPathError(str(exc)) from exc
    if candidate.absolute().parent != plugins_path.absolute():
        raise PluginPathError("plugin directory must be one direct child")
    return resolve_contained_directory(
        plugins_path,
        name,
        description="plugin directory",
        reject_root_link=False,
    )


def resolve_plugin_entry(plugins_dir: Path, plugin_dir: Path, raw_entry: str) -> Path:
    try:
        entry = canonical_python_entry(raw_entry)
    except ValueError as exc:
        raise PluginPathError(str(exc)) from exc
    plugin_root = resolve_plugin_root(plugins_dir, plugin_dir)
    return resolve_contained_regular_file(plugin_root, entry, description="plugin entry")


def validate_plugin_module_origin(origin: str | None, expected_entry: Path) -> Path:
    if not isinstance(origin, str) or not origin or origin in {"built-in", "frozen"}:
        raise PluginPathError("plugin module has no source origin")
    try:
        expected = Path(expected_entry).resolve(strict=True)
        actual = Path(origin).resolve(strict=True)
    except OSError as exc:
        raise PluginPathError("plugin module origin cannot be resolved") from exc
    if not expected.is_file() or not actual.is_file() or not os.path.samefile(expected, actual):
        raise PluginPathError("plugin module origin does not match its entry")
    return expected


_MAX_PLUGIN_MANIFEST_BYTES = 1024 * 1024
_MAX_PLUGIN_SOURCE_FILE_BYTES = 8 * 1024 * 1024
_MAX_PLUGIN_SOURCE_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_PLUGIN_WATCH_FILE_BYTES = 64 * 1024 * 1024
_MAX_PLUGIN_SNAPSHOT_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_PLUGIN_SNAPSHOT_FILES = 4096
_MAX_PLUGIN_SOURCE_DIRECTORIES = 512
_MAX_PLUGIN_DIRECTORY_ENTRIES = 8192
_MAX_PLUGIN_SCANNED_ENTRIES = 65536
_MAX_PLUGIN_RELATIVE_PATH_BYTES = 1024
_MAX_PLUGIN_RELATIVE_PATH_DEPTH = 32
_PLUGIN_SYNC_WORKERS = 4
_DEFAULT_GLOBAL_SYNC_QUEUE_LIMIT = 256
_WATCH_ERROR_LOG_INTERVAL_SECONDS = 30.0
_PLUGIN_FINGERPRINT_AUDIT_INTERVAL_SECONDS = 30.0
_MAX_MODULE_ORIGIN_CACHE_ENTRIES = 16384
_MAX_RECORDED_SCAN_ERRORS = 8
_MIN_PLUGIN_POLL_INTERVAL_SECONDS = 0.01
_DEFAULT_PLUGIN_POLL_INTERVAL_SECONDS = 3600.0
_PLUGIN_IMPORT_DRAIN_TIMEOUT_SECONDS = 5.0
_PLUGIN_IMPORT_LOCK = threading.RLock()
_PLUGIN_NAMESPACE_OWNERS: dict[str, object] = {}
_WATCH_MANIFEST_LOG_OWNER: ContextVar[object | None] = ContextVar(
    "xiaoqing_watch_manifest_log_owner",
    default=None,
)


class _OwnedImportPath(str):
    """Identity-bearing string inserted only by the manager lease registry."""


@dataclass(slots=True)
class _ProcessImportPathLease:
    """Reference-count one path entry while preserving pre-existing entries."""

    container: Any
    owners: int
    inserted: _OwnedImportPath | None


_ProcessImportPathLeaseKey = tuple[int, str]
_PROCESS_IMPORT_PATH_LEASES: dict[_ProcessImportPathLeaseKey, _ProcessImportPathLease] = {}


def _acquire_process_import_path(container: Any, value: str) -> _ProcessImportPathLeaseKey:
    """Acquire one process-global path without claiming an existing equal value."""

    key = (id(container), value)
    with _PLUGIN_IMPORT_LOCK:
        lease = _PROCESS_IMPORT_PATH_LEASES.get(key)
        if lease is not None and lease.container is container:
            lease.owners += 1
            return key

        inserted: _OwnedImportPath | None = None
        if value not in container:
            inserted = _OwnedImportPath(value)
            container.insert(0, inserted)
        _PROCESS_IMPORT_PATH_LEASES[key] = _ProcessImportPathLease(
            container=container,
            owners=1,
            inserted=inserted,
        )
    return key


def _release_process_import_paths(keys: tuple[_ProcessImportPathLeaseKey, ...]) -> None:
    """Release leases and remove only identity-matched entries inserted here."""

    with _PLUGIN_IMPORT_LOCK:
        for key in reversed(keys):
            lease = _PROCESS_IMPORT_PATH_LEASES.get(key)
            if lease is None or id(lease.container) != key[0]:
                continue
            lease.owners -= 1
            if lease.owners > 0:
                continue
            _PROCESS_IMPORT_PATH_LEASES.pop(key, None)
            inserted = lease.inserted
            if inserted is None:
                continue
            try:
                for index, candidate in enumerate(lease.container):
                    if candidate is inserted:
                        del lease.container[index]
                        break
            except (IndexError, TypeError):
                # External import machinery may replace a package path object.
                # In that case there is no longer an owned identity to remove.
                continue


def _meta_path_identity_index(finder: object) -> int | None:
    """Find a meta-path object without invoking third-party equality hooks."""

    for index, candidate in enumerate(sys.meta_path):
        if candidate is finder:
            return index
    return None


def _remove_meta_path_identity(finder: object) -> bool:
    index = _meta_path_identity_index(finder)
    if index is None:
        return False
    del sys.meta_path[index]
    return True


class _PluginNamespaceTombstone(importlib.abc.MetaPathFinder):
    """Aggregate deny finder for the entire manager-controlled namespace."""

    def find_spec(self, fullname: str, path=None, target=None):  # type: ignore[no-untyped-def]
        root_name, separator, remainder = fullname.partition(".")
        if root_name.casefold() != "plugins":
            return None
        if not separator and root_name == "plugins":
            return None
        plugin_name = remainder.split(".", 1)[0] if separator else "<root-alias>"
        raise ModuleNotFoundError(
            f"plugin namespace has no active authorized generation: plugins.{plugin_name}",
            name=fullname,
        )


_PLUGIN_NAMESPACE_GUARD = _PluginNamespaceTombstone()


def _install_namespace_tombstone_locked(
    *, replace: object | None = None
) -> _PluginNamespaceTombstone:
    """安装覆盖整个 ``plugins`` 命名空间的拒绝查找器。

    调用方必须持有插件导入锁。``replace`` 指向即将失效的代际查找器；先装聚合墓碑
    再移除它，确保两步之间不存在可被普通 ``PathFinder`` 越过的导入窗口。
    """

    if _meta_path_identity_index(_PLUGIN_NAMESPACE_GUARD) is None:
        sys.meta_path.insert(0, _PLUGIN_NAMESPACE_GUARD)
    if replace is not None:
        _remove_meta_path_identity(replace)
    return _PLUGIN_NAMESPACE_GUARD


class _PluginContentFingerprint(int):
    """Integer digest carrying the immutable Python sources it authorized."""

    sources: Mapping[str, bytes]
    manifest_payload: bytes
    file_identities: Mapping[str, tuple[int, int, int, int]]
    captured_at: float

    def __new__(
        cls,
        value: int,
        *,
        sources: Mapping[str, bytes],
        manifest_payload: bytes,
        file_identities: Mapping[str, tuple[int, int, int, int]],
        captured_at: float,
    ) -> "_PluginContentFingerprint":
        instance = int.__new__(cls, value)
        instance.sources = MappingProxyType(dict(sources))
        instance.manifest_payload = manifest_payload
        instance.file_identities = MappingProxyType(dict(file_identities))
        instance.captured_at = captured_at
        return instance


class _SourceOnlyPluginLoader(importlib.machinery.SourceFileLoader):
    """Execute one validated source snapshot without consulting bytecode caches."""

    def __init__(
        self,
        fullname: str,
        path: str,
        *,
        plugin_root: Path,
        relative: str,
        source: bytes,
        on_loaded: Callable[[str, ModuleType], None] | None = None,
        execution_guard: (Callable[[str, ModuleType], AbstractContextManager[None]] | None) = None,
        on_compromised: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(fullname, path)
        self._plugin_root = plugin_root
        self._relative = relative
        self._source = source
        self._on_loaded = on_loaded
        self._execution_guard = execution_guard
        self._on_compromised = on_compromised

    def get_code(self, fullname: str):  # type: ignore[no-untyped-def]
        if fullname != self.name:
            raise ImportError(f"source loader name mismatch: {fullname}")
        try:
            source_path = resolve_contained_regular_file(
                self._plugin_root,
                self._relative,
                description=f"plugin source {fullname}",
            )
            if source_path != Path(self.path):
                raise PluginPathError("plugin source loader path is not canonical")
            # The bytes come from the exact stable fingerprint that authorized
            # this generation.  Reading the live path again would reopen an ABA
            # window and would let later lazy imports mix deployment generations.
            return self.source_to_code(self._source, str(source_path))
        except (OSError, PluginPathError) as exc:
            raise ImportError(f"cannot read a stable authorized source for {fullname}") from exc

    def exec_module(self, module: ModuleType) -> None:
        # Compile and perform every live-path containment check before the
        # generation records that arbitrary plugin bytecode has started.
        code = self.get_code(self.name)
        if code is None:
            raise ImportError(f"authorized plugin source produced no code: {self.name}")
        guard = (
            self._execution_guard(self.name, module)
            if self._execution_guard is not None
            else nullcontext()
        )
        with guard:
            exec(code, ModuleType.__getattribute__(module, "__dict__"))
            if sys.modules.get(self.name) is not module:
                if self._on_compromised is not None:
                    self._on_compromised(
                        f"plugin source replaced its canonical module: {self.name}"
                    )
                raise ImportError(f"plugin source replaced its canonical module: {self.name}")
            if self._on_loaded is not None:
                self._on_loaded(self.name, module)


class _SourceOnlyNamespaceLoader(importlib.abc.Loader):
    """Record an authorized namespace package in the active generation."""

    def __init__(
        self,
        fullname: str,
        *,
        on_loaded: Callable[[str, ModuleType], None] | None,
        execution_guard: Callable[[str, ModuleType], AbstractContextManager[None]],
        on_compromised: Callable[[str], None],
    ) -> None:
        self._fullname = fullname
        self._on_loaded = on_loaded
        self._execution_guard = execution_guard
        self._on_compromised = on_compromised

    def create_module(self, spec):  # type: ignore[no-untyped-def]
        return None

    def exec_module(self, module: ModuleType) -> None:
        with self._execution_guard(self._fullname, module):
            if sys.modules.get(self._fullname) is not module:
                self._on_compromised(
                    f"plugin namespace lost its canonical module: {self._fullname}"
                )
                raise ImportError(f"plugin namespace lost its canonical module: {self._fullname}")
            if self._on_loaded is not None:
                self._on_loaded(self._fullname, module)


class _SourceOnlyPluginFinder(importlib.abc.MetaPathFinder):
    """Supply source-only specs for one plugin during its import transaction."""

    def __init__(
        self,
        plugin_root: Path,
        plugin_name: str,
        sources: Mapping[str, bytes],
        on_loaded: Callable[[str, ModuleType], None] | None = None,
        is_current: Callable[["_SourceOnlyPluginFinder"], bool] | None = None,
        on_compromised: Callable[[str], None] | None = None,
    ) -> None:
        self._plugin_root = plugin_root
        self._prefix = f"plugins.{plugin_name}"
        self._sources = MappingProxyType(dict(sources))
        self._on_loaded = on_loaded
        self._is_current = is_current
        self._on_compromised = on_compromised
        self._active = True
        self._active_loads = 0
        self._active_threads: dict[int, int] = {}
        self._seen_module_names: set[str] = set()
        self._compromised = False
        self._publication_paused = False
        self._on_execution_started: Callable[[str, ModuleType], None] | None = None
        self._idle = threading.Condition(_PLUGIN_IMPORT_LOCK)

    @property
    def compromised(self) -> bool:
        with _PLUGIN_IMPORT_LOCK:
            return self._compromised

    def _mark_compromised(self, reason: str) -> None:
        callback: Callable[[str], None] | None
        with _PLUGIN_IMPORT_LOCK:
            self._compromised = True
            callback = self._on_compromised
        if callback is not None:
            callback(reason)

    @contextmanager
    def execution_guard(self, fullname: str, module: ModuleType):
        """Lease this exact generation while one module body is executing."""

        thread_id = threading.get_ident()
        with _PLUGIN_IMPORT_LOCK:
            if not self._active or self._publication_paused:
                raise ImportError(f"plugin generation is draining: {self._prefix}")
            if self._is_current is not None and not self._is_current(self):
                raise ImportError(f"stale plugin generation loader: {fullname}")
            if sys.modules.get(fullname) is not module:
                raise ImportError(f"plugin module cache binding changed: {fullname}")
            self._active_loads += 1
            self._active_threads[thread_id] = self._active_threads.get(thread_id, 0) + 1
            self._seen_module_names.add(fullname)
        try:
            if self._on_execution_started is not None:
                self._on_execution_started(fullname, module)
            yield
        except BaseException as exc:
            if isinstance(exc, PluginPathError):
                self._mark_compromised(f"module ownership validation failed: {fullname}")
            raise
        finally:
            with _PLUGIN_IMPORT_LOCK:
                self._active_loads -= 1
                remaining = self._active_threads[thread_id] - 1
                if remaining:
                    self._active_threads[thread_id] = remaining
                else:
                    self._active_threads.pop(thread_id, None)
                self._idle.notify_all()

    @contextmanager
    def transaction_guard(self):
        """Keep purge behind a complete manager-driven import transaction."""

        thread_id = threading.get_ident()
        with _PLUGIN_IMPORT_LOCK:
            if not self._active or self._publication_paused:
                raise ImportError(f"plugin generation is draining: {self._prefix}")
            if self._is_current is not None and not self._is_current(self):
                raise ImportError(f"stale plugin generation loader: {self._prefix}")
            self._active_loads += 1
            self._active_threads[thread_id] = self._active_threads.get(thread_id, 0) + 1
        try:
            yield
        finally:
            with _PLUGIN_IMPORT_LOCK:
                self._active_loads -= 1
                remaining = self._active_threads[thread_id] - 1
                if remaining:
                    self._active_threads[thread_id] = remaining
                else:
                    self._active_threads.pop(thread_id, None)
                self._idle.notify_all()

    def deactivate_and_wait(
        self,
        *,
        timeout: float = _PLUGIN_IMPORT_DRAIN_TIMEOUT_SECONDS,
    ) -> tuple[str, ...]:
        """Become a blocking tombstone and wait for loader bodies to leave."""

        deadline = time.monotonic() + timeout
        thread_id = threading.get_ident()
        with _PLUGIN_IMPORT_LOCK:
            self._active = False
            self._publication_paused = True
            if self._active_threads.get(thread_id):
                self._compromised = True
                raise PluginPathError(
                    f"plugin import attempted to drain its own generation: {self._prefix}"
                )
            while self._active_loads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._compromised = True
                    raise PluginPathError(f"timed out draining plugin imports: {self._prefix}")
                self._idle.wait(remaining)
            return tuple(sorted(self.possible_module_names() | self._seen_module_names))

    def pause_for_publication(
        self,
        *,
        timeout: float = _PLUGIN_IMPORT_DRAIN_TIMEOUT_SECONDS,
    ) -> tuple[str, ...]:
        """Stop new loader leases and wait for detached imports to settle."""

        deadline = time.monotonic() + timeout
        thread_id = threading.get_ident()
        with _PLUGIN_IMPORT_LOCK:
            if not self._active or self._compromised:
                raise PluginPathError(f"cannot publish a closed generation: {self._prefix}")
            self._publication_paused = True
            if self._active_threads.get(thread_id):
                self._compromised = True
                raise PluginPathError(
                    f"plugin import attempted to publish its own generation: {self._prefix}"
                )
            while self._active_loads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._compromised = True
                    raise PluginPathError(f"timed out quiescing plugin imports: {self._prefix}")
                self._idle.wait(remaining)
            return tuple(sorted(self.possible_module_names() | self._seen_module_names))

    def resume_after_publication(self) -> None:
        """Reopen lazy-import admission after an exact publication commit."""

        with _PLUGIN_IMPORT_LOCK:
            if (
                not self._active
                or self._compromised
                or self._active_loads
                or not self._publication_paused
            ):
                raise PluginPathError(f"cannot resume plugin generation: {self._prefix}")
            self._publication_paused = False

    def possible_module_names(self) -> set[str]:
        names = {self._prefix}
        for relative in self._sources:
            parts = PurePosixPath(relative).parts
            if not parts or parts[-1] != "__init__.py":
                module_parts = parts[:-1] + (Path(parts[-1]).stem,) if parts else ()
            else:
                module_parts = parts[:-1]
            for index in range(1, len(module_parts) + 1):
                names.add(f"{self._prefix}." + ".".join(module_parts[:index]))
        return names

    @staticmethod
    def _lstat_optional(path: Path) -> os.stat_result | None:
        try:
            return path.lstat()
        except FileNotFoundError:
            return None

    def _source_spec(
        self,
        fullname: str,
        relative: str,
        *,
        source: bytes,
        package_dir: Path | None = None,
    ):
        source_path = resolve_contained_regular_file(
            self._plugin_root,
            relative,
            description=f"plugin import {fullname}",
        )
        loader = _SourceOnlyPluginLoader(
            fullname,
            str(source_path),
            plugin_root=self._plugin_root,
            relative=relative,
            source=source,
            on_loaded=self._on_loaded,
            execution_guard=self.execution_guard,
            on_compromised=self._mark_compromised,
        )
        search_locations = None if package_dir is None else [str(package_dir)]
        return importlib.util.spec_from_file_location(
            fullname,
            source_path,
            loader=loader,
            submodule_search_locations=search_locations,
        )

    def find_spec(self, fullname: str, path=None, target=None):  # type: ignore[no-untyped-def]
        if fullname == self._prefix:
            suffix_parts: tuple[str, ...] = ()
        elif fullname.startswith(f"{self._prefix}."):
            suffix_parts = tuple(fullname[len(self._prefix) + 1 :].split("."))
        else:
            return None
        with _PLUGIN_IMPORT_LOCK:
            if not self._active or self._publication_paused:
                raise ModuleNotFoundError(
                    f"plugin generation is draining: {self._prefix}",
                    name=fullname,
                )
            if self._is_current is not None and not self._is_current(self):
                raise ModuleNotFoundError(
                    f"stale plugin generation finder: {self._prefix}",
                    name=fullname,
                )
        if any(not canonical_python_module_part(part) for part in suffix_parts):
            raise ImportError(f"non-canonical plugin module name: {fullname}")

        relative_stem = "/".join(suffix_parts)
        module_relative = f"{relative_stem}.py" if relative_stem else None
        package_prefix = f"{relative_stem}/" if relative_stem else ""
        init_relative = f"{package_prefix}__init__.py"
        module_present = module_relative in self._sources if module_relative is not None else False
        package_present = init_relative in self._sources or any(
            relative.startswith(package_prefix) for relative in self._sources
        )
        if module_present and package_present:
            raise ImportError(f"ambiguous plugin module/package layout: {fullname}")
        if module_present:
            assert module_relative is not None
            return self._source_spec(
                fullname,
                module_relative,
                source=self._sources[module_relative],
            )
        if not package_present:
            # This finder owns the complete plugin namespace.  Falling through
            # would let PathFinder execute an orphan .pyc, extension module, or
            # a source file added after this generation's fingerprint.
            raise ModuleNotFoundError(
                f"module is not present in the authorized plugin snapshot: {fullname}",
                name=fullname,
            )

        package_dir = self._plugin_root.joinpath(*suffix_parts)
        package_info = self._lstat_optional(package_dir)
        if package_info is None:
            raise ImportError(f"authorized plugin package disappeared: {fullname}")
        if is_link_like(package_info) or not stat.S_ISDIR(package_info.st_mode):
            raise ImportError(f"unsafe plugin package directory: {fullname}")
        try:
            resolved_package = package_dir.resolve(strict=True)
            resolved_package.relative_to(self._plugin_root)
        except (OSError, ValueError) as exc:
            raise ImportError(f"plugin package escapes its root: {fullname}") from exc
        if resolved_package != package_dir:
            raise ImportError(f"plugin package path is not canonical: {fullname}")

        if init_relative in self._sources:
            return self._source_spec(
                fullname,
                init_relative,
                source=self._sources[init_relative],
                package_dir=package_dir,
            )

        loader = _SourceOnlyNamespaceLoader(
            fullname,
            on_loaded=self._on_loaded,
            execution_guard=self.execution_guard,
            on_compromised=self._mark_compromised,
        )
        spec = importlib.machinery.ModuleSpec(fullname, loader=loader, is_package=True)
        spec.submodule_search_locations = [str(package_dir)]
        return spec


@dataclass(slots=True)
class _ModuleImportBarrierJob:
    names: tuple[str, ...]
    callback: Callable[[], None] | None = None
    completed: threading.Event = field(default_factory=threading.Event)
    errors: list[BaseException] = field(default_factory=list)
    state: str = "waiting"


@dataclass(frozen=True, slots=True)
class _ModuleImportBarrierCapability:
    available: bool
    reason: str | None = None


class _ModuleImportBarrierCoordinator:
    """Bound module-lock waits to one process-wide daemon worker."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._capability_lock = threading.Lock()
        self._capability: _ModuleImportBarrierCapability | None = None
        self._job: _ModuleImportBarrierJob | None = None
        self._busy = False
        self._worker: threading.Thread | None = None

    @staticmethod
    def _lock_getter() -> Callable[[str], Any]:
        if sys.implementation.name != "cpython":
            raise PluginPathError("safe plugin import draining requires CPython")
        bootstrap = getattr(importlib, "_bootstrap", None)
        getter = getattr(bootstrap, "_get_module_lock", None)
        if not callable(getter):
            raise PluginPathError("CPython module-lock API is unavailable")
        return cast(Callable[[str], Any], getter)

    @classmethod
    def _probe_capability(cls) -> _ModuleImportBarrierCapability:
        """Verify that imports actually honor the private module barrier.

        Attribute presence is not enough: a new interpreter can retain the
        private names while changing their semantics.  The probe publishes a
        synthetic initializing module, owns its module lock, and confirms that
        an import in another thread cannot cross the barrier until release.
        """

        try:
            getter = cls._lock_getter()
        except Exception as exc:
            reason = str(exc) if isinstance(exc, PluginPathError) else type(exc).__name__
            return _ModuleImportBarrierCapability(
                False,
                f"module-lock capability is unavailable: {reason}",
            )

        probe_name = f"__xiaoqing_plugin_import_barrier_probe_{os.getpid()}_{time.monotonic_ns()}__"
        try:
            lock = getter(probe_name)
        except BaseException as exc:
            return _ModuleImportBarrierCapability(
                False,
                f"module lock creation failed: {type(exc).__name__}",
            )
        acquire = getattr(lock, "acquire", None)
        release = getattr(lock, "release", None)
        if not callable(acquire) or not callable(release):
            return _ModuleImportBarrierCapability(
                False,
                "CPython module-lock object is incompatible",
            )

        spec = importlib.machinery.ModuleSpec(probe_name, loader=None)
        try:
            vars(spec)["_initializing"] = True
        except BaseException as exc:
            return _ModuleImportBarrierCapability(
                False,
                f"ModuleSpec initialization marker is unavailable: {type(exc).__name__}",
            )
        if getattr(spec, "_initializing", None) is not True:
            return _ModuleImportBarrierCapability(
                False,
                "ModuleSpec initialization marker is not observable",
            )

        module = ModuleType(probe_name)
        module.__spec__ = spec
        started = threading.Event()
        completed = threading.Event()
        errors: list[BaseException] = []

        def import_probe() -> None:
            started.set()
            try:
                importlib.import_module(probe_name)
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(
            target=import_probe,
            name="xiaoqing-plugin-import-capability-probe",
            daemon=True,
        )
        acquired = False
        worker_started = False
        failure: str | None = None
        try:
            acquire()
            acquired = True
            sys.modules[probe_name] = module
            worker.start()
            worker_started = True
            if not started.wait(0.5):
                failure = "module import probe thread did not start"
            elif completed.wait(0.02):
                failure = "module imports do not honor the initialization lock barrier"
        except BaseException as exc:
            failure = f"module import barrier probe failed: {type(exc).__name__}"
        finally:
            vars(spec)["_initializing"] = False
            if acquired:
                release()

        if worker_started:
            worker.join(0.5)
        if sys.modules.get(probe_name) is module:
            sys.modules.pop(probe_name, None)
        if worker_started and worker.is_alive():
            return _ModuleImportBarrierCapability(
                False,
                "module import barrier did not release the waiting import",
            )
        if failure is not None:
            return _ModuleImportBarrierCapability(False, failure)
        if errors:
            return _ModuleImportBarrierCapability(
                False,
                f"module import barrier probe import failed: {type(errors[0]).__name__}",
            )
        return _ModuleImportBarrierCapability(True)

    def capability(self) -> _ModuleImportBarrierCapability:
        cached = self._capability
        if cached is not None:
            return cached
        with self._capability_lock:
            cached = self._capability
            if cached is None:
                cached = self._probe_capability()
                self._capability = cached
        return cached

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._job is None:
                    self._condition.wait()
                job = self._job
                self._job = None
                self._busy = True
            locks: list[Any] = []
            try:
                lock_getter = self._lock_getter()
                for module_name in job.names:
                    with self._condition:
                        if job.state == "cancelled":
                            break
                    module_lock = lock_getter(module_name)
                    module_lock.acquire()
                    locks.append(module_lock)
                with self._condition:
                    if job.state != "cancelled":
                        job.state = "running"
                        should_run = True
                    else:
                        should_run = False
                if should_run and job.callback is not None:
                    job.callback()
            except BaseException as exc:
                job.errors.append(exc)
            finally:
                for module_lock in reversed(locks):
                    module_lock.release()
                with self._condition:
                    if job.state != "cancelled":
                        job.state = "done"
                    self._busy = False
                    self._condition.notify_all()
                job.completed.set()

    def _submit(
        self,
        names: tuple[str, ...],
        *,
        timeout: float,
        callback: Callable[[], None] | None,
    ) -> None:
        deadline = time.monotonic() + timeout
        job = _ModuleImportBarrierJob(names=names, callback=callback)
        with self._condition:
            while self._busy or self._job is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PluginPathError("timed out waiting for the plugin import barrier worker")
                self._condition.wait(remaining)
            self._job = job
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run,
                    name="xiaoqing-plugin-import-barrier",
                    daemon=True,
                )
                self._worker.start()
            self._condition.notify()
        remaining = max(0.0, deadline - time.monotonic())
        if not job.completed.wait(remaining):
            with self._condition:
                if job.state == "waiting":
                    job.state = "cancelled"
                    raise PluginPathError("timed out waiting for plugin import module locks")
            # The bounded internal commit already owns every requested module
            # lock.  It contains no filesystem, plugin, router, or await calls,
            # so let that atomic commit finish rather than report a false abort.
            job.completed.wait()
        if job.state == "cancelled":
            raise PluginPathError("timed out waiting for plugin import module locks")
        if job.errors:
            raise PluginPathError("failed to cross plugin import module locks") from job.errors[0]

    def cross(self, names: tuple[str, ...], *, timeout: float) -> None:
        if not self.capability().available:
            return
        self._submit(names, timeout=timeout, callback=None)

    def run_locked(
        self,
        names: tuple[str, ...],
        callback: Callable[[], None],
        *,
        timeout: float,
    ) -> None:
        if not self.capability().available:
            callback()
            return
        self._submit(names, timeout=timeout, callback=callback)


_PLUGIN_IMPORT_BARRIER_COORDINATOR = _ModuleImportBarrierCoordinator()


def _validate_plugin_name(name: str) -> bool:
    """
    验证插件名称是否安全

    Args:
        name: 插件名称

    Returns:
        是否安全（只包含字母数字下划线）
    """
    try:
        canonical_plugin_name(name)
    except ValueError:
        return False
    return True


@dataclass
class PluginDefinition:
    name: str
    version: str
    entry: str
    commands: list[dict[str, Any]]
    schedule: list[dict[str, Any]]
    concurrency: PluginConcurrency
    enabled: bool = True  # 插件是否启用
    description: str | None = None
    author: str | None = None
    dependencies: list[str] | None = None
    services: tuple["PluginServiceDefinition", ...] = ()
    uses_services: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()
    watch_files: tuple[str, ...] = ()
    manifest_payload: bytes | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PluginServiceDefinition:
    name: str
    callback: str
    callers: frozenset[str]
    required_capability: str | None = None


@dataclass(frozen=True, slots=True)
class LoadedPluginService:
    owner: str
    definition: PluginServiceDefinition
    callback: Callable[..., Any]


@dataclass
class LoadedPlugin:
    definition: PluginDefinition
    module: ModuleType
    mtime: int | float
    execution_gate: PluginExecutionGate | None = None
    shutdown_attempted: bool = False
    shutdown_completed: bool = False
    shutdown_task: asyncio.Task[Any] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    services: Mapping[str, LoadedPluginService] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    authorized_entry: Path | None = None
    data_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class _PluginDataDirectory:
    """Exact filesystem identities captured for one writable data directory."""

    data_root: Path
    path: Path
    root_identity: os.stat_result
    data_identity: os.stat_result


@dataclass(slots=True)
class _PluginLoadTransaction:
    """Strong references owned by one imported-but-not-yet-live generation."""

    definition: PluginDefinition
    gate: PluginExecutionGate
    mtime: float
    track_init_task: bool = True
    import_attempted: bool = False
    import_completed: bool = False
    uncertain_external_code: bool = False
    unowned_canonical_namespace: bool = False
    namespace_claim_new: bool = False
    authorized_entry: Path | None = None
    module: ModuleType | None = None
    init_task: asyncio.Task[Any] | None = None


@dataclass(frozen=True, slots=True)
class _ReloadAuthorization:
    """Manifest and source fingerprint authorized for one reload attempt."""

    plugin_dir: Path
    definition: PluginDefinition
    mtime: int | float
    definition_changed: bool


@dataclass(frozen=True, slots=True)
class _RetiredPluginGeneration:
    """A cleanly stopped generation retained until candidate publication."""

    plugin: LoadedPlugin
    state: dict[str, Any]
    state_snapshot: dict[str, Any]
    gate: PluginExecutionGate
    modules: Mapping[str, ModuleType]


@dataclass(slots=True)
class _ReloadCandidateGeneration:
    """Mutable candidate facts needed for deterministic rollback."""

    gate: PluginExecutionGate
    transaction: _PluginLoadTransaction
    plugin: LoadedPlugin | None = None
    authorization: bool | None = None
    authorization_error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _LifecycleTaskOutcome:
    """Value-or-error envelope preventing fatal plugin errors escaping a Task early."""

    value: Any = None
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _PluginDirectoryScan:
    """One best-effort directory snapshot used by the long-lived watcher.

    ``complete`` is false when iteration of the plugin root stopped early.  In
    that case the snapshot is still useful for reconciling paths already seen,
    but it is not authoritative evidence that any live plugin was deleted.
    Individual paths whose metadata could not be read are retained in
    ``uncertain_names`` for the same reason.
    """

    directories: tuple[Path, ...]
    uncertain_names: frozenset[str]
    complete: bool
    errors: tuple[tuple[Path | None, OSError], ...]
    error_count: int
