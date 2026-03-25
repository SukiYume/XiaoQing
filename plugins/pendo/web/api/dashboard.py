"""Dashboard aggregation endpoint."""
from fastapi import APIRouter, Depends

from ...services.db import Database
from ..analytics.dashboard_overview import build_dashboard_overview
from ..deps import get_db, get_current_user

router = APIRouter()


@router.get("/dashboard")
def get_dashboard(
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Get dashboard overview data."""
    return {
        "ok": True,
        "data": build_dashboard_overview(db=db, owner_id=owner_id),
        "message": "",
    }
