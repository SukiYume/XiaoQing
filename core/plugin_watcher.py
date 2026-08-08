# mypy: disable-error-code=attr-defined
"""Plugin directory discovery, authorization snapshots, and watcher reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import logging
import os
import stat
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType, ModuleType
from typing import Any

from .constants import PLUGIN_INIT_TIMEOUT_SECONDS
from .exceptions import PluginLifecycleFatalError, PluginLoadError
from .models import (
    PluginManifest,
)
from .plugin_execution import (
    PluginExecutionGate,
    call_plugin_callback,
    callback_accepts_positional_context,
)
from .plugin_manager_support import (
    _MAX_PLUGIN_DIRECTORY_ENTRIES,
    _MAX_PLUGIN_MANIFEST_BYTES,
    _MAX_PLUGIN_RELATIVE_PATH_BYTES,
    _MAX_PLUGIN_RELATIVE_PATH_DEPTH,
    _MAX_PLUGIN_SCANNED_ENTRIES,
    _MAX_PLUGIN_SNAPSHOT_FILES,
    _MAX_PLUGIN_SNAPSHOT_TOTAL_BYTES,
    _MAX_PLUGIN_SOURCE_DIRECTORIES,
    _MAX_PLUGIN_SOURCE_FILE_BYTES,
    _MAX_PLUGIN_SOURCE_TOTAL_BYTES,
    _MAX_PLUGIN_WATCH_FILE_BYTES,
    _MAX_RECORDED_SCAN_ERRORS,
    _PLUGIN_FINGERPRINT_AUDIT_INTERVAL_SECONDS,
    _PLUGIN_IMPORT_LOCK,
    _WATCH_ERROR_LOG_INTERVAL_SECONDS,
    _WATCH_MANIFEST_LOG_OWNER,
    PluginDefinition,
    PluginPathError,
    PluginServiceDefinition,
    _PluginContentFingerprint,
    _PluginDirectoryScan,
    _PluginLoadTransaction,
    _ReloadAuthorization,
    _remove_meta_path_identity,
    _SourceOnlyPluginFinder,
    _validate_plugin_name,
    is_link_like,
    resolve_contained_regular_file,
    resolve_plugin_entry,
    resolve_plugin_root,
    validate_plugin_module_origin,
)
from .router import CommandSpec, build_command_catalog_node

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ManifestRejection:
    """A manifest decision that must not be collapsed into an absent manifest."""

    bucket: str


class PluginWatcherMixin:
    _next_watch_waiter_id: int
    _poll_interval: float
    _poll_revision: int
    _watch_waiters: dict[
        int,
        tuple[asyncio.AbstractEventLoop, asyncio.Event],
    ]

    def _is_plugin_dir(self, path: Path) -> bool:
        """检查真实、直接的插件目录并排除保留名称。"""
        name = path.name
        if name.startswith("__") or name.startswith("."):
            return False
        if name.endswith("_deprecated"):
            return False
        # ``Path.is_dir()`` intentionally converts many metadata failures to
        # ``False``.  The watcher must distinguish "not a directory" from
        # "could not determine" or it may retire a live plugin during a brief
        # permission/replace race.
        metadata = path.lstat()
        if is_link_like(metadata) or not stat.S_ISDIR(metadata.st_mode):
            return False
        try:
            resolve_plugin_root(self.plugins_dir, path)
        except PluginPathError as exc:
            # Preserve the watcher's distinction between an unsafe path and a
            # transient metadata failure.  The former is positively rejected;
            # the latter remains uncertain until a later scan can classify it.
            cause = exc.__cause__
            if isinstance(cause, OSError):
                raise cause from exc
            return False
        return True

    def _plugin_runtime_names(self) -> set[str]:
        """Return every plugin name represented in managed lifecycle state."""

        names = (
            set(self._plugins)
            | set(self._execution_gates)
            | set(self._plugin_states)
            | set(self._init_task_plugins.values())
            | set(self._pending_finalizers)
            | set(self._quarantined_plugins)
            | set(self._restart_required_plugins)
            | set(self._source_finders)
        )
        names.update(
            definition.name for definition, _module, _mtime in self._pending_plugins.values()
        )
        names.update(service.owner for service in self._services.values())
        return names

    def _has_plugin_runtime(self, name: str) -> bool:
        return name in self._plugin_runtime_names()

    def _definition_is_current(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
        mtime: int | float,
        *,
        module: ModuleType | None = None,
        authorized_entry: Path | None = None,
        transaction: _PluginLoadTransaction | None = None,
    ) -> bool:
        """Revalidate authorization and source fingerprint immediately before publish."""
        try:
            current = self._load_definition(plugin_dir)
            if (
                not isinstance(current, PluginDefinition)
                or not current.enabled
                or current != definition
            ):
                return False
            current_entry = resolve_plugin_entry(
                self.plugins_dir,
                plugin_dir,
                current.entry,
            )
            if authorized_entry is not None and current_entry != authorized_entry:
                return False
            if module is not None:
                entry_parts = current.entry.removesuffix(".py").replace("/", ".")
                self._validate_entry_module(
                    module,
                    f"plugins.{definition.name}.{entry_parts}",
                    current_entry,
                )
            current_fingerprint = self._authorize_plugin_snapshot(plugin_dir, current)
            return self._content_fingerprints_equal(current_fingerprint, mtime)
        except PluginPathError as exc:
            if transaction is not None and transaction.import_attempted:
                transaction.uncertain_external_code = True
            logger.warning("Plugin %s path revalidation failed: %s", definition.name, exc)
            return False
        except Exception as exc:
            logger.warning("Plugin %s revalidation failed: %s", definition.name, exc)
            return False

    async def _definition_is_current_async(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
        mtime: int | float,
        *,
        module: ModuleType | None = None,
        authorized_entry: Path | None = None,
        transaction: _PluginLoadTransaction | None = None,
    ) -> bool:
        """Run publication revalidation without performing file I/O on the loop."""

        return await asyncio.to_thread(
            self._definition_is_current,
            plugin_dir,
            definition,
            mtime,
            module=module,
            authorized_entry=authorized_entry,
            transaction=transaction,
        )

    @staticmethod
    def _content_fingerprints_equal(
        current: int | float,
        authorized: int | float,
    ) -> bool:
        """Compare both the digest and the exact frozen authorization payload."""

        if isinstance(current, _PluginContentFingerprint) and isinstance(
            authorized,
            _PluginContentFingerprint,
        ):
            return (
                int(current) == int(authorized)
                and current.sources == authorized.sources
                and current.manifest_payload == authorized.manifest_payload
            )
        return current == authorized

    def _log_watch_error(
        self,
        bucket: str,
        message: str,
        exc: BaseException,
        *,
        level: int = logging.WARNING,
    ) -> None:
        """Rate-limit recurring watcher failures without losing recovery context."""

        # Callers use a small fixed bucket set (root-scan, path, round).  Do not
        # key this state by attacker-controlled paths or exception text.
        now = time.monotonic()
        next_log_at = self._watch_error_next_log_at.get(bucket, 0.0)
        if now < next_log_at:
            self._watch_error_suppressed[bucket] = self._watch_error_suppressed.get(bucket, 0) + 1
            return

        suppressed = self._watch_error_suppressed.pop(bucket, 0)
        self._watch_error_next_log_at[bucket] = now + _WATCH_ERROR_LOG_INTERVAL_SECONDS
        suffix = f" ({suppressed} similar failure(s) suppressed)" if suppressed else ""
        logger.log(level, "%s: %s%s", message, exc, suffix)

    def _log_manifest_issue(
        self,
        bucket: str,
        level: int,
        message: str,
        *args: object,
        exc: BaseException,
    ) -> None:
        """Use bounded buckets only while a long-lived watch pass is reading."""

        if _WATCH_MANIFEST_LOG_OWNER.get() is self:
            self._log_watch_error(
                f"manifest-{bucket}",
                message % args,
                exc,
                level=level,
            )
            return
        logger.log(level, message, *args)

    def _load_definition_for_watch(
        self,
        plugin_dir: Path,
    ) -> PluginDefinition | _ManifestRejection:
        token = _WATCH_MANIFEST_LOG_OWNER.set(self)
        try:
            return self._load_definition(plugin_dir)
        finally:
            _WATCH_MANIFEST_LOG_OWNER.reset(token)

    def _scan_plugin_directories(self) -> _PluginDirectoryScan:
        """Return a deletion-safe best-effort snapshot of plugin directories."""

        directories: list[Path] = []
        uncertain_names: set[str] = set()
        errors: list[tuple[Path | None, OSError]] = []
        error_count = 0
        complete = True

        try:
            iterator = iter(self.plugins_dir.iterdir())
        except OSError as exc:
            return _PluginDirectoryScan(
                directories=(),
                uncertain_names=frozenset(),
                complete=False,
                errors=((None, exc),),
                error_count=1,
            )
        while True:
            try:
                path = next(iterator)
            except StopIteration:
                break
            except OSError as exc:
                # An iterator that failed midway cannot be resumed reliably.
                # Keep already discovered paths, but never infer deletions from
                # this incomplete root snapshot.
                complete = False
                error_count += 1
                if len(errors) < _MAX_RECORDED_SCAN_ERRORS:
                    errors.append((None, exc))
                break

            try:
                if self._is_plugin_dir(path):
                    directories.append(path)
            except OSError as exc:
                # The name itself is available without another filesystem
                # access.  Preserve a runtime with that name until a later
                # scan can positively classify the path.
                uncertain_names.add(path.name)
                error_count += 1
                if len(errors) < _MAX_RECORDED_SCAN_ERRORS:
                    errors.append((path, exc))

        return _PluginDirectoryScan(
            directories=tuple(sorted(directories, key=lambda item: item.name)),
            uncertain_names=frozenset(uncertain_names),
            complete=complete,
            errors=tuple(errors),
            error_count=error_count,
        )

    async def reconcile_plugins(self) -> None:
        """Converge live plugins to the complete fail-closed manifest snapshot."""

        self._require_hot_reload()
        try:
            async with self._lifecycle_lock.get():
                await self._reconcile_plugins_once()
            await self.wait_inits()
        except PluginLifecycleFatalError:
            raise
        except BaseException as exc:
            if self._is_fatal_base_exception(exc):
                raise PluginLifecycleFatalError("<reconcile>", exc) from None
            raise

    async def _reconcile_plugins_once(self) -> None:
        """Run one serialized manifest-to-runtime convergence pass."""

        scan = await asyncio.to_thread(self._scan_plugin_directories)
        for failed_path, exc in scan.errors:
            target = str(failed_path) if failed_path is not None else str(self.plugins_dir)
            self._log_watch_error(
                "root-scan" if failed_path is None else "path-stat",
                f"Plugin watcher could not inspect {target}; retrying next interval",
                exc,
            )
        if scan.error_count > len(scan.errors):
            self._log_watch_error(
                "path-stat",
                "Plugin watcher omitted additional path metadata failures",
                OSError(f"{scan.error_count - len(scan.errors)} additional failure(s)"),
            )

        plugin_dirs = scan.directories
        current_names = {plugin_dir.name for plugin_dir in plugin_dirs}

        # A removed directory is an explicit request to retire its runtime.
        if scan.complete:
            removed_names = self._plugin_runtime_names() - current_names - set(scan.uncertain_names)
            for existing_name in sorted(removed_names):
                try:
                    if existing_name in self._quarantined_plugins:
                        self.router.clear_plugin(existing_name)
                        self._log_watch_error(
                            "quarantined-deleted-plugin",
                            f"Keeping quarantined plugin {existing_name} after directory "
                            "removal; explicit operator cleanup or restart is required",
                            RuntimeError("quarantined generation is still owned"),
                        )
                        continue
                    logger.info("Detected deleted plugin %s", existing_name)
                    await self._unload_plugin_once(existing_name)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    if self._is_fatal_base_exception(exc):
                        raise
                    self._log_watch_error(
                        "deleted-plugin",
                        f"Plugin watcher could not retire deleted plugin {existing_name}; "
                        "retrying next interval",
                        exc,
                    )
        elif self._plugin_runtime_names() - current_names:
            self._log_watch_error(
                "root-scan",
                "Plugin watcher skipped deletion reconciliation after an incomplete root scan",
                OSError("plugin directory snapshot is incomplete"),
            )

        for plugin_dir in plugin_dirs:
            try:
                await self._reconcile_plugin_path(plugin_dir)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if self._is_fatal_base_exception(exc):
                    raise
                self._log_watch_error(
                    "plugin-path",
                    f"Plugin watcher skipped {plugin_dir.name}; retrying next interval",
                    exc,
                )

    async def _reconcile_plugin_path(self, plugin_dir: Path) -> None:
        """Reconcile one path without allowing routine I/O failure to abort its siblings."""

        directory_name = plugin_dir.name
        if directory_name in self._quarantined_plugins:
            self.router.clear_plugin(directory_name)
            logger.debug(
                "Skipping all automatic lifecycle changes for quarantined plugin %s",
                directory_name,
            )
            return
        try:
            definition = await asyncio.to_thread(self._load_definition_for_watch, plugin_dir)
        except BaseException as exc:
            if self._has_plugin_runtime(directory_name):
                try:
                    await self._unload_plugin_once(directory_name)
                except BaseException as cleanup_error:
                    self._raise_preferred_lifecycle_error(exc, cleanup_error)
            if self._is_fatal_base_exception(exc):
                raise
            self._log_watch_error(
                "plugin-definition",
                f"Plugin {directory_name} definition evaluation failed; "
                "retired fail-closed and retrying next interval",
                exc,
            )
            return
        if isinstance(definition, _ManifestRejection):
            if definition.bucket in {"dependency", "read"}:
                logger.warning(
                    "Plugin %s manifest check is transient (%s); keeping the current "
                    "generation and retrying next interval",
                    directory_name,
                    definition.bucket,
                )
                return
            if self._has_plugin_runtime(directory_name):
                logger.warning(
                    "Unloading plugin %s because its manifest is rejected (%s)",
                    directory_name,
                    definition.bucket,
                )
                await self._unload_plugin_once(directory_name)
            return
        if definition is None:
            # Test doubles and older embedders may still represent a rejected
            # manifest as None; retain the fail-closed retirement behavior.
            if self._has_plugin_runtime(directory_name):
                logger.warning(
                    "Unloading plugin %s because its manifest is unavailable or invalid",
                    directory_name,
                )
                await self._unload_plugin_once(directory_name)
            return

        if not definition.enabled:
            if self._has_plugin_runtime(definition.name):
                logger.info("Detected disabled plugin %s", definition.name)
                await self._unload_plugin_once(definition.name)
            return

        try:
            await asyncio.to_thread(
                resolve_plugin_entry,
                self.plugins_dir,
                plugin_dir,
                definition.entry,
            )
        except PluginPathError as exc:
            existing = self._plugins.get(definition.name)
            transient_cause = exc.__cause__
            transient_io = isinstance(transient_cause, OSError) and not isinstance(
                transient_cause,
                FileNotFoundError,
            )
            if transient_io and existing is not None and definition == existing.definition:
                self._log_watch_error(
                    "plugin-entry",
                    f"Cannot inspect plugin entry for {definition.name}; retrying next interval",
                    exc,
                )
                return
            if existing is not None and definition != existing.definition:
                logger.error(
                    "Plugin %s authorization changed but its replacement entry "
                    "could not be inspected; retiring the old generation: %s",
                    definition.name,
                    exc,
                )
            else:
                self._log_watch_error(
                    "plugin-entry",
                    f"Plugin {definition.name} manifest entry is unsafe or unavailable: "
                    f"{definition.entry}",
                    exc,
                    level=logging.ERROR,
                )
            if existing is not None:
                await self._unload_plugin_once(definition.name)
            return

        if definition.name in self._quarantined_plugins:
            logger.debug("Skipping automatic reload of quarantined plugin %s", definition.name)
            return

        existing = self._plugins.get(definition.name)
        if existing is None:
            try:
                await asyncio.to_thread(
                    self._ensure_plugin_data_dir,
                    definition.name,
                    force=True,
                )
            except (OSError, PluginPathError) as exc:
                logger.error(
                    "Cannot prepare data directory for plugin %s: %s",
                    definition.name,
                    exc,
                )
                return
        try:
            previous = (
                existing.mtime
                if existing is not None and definition == existing.definition
                else None
            )
            mtime = await self._capture_plugin_snapshot_async(
                plugin_dir,
                definition,
                previous=previous,
            )
        except BaseException as exc:
            if self._is_fatal_base_exception(exc):
                if existing is not None:
                    try:
                        await self._unload_plugin_once(definition.name)
                    except BaseException as cleanup_error:
                        self._raise_preferred_lifecycle_error(exc, cleanup_error)
                raise
            if existing is not None and definition != existing.definition:
                logger.error(
                    "Plugin %s authorization changed but its replacement "
                    "fingerprint failed; retiring the old generation: %s",
                    definition.name,
                    exc,
                )
                await self._unload_plugin_once(definition.name)
            else:
                self._log_watch_error(
                    "plugin-fingerprint",
                    f"Cannot fingerprint unchanged plugin {definition.name}; "
                    "retrying next interval",
                    exc,
                )
            return
        if not existing:
            await self._load_new_plugin_from_watch(plugin_dir, definition, mtime)
        elif definition != existing.definition or not self._content_fingerprints_equal(
            mtime,
            existing.mtime,
        ):
            logger.info("Detected changes in plugin %s", definition.name)
            await self._reload_plugin_once(
                definition.name,
                authorization=_ReloadAuthorization(
                    plugin_dir=plugin_dir,
                    definition=definition,
                    mtime=mtime,
                    definition_changed=definition != existing.definition,
                ),
            )
        else:
            # A periodic full audit may produce the same content with a fresh
            # audit timestamp.  Retain it so later stable polls use the cheap
            # metadata path instead of hashing again on every interval.
            existing.mtime = mtime

    async def _load_new_plugin_from_watch(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
        fingerprint: _PluginContentFingerprint,
    ) -> None:
        """Load one newly discovered plugin through the async transaction path."""

        if not _validate_plugin_name(plugin_dir.name):
            logger.warning(
                "Skipping plugin with invalid name '%s': must be a lowercase ASCII "
                "Python identifier",
                plugin_dir.name,
            )
            return
        if definition.name in self._quarantined_plugins:
            logger.warning(
                "Refusing to load quarantined plugin %s; explicit operator cleanup or "
                "restart is required",
                definition.name,
            )
            return
        if self._has_plugin_runtime(definition.name):
            logger.warning(
                "Refusing to create a second runtime generation for plugin %s",
                definition.name,
            )
            return
        authorization = _ReloadAuthorization(
            plugin_dir=plugin_dir,
            definition=definition,
            mtime=fingerprint,
            definition_changed=False,
        )
        candidate = self._create_reload_candidate(definition.name, authorization)
        try:
            await self._load_and_publish_reload_candidate(authorization, candidate)
        except BaseException as exc:
            rollback_clean = await self._rollback_reload_candidate(
                definition.name,
                authorization,
                candidate,
                exc,
            )
            if (
                rollback_clean
                and candidate.transaction.namespace_claim_new
                and not self._has_plugin_runtime(definition.name)
            ):
                self._release_plugin_namespace(definition.name)
            if not isinstance(exc, Exception):
                raise
            logger.error("Plugin %s failed during load: %s", definition.name, exc, exc_info=True)

    def _register_watch_waiter(
        self,
        loop: asyncio.AbstractEventLoop,
        wakeup: asyncio.Event,
    ) -> int:
        with self._watch_waiters_lock:
            self._next_watch_waiter_id += 1
            waiter_id = self._next_watch_waiter_id
            self._watch_waiters[waiter_id] = (loop, wakeup)
            return waiter_id

    def _unregister_watch_waiter(self, waiter_id: int) -> None:
        with self._watch_waiters_lock:
            self._watch_waiters.pop(waiter_id, None)

    def _watch_poll_snapshot(self) -> tuple[float, int]:
        with self._watch_waiters_lock:
            return self._poll_interval, self._poll_revision

    async def _wait_for_watch_poll(
        self,
        wakeup: asyncio.Event,
        interval: float,
    ) -> bool:
        """Return true when a config update interrupted the current wait."""

        try:
            await asyncio.wait_for(wakeup.wait(), timeout=interval)
        except asyncio.TimeoutError:
            return False
        return True

    async def watch(self) -> None:
        self._require_hot_reload()
        loop = asyncio.get_running_loop()
        wakeup = asyncio.Event()
        waiter_id = self._register_watch_waiter(loop, wakeup)
        try:
            while True:
                # Clear before snapshotting: an update before the clear is
                # represented by the new revision/value; an update after it
                # sets the event and interrupts this exact wait.
                wakeup.clear()
                interval, revision = self._watch_poll_snapshot()
                interrupted = await self._wait_for_watch_poll(wakeup, interval)
                _latest_interval, latest_revision = self._watch_poll_snapshot()
                if interrupted or latest_revision != revision:
                    continue
                try:
                    await self.reconcile_plugins()
                except asyncio.CancelledError:
                    raise
                except PluginLifecycleFatalError:
                    # A task-safe carrier for SystemExit/KeyboardInterrupt-style
                    # failures must remain visible to the application supervisor.
                    raise
                except Exception as exc:
                    self._log_watch_error(
                        "round",
                        "Plugin watcher reconciliation failed; retrying next interval",
                        exc,
                    )
        finally:
            self._unregister_watch_waiter(waiter_id)

    def _load_definition(self, plugin_dir: Path) -> PluginDefinition | _ManifestRejection:
        definition_path = plugin_dir / "plugin.json"
        try:
            # ``plugin.json`` is executable authorization data.  Treat it with
            # the same containment/link policy as the Python entry rather than
            # following a symlink or junction outside the plugin root.
            definition_path.lstat()
            plugin_root = resolve_plugin_root(self.plugins_dir, plugin_dir)
            definition_path = resolve_contained_regular_file(
                plugin_root,
                "plugin.json",
                description="plugin manifest",
            )
            # Anchor the snapshot to the file descriptor we actually read.
            # A path-level stat followed by ``read_bytes`` permits an ABA
            # replacement: validation can observe file A, the open can read B,
            # and the final path check can observe A again.  The descriptor's
            # identity closes that window and the extra byte keeps allocation
            # bounded while still distinguishing an oversized manifest.
            with definition_path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                payload = handle.read(_MAX_PLUGIN_MANIFEST_BYTES + 1)
                finished = os.fstat(handle.fileno())
        except FileNotFoundError as exc:
            self._log_manifest_issue(
                "missing",
                logging.WARNING,
                "Missing plugin.json in %s",
                plugin_dir,
                exc=exc,
            )
            return _ManifestRejection("missing")
        except PluginPathError as exc:
            self._log_manifest_issue(
                "invalid",
                logging.ERROR,
                "Invalid plugin.json path in %s: %s",
                plugin_dir,
                exc,
                exc=exc,
            )
            return _ManifestRejection("invalid")
        except OSError as exc:
            self._log_manifest_issue(
                "read",
                logging.ERROR,
                "Cannot read plugin.json in %s: %s",
                plugin_dir,
                exc,
                exc=exc,
            )
            return _ManifestRejection("read")
        try:
            current_definition_path = resolve_contained_regular_file(
                plugin_root,
                "plugin.json",
                description="plugin manifest",
            )
            current = current_definition_path.stat()
            opened_identity = self._watch_file_identity(opened)
            if (
                current_definition_path != definition_path
                or not os.path.samestat(opened, finished)
                or not os.path.samestat(opened, current)
                or self._watch_file_identity(finished) != opened_identity
                or self._watch_file_identity(current) != opened_identity
                or (opened.st_size <= _MAX_PLUGIN_MANIFEST_BYTES and len(payload) != opened.st_size)
            ):
                raise OSError("plugin manifest changed while being read")
        except (OSError, PluginPathError) as exc:
            self._log_manifest_issue(
                "read",
                logging.ERROR,
                "Cannot read a stable plugin.json in %s: %s",
                plugin_dir,
                exc,
                exc=exc,
            )
            return _ManifestRejection("read")
        if len(payload) > _MAX_PLUGIN_MANIFEST_BYTES:
            error = ValueError(f"manifest exceeds {_MAX_PLUGIN_MANIFEST_BYTES} bytes")
            self._log_manifest_issue(
                "invalid",
                logging.ERROR,
                "Invalid plugin.json in %s: manifest exceeds %d bytes",
                plugin_dir,
                _MAX_PLUGIN_MANIFEST_BYTES,
                exc=error,
            )
            return _ManifestRejection("invalid")
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            # Runtime authorization must reflect the current primary manifest;
            # an older AtomicJsonStore backup must never silently re-enable it.
            self._log_manifest_issue(
                "invalid",
                logging.ERROR,
                "Invalid plugin.json in %s: %s",
                plugin_dir,
                exc,
                exc=exc,
            )
            return _ManifestRejection("invalid")

        try:
            manifest = PluginManifest.model_validate(data)
        except Exception as exc:
            self._log_manifest_issue(
                "invalid",
                logging.ERROR,
                "Invalid plugin.json in %s: %s",
                plugin_dir,
                exc,
                exc=exc,
            )
            return _ManifestRejection("invalid")

        try:
            if not self._dependencies_available(manifest):
                return _ManifestRejection("dependency")
        except Exception as exc:
            self._log_manifest_issue(
                "dependency",
                logging.ERROR,
                "Cannot validate dependencies for plugin %s: %s",
                manifest.name,
                exc,
                exc=exc,
            )
            return _ManifestRejection("dependency")

        if manifest.name != plugin_dir.name:
            error = ValueError("manifest name does not match directory name")
            self._log_manifest_issue(
                "invalid",
                logging.ERROR,
                "Invalid plugin.json in %s: name must match directory name (name=%s dir=%s)",
                plugin_dir,
                manifest.name,
                plugin_dir.name,
                exc=error,
            )
            return _ManifestRejection("invalid")

        return PluginDefinition(
            name=manifest.name,
            version=manifest.version,
            entry=manifest.entry,
            commands=[c.model_dump() for c in manifest.commands],
            schedule=[s.model_dump() for s in manifest.schedule],
            concurrency=manifest.concurrency,
            enabled=manifest.enabled,
            description=manifest.description,
            author=manifest.author,
            dependencies=[dependency.name for dependency in manifest.dependencies],
            services=tuple(
                PluginServiceDefinition(
                    name=service.name,
                    callback=service.callback,
                    callers=frozenset(service.callers),
                    required_capability=service.required_capability,
                )
                for service in manifest.services
            ),
            uses_services=frozenset(manifest.uses_services),
            capabilities=frozenset(manifest.capabilities),
            watch_files=tuple(manifest.watch_files),
            manifest_payload=payload,
        )

    def _dependencies_available(self, manifest: PluginManifest) -> bool:
        """Fail closed when a manifest's required Python modules are unavailable."""

        for dependency in manifest.dependencies:
            try:
                available = self._dependency_spec_available(dependency.name)
            except (ImportError, ModuleNotFoundError, OSError, ValueError):
                available = False
            if available:
                continue
            if dependency.required:
                self._log_manifest_issue(
                    "dependency",
                    logging.ERROR,
                    "Plugin %s requires Python dependency %s, but it is not importable",
                    manifest.name,
                    dependency.name,
                    exc=ModuleNotFoundError(dependency.name),
                )
                return False
            self._log_manifest_issue(
                "optional-dependency",
                logging.INFO,
                "Plugin %s optional Python dependency %s is unavailable",
                manifest.name,
                dependency.name,
                exc=ModuleNotFoundError(dependency.name),
            )
        return True

    @staticmethod
    def _dependency_spec_available(module_name: str) -> bool:
        """Resolve a module path without importing any dotted parent package."""

        parts = module_name.split(".")
        if not parts or any(not part.isidentifier() for part in parts) or parts[0] == "plugins":
            return False
        if len(parts) == 1 and parts[0] in sys.builtin_module_names:
            return True

        search_path: list[str] | None = None
        qualified: list[str] = []
        for index, part in enumerate(parts):
            qualified.append(part)
            spec = importlib.machinery.PathFinder.find_spec(".".join(qualified), search_path)
            if spec is None:
                return False
            if index == len(parts) - 1:
                return True
            locations = spec.submodule_search_locations
            if locations is None:
                return False
            search_path = list(locations)
        return False

    @staticmethod
    def _module_origins(module: ModuleType) -> tuple[str, ...]:
        origins: list[str] = []
        spec = getattr(module, "__spec__", None)
        spec_origin = getattr(spec, "origin", None)
        module_file = getattr(module, "__file__", None)
        for origin in (spec_origin, module_file):
            if isinstance(origin, str) and origin not in origins:
                origins.append(origin)
        return tuple(origins)

    @classmethod
    def _validate_entry_module(
        cls,
        module: ModuleType,
        module_name: str,
        authorized_entry: Path,
    ) -> None:
        """Bind a canonical module object to the exact authorized source file."""

        if module.__name__ != module_name:
            raise PluginPathError("plugin module name does not match its canonical entry")
        spec = getattr(module, "__spec__", None)
        if getattr(spec, "name", None) != module_name:
            raise PluginPathError("plugin module spec name is not canonical")
        spec_origin = getattr(spec, "origin", None)
        module_file = getattr(module, "__file__", None)
        # Both values are set by Python source loaders.  Requiring both prevents
        # a stale or synthetic module object from being authorized by only one
        # mutable provenance attribute.
        validate_plugin_module_origin(spec_origin, authorized_entry)
        validate_plugin_module_origin(module_file, authorized_entry)

    @classmethod
    def _validate_cached_plugin_namespace(
        cls,
        plugin_root: Path,
        plugin_name: str,
        entry_module_name: str,
        authorized_entry: Path,
    ) -> None:
        """Reject canonical cache entries owned by another plugin root."""

        canonical = f"plugins.{plugin_name}"
        for module_name, module in list(sys.modules.items()):
            if module_name != canonical and not module_name.startswith(f"{canonical}."):
                continue
            if not isinstance(module, ModuleType):
                raise PluginPathError("canonical plugin cache contains a non-module value")
            if module_name == entry_module_name:
                cls._validate_entry_module(module, entry_module_name, authorized_entry)
                continue

            origins = cls._module_origins(module)
            if origins:
                for origin in origins:
                    if origin in {"namespace", "built-in", "frozen"}:
                        continue
                    try:
                        resolved = Path(origin).resolve(strict=True)
                        relative = resolved.relative_to(plugin_root).as_posix()
                        verified = resolve_contained_regular_file(
                            plugin_root,
                            relative,
                            description=f"cached module {module_name}",
                        )
                    except (OSError, ValueError, PluginPathError) as exc:
                        raise PluginPathError(
                            f"cached module {module_name} is owned by another plugin root"
                        ) from exc
                    if verified != resolved:
                        raise PluginPathError(
                            f"cached module {module_name} has a non-canonical origin"
                        )
                continue

            package_paths = getattr(module, "__path__", None)
            if package_paths is None:
                raise PluginPathError(
                    f"cached module {module_name} has no verifiable source origin"
                )
            for value in package_paths:
                try:
                    candidate = Path(value)
                    metadata = candidate.lstat()
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(plugin_root)
                except (OSError, TypeError, ValueError) as exc:
                    raise PluginPathError(
                        f"cached package {module_name} is owned by another plugin root"
                    ) from exc
                if is_link_like(metadata) or not stat.S_ISDIR(metadata.st_mode):
                    raise PluginPathError(f"cached package {module_name} has an unsafe search path")

    def _prepare_module_load(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
        transaction: _PluginLoadTransaction | None,
        *,
        fingerprint: _PluginContentFingerprint | None = None,
    ) -> tuple[Path, Path, Mapping[str, bytes]]:
        """解析并冻结本次导入允许读取的插件源码。"""

        try:
            plugin_root = resolve_plugin_root(self.plugins_dir, plugin_dir)
            authorized_entry = resolve_plugin_entry(
                self.plugins_dir,
                plugin_dir,
                definition.entry,
            )
        except PluginPathError as exc:
            raise PluginLoadError(
                definition.name,
                f"Unsafe or unavailable plugin entry: {definition.entry}",
                exc,
            ) from exc
        if fingerprint is None:
            try:
                fingerprint = self._capture_plugin_snapshot(plugin_dir, definition)
            except (OSError, PluginPathError) as exc:
                raise PluginLoadError(
                    definition.name,
                    "Plugin source snapshot could not be revalidated before import",
                    exc,
                ) from exc
        if transaction is not None and isinstance(
            transaction.mtime,
            _PluginContentFingerprint,
        ):
            authorized_fingerprint = transaction.mtime
            if not self._content_fingerprints_equal(fingerprint, authorized_fingerprint):
                error = PluginPathError(
                    "plugin source snapshot changed before any plugin code executed"
                )
                raise PluginLoadError(
                    definition.name,
                    "Plugin source changed before import",
                    error,
                ) from error
        sources = fingerprint.sources
        if definition.entry not in sources:
            raise PluginLoadError(
                definition.name,
                f"Plugin entry is absent from its authorized source snapshot: {definition.entry}",
            )
        try:
            for relative, source in sorted(sources.items()):
                compile(
                    source,
                    str(plugin_root / Path(*PurePosixPath(relative).parts)),
                    "exec",
                    dont_inherit=True,
                )
        except (SyntaxError, ValueError) as exc:
            raise PluginLoadError(
                definition.name,
                "Plugin source could not be compiled",
                exc,
            ) from exc
        if transaction is not None:
            transaction.authorized_entry = authorized_entry
        return plugin_root, authorized_entry, sources

    def _reject_plugin_module_aliases(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
        transaction: _PluginLoadTransaction | None,
    ) -> None:
        """拒绝同一插件源码被挂到非规范模块名下。"""

        aliases = self._plugin_module_aliases(plugin_dir, definition.name)
        if not aliases:
            return
        if transaction is not None:
            transaction.uncertain_external_code = True
        raise PluginLoadError(
            definition.name,
            f"Non-canonical plugin module aliases detected: {', '.join(aliases)}",
        )

    def _prepare_module_source_finder(
        self,
        plugin_root: Path,
        definition: PluginDefinition,
        full_module_name: str,
        authorized_entry: Path,
        sources: Mapping[str, bytes],
        transaction: _PluginLoadTransaction | None,
    ) -> tuple[_SourceOnlyPluginFinder, bool]:
        """安装本次导入使用的源码查找器，并返回其是否需要长期保留。"""

        module_name = f"plugins.{definition.name}"
        if transaction is not None:
            with _PLUGIN_IMPORT_LOCK:
                try:
                    transaction.namespace_claim_new = self._claim_plugin_namespace(definition.name)
                except PluginPathError:
                    transaction.unowned_canonical_namespace = True
                    raise
                preexisting_names = [
                    cached_name
                    for cached_name in list(sys.modules)
                    if cached_name == module_name or cached_name.startswith(f"{module_name}.")
                ]
                if preexisting_names:
                    transaction.unowned_canonical_namespace = True
                    raise PluginPathError(
                        "canonical plugin modules predate this import transaction"
                    )
                return (
                    self._activate_source_finder(
                        definition.name,
                        plugin_root,
                        sources,
                    ),
                    True,
                )

        # 私有测试入口可以复用缓存，但必须先证明缓存仍属于当前插件根目录。
        self._validate_cached_plugin_namespace(
            plugin_root,
            definition.name,
            full_module_name,
            authorized_entry,
        )
        with _PLUGIN_IMPORT_LOCK:
            source_finder = _SourceOnlyPluginFinder(
                plugin_root,
                definition.name,
                sources,
                lambda imported_name, imported_module: self._record_private_module(
                    definition.name,
                    imported_name,
                    imported_module,
                ),
            )
            sys.meta_path.insert(0, source_finder)
        return source_finder, False

    def _import_plugin_entry(
        self,
        source_finder: _SourceOnlyPluginFinder,
        plugin_dir: Path,
        plugin_root: Path,
        definition: PluginDefinition,
        full_module_name: str,
        authorized_entry: Path,
        transaction: _PluginLoadTransaction | None,
    ) -> ModuleType:
        """在查找器事务保护下导入入口，并复核导入后的文件身份。"""

        if transaction is not None:

            def mark_execution_started(
                imported_name: str,
                imported_module: ModuleType,
            ) -> None:
                transaction.import_attempted = True
                if imported_name == full_module_name:
                    transaction.module = imported_module

            source_finder._on_execution_started = mark_execution_started
        else:
            source_finder._on_execution_started = None

        with source_finder.transaction_guard():
            module = importlib.import_module(full_module_name)
            if not isinstance(module, ModuleType):
                raise PluginPathError("plugin entry import returned a non-module object")
            if transaction is not None:
                transaction.module = module
            if sys.modules.get(full_module_name) is not module:
                raise PluginPathError("plugin entry lost its canonical cache binding after import")
            try:
                current_entry = resolve_plugin_entry(
                    self.plugins_dir,
                    plugin_dir,
                    definition.entry,
                )
                if current_entry != authorized_entry:
                    raise PluginPathError("plugin entry changed during import")
                self._validate_entry_module(module, full_module_name, authorized_entry)
                self._validate_cached_plugin_namespace(
                    plugin_root,
                    definition.name,
                    full_module_name,
                    authorized_entry,
                )
            except PluginPathError:
                if transaction is not None:
                    transaction.uncertain_external_code = True
                raise
            if transaction is not None:
                transaction.import_completed = True
            return module

    def _retire_private_source_finder(
        self,
        source_finder: _SourceOnlyPluginFinder,
    ) -> None:
        """等待临时查找器中的导入结束，再从全局导入链移除它。"""

        module_names = source_finder.deactivate_and_wait()
        self._wait_for_module_import_barriers(module_names)
        with _PLUGIN_IMPORT_LOCK:
            _remove_meta_path_identity(source_finder)

    def _start_plugin_init(
        self,
        module: ModuleType,
        definition: PluginDefinition,
        gate: PluginExecutionGate,
        transaction: _PluginLoadTransaction | None,
    ) -> asyncio.Task[Any] | None:
        """启动插件初始化，并把异步任务纳入生命周期跟踪。"""

        if not hasattr(module, "init"):
            return None
        init_func = module.init
        accepts_context = callback_accepts_positional_context(init_func)

        async def run_init() -> None:
            if accepts_context:
                await call_plugin_callback(init_func, self.build_context(definition.name))
            else:
                await call_plugin_callback(init_func)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 同步嵌入仍受支持；异步 init 必须由正在运行的事件循环托管。
            result = (
                init_func(self.build_context(definition.name)) if accepts_context else init_func()
            )
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise RuntimeError("async plugin init requires a running event loop") from None
            return None

        init_task = asyncio.create_task(
            self._capture_lifecycle(
                asyncio.wait_for(
                    gate.run(run_init),
                    timeout=PLUGIN_INIT_TIMEOUT_SECONDS,
                )
            )
        )
        if transaction is not None:
            transaction.init_task = init_task
        if transaction is None or transaction.track_init_task:
            self._init_tasks.append(init_task)
            self._init_task_plugins[init_task] = definition.name
        return init_task

    def _load_module(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
        *,
        transaction: _PluginLoadTransaction | None = None,
        prepared: tuple[Path, Path, Mapping[str, bytes]] | None = None,
    ) -> tuple[ModuleType | None, asyncio.Task[Any] | None]:
        plugin_root, authorized_entry, sources = prepared or self._prepare_module_load(
            plugin_dir, definition, transaction
        )
        if transaction is not None:
            transaction.authorized_entry = authorized_entry

        # Import only through the repository's canonical namespace.  The
        # manifest validator makes this path-to-module mapping injective.
        module_name = f"plugins.{plugin_dir.name}"
        entry_stem = definition.entry.removesuffix(".py").replace("/", ".")
        full_module_name = f"{module_name}.{entry_stem}"
        self._reject_plugin_module_aliases(plugin_dir, definition, transaction)

        gate = transaction.gate if transaction is not None else self._execution_gate_for(definition)
        try:
            source_finder, persistent_source_finder = self._prepare_module_source_finder(
                plugin_root,
                definition,
                full_module_name,
                authorized_entry,
                sources,
                transaction,
            )
            try:
                module = self._import_plugin_entry(
                    source_finder,
                    plugin_dir,
                    plugin_root,
                    definition,
                    full_module_name,
                    authorized_entry,
                    transaction,
                )
            finally:
                source_finder._on_execution_started = None
                if not persistent_source_finder:
                    self._retire_private_source_finder(source_finder)

            self._reject_plugin_module_aliases(plugin_dir, definition, transaction)
            self._bind_declared_services(definition, module)
            return module, self._start_plugin_init(module, definition, gate, transaction)
        except Exception as exc:
            raise PluginLoadError(definition.name, "Failed to load plugin", exc) from exc

    def _build_command_specs(
        self,
        definition: PluginDefinition,
        module: ModuleType,
        execution_gate: PluginExecutionGate | None = None,
    ) -> list[CommandSpec]:
        if not hasattr(module, "handle"):
            logger.warning("Plugin %s missing handle()", definition.name)
            return []
        gate = execution_gate or self._execution_gate_for(definition)
        specs: list[CommandSpec] = []
        for command in definition.commands:
            catalog = build_command_catalog_node(definition.name, command, root=True)
            specs.append(
                CommandSpec(
                    plugin=definition.name,
                    name=command.get("name", ""),
                    triggers=command.get("triggers", []),
                    help_text=command.get("help", ""),
                    admin_only=command.get("admin_only", False),
                    handler=module.handle,
                    priority=command.get("priority", 0),
                    usage=command.get("usage"),
                    catalog=catalog,
                    execution_gate=gate,
                )
            )
        return specs

    def _authorize_plugin_snapshot(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
    ) -> _PluginContentFingerprint:
        """生成首次加载授权；导入阶段仍会独立复核同一份内容快照。"""

        return self._capture_plugin_snapshot(plugin_dir, definition)

    def _capture_plugin_snapshot(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
    ) -> _PluginContentFingerprint:
        """获取插件文件的内容指纹。

        仅依赖 mtime/size 会漏掉保留元数据的原子部署或同长度策略变更；
        这里散列相对路径和文件内容，作为发布授权的一部分。
        """
        plugin_root = resolve_plugin_root(self.plugins_dir, plugin_dir)
        digest = hashlib.blake2b(digest_size=16)
        paths = sorted(
            self._iter_watch_files(plugin_dir, definition), key=lambda item: item.as_posix()
        )
        digest.update(b"XIAOQING_PLUGIN_SNAPSHOT_V2\0")
        digest.update(len(paths).to_bytes(8, "big"))
        opened_identities: dict[Path, tuple[int, int, int, int]] = {}
        sources: dict[str, bytes] = {}
        manifest_payload: bytes | None = None
        source_total = 0
        snapshot_total = 0
        for path in paths:
            try:
                relative = path.relative_to(plugin_root).as_posix()
            except ValueError:
                relative = path.as_posix()
            relative_bytes = relative.encode("utf-8", errors="surrogatepass")
            content = bytearray()
            file_bytes = 0
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if relative == "plugin.json":
                    file_limit = _MAX_PLUGIN_MANIFEST_BYTES
                elif path.suffix == ".py":
                    file_limit = _MAX_PLUGIN_SOURCE_FILE_BYTES
                else:
                    file_limit = _MAX_PLUGIN_WATCH_FILE_BYTES
                if opened.st_size > file_limit:
                    raise PluginPathError(
                        f"plugin snapshot file exceeds {file_limit} bytes: {relative}"
                    )
                digest.update(b"F")
                digest.update(len(relative_bytes).to_bytes(8, "big"))
                digest.update(relative_bytes)
                digest.update(opened.st_size.to_bytes(16, "big"))
                while chunk := handle.read(1024 * 1024):
                    file_bytes += len(chunk)
                    if file_bytes > file_limit:
                        raise PluginPathError(
                            f"plugin snapshot file grew beyond {file_limit} bytes: {relative}"
                        )
                    snapshot_total += len(chunk)
                    if snapshot_total > _MAX_PLUGIN_SNAPSHOT_TOTAL_BYTES:
                        raise PluginPathError("plugin snapshot exceeds the total byte budget")
                    if path.suffix == ".py":
                        source_total += len(chunk)
                        if source_total > _MAX_PLUGIN_SOURCE_TOTAL_BYTES:
                            raise PluginPathError(
                                "plugin Python sources exceed the total byte budget"
                            )
                    digest.update(chunk)
                    if path.suffix == ".py" or relative == "plugin.json":
                        content.extend(chunk)
                finished = os.fstat(handle.fileno())
            current = path.stat()
            opened_identity = self._watch_file_identity(opened)
            opened_identities[path] = opened_identity
            if (
                self._watch_file_identity(finished) != opened_identity
                or self._watch_file_identity(current) != opened_identity
            ):
                raise OSError(f"plugin file changed while fingerprinting: {relative}")
            if path.suffix == ".py":
                sources[relative] = bytes(content)
            if relative == "plugin.json":
                manifest_payload = bytes(content)

        # A file added or removed after the first walk would otherwise produce
        # a digest for a mixed snapshot that never existed on disk.
        final_paths = sorted(
            self._iter_watch_files(plugin_dir, definition), key=lambda item: item.as_posix()
        )
        if [path.as_posix() for path in final_paths] != [path.as_posix() for path in paths]:
            raise OSError("plugin file set changed while fingerprinting")
        for path in paths:
            if self._watch_file_identity(path.stat()) != opened_identities[path]:
                raise OSError(
                    f"plugin file changed across fingerprint snapshot: "
                    f"{path.relative_to(plugin_root).as_posix()}"
                )
        if manifest_payload is None:
            raise OSError("plugin manifest was absent from its source snapshot")
        if (
            definition.manifest_payload is not None
            and manifest_payload != definition.manifest_payload
        ):
            raise OSError("plugin manifest changed between authorization and fingerprint")
        return _PluginContentFingerprint(
            int.from_bytes(digest.digest(), "big"),
            sources=sources,
            manifest_payload=manifest_payload,
            file_identities={
                path.relative_to(plugin_root).as_posix(): identity
                for path, identity in opened_identities.items()
            },
            captured_at=time.monotonic(),
        )

    @staticmethod
    def _watch_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )

    async def _capture_plugin_snapshot_async(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
        *,
        previous: int | float | None = None,
    ) -> _PluginContentFingerprint:
        """在线程中预检元数据，并在需要时捕获完整内容快照。"""

        return await asyncio.to_thread(
            self._capture_plugin_snapshot_for_watch,
            plugin_dir,
            definition,
            previous,
        )

    def _capture_plugin_snapshot_for_watch(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
        previous: int | float | None,
    ) -> _PluginContentFingerprint:
        """Reuse a recent snapshot only while every watched file identity is stable."""

        if isinstance(previous, _PluginContentFingerprint) and (
            time.monotonic() - previous.captured_at < _PLUGIN_FINGERPRINT_AUDIT_INTERVAL_SECONDS
        ):
            identities = self._capture_plugin_watch_identities(plugin_dir, definition)
            if identities == previous.file_identities:
                return previous
        return self._capture_plugin_snapshot(plugin_dir, definition)

    def _capture_plugin_watch_identities(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
    ) -> Mapping[str, tuple[int, int, int, int]]:
        """Capture the one-walk metadata key used by the stable watcher path."""

        plugin_root = resolve_plugin_root(self.plugins_dir, plugin_dir)
        identities: dict[str, tuple[int, int, int, int]] = {}
        for path in self._iter_watch_files(plugin_dir, definition):
            relative = path.relative_to(plugin_root).as_posix()
            identities[relative] = self._watch_file_identity(path.stat())
        return MappingProxyType(identities)

    def _iter_watch_files(self, plugin_dir: Path, definition: PluginDefinition) -> list[Path]:
        plugin_root = resolve_plugin_root(self.plugins_dir, plugin_dir)
        files: dict[str, Path] = {}
        explicit_watch_files = set(definition.watch_files)
        directory_count = 0
        scanned_entries = 0

        def validate_relative(relative: str) -> None:
            if len(relative.encode("utf-8", errors="surrogatepass")) > (
                _MAX_PLUGIN_RELATIVE_PATH_BYTES
            ):
                raise PluginPathError("plugin source path exceeds the metadata byte budget")
            if len(PurePosixPath(relative).parts) > _MAX_PLUGIN_RELATIVE_PATH_DEPTH:
                raise PluginPathError("plugin source path exceeds the depth budget")

        pending_directories = [plugin_root]
        while pending_directories:
            # Runtime data lives outside the plugin tree. Bytecode alone is
            # pruned before descent because it is never an authorized source.
            root_path = pending_directories.pop()
            directory_count += 1
            if directory_count > _MAX_PLUGIN_SOURCE_DIRECTORIES:
                raise PluginPathError("plugin source tree exceeds the directory budget")
            child_directories: list[Path] = []
            directory_entries = 0
            with os.scandir(root_path) as entries:
                for entry in entries:
                    directory_entries += 1
                    scanned_entries += 1
                    if directory_entries > _MAX_PLUGIN_DIRECTORY_ENTRIES:
                        raise PluginPathError("plugin source directory exceeds the entry budget")
                    if scanned_entries > _MAX_PLUGIN_SCANNED_ENTRIES:
                        raise PluginPathError("plugin source tree exceeds the scanned-entry budget")
                    path = root_path / entry.name
                    reserved_name = entry.name.casefold()
                    if reserved_name == "__pycache__":
                        continue
                    metadata = path.lstat()
                    relative = path.relative_to(plugin_root).as_posix()
                    if is_link_like(metadata):
                        # Linked resource trees are outside the immutable code
                        # inventory.  Never descend through them; an entry or
                        # explicit watch file that targets one is rejected by
                        # its own strict resolver below.
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        validate_relative(relative)
                        child_directories.append(path)
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        if path.suffix in {".py", ".json"}:
                            raise PluginPathError(
                                f"plugin source tree contains a non-ordinary source: {relative}"
                            )
                        continue
                    watched_json = path.suffix == ".json" and (
                        root_path == plugin_root or relative in explicit_watch_files
                    )
                    if path.suffix != ".py" and not watched_json:
                        continue
                    validate_relative(relative)
                    if len(files) >= _MAX_PLUGIN_SNAPSHOT_FILES and relative not in files:
                        raise PluginPathError("plugin source tree exceeds the file-count budget")
                    try:
                        resolved = path.resolve(strict=True)
                        resolved.relative_to(plugin_root)
                    except (OSError, ValueError) as exc:
                        raise PluginPathError(
                            f"plugin source file escapes its root: {relative}"
                        ) from exc
                    if resolved != path:
                        raise PluginPathError(f"plugin source path is not canonical: {relative}")
                    files[relative] = path
            pending_directories.extend(
                sorted(child_directories, key=lambda item: item.name, reverse=True)
            )

        # Bind the fingerprint to the manifest-authorized entry explicitly;
        # never rely on a broad walk having happened to include it.
        entry_path = resolve_plugin_entry(self.plugins_dir, plugin_dir, definition.entry)
        manifest_path = resolve_contained_regular_file(
            plugin_root,
            "plugin.json",
            description="plugin manifest",
        )
        files[entry_path.relative_to(plugin_root).as_posix()] = entry_path
        files["plugin.json"] = manifest_path
        for relative in explicit_watch_files:
            watched = resolve_contained_regular_file(
                plugin_root,
                relative,
                description="plugin watch file",
            )
            files[relative] = watched
        if len(files) > _MAX_PLUGIN_SNAPSHOT_FILES:
            raise PluginPathError("plugin source tree exceeds the file-count budget")
        return list(files.values())
