"""会话的重置代际标记：重置时更新，后台任务在远程调用前记下、提交前核对，旧结果不会写回。

标记文件放在 ``person_profiles/<会话>/.generation``；重置时同一目录下留着的旧人物资料文件一并删除。

旧人物资料、``hippo_memorizer`` 记忆文件和向量库里的会话文档是已经删除的功能留下的数据，
``legacy.py`` 还在读它们做导入；移除导入支持时，``clear_profiles_and_memory`` 里的这部分清理
和 ``MemoryDB.delete_chat`` 也可以一起删。
"""

from __future__ import annotations

import time
from pathlib import Path

from core.atomic_store import atomic_write_text, keyed_path_lock

from ..store_base import delete_json_artifacts
from .memory_db import MemoryDB


def _profile_dir(data_dir: Path, chat_id: str) -> Path:
    safe_chat_id = (chat_id or "").strip() or "default"
    return data_dir / "person_profiles" / safe_chat_id


def profile_generation_path(data_dir: Path, chat_id: str) -> Path:
    """代际标记与旧人物资料共用会话路径锁。"""
    root   = (data_dir / "person_profiles").resolve()
    target = (_profile_dir(data_dir, chat_id) / ".generation").resolve()
    if not target.is_relative_to(root):
        raise ValueError("invalid profile chat_id")
    return target


def get_profile_generation(data_dir: Path, chat_id: str) -> str:
    try:
        return profile_generation_path(data_dir, chat_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def session_is_current(data_dir: Path, chat_id: str, expected: str | None) -> bool:
    """会话自 ``expected`` 记下以来没有被重置；``expected`` 为 ``None`` 表示没记过代际，不核对。"""
    return expected is None or get_profile_generation(data_dir, chat_id) == expected


def clear_profiles_and_memory(data_dir: Path, chat_id: str, memory_db: MemoryDB | None) -> None:
    """重置代际并使所有旧后台任务失效；删掉这个会话在旧版本里留下的人物资料、话题摘要和向量记录。"""
    generation_path = profile_generation_path(data_dir, chat_id)
    with keyed_path_lock(generation_path):
        atomic_write_text(generation_path, str(time.time_ns()))
        for path in generation_path.parent.iterdir():
            if path.name.endswith((".json", ".json.bak")) and path.is_file():
                path.unlink()
        delete_json_artifacts(data_dir / "hippo_memorizer" / f"{chat_id}.json")
        if memory_db is not None and memory_db.delete_chat(chat_id):
            memory_db.save()
