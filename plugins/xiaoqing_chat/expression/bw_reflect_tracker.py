"""持久化表达反思队列，并消费操作者的后续判断。"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..llm.llm_client import chat_completions_raw_with_fallback_paths
from ..memory.memory import MemoryStore
from ..store_base import StoreBase
from ..utils.json_parsing import parse_first_json_object
from .bw_expression_store import ExpressionRecord, ExpressionStore
from .expr_utils import render_dialogue

_JUDGE_PROMPT = """
你是表达反馈判定器。只根据提供的后续对话，判断对方是否明确评价了待审核表达方式。

询问内容
情景: {situation}
风格: {style}

上下文对话
{context_block}

判断要求
1. 只有能明确对应这次询问的反馈才算回答，不从沉默、换话题、情绪或模糊附和推断态度。
2. 明确肯定返回 Approve；明确否定或提出修改返回 Reject；其余返回 Ignore。
3. 只有对方直接给出修正内容时才提取 corrected 字段，不自行生成建议。

请输出JSON格式：
```json
{{
  "judgment": "Approve" | "Reject" | "Ignore",
  "corrected_situation": "",
  "corrected_style": ""
}}
```""".strip()


@dataclass
class ReflectTrackerState:
    operator_chat_id: str
    expression_id: str
    created_time: float
    last_check_count: int = 0
    last_consumed_time: float = 0.0


class ReflectTrackerStore(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, list[ReflectTrackerState]] | None = None
        self._lock = threading.RLock()

    def bind(self, data_dir: Path) -> None:
        with self._lock:
            super().bind(data_dir)
            self._cache = None

    def _path(self) -> Path | None:
        return self._resolve_path("bw_learner", "reflect_trackers.json")

    def load(self) -> dict[str, ReflectTrackerState]:
        with self._lock:
            self._ensure_loaded()
            assert self._cache is not None
            return {chat_id: queue[0] for chat_id, queue in self._cache.items() if queue}

    def _ensure_loaded(self) -> None:
        if self._cache is not None:
            return
        try:
            raw = self._load_json_from_path_parts("bw_learner", "reflect_trackers.json", default={})
            if not isinstance(raw, dict):
                self._cache = {}
                return
            out: dict[str, list[ReflectTrackerState]] = {}
            for key, value in raw.items():
                values = value if isinstance(value, list) else [value]
                queue: list[ReflectTrackerState] = []
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    operator_chat_id = (
                        str(item.get("operator_chat_id", "") or "").strip() or str(key).strip()
                    )
                    expression_id = str(item.get("expression_id", "") or "").strip()
                    created_time = float(item.get("created_time", 0.0) or 0.0)
                    last_check_count = int(item.get("last_check_count", 0) or 0)
                    last_consumed_time = float(item.get("last_consumed_time", 0.0) or 0.0)
                    if operator_chat_id and expression_id and created_time > 0:
                        queue.append(
                            ReflectTrackerState(
                                operator_chat_id=operator_chat_id,
                                expression_id=expression_id,
                                created_time=created_time,
                                last_check_count=max(0, last_check_count),
                                last_consumed_time=max(0.0, last_consumed_time),
                            )
                        )
                if queue:
                    out[str(key)] = queue
            self._cache = out
        except (OSError, TypeError, ValueError):
            self._cache = {}

    def save(self) -> None:
        with self._lock:
            self._ensure_loaded()
            assert self._cache is not None
            payload: dict[str, Any] = {}
            for chat_id, queue in self._cache.items():
                payload[chat_id] = [
                    {
                        "operator_chat_id": state.operator_chat_id,
                        "expression_id": state.expression_id,
                        "created_time": state.created_time,
                        "last_check_count": state.last_check_count,
                        "last_consumed_time": state.last_consumed_time,
                    }
                    for state in queue
                ]
            self._save_json_to_path_parts("bw_learner", "reflect_trackers.json", data=payload)

    def set_tracker(
        self,
        operator_chat_id: str,
        expression_id: str,
        *,
        max_pending: int = 32,
    ) -> None:
        with self._lock:
            self._ensure_loaded()
            assert self._cache is not None
            queue = self._cache.setdefault(operator_chat_id, [])
            if any(state.expression_id == expression_id for state in queue):
                return
            queue.append(
                ReflectTrackerState(
                    operator_chat_id=operator_chat_id,
                    expression_id=expression_id,
                    created_time=time.time(),
                )
            )
            del queue[: max(0, len(queue) - max(1, int(max_pending)))]
            self.save()

    def remove_tracker(self, operator_chat_id: str, expression_id: str | None = None) -> None:
        with self._lock:
            self._ensure_loaded()
            assert self._cache is not None
            queue = self._cache.get(operator_chat_id, [])
            if expression_id is None:
                changed = bool(self._cache.pop(operator_chat_id, None))
            else:
                remaining = [state for state in queue if state.expression_id != expression_id]
                changed = len(remaining) != len(queue)
                if remaining:
                    self._cache[operator_chat_id] = remaining
                else:
                    self._cache.pop(operator_chat_id, None)
            if changed:
                self.save()

    def clear(self, chat_id: str) -> None:
        """清除一个会话的全部待反思队列。"""

        self.remove_tracker(chat_id)

    def consume_tracker(
        self,
        operator_chat_id: str,
        expression_id: str,
        *,
        consumed_message_time: float,
    ) -> None:
        with self._lock:
            self._ensure_loaded()
            assert self._cache is not None
            queue = self._cache.get(operator_chat_id, [])
            remaining = [state for state in queue if state.expression_id != expression_id]
            for state in remaining:
                state.last_consumed_time = max(
                    state.last_consumed_time, float(consumed_message_time)
                )
                state.last_check_count = 0
            if remaining:
                self._cache[operator_chat_id] = remaining
            else:
                self._cache.pop(operator_chat_id, None)
            self.save()

    def get_trackers(self, operator_chat_id: str) -> list[ReflectTrackerState]:
        with self._lock:
            self._ensure_loaded()
            assert self._cache is not None
            return list(self._cache.get(operator_chat_id, []))


def _find_expression(
    items: Sequence[ExpressionRecord], expression_id: str
) -> ExpressionRecord | None:
    for it in items:
        if it.expression_id == expression_id:
            return it
    return None


async def _tick_reflect_tracker_once(
    *,
    operator_chat_id: str,
    tracker: ReflectTrackerState,
    memory_store: MemoryStore,
    expr_store: ExpressionStore,
    tracker_store: ReflectTrackerStore,
    secrets: dict[str, Any],
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    bot_name: str,
    max_duration_seconds: float = 15 * 60,
    max_message_count: int = 30,
) -> bool:
    if time.time() - tracker.created_time > float(max_duration_seconds):
        tracker_store.remove_tracker(operator_chat_id, tracker.expression_id)
        return True

    history = await memory_store.get_async(operator_chat_id)
    new_msgs = [
        message
        for message in history
        if message.role == "user"
        and float(message.ts or 0.0) >= tracker.created_time
        and float(message.ts or 0.0) > tracker.last_consumed_time
    ]
    if len(new_msgs) > int(max_message_count):
        tracker_store.remove_tracker(operator_chat_id, tracker.expression_id)
        return True

    if len(new_msgs) <= int(tracker.last_check_count):
        return False
    # 仅在处理成功后更新计数，因此放在函数末尾。
    current_msg_count = len(new_msgs)

    expr_items = expr_store.load()
    expr = _find_expression(expr_items, tracker.expression_id)
    if not expr:
        tracker_store.remove_tracker(operator_chat_id, tracker.expression_id)
        return True

    if "_ai" in secrets and secrets.get("_ai") is None:
        return False

    context_block = render_dialogue(
        [
            message
            for message in history
            if float(message.ts or 0.0) >= tracker.created_time
            and float(message.ts or 0.0) > tracker.last_consumed_time
        ],
        bot_name=bot_name,
    )
    if not context_block:
        return False

    prompt = _JUDGE_PROMPT.format(
        situation=expr.situation, style=expr.style, context_block=context_block
    )
    resp, _path = await chat_completions_raw_with_fallback_paths(
        secrets=secrets,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        top_p=0.8,
        max_tokens=400,
        timeout_seconds=float(timeout_seconds),
        max_retry=int(max_retry),
        retry_interval_seconds=float(retry_interval_seconds),
    )
    content = (((resp.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
    obj = parse_first_json_object(str(content)) or {}
    judgment = str(obj.get("judgment", "") or "").strip().lower()
    corrected_situation = str(obj.get("corrected_situation", "") or "").strip()
    corrected_style = str(obj.get("corrected_style", "") or "").strip()

    resolved = False
    if judgment == "approve":
        expr.checked = True
        expr.rejected = False
        expr.modified_by = "user"
        resolved = True
    elif judgment == "reject":
        if corrected_situation or corrected_style:
            if corrected_situation:
                expr.situation = corrected_situation[:80].strip()
            if corrected_style:
                expr.style = corrected_style[:80].strip()
            expr.checked = True
            expr.rejected = False
            expr.modified_by = "user"
        else:
            expr.checked = False
            expr.rejected = True
            expr.modified_by = "user"
        resolved = True
    elif judgment == "ignore":
        resolved = False

    if resolved:
        expr_store.save(expr_items)
        tracker_store.consume_tracker(
            operator_chat_id,
            tracker.expression_id,
            consumed_message_time=max(float(message.ts or 0.0) for message in new_msgs),
        )
        return True
    # 只有未找到结论时才更新 last_check_count，避免重置到相同值后下一轮死循环。
    tracker.last_check_count = current_msg_count
    tracker_store.save()
    return False


async def tick_reflect_tracker(
    *,
    operator_chat_id: str,
    memory_store: MemoryStore,
    expr_store: ExpressionStore,
    tracker_store: ReflectTrackerStore,
    secrets: dict[str, Any],
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    bot_name: str,
    max_duration_seconds: float = 15 * 60,
    max_message_count: int = 30,
) -> bool:
    """检查待反思队列，且一条用户回复最多消费一道题。"""
    for tracker in tracker_store.get_trackers(operator_chat_id):
        changed = await _tick_reflect_tracker_once(
            operator_chat_id=operator_chat_id,
            tracker=tracker,
            memory_store=memory_store,
            expr_store=expr_store,
            tracker_store=tracker_store,
            secrets=secrets,
            timeout_seconds=timeout_seconds,
            max_retry=max_retry,
            retry_interval_seconds=retry_interval_seconds,
            max_duration_seconds=max_duration_seconds,
            max_message_count=max_message_count,
            bot_name=bot_name,
        )
        if changed:
            return True
    return False
