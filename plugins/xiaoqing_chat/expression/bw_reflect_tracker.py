from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from .bw_expression_store import ExpressionRecord, ExpressionStore
from ..llm.llm_client import chat_completions_raw_with_fallback_paths
from ..memory.memory import MemoryStore, StoredMessage

_RE_JSON_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
_RE_OBJ = re.compile(r"\{[\s\S]*\}")

_JUDGE_PROMPT = """
你是一个表达反思助手。Bot之前询问了表达方式是否合适。
你需要根据提供的上下文对话，判断是否对该表达方式做出了肯定或否定的评价。

询问内容
情景: {situation}
风格: {style}

上下文对话
{context_block}

判断要求
1. 判断对话中是否包含对上述询问的回答。
2. 如果是，判断是肯定（Approve）还是否定（Reject），或者是提供了修改意见。
3. 如果不是回答，或者是无关内容，请返回 Ignore。
4. 如果是否定并提供了修改意见，请提取修正后的情景和风格。

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

class ReflectTrackerStore:
    def __init__(self) -> None:
        self._data_dir: Optional[Path] = None
        self._cache: dict[str, ReflectTrackerState] = {}

    def bind(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def _path(self) -> Optional[Path]:
        if not self._data_dir:
            return None
        return self._data_dir / "bw_learner" / "reflect_trackers.json"

    def load(self) -> dict[str, ReflectTrackerState]:
        if self._cache:
            return dict(self._cache)
        path = self._path()
        if not path or not path.exists():
            self._cache = {}
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                self._cache = {}
                return {}
            out: dict[str, ReflectTrackerState] = {}
            for k, v in raw.items():
                if not isinstance(v, dict):
                    continue
                operator_chat_id = str(v.get("operator_chat_id", "") or "").strip() or str(k).strip()
                expression_id = str(v.get("expression_id", "") or "").strip()
                created_time = float(v.get("created_time", 0.0) or 0.0)
                last_check_count = int(v.get("last_check_count", 0) or 0)
                if operator_chat_id and expression_id and created_time > 0:
                    out[operator_chat_id] = ReflectTrackerState(
                        operator_chat_id=operator_chat_id,
                        expression_id=expression_id,
                        created_time=created_time,
                        last_check_count=last_check_count,
                    )
            self._cache = out
            return dict(out)
        except Exception:
            self._cache = {}
            return {}

    def save(self) -> None:
        path = self._path()
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {}
        for k, st in self._cache.items():
            payload[k] = {
                "operator_chat_id": st.operator_chat_id,
                "expression_id": st.expression_id,
                "created_time": st.created_time,
                "last_check_count": st.last_check_count,
            }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            return

    def set_tracker(self, operator_chat_id: str, expression_id: str) -> None:
        self._cache[operator_chat_id] = ReflectTrackerState(
            operator_chat_id=operator_chat_id,
            expression_id=expression_id,
            created_time=time.time(),
            last_check_count=0,
        )
        self.save()

    def remove_tracker(self, operator_chat_id: str) -> None:
        if operator_chat_id in self._cache:
            self._cache.pop(operator_chat_id, None)
            self.save()

    def get_tracker(self, operator_chat_id: str) -> Optional[ReflectTrackerState]:
        self.load()
        return self._cache.get(operator_chat_id)

def _extract_json_obj(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    blocks = _RE_JSON_BLOCK.findall(s)
    if blocks:
        s = blocks[0].strip()
    m = _RE_OBJ.search(s)
    if m:
        s = m.group(0)
    try:
        obj = json.loads(s)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}

def _render_context(messages: Sequence[StoredMessage], *, max_lines: int = 18) -> str:
    lines: list[str] = []
    for msg in messages[-max_lines:]:
        t = (msg.content or "").strip()
        if not t:
            continue
        if len(t) > 180:
            t = t[:140].rstrip() + "…"
        name = msg.name or ("小青" if msg.role == "assistant" else "用户")
        role = "小青" if msg.role == "assistant" else "对方"
        lines.append(f"{role}({name})：{t}")
    return "\n".join(lines).strip()

def _find_expression(items: Sequence[ExpressionRecord], expression_id: str) -> Optional[ExpressionRecord]:
    for it in items:
        if it.expression_id == expression_id:
            return it
    return None

async def tick_reflect_tracker(
    *,
    context,
    operator_chat_id: str,
    memory_store: MemoryStore,
    expr_store: ExpressionStore,
    tracker_store: ReflectTrackerStore,
    secrets: dict[str, Any],
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
    max_duration_seconds: float = 15 * 60,
    max_message_count: int = 30,
) -> bool:
    tracker = tracker_store.get_tracker(operator_chat_id)
    if not tracker:
        return False

    if time.time() - tracker.created_time > float(max_duration_seconds):
        tracker_store.remove_tracker(operator_chat_id)
        return True

    history = memory_store.get(operator_chat_id)
    new_msgs = [m for m in history if m.role == "user" and float(m.ts or 0.0) >= tracker.created_time]
    if len(new_msgs) > int(max_message_count):
        tracker_store.remove_tracker(operator_chat_id)
        return True

    if len(new_msgs) <= int(tracker.last_check_count):
        return False
    tracker.last_check_count = len(new_msgs)
    tracker_store.save()

    expr_items = expr_store.load()
    expr = _find_expression(expr_items, tracker.expression_id)
    if not expr:
        tracker_store.remove_tracker(operator_chat_id)
        return True

    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return False

    context_block = _render_context([m for m in history if float(m.ts or 0.0) >= tracker.created_time])
    if not context_block:
        return False

    prompt = _JUDGE_PROMPT.format(situation=expr.situation, style=expr.style, context_block=context_block)
    resp, _path = await chat_completions_raw_with_fallback_paths(
        session=context.http_session,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        top_p=0.8,
        max_tokens=400,
        timeout_seconds=float(timeout_seconds),
        max_retry=int(max_retry),
        retry_interval_seconds=float(retry_interval_seconds),
        proxy=proxy,
        endpoint_path=endpoint_path,
    )
    content = (((resp.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
    obj = _extract_json_obj(str(content))
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
        tracker_store.remove_tracker(operator_chat_id)
        return True
    return False
