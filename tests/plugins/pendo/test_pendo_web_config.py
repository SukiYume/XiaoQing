"""Pendo Web 只读配置路由的结构、顺序和响应隔离回归。"""

from __future__ import annotations

from typing import cast

from plugins.pendo.config import (
    DIARY_MOODS,
    DIARY_TEMPLATES,
    LEDGER_EXPENSE_CATEGORIES,
    LEDGER_INCOME_CATEGORIES,
    MOOD_ANALYSIS_CONFIG,
)
from plugins.pendo.web.api.config_routes import (
    get_categories,
    get_diary_moods,
    get_diary_templates,
    router,
)


def test_config_router_registers_current_and_legacy_read_paths() -> None:
    """当前配置路径和仍被旧客户端使用的模板别名都应保持注册。"""

    paths = {route.path for route in router.routes}

    assert paths == {
        "/config/categories",
        "/config/diary/templates",
        "/diary/templates",
        "/config/diary/moods",
    }


def test_category_response_is_an_ordered_isolated_snapshot() -> None:
    """分类响应应保持配置顺序，修改响应不得污染全局分类。"""

    payload = get_categories()
    data = cast(dict[str, list[dict[str, str]]], payload["data"])

    assert data["ledger_expense"] == LEDGER_EXPENSE_CATEGORIES
    assert data["ledger_income"] == LEDGER_INCOME_CATEGORIES
    assert data["ledger_expense"] is not LEDGER_EXPENSE_CATEGORIES
    assert data["ledger_expense"][0] is not LEDGER_EXPENSE_CATEGORIES[0]

    original_name = LEDGER_EXPENSE_CATEGORIES[0]["name"]
    data["ledger_expense"][0]["name"] = "被修改"
    assert LEDGER_EXPENSE_CATEGORIES[0]["name"] == original_name


def test_template_response_copies_prompt_lists_and_preserves_configuration_order() -> None:
    """模板顺序应稳定，提示列表必须与进程级配置解除别名。"""

    payload = get_diary_templates()
    data = cast(dict[str, list[dict[str, object]]], payload["data"])
    templates = data["templates"]

    assert [template["id"] for template in templates] == list(DIARY_TEMPLATES)
    for template in templates:
        template_id = cast(str, template["id"])
        assert template["name"] == DIARY_TEMPLATES[template_id]["name"]
        assert template["prompts"] == DIARY_TEMPLATES[template_id]["prompts"]
        assert template["prompts"] is not DIARY_TEMPLATES[template_id]["prompts"]

    prompts = cast(list[str], templates[0]["prompts"])
    original_count = len(DIARY_TEMPLATES[cast(str, templates[0]["id"])]["prompts"])
    prompts.append("不应写回配置")
    assert len(DIARY_TEMPLATES[cast(str, templates[0]["id"])]["prompts"]) == original_count


def test_mood_response_copies_rows_and_lookup_mappings() -> None:
    """情绪选项及两个查找表都应返回内容一致的独立副本。"""

    payload = get_diary_moods()
    data = cast(dict[str, object], payload["data"])
    moods = cast(list[dict[str, str]], data["moods"])
    emojis = cast(dict[str, str], data["mood_emojis"])
    labels = cast(dict[str, str], data["mood_labels"])

    assert moods == DIARY_MOODS
    assert emojis == MOOD_ANALYSIS_CONFIG["mood_emojis"]
    assert labels == MOOD_ANALYSIS_CONFIG["mood_labels"]
    assert moods is not DIARY_MOODS and moods[0] is not DIARY_MOODS[0]
    assert emojis is not MOOD_ANALYSIS_CONFIG["mood_emojis"]
    assert labels is not MOOD_ANALYSIS_CONFIG["mood_labels"]

    original_label = DIARY_MOODS[0]["label"]
    moods[0]["label"] = "被修改"
    emojis["happy"] = "X"
    assert DIARY_MOODS[0]["label"] == original_label
    assert MOOD_ANALYSIS_CONFIG["mood_emojis"]["happy"] != "X"
