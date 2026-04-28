import pytest

from plugins.xiaoqing_chat.memory.knowledge_extract import _parse_fact_json, _parse_word_json
from plugins.xiaoqing_chat.memory.memory_retrieval import _parse_question_json


def test_parse_question_json_accepts_embedded_json_object() -> None:
    text = 'prefix {"question": "  今晚吃什么  "} suffix'
    assert _parse_question_json(text) == "今晚吃什么"


def test_parse_word_json_accepts_embedded_json_object() -> None:
    text = (
        "说明：\n"
        '{"items":[{"word":"YYDS","definition":"永远的神"},{"word":"","definition":"skip"}]}'
        "\n结束"
    )
    items = _parse_word_json(text)
    assert [(item.word, item.definition) for item in items] == [("YYDS", "永远的神")]


def test_parse_fact_json_accepts_embedded_json_object() -> None:
    text = (
        "explain\n"
        '{"facts":[{"subject_id":"42","subject_name":"小王","fact":"喜欢火锅","evidence":"昨晚说过"}]}'
        "\nend"
    )
    facts = _parse_fact_json(text)
    assert len(facts) == 1
    assert facts[0].subject_id == 42
    assert facts[0].subject_name == "小王"
    assert facts[0].fact == "喜欢火锅"


def test_json_parsing_helper_extracts_embedded_object_and_named_list() -> None:
    from plugins.xiaoqing_chat.utils.json_parsing import (
        extract_named_list_field,
        parse_first_json_object,
    )

    text = 'note {"items": [{"k": 1}], "facts": [{"k": 2}]} done'
    obj = parse_first_json_object(text)

    assert obj == {"items": [{"k": 1}], "facts": [{"k": 2}]}
    assert extract_named_list_field(obj, "items") == [{"k": 1}]
    assert extract_named_list_field(obj, "missing") == []


def test_json_parsing_helper_strips_think_and_repairs_common_bad_json() -> None:
    from plugins.xiaoqing_chat.utils.json_parsing import parse_first_json_object

    text = '<think>先想一下</think>\n```json\n{suitable: true, reason: "ok",}\n```'

    assert parse_first_json_object(text) == {"suitable": True, "reason": "ok"}


def test_llm_content_extractor_ignores_reasoning_content_and_strips_think_tags() -> None:
    from plugins.xiaoqing_chat.llm.llm_client import extract_response_content

    data = {
        "choices": [
            {
                "message": {
                    "reasoning_content": "这里不应该参与解析",
                    "content": "<think>内部思考</think>\n{\"answer\":\"最终\"}",
                }
            }
        ]
    }

    assert extract_response_content(data) == '{"answer":"最终"}'


@pytest.mark.asyncio
async def test_reply_checker_uses_shared_content_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugins.xiaoqing_chat.llm import llm_client
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    async def fake_raw_with_fallback_paths(**_kwargs):
        return (
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '前缀 {"suitable": false, "reason": "重复", '
                                '"need_replan": true} 后缀'
                            )
                        }
                    }
                ]
            },
            "/v1/chat/completions",
        )

    called = {"count": 0}
    original_extractor = llm_client.extract_response_content

    def traced_extractor(data):
        called["count"] += 1
        return original_extractor(data)

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.llm.reply_checker.chat_completions_raw_with_fallback_paths",
        fake_raw_with_fallback_paths,
    )
    monkeypatch.setattr(llm_client, "extract_response_content", traced_extractor)

    result = await check_reply(
        http_session=None,
        secrets={"api_base": "https://example.com", "api_key": "k", "model": "m"},
        bot_name="小青",
        reply="好的",
        goal="聊天",
        policy_text="",
        history=[],
        chat_history_text="user: hi",
        enable_llm_checker=True,
        max_repeat_compare=3,
        similarity_threshold=0.9,
        max_assistant_in_row=5,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        proxy="",
        endpoint_path="/v1/chat/completions",
    )

    assert called["count"] == 1
    assert result.suitable is False
    assert result.reason == "重复"
    assert result.need_replan is True


@pytest.mark.asyncio
async def test_llm_client_fallback_wrapper_shared_logic_for_content_mode() -> None:
    from plugins.xiaoqing_chat.llm.llm_client import LLMError, _call_with_fallback_paths

    calls: list[str] = []

    async def invoke(path: str):
        calls.append(path)
        if path == "/v1/chat/completions":
            raise LLMError("http_404:not found")
        return "ok"

    value, used_path = await _call_with_fallback_paths(
        endpoint_path="/v1/chat/completions",
        invoke=invoke,
    )

    assert value == "ok"
    assert used_path == "/chat/completions"
    assert calls == ["/v1/chat/completions", "/chat/completions"]


@pytest.mark.asyncio
async def test_llm_client_fallback_wrapper_stops_on_non_404() -> None:
    from plugins.xiaoqing_chat.llm.llm_client import LLMError, _call_with_fallback_paths

    calls: list[str] = []

    async def invoke(path: str):
        calls.append(path)
        raise LLMError("http_500:boom")

    with pytest.raises(LLMError, match="http_500"):
        await _call_with_fallback_paths(endpoint_path="/v1/chat/completions", invoke=invoke)

    assert calls == ["/v1/chat/completions"]


@pytest.mark.asyncio
async def test_person_fact_extract_skips_empty_llm_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from unittest.mock import MagicMock

    from plugins.xiaoqing_chat.llm.llm_client import LLMError
    from plugins.xiaoqing_chat.memory.knowledge_extract import maybe_extract_person_facts
    from plugins.xiaoqing_chat.memory.memory import StoredMessage

    async def raise_empty_response(**_kwargs):
        raise LLMError("empty_response")

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.memory.knowledge_extract.chat_completions",
        raise_empty_response,
    )

    history = [
        StoredMessage(role="user", name=f"User{i}", content=f"消息{i}", ts=float(i))
        for i in range(20)
    ]
    memory_db = MagicMock()

    await maybe_extract_person_facts(
        data_dir=tmp_path,
        http_session=None,
        secrets={"api_base": "https://example.com", "api_key": "k", "model": "m"},
        memory_db=memory_db,
        bot_name="小青",
        chat_id="g1",
        history=history,
        temperature=0.8,
        top_p=0.9,
        max_tokens=512,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        proxy="",
        endpoint_path="/v1/chat/completions",
    )

    memory_db.upsert_text.assert_not_called()
