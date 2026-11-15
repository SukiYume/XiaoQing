"""一张 JSON 记录表上的“基线 + 增量”合并存储：表达库和黑话库共用。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from core.atomic_store import AtomicJsonStore, keyed_path_lock

from ..store_base import StoreBase

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

R = TypeVar("R", bound="DataclassInstance")


class BaselineMergeStore(StoreBase, Generic[R], ABC):
    """学习流程读一份、想一会儿、再写回，期间别的流程也可能写同一个文件。

    读的时候记下当时的版本作基线；写回时只把“这次相对基线改了什么”合到磁盘上最新的版本：
    别人新加的记录留着，别人删掉的不被写回来，同一条记录的改动按字段合并。子类只管三件事：
    一行 JSON 怎么变成记录、记录的键是什么、同一条记录的三个版本怎么合并。
    """

    # ``bw_learner/`` 下的文件名。
    _FILE: ClassVar[str]

    def __init__(self) -> None:
        super().__init__()
        # 每个异步学习流程拥有独立快照，跨会话等待期间保持基线配对。
        self._baseline_context: ContextVar[dict[str, R] | None] = ContextVar(
            "snapshot_baseline", default=None
        )

    @property
    def _baseline(self) -> dict[str, R]:
        value = self._baseline_context.get()
        return value if value is not None else {}

    @_baseline.setter
    def _baseline(self, value: dict[str, R]) -> None:
        self._baseline_context.set(value)

    def _drop_cache(self) -> None:
        self._baseline = {}

    def _path(self) -> Path | None:
        return self._resolve_path("bw_learner", self._FILE)

    @staticmethod
    @abstractmethod
    def _parse(item: dict[str, Any]) -> R | None:
        """一行 JSON 变成记录；不成样子的行返回 ``None``。"""

    @staticmethod
    @abstractmethod
    def _key(record: R) -> str:
        """记录在表里的键。"""

    @classmethod
    @abstractmethod
    def _merge_record(cls, latest: R | None, baseline: R | None, desired: R) -> R:
        """把这次想写的版本合到磁盘上最新的版本；``baseline`` 是这次读到时的样子。"""

    def _read_records(self) -> dict[str, R]:
        raw = self._load_json_from_path_parts("bw_learner", self._FILE, default=[])
        if not isinstance(raw, list):
            return {}
        records: dict[str, R] = {}
        for item in raw:
            record = self._parse(item) if isinstance(item, dict) else None
            if record is not None:
                records[self._key(record)] = record
        return records

    def _load_records(self) -> dict[str, R]:
        """读出全部记录并记为基线；没绑定数据目录时是空的。"""

        path = self._path()
        if path is None:
            return {}
        # 使用 core 的规范路径锁，模块热重载前后的类实例也共享同一把锁。
        with keyed_path_lock(path):
            records        = self._read_records()
            self._baseline = deepcopy(records)
            return records

    def _rewrite(self, keep: Callable[[dict[str, R]], dict[str, R]]) -> None:
        """整表改写（清掉一个会话的记录）：``keep`` 从最新版本里挑出要留下的。

        和 ``_merge_and_save`` 不同，写失败时抛出异常，并让备份文件也是清理后的内容：
        重置会话后旧记录不能再从备份里恢复出来。
        """

        path = self._path()
        if path is None:
            return
        with keyed_path_lock(path):
            remaining = keep(self._read_records())
            AtomicJsonStore(path).write_with_backup([asdict(item) for item in remaining.values()])
            self._baseline = deepcopy(remaining)

    def _merge_and_save(self, desired: dict[str, R], baseline: dict[str, R]) -> bool:
        """把 ``desired`` 相对 ``baseline`` 的改动合到最新版本并落盘；没写成返回 ``False``。"""

        path = self._path()
        if path is None:
            return False
        with keyed_path_lock(path):
            latest = self._read_records()
            merged = dict(latest)
            for key, record in desired.items():
                if key in baseline and key not in latest:
                    continue  # 读到之后被别人删了：不写回来
                merged[key] = self._merge_record(latest.get(key), baseline.get(key), record)
            for key in set(baseline) - set(desired):
                merged.pop(key, None)  # 这次删掉的
            # 别人在这期间新加的排前面，其余按这次给的顺序。
            concurrent = [key for key in latest if key not in baseline and key not in desired]
            ordered    = {key: merged[key] for key in (*concurrent, *desired) if key in merged}
            payload    = [asdict(item) for item in ordered.values()]
            if not self._save_json_to_path_parts("bw_learner", self._FILE, data=payload):
                return False
            self._baseline = deepcopy(ordered)
            return True
