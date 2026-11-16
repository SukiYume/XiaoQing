"""模型输出的 JSON 解析和聊天模型网关：取 JSON 对象、提取回复正文、返回所用的模型配置、错误透传。"""

import pytest


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('note {"items": [{"k": 1}], "facts": [{"k": 2}]} done', id="embedded-object"),
        pytest.param(
            '<think>先想一下</think>\n```json\n{suitable: true, reason: "ok",}\n```',
            id="think-prefix",
        ),
    ],
)
def test_json_parsing_helper_rejects_text_that_is_not_a_bare_object(text: str) -> None:
    from plugins.xiaoqing_chat.utils.json_parsing import parse_first_json_object

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
async def test_gateway_returns_response_and_the_profile_that_served_it() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core.ai import AICompletionResult
    from plugins.xiaoqing_chat.llm.gateway import (
        chat_completions_raw_with_profile,
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
    response, used_profile = await chat_completions_raw_with_profile(
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
async def test_gateway_propagates_non_404_errors_without_retrying() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core.ai import AIRequestError
    from plugins.xiaoqing_chat.llm.gateway import (
        chat_completions_raw_with_profile,
    )

    service = SimpleNamespace(
        complete=AsyncMock(side_effect=AIRequestError("authentication", status=401))
    )
    with pytest.raises(AIRequestError, match="ai_authentication"):
        await chat_completions_raw_with_profile(
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
