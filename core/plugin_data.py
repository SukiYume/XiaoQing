# mypy: disable-error-code=attr-defined
"""Plugin writable-data directory ownership and migration."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .models import (
    canonical_plugin_name,
)
from .plugin_base import ensure_dir
from .plugin_manager_support import (
    LoadedPlugin,
    PluginPathError,
    _PluginDataDirectory,
    is_link_like,
    resolve_contained_directory,
    resolve_plugin_root,
)

logger = logging.getLogger(__name__)


class PluginDataMixin:
    data_root: Path
    plugins_dir: Path
    _data_directories: dict[str, _PluginDataDirectory]

    def _plugin_data_dir(self, plugin_name: str) -> Path:
        canonical_name: str = canonical_plugin_name(plugin_name)
        return self.data_root / canonical_name

    def _ensure_data_root(self) -> Path:
        try:
            self.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = self.data_root.lstat()
            if is_link_like(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise PluginPathError("plugin data root must be an ordinary directory")
            return self.data_root.resolve(strict=True)
        except PluginPathError:
            raise
        except OSError as exc:
            raise PluginPathError("cannot create plugin data root") from exc

    @staticmethod
    def _remove_migration_staging(path: Path) -> None:
        """Remove only the private staging directory created by this migration."""

        try:
            if path.exists():
                shutil.rmtree(path)
        except OSError:
            logger.warning("Unable to remove plugin data migration staging directory: %s", path)

    @staticmethod
    def _ensure_legacy_data_archive_root(data_root: Path) -> Path:
        archive_root = data_root / ".legacy-plugin-data"
        try:
            archive_root.mkdir(mode=0o700, exist_ok=True)
            metadata = archive_root.lstat()
            if is_link_like(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise PluginPathError("legacy plugin data archive must be an ordinary directory")
            return archive_root.resolve(strict=True)
        except PluginPathError:
            raise
        except OSError as exc:
            raise PluginPathError("cannot create legacy plugin data archive") from exc

    def _archive_legacy_data_dir(
        self,
        plugin_name: str,
        *,
        legacy: Path,
        data_root: Path,
    ) -> Path:
        """Retire one migrated source-tree directory without deleting its fallback copy."""

        archive_root = self._ensure_legacy_data_archive_root(data_root)
        archive = archive_root / plugin_name
        try:
            archive.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PluginPathError("cannot inspect legacy plugin data archive") from exc
        else:
            raise PluginPathError(
                f"legacy plugin data archive already exists for {plugin_name}; "
                "refusing to overwrite it"
            )
        try:
            legacy.rename(archive)
        except OSError as exc:
            cross_device = exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17
            if not cross_device:
                raise PluginPathError("cannot retire legacy plugin data directory") from exc
            try:
                staging = Path(
                    tempfile.mkdtemp(
                        prefix=f".{plugin_name}.archiving-",
                        dir=archive_root,
                    )
                )
            except OSError as staging_exc:
                raise PluginPathError("cannot stage legacy plugin data archive") from staging_exc
            try:
                shutil.copytree(legacy, staging, dirs_exist_ok=True, symlinks=True)
                staging.chmod(0o700)
                staging.rename(archive)
                shutil.rmtree(legacy)
            except OSError as migration_exc:
                raise PluginPathError(
                    "cannot retire legacy plugin data directory"
                ) from migration_exc
            finally:
                self._remove_migration_staging(staging)
        logger.info("Archived legacy plugin data for %s at %s", plugin_name, archive)
        return archive

    def _seed_external_data_dir(
        self,
        plugin_name: str,
        *,
        plugin_root: Path,
        data_root: Path,
    ) -> None:
        """Atomically seed a new external directory from the legacy source-tree location."""

        target = data_root / plugin_name
        legacy = plugin_root / "data"
        if not legacy.exists():
            if target.exists():
                return
            try:
                target.mkdir(mode=0o700)
                return
            except FileExistsError:
                return
            except OSError as exc:
                raise PluginPathError("cannot create plugin data directory") from exc

        legacy = resolve_contained_directory(
            plugin_root,
            "data",
            description="legacy plugin data directory",
            reject_root_link=True,
        )
        if not target.exists():
            try:
                staging = Path(
                    tempfile.mkdtemp(
                        prefix=f".{plugin_name}.migrating-",
                        dir=data_root,
                    )
                )
            except OSError as exc:
                raise PluginPathError("cannot stage legacy plugin data migration") from exc
            try:
                shutil.copytree(legacy, staging, dirs_exist_ok=True, symlinks=True)
                staging.chmod(0o700)
                try:
                    staging.rename(target)
                except OSError as exc:
                    if not target.exists():
                        raise PluginPathError("cannot publish migrated plugin data") from exc
            except PluginPathError:
                raise
            except OSError as exc:
                raise PluginPathError("cannot copy legacy plugin data") from exc
            finally:
                self._remove_migration_staging(staging)
        self._archive_legacy_data_dir(
            plugin_name,
            legacy=legacy,
            data_root=data_root,
        )

    @staticmethod
    def _verify_data_directory_record(record: _PluginDataDirectory) -> Path:
        """Perform a cheap per-context identity check without rescanning sources."""

        try:
            current_root = record.data_root.lstat()
            current_data = record.path.lstat()
        except OSError as exc:
            raise PluginPathError("plugin data directory is no longer available") from exc
        if (
            is_link_like(current_root)
            or is_link_like(current_data)
            or not stat.S_ISDIR(current_root.st_mode)
            or not stat.S_ISDIR(current_data.st_mode)
            or not os.path.samestat(record.root_identity, current_root)
            or not os.path.samestat(record.data_identity, current_data)
        ):
            raise PluginPathError("plugin data directory identity changed")
        return record.path

    def _ensure_plugin_data_dir(self, plugin_name: str, *, force: bool = False) -> Path:
        cached = self._data_directories.get(plugin_name)
        if cached is not None and not force:
            return self._verify_data_directory_record(cached)
        plugin_dir = self.plugins_dir / plugin_name
        plugin_root = resolve_plugin_root(self.plugins_dir, plugin_dir)
        data_root = self._ensure_data_root()
        self._seed_external_data_dir(
            canonical_plugin_name(plugin_name),
            plugin_root=plugin_root,
            data_root=data_root,
        )
        verified = resolve_contained_directory(
            data_root,
            canonical_plugin_name(plugin_name),
            description="plugin data directory",
            reject_root_link=True,
        )
        record = _PluginDataDirectory(
            data_root=data_root,
            path=verified,
            root_identity=data_root.lstat(),
            data_identity=verified.lstat(),
        )
        self._verify_data_directory_record(record)
        self._data_directories[plugin_name] = record
        return verified

    def _shutdown_data_dir(self, name: str, plugin: LoadedPlugin) -> Path | None:
        """Return generation-captured data only while its exact identity survives."""

        record = self._data_directories.get(name)
        if record is not None and (plugin.data_dir is None or plugin.data_dir == record.path):
            try:
                return self._verify_data_directory_record(record)
            except PluginPathError:
                return None
        if plugin.authorized_entry is not None:
            return None
        # Compatibility for synthetic/in-memory generations that never crossed
        # the filesystem authorization path used by production plugins.
        data_dir = self._plugin_data_dir(name)
        ensure_dir(data_dir)
        return data_dir
