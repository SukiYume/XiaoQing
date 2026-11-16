# 插件数据目录：明确可写路径的归属。
"""Plugin writable-data directory ownership."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .models import canonical_plugin_name
from .plugin_manager_support import (
    LoadedPlugin,
    PluginPathError,
    _PluginDataDirectory,
    is_link_like,
    resolve_contained_directory,
    resolve_plugin_root,
)


class PluginDataMixin:
    data_root        : Path
    plugins_dir      : Path
    _data_directories: dict[str, _PluginDataDirectory]

    @staticmethod
    def _ensure_private_directory(path: Path, description: str) -> Path:
        """创建 0700 目录并确认它是普通目录而不是链接，返回解析后的真实路径。"""

        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = path.lstat()
            if is_link_like(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise PluginPathError(f"{description} must be an ordinary directory")
            return path.resolve(strict=True)
        except PluginPathError:
            raise
        except OSError as exc:
            raise PluginPathError(f"cannot create {description}") from exc

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
        # 只给真实存在的插件目录建数据目录。
        resolve_plugin_root(self.plugins_dir, self.plugins_dir / plugin_name)
        data_root = self._ensure_private_directory(self.data_root, "plugin data root")
        try:
            (data_root / canonical_plugin_name(plugin_name)).mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise PluginPathError("cannot create plugin data directory") from exc
        verified = resolve_contained_directory(
            data_root,
            canonical_plugin_name(plugin_name),
            description      = "plugin data directory",
            reject_root_link = True,
        )
        record = _PluginDataDirectory(
            data_root     = data_root,
            path          = verified,
            root_identity = data_root.lstat(),
            data_identity = verified.lstat(),
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
        return None
