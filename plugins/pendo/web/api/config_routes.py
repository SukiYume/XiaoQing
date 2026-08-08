"""提供记账分类、日记模板和情绪元数据等只读配置快照。"""

from collections.abc import Mapping

from fastapi import APIRouter

from ...config import (
    DIARY_MOODS,
    DIARY_TEMPLATES,
    LEDGER_EXPENSE_CATEGORIES,
    LEDGER_INCOME_CATEGORIES,
    MOOD_ANALYSIS_CONFIG,
)

router = APIRouter()


def _copy_text_mapping(value: object) -> dict[str, str]:
    """在配置/API 边界校验并复制字符串映射，避免返回进程级可变对象。"""

    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(label) for key, label in value.items()}


@router.get("/config/categories")
def get_categories() -> dict[str, object]:
    """返回与进程级配置隔离的记账收支分类。"""

    return {
        "ok": True,
        "data": {
            "ledger_expense": [dict(category) for category in LEDGER_EXPENSE_CATEGORIES],
            "ledger_income": [dict(category) for category in LEDGER_INCOME_CATEGORIES],
        },
        "message": "",
    }


@router.get("/diary/templates")
@router.get("/config/diary/templates")
def get_diary_templates() -> dict[str, object]:
    """按配置顺序返回模板及复制后的提示列表，并保留旧路径别名。"""

    templates = [
        {
            "id": template_id,
            "name": template_data.get("name", template_id),
            "prompts": list(template_data.get("prompts", [])),
        }
        for template_id, template_data in DIARY_TEMPLATES.items()
    ]
    return {"ok": True, "data": {"templates": templates}, "message": ""}


@router.get("/config/diary/moods")
def get_diary_moods() -> dict[str, object]:
    """返回相互一致且不会反向修改配置的情绪标签、表情和选项。"""

    return {
        "ok": True,
        "data": {
            "mood_emojis": _copy_text_mapping(MOOD_ANALYSIS_CONFIG.get("mood_emojis")),
            "mood_labels": _copy_text_mapping(MOOD_ANALYSIS_CONFIG.get("mood_labels")),
            "moods": [dict(mood) for mood in DIARY_MOODS],
        },
        "message": "",
    }
