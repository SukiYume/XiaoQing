"""Regression tests for Pendo web settings persistence."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.pendo.commands.settings import handle_settings
from plugins.pendo.services.db import Database
from plugins.pendo.utils.settings_utils import (
    normalize_settings_json,
    parse_custom_settings,
    parse_toggle_value,
    resolve_default_category,
    save_user_setting,
)
from plugins.pendo.web.api import settings as settings_api
from tests.helpers.assertions import assert_http_error as _assert_http_error


def test_settings_router_registers_only_get_and_put() -> None:
    """设置模块只应暴露同一路径的读取与更新入口。"""

    registered = {
        (route.path, frozenset(getattr(route, "methods", set())))
        for route in settings_api.router.routes
    }
    assert registered == {
        ("/settings", frozenset({"GET"})),
        ("/settings", frozenset({"PUT"})),
    }


def test_settings_payload_normalizes_all_public_fields_without_mutating_input() -> None:
    """时区、时间、分类和扩展设置应统一规范化，原补丁保持不变。"""

    raw: dict[str, Any] = {
        "timezone": " Asia/Shanghai ",
        "quiet_hours_start": " 9:05 ",
        "quiet_hours_end": "23:00",
        "daily_report_time": "8:30",
        "diary_remind_time": "21:07",
        "default_category": " 工作手稿 ",
        "settings_json": {
            "daily_report_enabled": "off",
            "extension_flag": "保留",
        },
    }

    normalized = settings_api._normalize_settings_payload(raw)

    assert normalized == {
        "timezone": "Asia/Shanghai",
        "quiet_hours_start": "09:05",
        "quiet_hours_end": "23:00",
        "daily_report_time": "08:30",
        "diary_remind_time": "21:07",
        "default_category": "工作手稿",
        "settings_json": {
            "daily_briefing_enabled": False,
            "extension_flag": "保留",
        },
    }
    assert raw["timezone"] == " Asia/Shanghai "


@pytest.mark.parametrize(
    "payload",
    (
        {"timezone": ""},
        {"timezone": "../../etc/passwd"},
        {"daily_report_time": "25:00"},
        {"default_category": "非法!"},
        {"settings_json": []},
    ),
)
def test_invalid_settings_payloads_are_rejected(payload: dict[str, Any]) -> None:
    """路径式时区、非法时间/分类和非对象扩展设置必须失败关闭。"""

    with pytest.raises(ValueError):
        settings_api._normalize_settings_payload(payload)


def test_settings_http_rejects_unknown_fields_and_returns_defaults(db: Database) -> None:
    """真实 HTTP 层应拒绝未知键，并按认证所有者返回默认设置。"""

    owner_id = "owner-settings-http"
    app = FastAPI()
    app.include_router(settings_api.router)
    app.dependency_overrides[settings_api.get_current_user] = lambda: owner_id
    app.dependency_overrides[settings_api.get_db] = lambda: db

    with TestClient(app) as client:
        defaults = client.get("/settings")
        invalid = client.put("/settings", json={"unknown_setting": True})

    assert defaults.status_code == 200
    assert defaults.json()["data"]["user_id"] == owner_id
    assert invalid.status_code == 422


def test_settings_update_rejects_empty_and_skips_normalized_noop(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空补丁返回 422，相同值不能写库或增加设置版本。"""

    owner_id = "owner-settings-noop"
    assert db.update_user_settings(
        owner_id,
        {
            "timezone": "Asia/Shanghai",
            "settings_json": {"reminder_enabled": True},
        },
    )
    version_before = (
        db.get_connection()
        .execute(
            "SELECT version FROM user_settings WHERE user_id = ?",
            (owner_id,),
        )
        .fetchone()[0]
    )
    calls = 0

    def unexpected_update(*_args: object, **_kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(db, "update_user_settings", unexpected_update)

    _assert_http_error(
        422,
        lambda: settings_api.update_settings(
            settings_api.SettingsUpdate(),
            owner_id=owner_id,
            db=db,
        ),
    )
    response = settings_api.update_settings(
        settings_api.SettingsUpdate(
            timezone=" Asia/Shanghai ",
            settings_json={"reminder_enabled": True},
        ),
        owner_id=owner_id,
        db=db,
    )

    assert response["message"] == "无变化"
    assert calls == 0
    version_after = (
        db.get_connection()
        .execute(
            "SELECT version FROM user_settings WHERE user_id = ?",
            (owner_id,),
        )
        .fetchone()[0]
    )
    assert version_after == version_before


def test_settings_update_persists_normalized_patch_and_maps_storage_failure(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实变化应持久化；存储层拒绝时路由统一返回 500。"""

    owner_id = "owner-settings-update"
    response = settings_api.update_settings(
        settings_api.SettingsUpdate(
            timezone=" Asia/Shanghai ",
            daily_report_time="8:15",
            settings_json={"weekly_report_enabled": True},
        ),
        owner_id=owner_id,
        db=db,
    )
    data = cast(dict[str, Any], response["data"])
    assert response["message"] == "设置已更新"
    assert data["timezone"] == "Asia/Shanghai"
    assert data["daily_report_time"] == "08:15"
    assert cast(dict[str, Any], data["settings_json"])["weekly_report_enabled"] is True

    json_only = settings_api.update_settings(
        settings_api.SettingsUpdate(settings_json={"extension_only": "新值"}),
        owner_id=owner_id,
        db=db,
    )
    json_data = cast(dict[str, Any], json_only["data"])
    assert cast(dict[str, Any], json_data["settings_json"])["extension_only"] == "新值"

    invalid = _assert_http_error(
        422,
        lambda: settings_api.update_settings(
            settings_api.SettingsUpdate(daily_report_time="25:00"),
            owner_id=owner_id,
            db=db,
        ),
    )
    assert invalid.detail == "Invalid daily_report_time, expected HH:MM"

    monkeypatch.setattr(db, "update_user_settings", lambda *_args, **_kwargs: False)
    error = _assert_http_error(
        500,
        lambda: settings_api.update_settings(
            settings_api.SettingsUpdate(timezone="UTC"),
            owner_id=owner_id,
            db=db,
        ),
    )
    assert error.detail == "Failed to update settings"


def test_user_settings_round_trip_parses_and_merges_settings_json(db: Database) -> None:
    """连续 JSON 补丁应合并而非覆盖其他已保存开关。"""

    owner_id = "u-settings"
    assert db.update_user_settings(
        owner_id,
        {
            "timezone": "Asia/Shanghai",
            "settings_json": {
                "reminder_enabled": False,
                "daily_briefing_enabled": True,
            },
        },
    )
    assert db.update_user_settings(
        owner_id,
        {"settings_json": {"extension_enabled": True}},
    )

    settings = db.get_user_settings(owner_id)
    assert settings["timezone"] == "Asia/Shanghai"
    assert settings["settings_json"]["reminder_enabled"] is False
    assert settings["settings_json"]["daily_briefing_enabled"] is True
    assert settings["settings_json"]["extension_enabled"] is True


def test_settings_payload_rejects_path_like_timezone_without_internal_tzpath() -> None:
    """非法时区错误不得泄漏系统 TZPATH 或原始路径。"""
    with pytest.raises(ValueError, match="^Invalid timezone$") as exc_info:
        settings_api._normalize_settings_payload({"timezone": "../../etc/passwd"})

    assert "TZPATH" not in str(exc_info.value)
    assert "../../etc/passwd" not in str(exc_info.value)


def test_parse_custom_settings_reads_dict_payload() -> None:
    """字典形式扩展设置应补齐默认键并保留显式开关。"""
    custom = parse_custom_settings(
        {
            "settings_json": {
                "reminder_enabled": False,
                "ai_sensitive_data_consent": True,
            },
        }
    )

    assert custom["reminder_enabled"] is False
    assert custom["daily_briefing_enabled"] is True
    assert custom["ai_sensitive_data_consent"] is True


def test_normalize_settings_json_preserves_unknown_keys_and_legacy_alias() -> None:
    """扩展键应保留，旧日报键应迁移到当前简报键。"""
    custom = normalize_settings_json(
        {
            "daily_report_enabled": False,
            "privacy_mode": False,
            "weekly_report_enabled": True,
        }
    )

    assert custom["daily_briefing_enabled"] is False
    assert "privacy_mode" not in custom
    assert custom["weekly_report_enabled"] is True
    assert "daily_report_enabled" not in custom


def test_normalize_settings_json_coerces_string_booleans_safely() -> None:
    """字符串形式的关闭值不能被 Python 真值规则误判为开启。"""
    custom = normalize_settings_json(
        {
            "reminder_enabled": "false",
            "daily_briefing_enabled": "off",
            "ai_sensitive_data_consent": "0",
        }
    )

    assert custom["reminder_enabled"] is False
    assert custom["daily_briefing_enabled"] is False
    assert custom["ai_sensitive_data_consent"] is False


def test_resolve_default_category_prefers_user_setting(db: Database) -> None:
    """默认分类解析应优先使用用户持久化值。"""

    owner_id = "u-settings-category"

    assert db.update_user_settings(owner_id, {"default_category": "工作手稿"}) is True
    assert resolve_default_category(db, owner_id) == "工作手稿"


def test_user_settings_normalizes_legacy_daily_report_enabled_key(db: Database) -> None:
    """旧每日报告开关应迁移到当前每日简报键。"""

    owner_id = "u-settings-legacy"

    assert db.update_user_settings(
        owner_id,
        {"settings_json": {"daily_report_enabled": False}},
    )

    settings = db.get_user_settings(owner_id)
    assert settings["settings_json"]["daily_briefing_enabled"] is False
    assert "daily_report_enabled" not in settings["settings_json"]


def test_save_user_setting_updates_dict_backed_settings_json(db: Database) -> None:
    """单键聊天设置更新应保留现有 JSON 扩展值。"""

    owner_id = "u-settings-save"

    assert db.update_user_settings(
        owner_id,
        {"settings_json": {"extension_enabled": True}},
    )
    save_user_setting(owner_id, "weekly_report_enabled", True, db)

    settings = db.get_user_settings(owner_id)
    assert settings["settings_json"]["extension_enabled"] is True
    assert settings["settings_json"]["weekly_report_enabled"] is True


def test_concurrent_single_setting_patches_preserve_both_keys(db: Database) -> None:
    """并发聊天设置写入只能合并各自键，不能回写陈旧全量快照。"""

    owner_id = "u-settings-concurrent"
    barrier = threading.Barrier(2)

    def save(key: str) -> None:
        barrier.wait(timeout=5)
        save_user_setting(owner_id, key, True, db)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(save, ("extension_a", "extension_b")))

    custom = db.get_user_settings(owner_id)["settings_json"]
    assert custom["extension_a"] is True
    assert custom["extension_b"] is True


def test_default_settings_enable_reminder_and_briefing(db: Database) -> None:
    """未持久化用户应获得当前有效开关的默认值。"""

    owner_id = "u-settings-defaults"

    settings = db.get_user_settings(owner_id)
    assert settings["settings_json"]["reminder_enabled"] is True
    assert settings["settings_json"]["daily_briefing_enabled"] is True
    assert settings["settings_json"]["ai_sensitive_data_consent"] is False
    assert "privacy_mode" not in settings["settings_json"]


def test_web_saved_settings_reflect_in_plugin_settings_output(db: Database) -> None:
    """Web 保存的设置应原样反映到聊天端设置说明。"""

    owner_id = "u-settings-plugin"

    assert db.update_user_settings(
        owner_id,
        {
            "timezone": "Asia/Shanghai",
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "07:00",
            "daily_report_time": "08:00",
            "diary_remind_time": "21:30",
            "settings_json": {
                "reminder_enabled": False,
                "daily_briefing_enabled": False,
                "ai_sensitive_data_consent": True,
            },
        },
    )
    message = asyncio.run(handle_settings(owner_id, "", db))

    assert "🔔 提醒: 关闭" in message
    assert "🗓️ 每日简报: 关闭 (08:00)" in message
    assert "🤖 日记 AI 数据共享: 允许" in message
    assert "默认视图" not in message


def test_plugin_settings_defaults_match_config_defaults(db: Database) -> None:
    """聊天端默认说明应与配置常量保持一致。"""

    owner_id = "u-settings-config"

    message = asyncio.run(handle_settings(owner_id, "", db))

    assert "🔔 提醒: 开启" in message
    assert "🗓️ 每日简报: 开启 (08:00)" in message
    assert "🤖 日记 AI 数据共享: 禁止" in message
    assert "默认视图" not in message


def test_plugin_settings_single_value_commands_persist_by_storage_layer(db: Database) -> None:
    """聊天端单值设置应按约定分别写入顶层列或扩展设置。"""

    owner_id = "u-settings-commands"
    commands = (
        ("reminder off", "🔔 提醒通知: 关闭"),
        ("timezone Asia/Shanghai", "🌍 时区: Asia/Shanghai"),
        ("daily_report 09:15", "🗓️ 每日简报时间: 09:15"),
        ("daily_briefing off", "🗓️ 每日简报: 关闭"),
        ("diary_remind 22:10", "📝 日记提醒时间: 22:10"),
        ("ai_consent on", "✅ 已同意"),
    )

    for command, expected_text in commands:
        message = asyncio.run(handle_settings(owner_id, command, db))
        assert expected_text in message

    settings = db.get_user_settings(owner_id)
    custom = cast(dict[str, Any], settings["settings_json"])
    assert settings["timezone"] == "Asia/Shanghai"
    assert settings["daily_report_time"] == "09:15"
    assert settings["diary_remind_time"] == "22:10"
    assert custom["reminder_enabled"] is False
    assert custom["daily_briefing_enabled"] is False
    assert custom["ai_sensitive_data_consent"] is True


def test_plugin_settings_ai_privacy_alias_updates_same_consent_key(db: Database) -> None:
    """旧 AI 隐私命令别名应复用同一设置键，避免产生重复配置。"""

    owner_id = "u-settings-ai-alias"
    message = asyncio.run(handle_settings(owner_id, "ai_privacy off", db))

    custom = cast(dict[str, Any], db.get_user_settings(owner_id)["settings_json"])
    assert "🔒 已关闭" in message
    assert custom["ai_sensitive_data_consent"] is False
    assert "ai_privacy" not in custom


def test_plugin_settings_quiet_hours_are_written_atomically(db: Database) -> None:
    """合法静默时段应一次写入起止时间，并允许两侧空白。"""

    owner_id = "u-settings-quiet-hours"
    message = asyncio.run(handle_settings(owner_id, "quiet_hours 23:00 - 07:00", db))

    settings = db.get_user_settings(owner_id)
    assert "🔕 静默时段: 23:00 - 07:00" in message
    assert settings["quiet_hours_start"] == "23:00"
    assert settings["quiet_hours_end"] == "07:00"


@pytest.mark.parametrize(
    ("command", "expected_text"),
    (
        ("timezone ../../etc/passwd", "❌ 无效的时区"),
        ("daily_report 24:00", "❌ 无效的时间格式"),
        ("diary_remind 9:00", "❌ 无效的时间格式"),
        ("reminder maybe", "请指定 on 或 off"),
        ("quiet_hours 23:00-07:00-extra", "请指定静默时段"),
    ),
)
def test_plugin_settings_invalid_values_do_not_change_defaults(
    db: Database,
    command: str,
    expected_text: str,
) -> None:
    """非法值必须失败关闭，不能把半成品配置写入数据库。"""

    owner_id = f"u-settings-invalid-{command.split()[0]}"
    before = db.get_user_settings(owner_id)

    message = asyncio.run(handle_settings(owner_id, command, db))

    assert expected_text in message
    assert db.get_user_settings(owner_id) == before


def test_plugin_settings_whitespace_only_args_show_current_settings(db: Database) -> None:
    """纯空白参数应等同于查看设置，不能被当成未知设置项。"""

    owner_id = "u-settings-whitespace"

    empty_message = asyncio.run(handle_settings(owner_id, "", db))
    whitespace_message = asyncio.run(handle_settings(owner_id, "  \t  ", db))

    assert whitespace_message == empty_message


def test_note_and_event_handlers_apply_persisted_default_category(db: Database) -> None:
    """聊天端创建笔记和日程时都应使用用户持久化的默认分类。"""
    from plugins.pendo.handlers.event import EventHandler
    from plugins.pendo.handlers.note import NoteHandler

    owner_id = "u-handler-default-category"
    assert db.update_user_settings(owner_id, {"default_category": "工作手稿"})

    note_result = asyncio.run(
        NoteHandler(db).create_note(owner_id, "默认分类笔记", cast(Any, None))
    )

    class _AIParser:
        @staticmethod
        def build_remind_times_from_offsets(*_args: object, **_kwargs: object) -> list[str]:
            return []

    class _ReminderService:
        @staticmethod
        def detect_conflict(*_args: object, **_kwargs: object) -> list[object]:
            return []

    event_result = asyncio.run(
        EventHandler(db, cast(Any, _AIParser()), cast(Any, _ReminderService())).create_event(
            owner_id,
            {"title": "默认分类日程", "start_time": "2030-01-02T09:00:00"},
            cast(Any, None),
        )
    )

    assert note_result["status"] == "success"
    assert event_result["status"] == "success"
    note = db.get_item(str(note_result["item_id"]), owner_id)
    event = db.get_item(str(event_result["item_id"]), owner_id)
    assert note is not None and note.category == "工作手稿"
    assert event is not None and event.category == "工作手稿"


def test_parse_toggle_value_accepts_expected_aliases() -> None:
    """聊天开关解析应接受约定的中英文别名。"""
    assert parse_toggle_value("on") == (True, True)
    assert parse_toggle_value("关闭") == (True, False)
    assert parse_toggle_value("") == (False, "请指定 on 或 off")
