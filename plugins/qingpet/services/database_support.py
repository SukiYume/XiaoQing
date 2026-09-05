"""Qingpet 数据库仓储共享的限额常量与安全错误日志。"""

import logging
import re

from ..utils.constants import DAILY_LIMITS

logger = logging.getLogger(__name__)

_SAFE_OPERATION_RE  = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_ERROR_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,95}\Z")

_PET_ACTION_COUNTERS = {
    "feed": ("today_feed_count", "total_feed_count"),
    "clean": ("today_clean_count", "total_clean_count"),
    "play": ("today_play_count", "total_play_count"),
    "train": ("today_train_count", "total_train_count"),
    "explore": ("today_explore_count", "total_explore_count"),
}
_DAILY_TASK_TEMPLATES = (
    ("feed", 3, 30),
    ("clean", 2, 20),
    ("play", 3, 25),
    ("visit", 2, 20),
)
_WEEKLY_RANKING_REWARDS = (100, 50, 30)
_DAILY_COIN_LIMIT       = int(DAILY_LIMITS["coins"])


def _log_database_failure(operation: str, exc: BaseException) -> None:
    """仅记录稳定的操作名和异常类型，避免数据库内容进入日志。"""

    safe_operation = operation if _SAFE_OPERATION_RE.fullmatch(operation) else "unknown"
    error_type     = type(exc).__name__
    if not _SAFE_ERROR_TYPE_RE.fullmatch(error_type):
        error_type = "Exception"
    logger.error(
        "QingPet database operation failed operation=%s error_type=%s",
        safe_operation,
        error_type,
    )
