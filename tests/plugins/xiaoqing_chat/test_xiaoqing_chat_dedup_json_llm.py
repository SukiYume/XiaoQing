import pytest

from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from plugins.xiaoqing_chat.memory.knowledge_extract import _parse_fact_json
from plugins.xiaoqing_chat.memory.memory_retrieval import _parse_question_json


def test_parse_question_json_rejects_embedded_json_object() -> None:
    text = 'prefix {"question": "  今晚吃什么  "} suffix'
    assert _parse_question_json(text) == ""


def test_parse_fact_json_rejects_embedded_json_object() -> None:
    text = (
        "explain\n"
        '{"facts":[{"subject_id":"42","subject_name":"小王","fact":"喜欢火锅","evidence":"昨晚说过"}]}'
        "\nend"
    )
    facts = _parse_fact_json(text)
    assert facts == []


def test_json_parsing_helper_rejects_embedded_object() -> None:
    from plugins.xiaoqing_chat.utils.json_parsing import (
        extract_named_list_field,
        parse_first_json_object,
    )

    text = 'note {"items": [{"k": 1}], "facts": [{"k": 2}]} done'
    obj  = parse_first_json_object(text)

    assert obj is None
    assert extract_named_list_field(obj, "items") == []
    assert extract_named_list_field(obj, "missing") == []


def test_json_parsing_helper_rejects_think_prefix() -> None:
    from plugins.xiaoqing_chat.utils.json_parsing import parse_first_json_object

    text = '<think>先想一下</think>\n```json\n{suitable: true, reason: "ok",}\n```'

    assert parse_first_json_object(text) is None


@pytest.mark.parametrize(
    ("payload", "extractor_name"),
    [
        ('{"text":"右括号 } 与转义引号 \\"","nested":{"items":[{"k":1}]}}', "object"),
        ('[{"text":"右方括号 ] 与转义引号 \\"","nested":[[1],2]}]', "array"),
    ],
)
def test_json_root_scanner_handles_nested_values_and_escaped_delimiters(
    payload: str,
    extractor_name: str,
) -> None:
    from plugins.xiaoqing_chat.utils.json_parsing import (
        extract_first_json_array_text,
        extract_first_json_object_text,
    )

    extractor = (
        extract_first_json_object_text
        if extractor_name == "object"
        else extract_first_json_array_text
    )
    assert extractor(payload) == payload


@pytest.mark.parametrize(
    ("payload", "extractor_name"),
    [
        ('{"ok":true} trailing', "object"),
        ('{"text":"unterminated}', "object"),
        ('[{"ok":true}] trailing', "array"),
        ('[{"text":"unterminated}]', "array"),
    ],
)
def test_json_root_scanner_rejects_trailing_or_unterminated_input(
    payload: str,
    extractor_name: str,
) -> None:
    from plugins.xiaoqing_chat.utils.json_parsing import (
        extract_first_json_array_text,
        extract_first_json_object_text,
    )

    extractor = (
        extract_first_json_object_text
        if extractor_name == "object"
        else extract_first_json_array_text
    )
    assert extractor(payload) == ""


def test_llm_content_extractor_preserves_untrusted_think_prefix() -> None:
    from plugins.xiaoqing_chat.llm.gateway import extract_response_content

    data = {
        "choices": [
            {
                "message": {
                    "reasoning_content": "这里不应该参与解析",
                    "content"          : '<think>内部思考</think>\n{"answer":"最终"}',
                }
            }
        ]
    }

    assert extract_response_content(data) == '<think>内部思考</think>\n{"answer":"最终"}'


@pytest.mark.asyncio
async def test_gateway_fallback_wrapper_shared_logic_for_content_mode() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core.ai import AICompletionResult
    from plugins.xiaoqing_chat.llm.gateway import (
        chat_completions_raw_with_fallback_paths,
    )

    service = SimpleNamespace(
        complete=AsyncMock(
            return_value=AICompletionResult(
                response      = {"choices": [{"message": {"content": "ok"}}]},
                profile       = "fallback-profile",
                provider      = "provider",
                model         = "actual-model",
                finish_reason = "stop",
                attempts      = 2,
            )
        )
    )
    response, used_profile = await chat_completions_raw_with_fallback_paths(
        secrets                = {"_ai": service, "_route": "chat"},
        messages               = [{"role": "user", "content": "hi"}],
        temperature            = 0.2,
        top_p                  = 0.9,
        max_tokens             = 32,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )

    assert response["choices"][0]["message"]["content"] == "ok"
    assert used_profile == "fallback-profile"
    assert "model" not in service.complete.await_args.kwargs


@pytest.mark.asyncio
async def test_gateway_fallback_wrapper_stops_on_non_404() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core.ai import AIRequestError
    from plugins.xiaoqing_chat.llm.gateway import (
        chat_completions_raw_with_fallback_paths,
    )

    service = SimpleNamespace(
        complete=AsyncMock(side_effect=AIRequestError("authentication", status=401))
    )
    with pytest.raises(AIRequestError, match="ai_authentication"):
        await chat_completions_raw_with_fallback_paths(
            secrets                = {"_ai": service, "_route": "chat"},
            messages               = [{"role": "user", "content": "hi"}],
            temperature            = 0.2,
            top_p                  = 0.9,
            max_tokens             = 32,
            timeout_seconds        = 1.0,
            max_retry              = 0,
            retry_interval_seconds = 0.0,
        )

    service.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_person_fact_extract_skips_empty_llm_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from unittest.mock import MagicMock

    from plugins.xiaoqing_chat.llm.gateway import LLMError
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
        data_dir  = tmp_path,
        secrets   = {"api_base": "https://example.com", "api_key": "k", "model": "m"},
        memory_db = memory_db,
        chat_id   = "g1",
        history   = history,
        llm       = LLMCallConfig(
            temperature            = 0.8,
            top_p                  = 0.9,
            max_tokens             = 512,
            timeout_seconds        = 1.0,
            max_retry              = 0,
            retry_interval_seconds = 0.0,
        ),
    )

    memory_db.upsert_text.assert_not_called()


@pytest.mark.asyncio
async def test_person_fact_extract_throttle_survives_capped_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from plugins.xiaoqing_chat.memory.knowledge_extract import maybe_extract_person_facts
    from plugins.xiaoqing_chat.memory.memory import StoredMessage

    complete = AsyncMock(return_value='{"facts":[]}')
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.memory.knowledge_extract.chat_completions",
        complete,
    )

    def history(start: int, stop: int) -> list[StoredMessage]:
        return [
            StoredMessage(
                role="user", name="User", content=f"消息 {index}", ts=float(index), user_id=10001
            )
            for index in range(start, stop)
        ]

    common = {
        "data_dir" : tmp_path,
        "secrets"  : {"api_base": "https://example.com", "api_key": "k", "model": "m"},
        "memory_db": MagicMock(),
        "chat_id"  : "g-capped",
        "llm"      : LLMCallConfig(
            temperature            = 0.8,
            top_p                  = 0.9,
            max_tokens             = 512,
            timeout_seconds        = 1.0,
            max_retry              = 0,
            retry_interval_seconds = 0.0,
        ),
    }

    from plugins.xiaoqing_chat.memory.fact_extraction_checkpoint import load_last_observed_ts

    # 每批最多送入 50 条：水位只推进到实际送入模型的最后一条，积压的消息留给后续轮次。
    await maybe_extract_person_facts(history=history(1, 201), **common)
    assert load_last_observed_ts(tmp_path, "g-capped") == 50.0
    await maybe_extract_person_facts(history=history(2, 202), **common)
    assert load_last_observed_ts(tmp_path, "g-capped") == 100.0
    # 新消息不足 20 条时节流仍然生效。
    await maybe_extract_person_facts(history=history(2, 110), **common)

    assert complete.await_count == 2
