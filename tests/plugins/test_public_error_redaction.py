from __future__ import annotations

import ast
import importlib
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest
from fastapi import HTTPException

from plugins.adnmb import main as adnmb
from plugins.ads_paper import main as ads_paper
from plugins.apod import main as apod
from plugins.arxiv_filter import main as arxiv_filter
from plugins.astro_tools import main as astro_tools
from plugins.bot_core import main as bot_core
from plugins.chat import main as chat
from plugins.chime import main as chime
from plugins.choice import main as choice
from plugins.color import main as color
from plugins.dict import main as dictionary
from plugins.earthquake import main as earthquake
from plugins.echo import main as echo
from plugins.github import main as github
from plugins.guess_number import main as guess_number
from plugins.pendo import main as pendo
from plugins.pendo.services.ai_parser import AIParser
from plugins.pendo.services.reminder import ReminderService
from plugins.pendo.utils.error_handlers import handle_command_errors
from plugins.pendo.utils.settings_utils import resolve_default_category, save_user_setting
from plugins.pendo.web import server as pendo_web_server
from plugins.pendo.web.api import transfer as pendo_transfer
from plugins.qingpet import main as qingpet
from plugins.signin import main as signin
from plugins.smalltalk import main as smalltalk
from plugins.twitter import main as twitter
from plugins.url_parser import main as url_parser
from plugins.voice import main as voice
from plugins.wolframalpha import main as wolframalpha
from plugins.xiaoqing_chat import main as xiaoqing_chat

CANARY = "CR219_SECRET_CANARY"
SENSITIVE_ERROR = (
    f"Authorization: Bearer {CANARY} "
    f"https://user:password@example.test/api?token={CANARY} "
    rf"C:\Users\victim\{CANARY}.txt"
)

_PUBLIC_ERROR_PLUGIN_DIRS = (
    "adnmb",
    "ads_paper",
    "apod",
    "arxiv_filter",
    "astro_tools",
    "bot_core",
    "chat",
    "chime",
    "choice",
    "color",
    "dict",
    "earthquake",
    "echo",
    "github",
    "guess_number",
    "pendo",
    "qingpet",
    "signin",
    "smalltalk",
    "twitter",
    "url_parser",
    "voice",
    "wolframalpha",
    "xiaoqing_chat",
)


def test_public_plugin_runtime_never_uses_unredacted_traceback_logging() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    sources = [repo_root / "core" / "dispatcher.py"]
    for plugin_name in _PUBLIC_ERROR_PLUGIN_DIRS:
        sources.extend((repo_root / "plugins" / plugin_name).rglob("*.py"))

    violations: list[str] = []
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "exception":
                violations.append(f"{source.relative_to(repo_root)}:{node.lineno}: .exception()")
            for keyword in node.keywords:
                if keyword.arg != "exc_info":
                    continue
                if not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value in (False, None)
                ):
                    violations.append(
                        f"{source.relative_to(repo_root)}:{node.lineno}: exc_info enabled"
                    )

    assert violations == []


@dataclass
class _PublicContext:
    request_id: str
    plugin_dir: Path
    data_dir: Path
    secrets: dict
    logger: logging.Logger

    def get_settings_snapshot(self):
        return SimpleNamespace(
            plugin_secrets=lambda _plugin_name: {},
            plugin_config=lambda _plugin_name: {},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "patch_name"),
    [
        pytest.param(adnmb, "parse", id="adnmb"),
        pytest.param(apod, "parse", id="apod"),
        pytest.param(arxiv_filter, "parse", id="arxiv"),
        pytest.param(astro_tools, "parse", id="astro"),
        pytest.param(chime, "parse", id="chime"),
        pytest.param(choice, "parse_choice_args", id="choice"),
        pytest.param(color, "parse", id="color"),
        pytest.param(dictionary, "_parse_request", id="dict"),
        pytest.param(earthquake, "_parse_action", id="earthquake"),
        pytest.param(echo, "segments", id="echo"),
        pytest.param(github, "_parse_action", id="github"),
        pytest.param(guess_number, "_parse_request", id="guess"),
        pytest.param(twitter, "parse", id="twitter"),
        pytest.param(chat, "validate_config", id="chat"),
        pytest.param(signin, "parse", id="signin"),
        pytest.param(smalltalk, "parse", id="smalltalk"),
        pytest.param(voice, "segments", id="voice"),
        pytest.param(wolframalpha, "segments", id="wolframalpha"),
    ],
)
async def test_public_plugin_unexpected_errors_never_echo_internal_details(
    module: ModuleType,
    patch_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request_id = "req-cr219-canary"
    context = _PublicContext(
        request_id=request_id,
        plugin_dir=tmp_path,
        data_dir=tmp_path,
        secrets={"plugins": {module.__name__: {"token": CANARY}}},
        logger=logging.getLogger(f"test.{module.__name__}"),
    )
    monkeypatch.setattr(
        module,
        patch_name,
        Mock(side_effect=RuntimeError(SENSITIVE_ERROR)),
    )

    with caplog.at_level(logging.ERROR):
        response = await module.handle("test", "ordinary input", {}, context)

    serialized_response = json.dumps(response, ensure_ascii=False)
    serialized_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "XQ-PLUGIN-UNEXPECTED" in serialized_response
    assert request_id in serialized_response
    assert request_id in serialized_logs
    assert "RuntimeError" in serialized_logs
    for forbidden in (
        CANARY,
        "Authorization",
        "Bearer",
        "user:password",
        "C:\\Users\\victim",
    ):
        assert forbidden not in serialized_response
    for forbidden in (
        CANARY,
        "user:password",
        "C:\\Users\\victim",
    ):
        assert forbidden not in serialized_logs
    assert "<redacted-credential>" in serialized_logs
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_url_parser_unexpected_error_is_redacted_even_without_a_reply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request_id = "req-cr219-url-parser"
    context = SimpleNamespace(
        request_id=request_id,
        secrets={"plugins": {"url_parser": {"token": CANARY}}},
        logger=logging.getLogger("test.cr219.url_parser"),
        data_dir=tmp_path,
        http_session=object(),
    )
    monkeypatch.setattr(
        url_parser,
        "fetch_public_html",
        AsyncMock(side_effect=RuntimeError(SENSITIVE_ERROR)),
    )

    with caplog.at_level(logging.ERROR):
        response = await url_parser.handle_url("https://example.test", {}, context)

    serialized_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert response == []
    assert request_id in serialized_logs
    assert CANARY not in serialized_logs
    assert "user:password" not in serialized_logs
    assert all(record.exc_info is None for record in caplog.records)


def test_arxiv_internal_inference_failure_propagates_to_the_public_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_torch = ModuleType("torch")
    fake_torch.cuda = SimpleNamespace(is_available=lambda: False)
    fake_torch.device = lambda value: value
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    module_name = "plugins.arxiv_filter.inference.runner"
    sys.modules.pop(module_name, None)
    try:
        arxiv_runner = importlib.import_module(module_name)
        monkeypatch.setattr(arxiv_runner, "resolve_params", Mock(return_value=object()))
        monkeypatch.setattr(
            arxiv_runner,
            "_dispatch_inference",
            Mock(side_effect=RuntimeError(CANARY)),
        )

        with pytest.raises(RuntimeError, match=CANARY):
            arxiv_runner.run_inference_for_dataframe(
                pd.DataFrame([{"Title": "paper", "Abstract": "abstract"}]),
            )
    finally:
        sys.modules.pop(module_name, None)


def test_pendo_web_start_error_never_stores_raw_exception_text() -> None:
    detail = pendo_web_server._format_start_error(
        "127.0.0.1",
        8765,
        RuntimeError(f"C:\\private\\{CANARY}.txt"),
    )

    assert "RuntimeError" in detail
    assert CANARY not in detail
    assert "C:\\private" not in detail


@pytest.mark.asyncio
async def test_nested_pendo_error_decorators_emit_one_safe_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = SimpleNamespace(
        request_id="req-cr219-pendo-nested",
        secrets={"token": CANARY},
        logger=logging.getLogger("test.cr219.pendo.nested"),
    )

    @handle_command_errors
    async def inner(*, context):
        raise RuntimeError(f"Bearer {CANARY}")

    @handle_command_errors
    async def outer(*, context):
        return await inner(context=context)

    with caplog.at_level(logging.ERROR):
        result = await outer(context=context)

    assert result["status"] == "error"
    assert "XQ-PLUGIN-UNEXPECTED" in result["message"]
    public_records = [record for record in caplog.records if "public_error" in record.getMessage()]
    assert len(public_records) == 1
    assert CANARY not in public_records[0].getMessage()
    assert public_records[0].exc_info is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plugin_name",
    ["ads_paper", "bot_core", "pendo", "qingpet", "xiaoqing_chat"],
)
async def test_remaining_public_plugins_share_the_same_safe_boundary(
    plugin_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request_id = f"req-cr219-{plugin_name}"
    context = SimpleNamespace(
        request_id=request_id,
        secrets={"plugins": {plugin_name: {"token": CANARY}}},
        logger=logging.getLogger(f"test.cr219.{plugin_name}"),
        data_dir=tmp_path,
        plugin_dir=tmp_path,
        state={},
        http_session=object(),
    )
    error = RuntimeError(SENSITIVE_ERROR)

    if plugin_name == "ads_paper":
        monkeypatch.setattr(ads_paper, "_get_ads_token", Mock(return_value="configured"))
        monkeypatch.setattr(ads_paper.paper_commands, "cmd_search", AsyncMock(side_effect=error))
        invocation = ads_paper.handle("paper", "search", {"user_id": 1}, context)
    elif plugin_name == "bot_core":
        monkeypatch.setattr(bot_core, "_handle_help", Mock(side_effect=error))
        invocation = bot_core.handle("help", "", {"user_id": 1}, context)
    elif plugin_name == "pendo":
        monkeypatch.setattr(pendo, "_has_active_session", AsyncMock(side_effect=error))
        invocation = pendo.handle("pendo", "", {"user_id": 1}, context)
    elif plugin_name == "qingpet":
        monkeypatch.setattr(qingpet, "_db_instance", object())
        monkeypatch.setattr(qingpet.asyncio, "to_thread", AsyncMock(side_effect=error))
        invocation = qingpet.handle("qingpet", "查看", {"user_id": 1}, context)
    else:
        monkeypatch.setattr(xiaoqing_chat, "_resolve_invocation", Mock(side_effect=error))
        invocation = xiaoqing_chat.handle("xc", "hello", {"user_id": 1}, context)

    with caplog.at_level(logging.ERROR):
        response = await invocation

    serialized_response = json.dumps(response, ensure_ascii=False)
    serialized_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "XQ-PLUGIN-UNEXPECTED" in serialized_response
    assert request_id in serialized_response
    assert request_id in serialized_logs
    assert CANARY not in serialized_response
    assert CANARY not in serialized_logs
    assert "user:password" not in serialized_logs
    assert all(record.exc_info is None for record in caplog.records)


def test_pendo_reminder_terminal_failure_propagates_without_raw_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = SimpleNamespace(
        prune_reminder_logs=Mock(return_value=0),
        get_due_reminder_items=Mock(side_effect=RuntimeError(SENSITIVE_ERROR)),
    )

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match=CANARY):
        ReminderService(db).check_and_send_reminders()

    serialized_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert CANARY not in serialized_logs
    assert all(record.exc_info is None for record in caplog.records)


def test_pendo_recoverable_internal_failures_log_only_error_type(
    caplog: pytest.LogCaptureFixture,
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reminder_db = SimpleNamespace(
        get_unconfirmed_sent_reminders=Mock(side_effect=RuntimeError(SENSITIVE_ERROR))
    )
    monkeypatch.setattr(
        db,
        "get_user_settings",
        Mock(side_effect=RuntimeError(SENSITIVE_ERROR)),
    )

    with caplog.at_level(logging.WARNING):
        assert ReminderService(reminder_db)._check_unconfirmed_repeats() == []
        assert resolve_default_category(db, "user-1", "fallback") == "fallback"

    serialized_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in serialized_logs
    assert CANARY not in serialized_logs
    assert "Authorization" not in serialized_logs
    assert all(record.exc_info is None for record in caplog.records)


def test_pendo_setting_write_failure_preserves_exception_without_duplicate_log(
    caplog: pytest.LogCaptureFixture,
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        db,
        "update_user_settings",
        Mock(side_effect=RuntimeError(SENSITIVE_ERROR)),
    )

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match=CANARY):
        save_user_setting("user-1", "reminder_enabled", True, db)

    assert not caplog.records


@pytest.mark.asyncio
async def test_pendo_ai_and_llm_degradation_never_logs_raw_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class AI:
        async def complete(self, *_args, **_kwargs):
            raise RuntimeError(SENSITIVE_ERROR)

    context = SimpleNamespace(
        request_id="req-cr219-pendo-ai",
        secrets={
            "plugins": {
                "pendo": {
                    "api_base": "https://example.test",
                    "api_key": CANARY,
                    "model": "test-model",
                }
            }
        },
        capabilities=SimpleNamespace(ai=AI()),
        logger=logging.getLogger("test.cr219.pendo.ai"),
    )
    parser = AIParser(context=context)

    with caplog.at_level(logging.WARNING):
        assert await parser._call_llm([{"role": "user", "content": "hello"}]) is None

    serialized_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in serialized_logs
    assert CANARY not in serialized_logs
    assert "<redacted-credential>" in serialized_logs
    assert all(record.exc_info is None for record in caplog.records)


def test_pendo_transfer_validation_and_constraint_detection_ignore_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _reject_record(record, *, partial):
        raise ValueError(SENSITIVE_ERROR)

    monkeypatch.setattr(
        pendo_transfer,
        "get_item_normalizer",
        Mock(return_value=_reject_record),
    )
    warning = pendo_transfer._export_record_warning({"_type": "event", "id": "event-1"})
    assert warning == "event/event-1: 记录字段校验失败"
    assert CANARY not in warning

    monkeypatch.setattr(
        pendo_transfer,
        "inspect_bundle_bytes",
        Mock(side_effect=pendo_transfer.BundleValidationError(SENSITIVE_ERROR)),
    )
    with pytest.raises(HTTPException) as caught:
        pendo_transfer._inspect_bundle_data(b"invalid")
    assert caught.value.status_code == 422
    assert caught.value.detail == "导入包格式或内容校验失败"
    assert CANARY not in str(caught.value.detail)

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE sample (id TEXT UNIQUE)")
        connection.execute("INSERT INTO sample VALUES ('same')")
        with pytest.raises(sqlite3.IntegrityError) as sqlite_failure:
            connection.execute("INSERT INTO sample VALUES ('same')")
        try:
            raise RuntimeError("stable import wrapper") from sqlite_failure.value
        except RuntimeError as wrapped:
            assert pendo_transfer._is_unique_constraint_failure(wrapped) is True
    finally:
        connection.close()

    forged = RuntimeError("UNIQUE constraint failed: items.id " + SENSITIVE_ERROR)
    assert pendo_transfer._is_unique_constraint_failure(forged) is False
