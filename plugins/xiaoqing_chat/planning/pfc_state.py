from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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

class PFCStateStore:
    def __init__(self) -> None:
        self._data_dir: Optional[Path] = None
        self._cache: dict[str, PFCConversationState] = {}

    def bind(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def _path(self, chat_id: str) -> Optional[Path]:
        if not self._data_dir:
            return None
        return self._data_dir / "pfc_state" / f"{chat_id}.json"

    _MAX_CACHE_SIZE = 200

    def get(self, chat_id: str) -> PFCConversationState:
        cid = str(chat_id)
        if cid in self._cache:
            return self._cache[cid]
        st = PFCConversationState(chat_id=cid)
        path = self._path(cid)
        if path and path.exists():
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    st.ignore_until_ts = float(obj.get("ignore_until_ts", 0.0) or 0.0)
                    st.ended = bool(obj.get("ended", False))
                    st.last_successful_reply_action = str(obj.get("last_successful_reply_action", "") or "")
                    
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
            except Exception:
                st = PFCConversationState(chat_id=cid)
        self._cache[cid] = st
        # Evict oldest entries when cache exceeds limit
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
        path.parent.mkdir(parents=True, exist_ok=True)
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
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            return
