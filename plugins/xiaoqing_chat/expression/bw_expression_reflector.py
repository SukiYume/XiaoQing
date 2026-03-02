from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any, Optional

from core.plugin_base import build_action, segments, text

from .bw_expression_store import ExpressionRecord, ExpressionStore
from .bw_reflect_tracker import ReflectTrackerStore

def _state_path(data_dir: Path) -> Path:
    return data_dir / "bw_learner" / "reflector_state.json"

def _load_state(data_dir: Path) -> dict[str, Any]:
    path = _state_path(data_dir)
    if not path.exists():
        return {"last_sent_ts": 0.0}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {"last_sent_ts": 0.0}
    except Exception:
        return {"last_sent_ts": 0.0}

def _save_state(data_dir: Path, st: dict[str, Any]) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return

def _pick_candidates(items: list[ExpressionRecord], *, max_pick: int) -> list[ExpressionRecord]:
    cands = [x for x in items if (not x.checked) and (not x.rejected) and x.situation and x.style]
    cands.sort(key=lambda x: (-x.count, -x.last_active_time))
    if not cands:
        return []
    top = cands[: min(80, len(cands))]
    random.shuffle(top)
    return top[: max(1, int(max_pick))]

def _operator_chat_id(operator_user_id: int, operator_group_id: int) -> str:
    if operator_group_id:
        return f"g{int(operator_group_id)}"
    return f"u{int(operator_user_id)}"

async def maybe_ask_for_reflection(
    *,
    context,
    expr_store: ExpressionStore,
    tracker_store: ReflectTrackerStore,
    operator_user_id: int,
    operator_group_id: int,
    min_interval_seconds: float,
    ask_per_check: int,
) -> int:
    if not operator_user_id and not operator_group_id:
        return 0
    data_dir: Path = context.data_dir
    st = _load_state(data_dir)
    last_sent = float(st.get("last_sent_ts", 0.0) or 0.0)
    now = time.time()
    if min_interval_seconds > 0 and now - last_sent < float(min_interval_seconds):
        return 0

    expr_store.bind(data_dir)
    tracker_store.bind(data_dir)
    tracker_store.load()

    items = expr_store.load()
    picks = _pick_candidates(items, max_pick=int(ask_per_check or 1))
    if not picks:
        return 0

    op_chat_id = _operator_chat_id(int(operator_user_id), int(operator_group_id))

    sent = 0
    for ex in picks:
        msg = (
            "表达反思：这条“说话方式”你觉得合适吗？\n"
            f"- 情景：{ex.situation}\n"
            f"- 风格：{ex.style}\n\n"
            "你可以直接回复：\n"
            "- 同意 / 可以\n"
            "- 不行 / 拒绝\n"
            "- 给修改意见（例如：‘情景改成… 风格改成…’）\n"
        ).strip()
        segs = segments([text(msg)])
        action = build_action(
            segs,
            user_id=int(operator_user_id) if operator_user_id else None,
            group_id=int(operator_group_id) if operator_group_id else None,
        )
        if action:
            await context.send_action(action)
            tracker_store.set_tracker(op_chat_id, ex.expression_id)
            sent += 1

    if sent:
        st["last_sent_ts"] = now
        _save_state(data_dir, st)
    return sent
