from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from plugins.pendo import main as pendo_main


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret",
    [
        "账目: 工资 50000",
        "日记: 私人病历",
        "笔记: INTERNAL-CANARY",
        "待办: 与律师通话",
    ],
)
@pytest.mark.parametrize("failure", [False, RuntimeError("offline")])
async def test_sensitive_group_reply_fails_closed(secret: str, failure, monkeypatch) -> None:
    context = type("Context", (), {})()
    if isinstance(failure, BaseException):
        context.send_action = AsyncMock(side_effect=failure)
    else:
        context.send_action = AsyncMock(return_value=failure)

    async def privacy_enabled(_user_id, _context):
        return True

    monkeypatch.setattr(pendo_main, "_get_user_privacy_mode", privacy_enabled)
    result = await pendo_main._format_result(
        "12345",
        {"message": secret},
        group_id=67890,
        context=context,
    )
    rendered = str(result)
    assert "私聊发送失败" in rendered
    assert secret not in rendered
    assert context.send_action.await_args.args[0]["action"] == "send_private_msg"
