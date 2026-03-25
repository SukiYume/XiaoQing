"""Configuration data endpoints (categories, templates)."""
from fastapi import APIRouter

from ...config import PendoConfig, LEDGER_EXPENSE_CATEGORIES, LEDGER_INCOME_CATEGORIES, DIARY_TEMPLATES

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
def get_diary_templates():
    """Get available diary templates."""
    templates = []
    for tid, tdata in DIARY_TEMPLATES.items():
        templates.append({
            "id": tid,
            "name": tdata.get("name", tid),
            "prompts": tdata.get("prompts", []),
        })
    return {"ok": True, "data": templates, "message": ""}
