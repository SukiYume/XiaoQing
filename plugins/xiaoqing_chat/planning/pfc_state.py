"""按会话持久化 PFC 规划器状态，并安全处理并发写入。"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..store_base import AsyncKeyedStore, delete_json_artifacts


@dataclass
class PFCConversationState:
    chat_id: str
    ignore_until_ts: float            = 0.0
    ended: bool                       = False
    last_successful_reply_action: str = ""
    goal_list: list[dict[str, Any]] = field(default_factory=list)
    knowledge_list: list[dict[str, Any]] = field(default_factory=list)
    planner_fail_ts: list[float] = field(default_factory=list)
    planner_skip_until: float = 0.0
    updated_at: float = field(default_factory=lambda: time.time())


class PFCStateStore(AsyncKeyedStore[PFCConversationState]):
    _MAX_CACHE_SIZE = 200

    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, PFCConversationState] = {}
        self._dirty_chat_ids: set[str]               = set()
        self._lock                                   = threading.RLock()

    def _path(self, chat_id: str) -> Path | None:
        return self._resolve_path("pfc_state", f"{chat_id}.json")

    def get(self, chat_id: str) -> PFCConversationState:
        with self._lock:
            return self._get_unlocked(chat_id)

    def _get_unlocked(self, chat_id: str) -> PFCConversationState:
        cid = str(chat_id)
        if cid in self._cache:
            return self._cache[cid]
        st = PFCConversationState(chat_id=cid)
        path = self._path(cid)
        if path:
            obj = self._load_json(path, default=None)
            if isinstance(obj, dict):
                st.ignore_until_ts              = float(obj.get("ignore_until_ts", 0.0) or 0.0)
                st.ended                        = bool(obj.get("ended", False))
                st.last_successful_reply_action = str(
                    obj.get("last_successful_reply_action", "") or ""
                )
                gl = obj.get("goal_list", [])
                if isinstance(gl, list):
                    st.goal_list = [x for x in gl if isinstance(x, dict)]
                kl = obj.get("knowledge_list", [])
                if isinstance(kl, list):
                    st.knowledge_list = [x for x in kl if isinstance(x, dict)]
                pft = obj.get("planner_fail_ts", [])
                if isinstance(pft, list):
                    st.planner_fail_ts = [float(x) for x in pft if isinstance(x, (int, float))]
                st.planner_skip_until = float(obj.get("planner_skip_until", 0.0) or 0.0)
                st.updated_at         = float(obj.get("updated_at", time.time()) or time.time())
        self._cache[cid] = st
        self._evict_clean_states_unlocked(protected_chat_id=cid)
        return st

    def _evict_clean_states_unlocked(self, *, protected_chat_id: str | None = None) -> None:
        """只淘汰已经落盘的状态；dirty 状态保留到防抖写入成功。"""
        overflow = len(self._cache) - self._MAX_CACHE_SIZE
        if overflow <= 0:
            return
        candidates = sorted(
            (
                key
                for key in self._cache
                if key != protected_chat_id and key not in self._dirty_chat_ids
            ),
            key=lambda key: self._cache[key].updated_at,
        )
        for key in candidates[:overflow]:
            del self._cache[key]

    @staticmethod
    def _payload(st: PFCConversationState) -> dict[str, Any]:
        return {
            "ignore_until_ts": st.ignore_until_ts,
            "ended": st.ended,
            "last_successful_reply_action": st.last_successful_reply_action,
            "goal_list": st.goal_list,
            "knowledge_list": st.knowledge_list,
            "planner_fail_ts": st.planner_fail_ts,
            "planner_skip_until": st.planner_skip_until,
            "updated_at": st.updated_at,
        }

    def mark_dirty(self, chat_id: str) -> None:
        """标记一次尚未落盘的原地修改，供防抖保存期间保护缓存项。"""
        with self._lock:
            cid = str(chat_id)
            if cid in self._cache:
                self._dirty_chat_ids.add(cid)

    def save(self, chat_id: str) -> None:
        with self._lock:
            cid = str(chat_id)
            st  = self._cache.get(cid)
            if not st:
                return
            path = self._path(cid)
            if not path:
                return
            st.updated_at = time.time()
            if self._save_json(path, self._payload(st)):
                self._dirty_chat_ids.discard(cid)
                self._evict_clean_states_unlocked()

    def set_state(self, chat_id: str, state: PFCConversationState) -> None:
        with self._lock:
            cid              = str(chat_id)
            state.chat_id    = cid
            state.updated_at = time.time()
            self._cache[cid] = state
            self._dirty_chat_ids.add(cid)
            self._evict_clean_states_unlocked(protected_chat_id=cid)

    async def save_async(self, chat_id: str) -> None:
        await asyncio.to_thread(self.save, chat_id)

    def flush(self) -> None:
        """持久化全部 dirty 状态，供插件关闭时绕过尚未触发的防抖任务。"""
        with self._lock:
            dirty_chat_ids = list(self._dirty_chat_ids)
        for chat_id in dirty_chat_ids:
            self.save(chat_id)

    def clear(self, chat_id: str) -> None:
        """清除会话 PFC 状态，不写回无意义的默认状态文件。"""
        with self._lock:
            cid = str(chat_id)
            self._cache.pop(cid, None)
            self._dirty_chat_ids.discard(cid)
            path = self._path(cid)
            if path:
                delete_json_artifacts(path)
