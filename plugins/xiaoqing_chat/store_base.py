"""
存储基类

为所有存储类提供通用的接口和方法，减少代码重复。
"""

from __future__ import annotations

import asyncio
import math
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Generic, TypeVar

from core.atomic_store import AtomicJsonStore, keyed_path_lock
from core.plugin_base import load_json, write_json

T = TypeVar("T")


def coerce_int(value: Any, *, default: int, minimum: int | None = None) -> int:
    """读取 JSON 整数；布尔值和畸形值回退，必要时执行下界约束。"""

    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed) if minimum is not None else parsed


def coerce_optional_int(value: Any) -> int | None:
    """读取可空 JSON 整数；``null``、布尔值和畸形值均返回空。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def coerce_finite_float(
    value: Any,
    *,
    default: float,
    minimum: float | None = None,
) -> float:
    """读取有限 JSON 浮点数；拒绝布尔值、非数和无穷值。"""

    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or (minimum is not None and parsed < minimum):
        return default
    return parsed


def coerce_json_bool(value: Any, *, default: bool) -> bool:
    """只接受真正的 JSON 布尔值，避免字符串 ``"false"`` 被当成真。"""

    return value if isinstance(value, bool) else default


class LockedDirtyStateMixin:
    """为带同步锁和 ``_dirty`` 标志的存储提供唯一的线程安全读取实现。"""

    _lock: AbstractContextManager[Any]
    _dirty: bool

    def is_dirty(self) -> bool:
        with self._lock:
            return bool(self._dirty)


def delete_json_artifacts(path: Path) -> None:
    """删除一个原子 JSON 存储的主文件和备份文件。

    重置会话属于数据清除操作；只删主文件会让旧内容继续留在 ``.bak`` 中，
    甚至可能在后续恢复流程中重新出现。两个文件都尝试删除后再报告首个错误，避免
    因主文件删除失败而跳过备份清理。
    """
    store = AtomicJsonStore(path)
    first_error: OSError | None = None
    with keyed_path_lock(path):
        for candidate in (store.path, store.backup_path):
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                if first_error is None:
                    first_error = exc
    if first_error is not None:
        raise first_error


def load_json_file(path: Path, default: Any = None) -> Any:
    """通过 core 原子存储协议读取 JSON，并在主文件损坏时恢复备份。"""

    return load_json(path, default=default)


class StoreBase:
    """
    存储基类，提供通用的数据目录绑定和文件操作方法。

    所有存储类都应该继承此类，以获得一致的接口。
    """

    def __init__(self) -> None:
        self._data_dir: Path | None = None

    # 子类沿用原有存储接口，底层实现同时供实验工具复用。
    _load_json = staticmethod(load_json_file)

    def bind(self, data_dir: Path) -> None:
        """
        绑定数据目录。

        Args:
            data_dir: 数据目录路径
        """
        self._data_dir = data_dir

    def _resolve_path(self, *parts: str) -> Path | None:
        """
        解析路径，但不确保目录存在。

        Args:
            *parts: 路径的各个部分

        Returns:
            完整路径，如果未绑定则返回 None
        """
        if not self._data_dir:
            return None
        return self._data_dir.joinpath(*parts)

    def _load_json_from_path_parts(self, *parts: str, default: Any = None) -> Any:
        path = self._resolve_path(*parts)
        if not path:
            return default
        return self._load_json(path, default=default)

    def _save_json(self, path: Path, data: Any, ensure_parent: bool = True) -> bool:
        """
        安全地保存 JSON 文件。

        Args:
            path: 文件路径
            data: 要保存的数据
            ensure_parent: 是否确保父目录存在

        Returns:
            是否保存成功
        """
        try:
            if ensure_parent:
                path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, data)
            return True
        except OSError:
            return False

    def _save_json_to_path_parts(self, *parts: str, data: Any) -> bool:
        path = self._resolve_path(*parts)
        if not path:
            return False
        return self._save_json(path, data, ensure_parent=True)


class AsyncKeyedStore(StoreBase, Generic[T], ABC):
    """统一把按会话键读写的同步存储桥接到异步调用方。"""

    @abstractmethod
    def get(self, _chat_id: str) -> T:
        """同步读取一个会话状态。"""

    @abstractmethod
    def clear(self, _chat_id: str) -> None:
        """同步清除一个会话状态。"""

    async def get_async(self, chat_id: str) -> T:
        return await asyncio.to_thread(self.get, chat_id)

    async def clear_async(self, chat_id: str) -> None:
        await asyncio.to_thread(self.clear, chat_id)
