import asyncio

import pytest

from plugins.pendo.services.ai_parser import AIParser
from plugins.pendo.services.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "pendo.db"))
    try:
        yield database
    finally:
        database.cleanup()


def test_diary_text_is_not_sent_to_ai_without_explicit_consent(db, monkeypatch):
    parser = AIParser(db=db)
    calls: list[object] = []

    async def should_not_run(*_args, **_kwargs):
        calls.append(object())
        return '{"mood":"calm","mood_score":5}'

    monkeypatch.setattr(parser, "_call_llm", should_not_run)
    asyncio.run(parser.analyze_diary_mood("CANARY-private-diary-text", "u1"))

    assert calls == []


def test_diary_ai_consent_is_explicit_and_revocable(db, monkeypatch):
    parser = AIParser(db=db)
    db.update_user_settings("u1", {"settings_json": {"ai_sensitive_data_consent": True}})
    calls: list[object] = []

    async def fake_llm(*_args, **_kwargs):
        calls.append(object())
        return '{"mood":"calm","mood_score":5}'

    monkeypatch.setattr(parser, "_call_llm", fake_llm)
    assert asyncio.run(parser.analyze_diary_mood("private text", "u1")) == ("calm", 5)
    assert calls

    db.update_user_settings("u1", {"settings_json": {"ai_sensitive_data_consent": False}})
    calls.clear()
    asyncio.run(parser.analyze_diary_mood("private text", "u1"))
    assert calls == []
