"""提供 Pendo Web 首页所需的聚合看板端点。"""

from fastapi import APIRouter, Depends

from ...services.db import Database
from ..analytics.dashboard_overview import build_dashboard_overview
from ..deps import get_current_user, get_db

router = APIRouter()


@router.get("/dashboard")
def get_dashboard(
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, object]:
    """在当前所有者范围内构建并返回看板概览。"""

    return {
        "ok": True,
        "data": build_dashboard_overview(db=db, owner_id=owner_id),
        "message": "",
    }
