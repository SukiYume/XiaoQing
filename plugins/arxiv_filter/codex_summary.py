"""把筛选出的论文链接异步交给 Codex 摘要能力。"""

from __future__ import annotations

import asyncio
import logging
import re

from core.interfaces import PluginContextProtocol
from core.log import LogEvent
from core.public_errors import log_public_error

from .utils import ARXIV_ABS_URL_PREFIX

log = logging.getLogger(__name__)

_ARXIV_ABS_LINK_RE = re.compile(r"https://arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?")


def extract_arxiv_links(text: str) -> list[str]:
    links: list[str] = []
    for match in _ARXIV_ABS_LINK_RE.finditer(text):
        links.append(f"{ARXIV_ABS_URL_PREFIX}{match.group(1)}")
    return list(dict.fromkeys(links))


def schedule_codex_summary_from_filter_result(
    context: PluginContextProtocol,
    *,
    date: str,
    filter_text: str,
) -> asyncio.Task[None] | None:
    links = extract_arxiv_links(filter_text)
    return schedule_codex_summary(
        context,
        date  = date,
        links = links,
    )


def schedule_codex_summary(
    context: PluginContextProtocol,
    *,
    date: str,
    links: list[str],
) -> asyncio.Task[None] | None:
    if not links:
        log.info(LogEvent("codex_summary.skip", reason_code="no_links", date=date))
        return None

    async def _runner() -> None:
        try:
            log.info(
                LogEvent(
                    "codex_summary.enqueue",
                    date    = date,
                    links   = len(links),
                    targets = len(
                        tuple(getattr(getattr(context, "principal", None), "delivery_targets", ()))
                    ),
                )
            )
            capabilities = getattr(context, "capabilities", None)
            service      = (
                getattr(capabilities, "codex_arxiv_summary", None)
                if capabilities is not None
                else None
            )
            if service is None:
                log.warning(LogEvent("codex_summary.skip", reason_code="capability_unavailable"))
                return
            # 返回值是给用户看的中文结果；新任务的任务号由 codex 插件的 summary.queued 记录。
            await service.enqueue_or_replay(date=date, links=links)
            log.info(LogEvent("codex_summary.enqueued"))
        except Exception as exc:
            log_public_error(
                context,
                exc,
                component="arxiv_filter.codex_enqueue",
            )

    try:
        task = asyncio.create_task(_runner())
        # 登记到插件代内的后台任务集合，卸载时可见、可等待。
        tasks = context.state.setdefault("arxiv_background_tasks", set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task
    except Exception as exc:
        log_public_error(
            context,
            exc,
            component="arxiv_filter.codex_schedule",
        )
        return None
