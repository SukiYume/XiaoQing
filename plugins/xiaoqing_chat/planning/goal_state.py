from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

@dataclass
class GoalState:
    ts: float = 0.0
    goal: str = ""
    source: str = ""

class GoalStore:
    def __init__(self) -> None:
        self._data_dir: Optional[Path] = None
        self._cache: dict[str, GoalState] = {}

    def bind(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def _path(self, chat_id: str) -> Optional[Path]:
        if not self._data_dir:
            return None
        return self._data_dir / "goal_state" / f"{chat_id}.json"

    def get(self, chat_id: str) -> GoalState:
        if chat_id in self._cache:
            return self._cache[chat_id]
        st = GoalState()
        path = self._path(chat_id)
        if path and path.exists():
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    st.ts = float(obj.get("ts", 0.0) or 0.0)
                    st.goal = str(obj.get("goal", "") or "").strip()
                    st.source = str(obj.get("source", "") or "").strip()
            except Exception:
                st = GoalState()
        self._cache[chat_id] = st
        return st

    def set(self, chat_id: str, *, goal: str, source: str) -> GoalState:
        g = (goal or "").strip()
        if not g:
            return self.get(chat_id)
        if len(g) > 80:
            # Truncate at sentence/phrase boundary to avoid cutting mid-word
            cut = g[:80]
            # Try to find a natural break point
            for sep in ("。", "，", "；", "！", "？", " ", "、"):
                idx = cut.rfind(sep)
                if idx >= 20:  # at least keep 20 chars
                    cut = cut[: idx + 1].rstrip()
                    break
            else:
                cut = cut[:77].rstrip()
            g = cut + "…"
        st = GoalState(ts=time.time(), goal=g, source=(source or "").strip())
        self._cache[chat_id] = st
        path = self._path(chat_id)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.write_text(
                    json.dumps({"ts": st.ts, "goal": st.goal, "source": st.source}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass
        return st

def load_latest_topic_summary(data_dir: Path, chat_id: str) -> str:
    path = data_dir / "hippo_memorizer" / f"{chat_id}.json"
    if not path.exists():
        return ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not raw:
            return ""
        item = raw[-1]
        if not isinstance(item, dict):
            return ""
        topic = str(item.get("topic", "")).strip()
        if not topic:
            return ""
        return topic
    except Exception:
        return ""

_RE_GOAL = re.compile(r"(?:目标|要点|意图)[:：]\s*(.{2,80})")

def derive_goal(
    *,
    data_dir: Path,
    chat_id: str,
    current_text: str,
    planner_reasoning: str,
) -> str:
    pr = (planner_reasoning or "").strip()
    if pr:
        m = _RE_GOAL.search(pr)
        if m:
            g = (m.group(1) or "").strip()
            if g:
                return g
        if len(pr) <= 60:
            return pr
    t = (current_text or "").strip()
    if t:
        if "?" in t or "？" in t or t.endswith("吗") or t.endswith("嘛"):
            if len(t) <= 28:
                return f"回答用户问题：{t}"
            return "回答用户问题"
        if len(t) <= 14:
            return f"围绕“{t}”继续聊"
    topic = load_latest_topic_summary(data_dir, chat_id)
    if topic:
        return f"围绕话题“{topic}”自然聊天"
    return "自然聊天"
