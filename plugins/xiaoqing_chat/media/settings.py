"""媒体子系统的配置读取、收件目录与下载超时。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.config import MediaConfig
    from ..runtime_state import ChatRuntime

MEDIA_ANALYSIS_PROMPT_VERSION = 8


MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 20.0


def media_config(runtime: ChatRuntime) -> MediaConfig:
    return runtime.cfg.media


def figures_inbox_dir(context) -> Path:
    return Path(context.data_dir) / "media" / "inbox"
