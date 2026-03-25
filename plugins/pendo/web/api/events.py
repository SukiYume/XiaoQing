"""Events-specific API routes for the web UI."""

from fastapi import APIRouter, Depends, HTTPException

from ...services.db import Database
from ..analytics.events_overview import build_event_detail, build_events_overview
from ..deps import get_current_user, get_db

router = APIRouter()


@router.get("/events/overview")
def get_events_overview(
    start_date: str,
    end_date: str,
    keyword: str = "",
    category: str = "",
    kind: str = "all",
    reminder: str = "all",
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    return {
        "ok": True,
        "data": build_events_overview(
            db=db,
            owner_id=owner_id,
            start_date=start_date,
            end_date=end_date,
            keyword=keyword,
            category=category,
            kind=kind,
            reminder=reminder,
        ),
        "message": "",
    }


@router.get("/events/categories")
def get_event_categories(
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    overview = build_events_overview(
        db=db,
        owner_id=owner_id,
        start_date="1970-01-01",
        end_date="2099-12-31",
    )
    return {"ok": True, "data": {"categories": overview["categories"]}, "message": ""}


@router.get("/events/{event_id}/detail")
def get_event_detail(
    event_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    detail = build_event_detail(db=db, owner_id=owner_id, event_id=event_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"ok": True, "data": detail, "message": ""}
