"""媒体子系统的配置读取、日志、收件目录与下载超时。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from ..config.config import MediaConfig
    from ..runtime_state import ChatRuntime

MEDIA_ANALYSIS_PROMPT_VERSION = 8


MEDIA_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10, sock_read=15)


def media_config(runtime: ChatRuntime) -> MediaConfig:
    return runtime.cfg.media


def media_log(
    context, runtime, *, step: str, fields: dict[str, Any] | None = None, level: str = "info"
) -> None:
    from ..logging_utils import sanitize_log_fields

    if not runtime.cfg.debug.log_steps:
        return
    payload: dict[str, Any] = {"step": str(step)}
    if fields:
        payload.update(sanitize_log_fields(fields))
    try:
        getattr(context.logger, level)(
            "xiaoqing_chat media=%s", json.dumps(payload, ensure_ascii=False)
        )
    except Exception:
        return


def figures_inbox_dir(context) -> Path:
    return Path(context.data_dir) / "media" / "inbox"
