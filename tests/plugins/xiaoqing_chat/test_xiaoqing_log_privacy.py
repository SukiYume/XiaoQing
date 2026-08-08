from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.xiaoqing_chat.llm.prompt_builder import ChatMessage
from plugins.xiaoqing_chat.logging_utils import _log_step, sanitize_log_fields
from plugins.xiaoqing_chat.reply_generator import _log_prompt_audit_metadata
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
CANARY = "CR220_XIAOQING_PRIVATE_PROMPT_CANARY"


@pytest.mark.parametrize(
    "field_name",
    [
        "system_prompt",
        "user_prompt",
        "history",
        "dialogue",
        "model_response",
        "raw_output",
        "current_text",
        "message",
        "goal",
        "reason",
        "hint",
        "description",
        "marker",
        "term",
        "emotion_tags",
        "chat_id",
        "user_id",
        "group_id",
        "operator_user_id",
    ],
)
def test_sensitive_chat_log_fields_are_fingerprinted_not_truncated(field_name: str) -> None:
    sanitized = sanitize_log_fields({field_name: CANARY})
    serialized = json.dumps(sanitized, ensure_ascii=False)

    assert CANARY not in serialized
    assert "fingerprint=hmac-sha256:" in serialized
    assert "length=" in serialized


def test_sensitive_nested_chat_log_fields_are_fingerprinted() -> None:
    sanitized = sanitize_log_fields(
        {
            "status": "ok",
            "payload": {
                "role": "user",
                "content": CANARY,
            },
        }
    )
    serialized = json.dumps(sanitized, ensure_ascii=False)

    assert sanitized["status"] == "ok"
    assert sanitized["payload"]["role"] == "user"
    assert CANARY not in serialized
    assert "fingerprint=hmac-sha256:" in serialized


def test_model_reason_that_looks_like_a_status_token_is_still_fingerprinted() -> None:
    sanitized = sanitize_log_fields({"reason": "private", "reason_code": "parse_failed"})

    assert CANARY not in json.dumps(sanitized, ensure_ascii=False)
    assert sanitized["reason"] != "private"
    assert "fingerprint=hmac-sha256:" in sanitized["reason"]
    assert sanitized["reason_code"] == "parse_failed"


def test_log_step_keeps_stage_and_correlation_but_never_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = SimpleNamespace(logger=logging.getLogger("test.cr220.xiaoqing.step"))
    runtime = SimpleNamespace(cfg=SimpleNamespace(debug=SimpleNamespace(log_steps=True)))

    with caplog.at_level(logging.INFO):
        _log_step(
            context,
            runtime,
            chat_id="private-chat-id",
            step="reply.debug",
            fields={
                "status": "prepared",
                "request_id": "req-cr220",
                "task_id": "task-cr220",
                "role": "system",
                "prompt": CANARY,
                "history": CANARY,
                "response": CANARY,
            },
        )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "reply.debug" in logged
    assert "prepared" in logged
    assert "req-cr220" in logged
    assert "task-cr220" in logged
    assert "system" in logged
    assert CANARY not in logged
    assert logged.count("fingerprint=hmac-sha256:") >= 4


def test_prompt_debug_metadata_preserves_roles_and_correlation_without_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = SimpleNamespace(logger=logging.getLogger("test.cr220.xiaoqing.prompt"))
    messages = [
        ChatMessage(role="system", content=f"system {CANARY}"),
        ChatMessage(role="user", content=f"user {CANARY}"),
    ]

    with caplog.at_level(logging.INFO):
        _log_prompt_audit_metadata(context, messages, request_id="req-cr220-prompt")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert CANARY not in logged
    assert "operation=reply_prompt" in logged
    assert "status=prepared" in logged
    assert "request_id=req-cr220-prompt" in logged
    assert "role=system" in logged
    assert "role=user" in logged
    assert logged.count("fingerprint=hmac-sha256:") == 2


def test_prompt_debug_rejects_malicious_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    malicious_request_id = f"req\nAuthorization: Bearer {CANARY}"
    context = SimpleNamespace(logger=logging.getLogger("test.cr220.xiaoqing.bad-request"))

    with caplog.at_level(logging.INFO):
        _log_prompt_audit_metadata(
            context,
            [ChatMessage(role="user", content="safe prompt")],
            request_id=malicious_request_id,
        )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "request_id=-" in logged
    assert malicious_request_id not in logged
    assert CANARY not in logged


def test_log_step_rejects_malicious_correlation_ids() -> None:
    fields = sanitize_log_fields(
        {
            "request_id": f"req\n{CANARY}",
            "task_id": "task-ok_1",
            "job_id": "x" * 65,
        }
    )

    assert fields == {"request_id": "-", "task_id": "task-ok_1", "job_id": "-"}


@pytest.mark.asyncio
async def test_reset_audit_fingerprints_all_actor_identifiers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from plugins.xiaoqing_chat.handlers_internal import handle_internal_impl

    chat_id = f"chat-{CANARY}"
    event = {
        "message_type": "group",
        "group_id": f"group-{CANARY}",
        "user_id": f"operator-{CANARY}",
    }
    state = SimpleNamespace(
        pop_persist_task=lambda _chat_id: None,
        inc_stats=lambda *_args: None,
    )
    hctx = SimpleNamespace(
        chat_id=chat_id,
        runtime=SimpleNamespace(),
        state=state,
        data_dir=Path("test-data"),
    )
    context = SimpleNamespace(logger=logging.getLogger("test.cr220.xiaoqing.reset-audit"))

    async def reset_chat_session(_state, _chat_id: str, _data_dir: Path) -> None:
        return None

    with caplog.at_level(logging.INFO):
        result = await handle_internal_impl(
            "重置",
            "确认",
            event,
            context,
            handler_context_from_event=lambda _event, _context: hctx,
            get_lock=lambda _chat_id: asyncio.Lock(),
            reset_chat_session=reset_chat_session,
            cancel_pending_task=lambda _task: None,
            is_admin_operator_fn=lambda _event, _context: True,
        )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    assert CANARY not in logged
    assert "scope=group" in logged
    assert logged.count("fingerprint=hmac-sha256:") == 3


@pytest.mark.parametrize(
    "relative_path",
    [
        "plugins/xiaoqing_chat/expression/bw_message_recorder.py",
        "plugins/xiaoqing_chat/handlers.py",
        "plugins/xiaoqing_chat/handlers_internal.py",
        "plugins/xiaoqing_chat/smalltalk_execution.py",
    ],
)
def test_identifier_log_paths_have_no_unredacted_direct_logger_arguments(
    relative_path: str,
) -> None:
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations: list[str] = []
    identifier_fragments = ("chat_id", "group_id", "user_id", "operator")

    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        callable_name = ast.unparse(call.func).lower()
        if "logger." not in callable_name:
            continue
        value_expressions = [*call.args[1:], *(item.value for item in call.keywords)]
        for expression in value_expressions:
            source = ast.unparse(expression)
            if any(
                fragment in source for fragment in identifier_fragments
            ) and not source.startswith("_redacted_value("):
                violations.append(f"{path.name}:{call.lineno}: {source}")

    assert violations == []


def test_reply_prompt_debug_never_passes_prompt_content_to_ordinary_logger() -> None:
    path = ROOT / "plugins" / "xiaoqing_chat" / "reply_generator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations: list[str] = []

    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        callable_name = ast.unparse(call.func)
        if "logger." not in callable_name.lower():
            continue
        source = ast.unparse(call)
        if any(
            fragment in source
            for fragment in (
                ".content",
                "system_prompt",
                "user_prompt",
                "payload_msgs",
                "trimmed_history",
                "raw_output",
            )
        ):
            violations.append(f"{path.name}:{call.lineno}: {source}")

    assert violations == []
