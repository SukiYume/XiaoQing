"""Regression tests for Pendo web settings persistence."""

import asyncio
from pathlib import Path
import shutil
import uuid

from plugins.pendo.commands.settings import handle_settings
from plugins.pendo.config import PendoConfig
from plugins.pendo.services.db import Database
from plugins.pendo.utils.settings_utils import (
    PLUGIN_SETTINGS_HELP_LINES,
    normalize_settings_json,
    parse_custom_settings,
    parse_toggle_value,
    resolve_default_category,
    save_user_setting,
)


ROOT = Path(__file__).resolve().parents[2]


def test_user_settings_round_trip_parses_and_merges_settings_json():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_settings_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-settings"

    try:
        assert db.update_user_settings(owner_id, {
            "timezone": "Asia/Shanghai",
            "settings_json": {
                "reminder_enabled": False,
                "daily_briefing_enabled": True,
            },
        }) is True

        assert db.update_user_settings(owner_id, {
            "settings_json": {
                "privacy_mode": True,
            },
        }) is True

        settings = db.get_user_settings(owner_id)
        assert settings["timezone"] == "Asia/Shanghai"
        assert settings["settings_json"]["reminder_enabled"] is False
        assert settings["settings_json"]["daily_briefing_enabled"] is True
        assert settings["settings_json"]["privacy_mode"] is True
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_settings_api_source_normalizes_time_and_category_inputs():
    src = (ROOT / "plugins" / "pendo" / "web" / "api" / "settings.py").read_text(encoding="utf-8")

    assert "_normalize_time_text" in src
    assert "ZoneInfo(timezone)" in src
    assert "ZoneInfoNotFoundError" in src
    assert "Invalid timezone" in src
    assert 'validate_category(category or "未分类")' in src
    assert "normalize_settings_json" in src


def test_settings_page_source_collects_form_before_rerender_on_save():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "settings.js").read_text(encoding="utf-8")

    assert "const payload = collectFormData();" in src
    assert src.index("const payload = collectFormData();") < src.index("_saving = true;")


def test_settings_page_source_scales_summary_values_for_mid_width_layouts():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "settings.js").read_text(encoding="utf-8")

    assert ".settings-summary-card { padding: 18px; min-width: 0; }" in src
    assert "font-size: clamp(18px, 1.55vw, 24px);" in src
    assert "overflow-wrap: anywhere;" in src
    assert "word-break: break-word;" in src


def test_parse_custom_settings_reads_dict_payload():
    custom = parse_custom_settings({
        "settings_json": {
            "privacy_mode": True,
            "reminder_enabled": False,
        },
    })

    assert custom["privacy_mode"] is True
    assert custom["reminder_enabled"] is False
    assert custom["daily_briefing_enabled"] is True


def test_normalize_settings_json_preserves_unknown_keys_and_legacy_alias():
    custom = normalize_settings_json({
        "daily_report_enabled": False,
        "privacy_mode": False,
        "weekly_report_enabled": True,
    })

    assert custom["daily_briefing_enabled"] is False
    assert custom["privacy_mode"] is False
    assert custom["weekly_report_enabled"] is True
    assert "daily_report_enabled" not in custom


def test_normalize_settings_json_coerces_string_booleans_safely():
    custom = normalize_settings_json({
        "reminder_enabled": "false",
        "daily_briefing_enabled": "off",
        "privacy_mode": "0",
    })

    assert custom["reminder_enabled"] is False
    assert custom["daily_briefing_enabled"] is False
    assert custom["privacy_mode"] is False


def test_resolve_default_category_prefers_user_setting():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_settings_default_category_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-settings-category"

    try:
        assert db.update_user_settings(owner_id, {"default_category": "工作手稿"}) is True
        assert resolve_default_category(db, owner_id) == "工作手稿"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_user_settings_normalizes_legacy_daily_report_enabled_key():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_settings_legacy_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-settings-legacy"

    try:
        assert db.update_user_settings(owner_id, {
            "settings_json": {
                "daily_report_enabled": False,
            },
        }) is True

        settings = db.get_user_settings(owner_id)
        assert settings["settings_json"]["daily_briefing_enabled"] is False
        assert "daily_report_enabled" not in settings["settings_json"]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_save_user_setting_updates_dict_backed_settings_json():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_settings_save_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-settings-save"

    try:
        assert db.update_user_settings(owner_id, {
            "settings_json": {
                "privacy_mode": True,
            },
        }) is True

        save_user_setting(owner_id, "weekly_report_enabled", True, db)

        settings = db.get_user_settings(owner_id)
        assert settings["settings_json"]["privacy_mode"] is True
        assert settings["settings_json"]["weekly_report_enabled"] is True
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_default_settings_enable_reminder_briefing_and_privacy():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_settings_defaults_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-settings-defaults"

    try:
        settings = db.get_user_settings(owner_id)
        assert settings["settings_json"]["reminder_enabled"] is True
        assert settings["settings_json"]["daily_briefing_enabled"] is True
        assert settings["settings_json"]["privacy_mode"] is True
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_web_saved_settings_reflect_in_plugin_settings_output():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_settings_plugin_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-settings-plugin"

    try:
        assert db.update_user_settings(owner_id, {
            "timezone": "Asia/Shanghai",
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "07:00",
            "daily_report_time": "08:00",
            "diary_remind_time": "21:30",
            "settings_json": {
                "reminder_enabled": False,
                "daily_briefing_enabled": False,
                "privacy_mode": False,
            },
        }) is True

        message = asyncio.run(handle_settings(owner_id, "", db))

        assert "🔔 提醒: 关闭" in message
        assert "🗓️ 每日简报: 关闭 (08:00)" in message
        assert "🔒 隐私模式: 关闭" in message
        assert "默认视图" not in message
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_plugin_settings_defaults_match_config_defaults():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_settings_config_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-settings-config"

    try:
        message = asyncio.run(handle_settings(owner_id, "", db))

        assert "🔔 提醒: 开启" in message
        assert "🗓️ 每日简报: 开启 (08:00)" in message
        assert f"🔒 隐私模式: {'开启' if PendoConfig.MESSAGE_PRIVACY_MODE_DEFAULT else '关闭'}" in message
        assert "默认视图" not in message
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_note_and_event_handlers_source_use_resolved_default_category():
    note_src = (ROOT / "plugins" / "pendo" / "handlers" / "note.py").read_text(encoding="utf-8")
    event_src = (ROOT / "plugins" / "pendo" / "handlers" / "event.py").read_text(encoding="utf-8")

    assert "resolve_default_category" in note_src
    assert 'if not parsed["category"]:' in note_src
    assert "resolve_default_category(self.db, user_id)" in event_src


def test_parse_toggle_value_accepts_expected_aliases():
    assert parse_toggle_value("on") == (True, True)
    assert parse_toggle_value("关闭") == (True, False)
    assert parse_toggle_value("") == (False, "请指定 on 或 off")


def test_plugin_settings_help_is_shared_with_main_help():
    main_src = (ROOT / "plugins" / "pendo" / "main.py").read_text(encoding="utf-8")

    assert "*PLUGIN_SETTINGS_HELP_LINES" in main_src
    assert "default_view" not in main_src


def test_event_handler_source_uses_shared_support_module():
    event_src = (ROOT / "plugins" / "pendo" / "handlers" / "event.py").read_text(
        encoding="utf-8"
    )

    assert "from .event_support import" in event_src
    assert "def _ensure_reminders" not in event_src
    assert "def _format_event_created" not in event_src
