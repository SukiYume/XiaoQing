from __future__ import annotations

import importlib
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NoReturn
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest

import tests.helpers.app_test_support as _fixture_support
from plugins.adnmb import adapi as adnmb_api
from plugins.adnmb import main as adnmb
from plugins.ads_paper import main as ads_paper
from plugins.ads_paper import scix_client
from plugins.apod import main as apod
from plugins.arxiv_filter import main as arxiv_filter
from plugins.arxiv_filter.inference import knn_backend
from plugins.arxiv_filter.inference.runner import run_inference_for_dataframe
from plugins.astro_tools import main as astro_tools
from plugins.astro_tools import obj as astro_obj
from plugins.bot_core import main as bot_core
from plugins.chat import main as chat
from plugins.chime import main as chime
from plugins.choice import main as choice
from plugins.color import main as color
from plugins.dict import main as dictionary
from plugins.earthquake import main as earthquake
from plugins.echo import main as echo
from plugins.flickr import main as flickr
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
from plugins.signin import yingshi
from plugins.smalltalk import main as smalltalk
from plugins.twitter import main as twitter
from plugins.url_parser import main as url_parser
from plugins.voice import main as voice
from plugins.wolframalpha import main as wolframalpha
from plugins.xiaoqing_chat import main as xiaoqing_chat
from tests.helpers.app_test_support import LoggedLines
from tests.helpers.assertions import (
    logged_events,
    logged_records,
    public_error_reply,
    text_segments_text,
)

restore_logging = _fixture_support.restore_logging
logged_lines    = _fixture_support.logged_lines

CANARY = "SECRET_CANARY"
# 异常消息里的探针：用户原话、几种凭据写法、URL、Windows 和 POSIX 路径。
SENSITIVE_ERROR = (
    f"用户原话 {CANARY} "
    f"Authorization: Bearer {CANARY} token={CANARY} Cookie: session={CANARY} "
    f'{{"api_key": "{CANARY}"}} '
    f"https://user:password@example.test/api?token={CANARY} "
    rf"C:\Users\user\{CANARY}.txt /home/user/{CANARY}"
)
_FORBIDDEN = (CANARY, "Authorization", "Cookie", "user:password", "C:\\Users\\user", "/home/user")


class _LeakingDatabase:
    """任何访问都抛出带泄漏探针的错误，代替宠物插件初始化后的数据库。"""

    def __getattr__(self, name: str) -> NoReturn:
        raise RuntimeError(SENSITIVE_ERROR)


def _leaking_sync(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError(SENSITIVE_ERROR)


async def _leaking_async(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError(SENSITIVE_ERROR)


class _PublicContext:
    """测试自己造的插件上下文；`failing` 里的属性一被访问就抛出带泄漏探针的错误。"""

    def __init__(
        self,
        *,
        request_id: str,
        plugin_dir: Path,
        data_dir: Path,
        secrets: dict,
        plugin_secrets: dict | None = None,
        failing: tuple[str, ...]    = (),
    ) -> None:
        self.request_id       = request_id
        self._plugin_dir      = plugin_dir
        self._data_dir        = data_dir
        self.secrets          = secrets
        self._plugin_secrets  = plugin_secrets or {}
        self._failing         = failing
        self.http_session     = object()
        self.state: dict      = {}
        self.current_user_id  = 1
        self.current_group_id = None

    def __getattribute__(self, name: str) -> Any:
        if name in object.__getattribute__(self, "_failing"):
            raise RuntimeError(SENSITIVE_ERROR)
        return object.__getattribute__(self, name)

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def plugin_dir(self) -> Path:
        return self._plugin_dir

    def now(self) -> datetime:
        return datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

    def get_settings_snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(
            plugin_secrets = lambda plugin_name: self._plugin_secrets.get(plugin_name, {}),
            plugin_config  = lambda _plugin_name: {},
            config         = {},
            secrets_status = "valid",
        )


class _FailingEvent(dict):
    def get(self, *_args: object) -> NoReturn:
        raise RuntimeError(SENSITIVE_ERROR)


@dataclass(frozen=True)
class _PublicCase:
    """一次公开命令调用，以及让它失败的地方：外部调用、随机源、上下文属性；真实输入到不了
    命令入口最外层的兜底时，换掉参数解析这一步。"""

    module        : ModuleType
    command       : str
    args          : str
    boundary      : tuple[object, str, object] | None = None
    plugin_secrets: dict | None                       = None
    failing       : tuple[str, ...]                   = ()
    failing_event : bool                              = False


_NO_RANDOMNESS = SimpleNamespace(
    choice  = _leaking_sync,
    choices = _leaking_sync,
    sample  = _leaking_sync,
)

_PUBLIC_CASES = [
    pytest.param(
        _PublicCase(adnmb, "adnmb", "-t", (adnmb_api, "aiohttp_request_bounded", _leaking_async)),
        id="adnmb",
    ),
    pytest.param(
        _PublicCase(apod, "apod", "", (apod, "fetch_public_html", _leaking_async)),
        id="apod",
    ),
    pytest.param(
        _PublicCase(
            astro_tools, "astro", "obj M31", (astro_obj, "requests_request_bounded", _leaking_sync)
        ),
        id="astro",
    ),
    pytest.param(
        _PublicCase(chime, "chime", "", (chime, "fetch_chime_repeaters", _leaking_async)),
        id="chime",
    ),
    pytest.param(
        _PublicCase(choice, "choice", "a b c", (choice, "_RNG", _NO_RANDOMNESS)),
        id="choice",
    ),
    pytest.param(
        _PublicCase(color, "color", "G2V", failing=("data_dir", "plugin_dir")),
        id="color",
    ),
    pytest.param(
        _PublicCase(dictionary, "dict", "star", failing=("data_dir", "plugin_dir")),
        id="dict",
    ),
    # 下面几组的失败落到命令入口最外层的兜底。这些插件的里层把外部调用和各子命令的失败都接住了，
    # 真实输入到不了最外层，所以在参数解析（bot_core 是子命令处理函数）这一步注入失败。
    pytest.param(
        _PublicCase(dictionary, "dict", "star", (dictionary, "_parse_request", _leaking_sync)),
        id="dict-command-entry",
    ),
    pytest.param(
        _PublicCase(astro_tools, "astro", "time now", (astro_tools, "parse", _leaking_sync)),
        id="astro-command-entry",
    ),
    pytest.param(
        _PublicCase(signin, "signin", "yingshi", failing=("get_settings_snapshot",)),
        id="signin-command-entry",
    ),
    pytest.param(
        _PublicCase(
            earthquake, "earthquake", "", (earthquake, "requests_request_bounded", _leaking_sync)
        ),
        id="earthquake",
    ),
    pytest.param(
        _PublicCase(earthquake, "earthquake", "", (earthquake, "_parse_action", _leaking_sync)),
        id="earthquake-command-entry",
    ),
    pytest.param(_PublicCase(echo, "hello", "", failing_event=True), id="echo"),
    pytest.param(
        _PublicCase(
            flickr,
            "flickr",
            "",
            plugin_secrets = {"flickr": {"api_key": "test-flickr-api-key-not-a-real-credential"}},
            failing        = ("get_settings_snapshot",),
        ),
        id="flickr",
    ),
    pytest.param(
        _PublicCase(github, "github", "", (github, "fetch_public_html", _leaking_async)),
        id="github",
    ),
    pytest.param(
        _PublicCase(github, "github", "", (github, "_parse_action", _leaking_sync)),
        id="github-command-entry",
    ),
    pytest.param(_PublicCase(guess_number, "guess", "", failing=("get_session",)), id="guess"),
    pytest.param(
        _PublicCase(twitter, "twimg", "", failing=("data_dir", "plugin_dir")),
        id="twitter",
    ),
    pytest.param(
        _PublicCase(
            chat,
            "chat",
            "hello",
            plugin_secrets = {"chat": {"token": "token", "bot_id": "bot"}},
            failing        = ("now",),
        ),
        id="chat",
    ),
    pytest.param(
        _PublicCase(
            signin,
            "signin",
            "yingshi",
            (yingshi, "aiohttp_request_bounded", _leaking_async),
            plugin_secrets={
                "signin": {
                    "yingshijufeng": {
                        "app_id"      : "app",
                        "kdt_id"      : "1",
                        "access_token": "token",
                        "sid"         : "sid",
                    }
                }
            },
        ),
        id="signin",
    ),
    pytest.param(
        _PublicCase(smalltalk, "qa", "question answer", failing=("data_dir", "plugin_dir")),
        id="smalltalk",
    ),
    pytest.param(
        _PublicCase(voice, "tts", "hello", failing=("get_settings_snapshot",)),
        id="voice",
    ),
    pytest.param(
        _PublicCase(
            wolframalpha,
            "alpha",
            "1+1",
            (wolframalpha, "aiohttp_request_bounded", _leaking_async),
            plugin_secrets={"wolframalpha": {"appid": "ABCDEF-1234567890"}},
        ),
        id="wolframalpha",
    ),
    pytest.param(
        _PublicCase(
            ads_paper,
            "paper",
            "search frb",
            (scix_client, "aiohttp_request_bounded", _leaking_async),
            plugin_secrets={"ads_paper": {"ads_token": "configured"}},
        ),
        id="ads_paper",
    ),
    pytest.param(
        _PublicCase(bot_core, "help", "", failing=("get_command_catalog",)),
        id="bot_core",
    ),
    pytest.param(
        _PublicCase(bot_core, "plugins", "", (bot_core, "_handle_plugins", _leaking_sync)),
        id="bot_core-command-entry",
    ),
    pytest.param(_PublicCase(pendo, "pendo", "", failing=("session_manager",)), id="pendo"),
    pytest.param(
        _PublicCase(qingpet, "qingpet", "查看", (qingpet, "_db_instance", _LeakingDatabase())),
        id="qingpet",
    ),
    pytest.param(
        _PublicCase(xiaoqing_chat, "xc", "hello", failing=("get_command_catalog",)),
        id="xiaoqing_chat",
    ),
]


def _reply_text(response: object) -> str:
    if isinstance(response, str):
        return response
    assert isinstance(response, list)
    return text_segments_text(response)


def _assert_public_error_is_redacted(
    response: object,
    logged_lines: LoggedLines,
    request_id: str,
) -> None:
    """回复是一条公开错误：类型、插件里的出错位置和这一轮的 request id；回复和日志文件里
    都没有异常消息里的任何探针。"""

    serialized_response = json.dumps(response, ensure_ascii=False)
    log_text = "\n".join(logged_lines.lines())
    for forbidden in _FORBIDDEN:
        assert forbidden not in serialized_response
        assert forbidden not in log_text
    reply = public_error_reply(_reply_text(response))
    assert reply["type_name"] == "RuntimeError"
    assert reply["location"].startswith(("plugins/", "core/"))
    assert reply["request_id"] == request_id
    # 同一轮里尽力而为的步骤也可能各记一条；每条都带这一轮的 request id。
    request_ids = logged_lines.request_ids_of("public_error")
    assert request_ids and set(request_ids) == {request_id}
    assert f"category=internal error=RuntimeError error.at={reply['location']}" in log_text


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _PUBLIC_CASES)
async def test_public_plugin_unexpected_errors_never_echo_internal_details(
    case: _PublicCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    logged_lines: LoggedLines,
) -> None:
    request_id = "req-canary"
    context    = _PublicContext(
        request_id     = request_id,
        plugin_dir     = tmp_path,
        data_dir       = tmp_path,
        secrets        = {"plugins": {case.module.__name__: {"token": CANARY}}},
        plugin_secrets = case.plugin_secrets,
        failing        = case.failing,
    )
    if case.boundary is not None:
        monkeypatch.setattr(*case.boundary)
    event = _FailingEvent({"user_id": 1}) if case.failing_event else {"user_id": 1}

    response = await case.module.handle(case.command, case.args, event, context)

    _assert_public_error_is_redacted(response, logged_lines, request_id)


@pytest.mark.asyncio
async def test_arxiv_public_unexpected_error_never_echoes_internal_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    logged_lines: LoggedLines,
) -> None:
    module_name = "plugins.arxiv_filter.arxiv_today"
    monkeypatch.setitem(sys.modules, "feedparser", ModuleType("feedparser"))
    sys.modules.pop(module_name, None)
    context = _PublicContext(
        request_id = "req-canary",
        plugin_dir = tmp_path,
        data_dir   = tmp_path,
        secrets    = {"plugins": {"arxiv_filter": {"token": CANARY}}},
    )
    try:
        arxiv_today = importlib.import_module(module_name)
        monkeypatch.setattr(arxiv_today, "requests_request_bounded", _leaking_sync)

        response = await arxiv_filter.handle("arxiv", "", {"user_id": 1}, context)
    finally:
        sys.modules.pop(module_name, None)

    _assert_public_error_is_redacted(response, logged_lines, "req-canary")


@pytest.mark.asyncio
async def test_url_parser_unexpected_error_is_redacted_even_without_a_reply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    logged_lines: LoggedLines,
) -> None:
    request_id = "req-url-parser"
    context    = SimpleNamespace(
        request_id   = request_id,
        secrets      = {"plugins": {"url_parser": {"token": CANARY}}},
        data_dir     = tmp_path,
        http_session = object(),
    )
    monkeypatch.setattr(
        url_parser,
        "fetch_public_html",
        AsyncMock(side_effect=RuntimeError(SENSITIVE_ERROR)),
    )

    response = await url_parser.handle_url("https://example.test", {}, context)

    log_text = "\n".join(logged_lines.lines())
    assert response == []
    assert logged_lines.request_id_of("public_error") == request_id
    for forbidden in _FORBIDDEN:
        assert forbidden not in log_text


def test_arxiv_internal_inference_failure_propagates_to_the_public_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "training_config.json").write_text(
        json.dumps({"model_type": "knn"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(knn_backend, "run_knn_inference", _leaking_sync_canary)

    with pytest.raises(RuntimeError, match=CANARY):
        run_inference_for_dataframe(
            pd.DataFrame([{"Title": "paper", "Abstract": "abstract"}]),
            model_path=str(tmp_path),
        )


def _leaking_sync_canary(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError(CANARY)


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
        request_id = "req-pendo-nested",
        secrets    = {"token": CANARY},
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
    assert public_error_reply(result["message"])["request_id"] == "req-pendo-nested"
    assert CANARY not in result["message"]
    [public_record] = logged_records(caplog, "public_error")
    assert public_record.msg.fields["component"] == "pendo.outer"
    assert CANARY not in public_record.getMessage()


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
) -> None:
    reminder_db = SimpleNamespace(
        get_unconfirmed_sent_reminders=Mock(side_effect=RuntimeError(SENSITIVE_ERROR))
    )
    settings_db = SimpleNamespace(get_user_settings=Mock(side_effect=RuntimeError(SENSITIVE_ERROR)))

    with caplog.at_level(logging.WARNING):
        assert ReminderService(reminder_db)._check_unconfirmed_repeats() == []
        assert resolve_default_category(settings_db, "user-1", "fallback") == "fallback"

    serialized_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in serialized_logs
    assert CANARY not in serialized_logs
    assert "Authorization" not in serialized_logs
    assert all(record.exc_info is None for record in caplog.records)


def test_pendo_setting_write_failure_preserves_exception_without_duplicate_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = SimpleNamespace(update_user_settings=Mock(side_effect=RuntimeError(SENSITIVE_ERROR)))

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
        request_id = "req-pendo-ai",
        secrets    = {
            "plugins": {
                "pendo": {
                    "api_base": "https://example.test",
                    "api_key" : CANARY,
                    "model"   : "test-model",
                }
            }
        },
        capabilities=SimpleNamespace(ai=AI()),
    )
    parser = AIParser(context=context)

    with caplog.at_level(logging.WARNING):
        assert await parser._call_llm([{"role": "user", "content": "hello"}]) is None

    serialized_logs = "\n".join(record.getMessage() for record in caplog.records)
    [degraded] = logged_events(caplog, "public_error")
    assert degraded.fields["component"] == "pendo.ai_parser.llm"
    assert degraded.fields["error"] == "RuntimeError"
    assert CANARY not in serialized_logs


def test_unique_constraint_detection_uses_error_code_not_message() -> None:
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
