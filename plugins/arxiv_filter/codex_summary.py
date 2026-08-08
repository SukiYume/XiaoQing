"""把筛选出的论文链接异步交给 Codex 摘要能力。"""

from __future__ import annotations

import asyncio
import logging
import re

from core.interfaces import PluginContextProtocol
from core.public_errors import public_error_message

logger = logging.getLogger(__name__)

_ARXIV_ABS_LINK_RE = re.compile(r"https://arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?")


def extract_arxiv_links(text: str) -> list[str]:
    links: list[str] = []
    for match in _ARXIV_ABS_LINK_RE.finditer(text):
        links.append(f"https://arxiv.org/abs/{match.group(1)}")
    return list(dict.fromkeys(links))


def schedule_codex_summary_from_filter_result(
    context: PluginContextProtocol,
    *,
    date: str,
    filter_text: str,
) -> asyncio.Task[None] | None:
    links = extract_arxiv_links(filter_text)
    logger.info(
        "arXiv Codex summary sidecar extracted %d links for %s",
        len(links),
        date,
    )
    return schedule_codex_summary(
        context,
        date=date,
        links=links,
    )


def schedule_codex_summary(
    context: PluginContextProtocol,
    *,
    date: str,
    links: list[str],
) -> asyncio.Task[None] | None:
    if not links:
        logger.info("skip Codex arXiv summary enqueue for %s: no links", date)
        return None
    if not hasattr(context, "send_action"):
        logger.debug("skip Codex arXiv summary enqueue: context has no send_action")
        return None

    async def _runner() -> None:
        try:
            logger.info(
                "enqueue Codex arXiv summary sidecar: date=%s links=%d targets=%d",
                date,
                len(links),
                len(tuple(getattr(getattr(context, "principal", None), "delivery_targets", ()))),
            )
            capabilities = getattr(context, "capabilities", None)
            service = (
                getattr(capabilities, "codex_arxiv_summary", None)
                if capabilities is not None
                else None
            )
            if service is None:
                logger.info("skip Codex arXiv summary enqueue: capability unavailable")
                return
            result = await service.enqueue_or_replay(
                date=date,
                links=links,
            )
            logger.info("Codex arXiv summary sidecar enqueue result: %s", result)
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger=logger,
                component="arxiv_filter.codex_enqueue",
            )

    try:
        task = asyncio.create_task(_runner())
        # 真实插件上下文会提供代际 state；轻量测试替身或旧嵌入方没有该字段时，
        # 任务仍可执行，只是不登记到卸载期可见的后台任务集合。
        state = getattr(context, "state", None)
        if isinstance(state, dict):
            tasks = state.setdefault("arxiv_background_tasks", set())
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return task
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="arxiv_filter.codex_schedule",
        )
        return None
