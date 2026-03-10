from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..store_base import StoreBase


@dataclass
class PFCConversationState:
    chat_id: str
    ignore_until_ts: float = 0.0
    ended: bool = False
    last_successful_reply_action: str = ""
    goal_list: list[dict[str, Any]] = field(default_factory=list)
    knowledge_list: list[dict[str, Any]] = field(default_factory=list)
    planner_fail_ts: list[float] = field(default_factory=list)
    planner_skip_until: float = 0.0
    updated_at: float = field(default_factory=lambda: time.time())


class PFCStateStore(StoreBase):
    _MAX_CACHE_SIZE = 200

    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, PFCConversationState] = {}

    def _path(self, chat_id: str) -> Optional[Path]:
        return self._resolve_path("pfc_state", f"{chat_id}.json")

    def get(self, chat_id: str) -> PFCConversationState:
        cid = str(chat_id)
        if cid in self._cache:
            return self._cache[cid]
        st = PFCConversationState(chat_id=cid)
        path = self._path(cid)
        if path:
            obj = self._load_json(path, default=None)
            if isinstance(obj, dict):
                st.ignore_until_ts = float(obj.get("ignore_until_ts", 0.0) or 0.0)
                st.ended = bool(obj.get("ended", False))
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
                st.updated_at = float(obj.get("updated_at", time.time()) or time.time())
        self._cache[cid] = st
        if len(self._cache) > self._MAX_CACHE_SIZE:
            sorted_keys = sorted(
                self._cache.keys(),
                key=lambda k: self._cache[k].updated_at,
            )
            for k in sorted_keys[: len(self._cache) - self._MAX_CACHE_SIZE]:
                del self._cache[k]
        return st

    def save(self, chat_id: str) -> None:
        cid = str(chat_id)
        st = self._cache.get(cid)
        if not st:
            return
        path = self._path(cid)
        if not path:
            return
        st.updated_at = time.time()
        payload = {
            "ignore_until_ts": st.ignore_until_ts,
            "ended": st.ended,
            "last_successful_reply_action": st.last_successful_reply_action,
            "goal_list": st.goal_list,
            "knowledge_list": st.knowledge_list,
            "planner_fail_ts": st.planner_fail_ts,
            "planner_skip_until": st.planner_skip_until,
            "updated_at": st.updated_at,
        }
        self._save_json(path, payload)

    async def get_async(self, chat_id: str) -> PFCConversationState:
        return await asyncio.to_thread(self.get, chat_id)

    def set_state(self, chat_id: str, state: PFCConversationState) -> None:
        cid = str(chat_id)
        state.chat_id = cid
        state.updated_at = time.time()
        self._cache[cid] = state

    async def save_async(self, chat_id: str) -> None:
        await asyncio.to_thread(self.save, chat_id)

    async def set_state_async(self, chat_id: str, state: PFCConversationState) -> None:
        await asyncio.to_thread(self.set_state, chat_id, state)
