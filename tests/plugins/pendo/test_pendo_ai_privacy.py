# 验证 Pendo AI 请求在用户授权范围内选取数据。
import asyncio
from types import SimpleNamespace

from plugins.pendo.services.ai_parser import AIParser


class _RecordingAi:
    """模型边界替身：记录收到的每次调用并返回固定的情绪 JSON。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    async def complete(self, route, messages, **_kwargs):
        self.calls.append((route, messages))
        return SimpleNamespace(content='{"mood":"calm","mood_score":5}')


def _parser_with_ai(db) -> tuple[AIParser, _RecordingAi]:
    ai = _RecordingAi()
    context = SimpleNamespace(capabilities=SimpleNamespace(ai=ai))
    return AIParser(context=context, db=db), ai


def test_diary_text_is_not_sent_to_ai_without_explicit_consent(db):
    parser, ai = _parser_with_ai(db)

    asyncio.run(parser.analyze_diary_mood("CANARY-private-diary-text", "u1"))

    assert ai.calls == []


def test_diary_ai_consent_is_explicit_and_revocable(db):
    parser, ai = _parser_with_ai(db)
    db.update_user_settings("u1", {"settings_json": {"ai_sensitive_data_consent": True}})

    assert asyncio.run(parser.analyze_diary_mood("private text", "u1")) == ("calm", 5)
    assert ai.calls

    db.update_user_settings("u1", {"settings_json": {"ai_sensitive_data_consent": False}})
    ai.calls.clear()
    asyncio.run(parser.analyze_diary_mood("private text", "u1"))
    assert ai.calls == []
