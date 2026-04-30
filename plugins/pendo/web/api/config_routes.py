"""Configuration data endpoints (categories, templates, diary metadata)."""
from fastapi import APIRouter

from ...config import (
    DIARY_MOODS,
    DIARY_TEMPLATES,
    LEDGER_EXPENSE_CATEGORIES,
    LEDGER_INCOME_CATEGORIES,
    MOOD_ANALYSIS_CONFIG,
)

router = APIRouter()


@router.get("/config/categories")
def get_categories():
    """Get available categories for all modules."""
    return {
        "ok": True,
        "data": {
            "ledger_expense": LEDGER_EXPENSE_CATEGORIES,
            "ledger_income": LEDGER_INCOME_CATEGORIES,
        },
        "message": "",
    }


@router.get("/diary/templates")
@router.get("/config/diary/templates")
def get_diary_templates():
    """Get available diary templates."""
    templates = []
    for tid, tdata in DIARY_TEMPLATES.items():
        templates.append({
            "id": tid,
            "name": tdata.get("name", tid),
            "prompts": tdata.get("prompts", []),
        })
    return {"ok": True, "data": {"templates": templates}, "message": ""}


@router.get("/config/diary/moods")
def get_diary_moods():
    """Get diary mood metadata for web clients."""
    mood_emojis = dict(MOOD_ANALYSIS_CONFIG.get("mood_emojis", {}))
    return {
        "ok": True,
        "data": {
            "mood_emojis": mood_emojis,
            "mood_labels": dict(MOOD_ANALYSIS_CONFIG.get("mood_labels", {})),
            "moods": DIARY_MOODS,
        },
        "message": "",
    }
