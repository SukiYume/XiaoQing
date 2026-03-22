"""
设置管理工具
"""
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def parse_custom_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """解析自定义设置JSON"""
    custom = {}
    if settings.get("settings_json"):
        try:
            custom = json.loads(settings["settings_json"])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Failed to parse custom settings: %s", e)
    return custom


def save_user_setting(user_id: str, key: str, value: Any, db):
    """保存用户设置"""
    try:
        settings = db.settings.get_user_settings(user_id)
        custom = parse_custom_settings(settings)
        custom[key] = value
        settings["settings_json"] = json.dumps(custom, ensure_ascii=False)
        db.settings.update_user_settings(user_id, settings)
    except Exception as e:
        logger.exception("Failed to save setting for user %s: %s", user_id, e)
        raise
