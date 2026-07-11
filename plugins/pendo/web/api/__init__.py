"""API route aggregation for Pendo Web UI."""

from fastapi import APIRouter

from .auth_routes import router as auth_router
from .config_routes import router as config_router
from .dashboard import router as dashboard_router
from .events import router as events_router
from .items import router as items_router
from .search import router as search_router
from .settings import router as settings_router
from .stats import router as stats_router
from .transfer import router as transfer_router
from .widget import router as widget_router


def create_api_router() -> APIRouter:
    """Create the main API router with all sub-routers."""
    router = APIRouter()
    router.include_router(auth_router, tags=["auth"])
    router.include_router(items_router, tags=["items"])
    router.include_router(events_router, tags=["events"])
    router.include_router(dashboard_router, tags=["dashboard"])
    router.include_router(search_router, tags=["search"])
    router.include_router(stats_router, tags=["stats"])
    router.include_router(settings_router, tags=["settings"])
    router.include_router(config_router, tags=["config"])
    router.include_router(transfer_router, tags=["transfer"])
    router.include_router(widget_router, tags=["widget"])
    return router
