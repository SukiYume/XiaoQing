"""
存储基类

为所有存储类提供通用的接口和方法，减少代码重复。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, TypeVar

T = TypeVar("T")


class StoreBase:
    """
    存储基类，提供通用的数据目录绑定和文件操作方法。

    所有存储类都应该继承此类，以获得一致的接口。
    """

    def __init__(self) -> None:
        self._data_dir: Optional[Path] = None

    def bind(self, data_dir: Path) -> None:
        """
        绑定数据目录。

        Args:
            data_dir: 数据目录路径
        """
        self._data_dir = data_dir

    def _is_bound(self) -> bool:
        """
        检查是否已绑定数据目录。

        Returns:
            是否已绑定
        """
        return self._data_dir is not None

    def _ensure_dir(self, *parts: str) -> Optional[Path]:
        """
        确保目录存在，返回目录路径。

        Args:
            *parts: 目录的各个部分

        Returns:
            目录的完整路径，如果未绑定则返回 None
        """
        if not self._data_dir:
            return None
        path = self._data_dir.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_path(self, *parts: str) -> Optional[Path]:
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

    def _load_json(self, path: Path, default: Any = None) -> Any:
        """
        安全地加载 JSON 文件。

        Args:
            path: 文件路径
            default: 默认值

        Returns:
            JSON 内容，如果加载失败则返回默认值
        """
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

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
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except OSError:
            return False
