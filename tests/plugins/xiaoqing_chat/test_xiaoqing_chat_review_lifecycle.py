"""xiaoqing_chat 管理审查命令的真实会话 ID 生命周期。"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugins.xiaoqing_chat import handlers as chat_handlers
from plugins.xiaoqing_chat.memory.review_sessions import ReviewStore
from tests.helpers.assertions import text_segments_text


def _open_session(
    store: ReviewStore,
    *,
    kind: str,
    chat_id: str,
    payload: dict[str, Any],
):
    session = store.open_session_if_allowed(
        kind             = kind,
        chat_id          = chat_id,
        payload          = payload,
        timeout_seconds  = 600,
        cooldown_seconds = 0,
        max_pending      = 20,
        now              = time.time(),
    )
    assert session is not None
    return session


@pytest.mark.asyncio
async def test_review_commands_reuse_real_session_ids_and_reject_invalid_mutations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``ok/answer/close/no`` 串联真实 ID，并检查越权和错误输入不改状态。"""

    # 生产 ``_chat_id`` 使用 ``g<群号>``，同时避免 Windows 文件名中的冒号。
    chat_id = "g880721010"
    store   = ReviewStore()
    store.bind(tmp_path)
    reflection = SimpleNamespace(goal_lock_seconds=600.0, max_avoid_patterns=5)
    hctx = SimpleNamespace(
        chat_id=chat_id,
        state=SimpleNamespace(review_store=store),
        runtime=SimpleNamespace(cfg=SimpleNamespace(reflection=reflection)),
    )
    context = SimpleNamespace(logger=MagicMock())
    event = {
        "message_type": "group",
        "group_id": 880_721_010,
        "user_id": 880_721_011,
    }

    monkeypatch.setattr(
        chat_handlers.HandlerContext,
        "from_event",
        staticmethod(lambda _event, _context: hctx),
    )
    monkeypatch.setattr(chat_handlers, "_is_admin_operator", lambda _event, _context: True)

    async def call(args: str) -> str:
        return text_segments_text(
            await chat_handlers.handle_review(args, event, context),
            separator="\n",
        )

    bad_reply = _open_session(
        store,
        kind    = "bad_reply_pattern",
        chat_id = chat_id,
        payload = {"goal": "自然参与群聊", "reason": "连续追问"},
    )
    assert "必须提供" in await call(f"answer {bad_reply.session_id}")
    unchanged = store.get_session(bad_reply.session_id)
    assert unchanged is not None and unchanged.step == 0 and unchanged.answers == []
    assert store.get_policy(chat_id).avoid_patterns == []

    assert "跨话题复用的判断原则" in await call(f"ok {bad_reply.session_id}")
    advanced = store.get_session(bad_reply.session_id)
    assert advanced is not None and advanced.step == 1

    rule = "不要连续追问，先接住群友原话"
    assert "长期规避模式" in await call(f"answer {bad_reply.session_id} {rule}")
    answered = store.get_session(bad_reply.session_id)
    assert answered is not None
    assert answered.session_id == bad_reply.session_id
    assert answered.step == 2
    assert answered.answers == [rule]
    assert store.get_policy(chat_id).avoid_patterns == [rule]

    assert "已关闭" in await call(f"close {bad_reply.session_id}")
    assert store.get_session(bad_reply.session_id) is None
    assert "不存在" in await call(f"answer {bad_reply.session_id} 不应写入")
    assert store.get_policy(chat_id).avoid_patterns == [rule]

    goal_session = _open_session(
        store,
        kind    = "goal_strategy",
        chat_id = chat_id,
        payload = {"goal": "自然聊天", "stats": "参与偏少"},
    )
    assert "已更新目标" in await call(
        f"answer {goal_session.session_id} goal: 主动参与值得接话的话题"
    )
    assert "已更新策略备注" in await call(
        f"answer {goal_session.session_id} strategy: 先回应具体内容，少说套话"
    )
    policy = store.get_policy(chat_id)
    assert policy.goal_override == "主动参与值得接话的话题"
    assert policy.strategy_note == "先回应具体内容，少说套话"
    same_goal_session = store.get_session(goal_session.session_id)
    assert same_goal_session is not None
    assert same_goal_session.answers == [
        "goal: 主动参与值得接话的话题",
        "strategy: 先回应具体内容，少说套话",
    ]
    assert "无需修改" in await call(f"ok {goal_session.session_id}")
    assert store.get_session(goal_session.session_id) is None

    rejected = _open_session(
        store,
        kind    = "goal_strategy",
        chat_id = chat_id,
        payload = {"goal": "保持自然"},
    )
    assert "已关闭" in await call(f"no {rejected.session_id}")
    assert store.get_session(rejected.session_id) is None

    other_chat = _open_session(
        store,
        kind    = "goal_strategy",
        chat_id = "gother",
        payload = {"goal": "隔离会话"},
    )
    assert "不属于当前会话" in await call(f"close {other_chat.session_id}")
    assert store.get_session(other_chat.session_id) is not None

    protected = _open_session(
        store,
        kind    = "bad_reply_pattern",
        chat_id = chat_id,
        payload = {"goal": "权限测试"},
    )
    monkeypatch.setattr(chat_handlers, "_is_admin_operator", lambda _event, _context: False)
    assert "仅 Bot 管理员" in await call(f"close {protected.session_id}")
    assert store.get_session(protected.session_id) is not None

    assert store.close_session(other_chat.session_id)
    assert store.close_session(protected.session_id)
