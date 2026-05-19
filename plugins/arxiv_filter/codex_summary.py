from __future__ import annotations

import asyncio
import importlib
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_ARXIV_ABS_LINK_RE = re.compile(r"https://arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?")


def extract_arxiv_links(text: str) -> list[str]:
    links: list[str] = []
    for match in _ARXIV_ABS_LINK_RE.finditer(text):
        links.append(f"https://arxiv.org/abs/{match.group(1)}")
    return list(dict.fromkeys(links))


def _default_group_id(context: Any) -> int | None:
    current = getattr(context, "current_group_id", None)
    if current:
        return int(current)
    default_groups = []
    if hasattr(context, "default_groups"):
        try:
            default_groups = list(context.default_groups())
        except Exception:
            default_groups = []
    if not default_groups:
        config = getattr(context, "config", {}) or {}
        if isinstance(config, dict):
            default_groups = list(config.get("default_group_ids", []) or [])
    if len(default_groups) == 1:
        return int(default_groups[0])
    return None


def _load_codex_summary_entrypoint() -> Any:
    errors: list[Exception] = []
    for module_name in ("codex.arxiv_summary", "plugins.codex.arxiv_summary"):
        try:
            module = importlib.import_module(module_name)
            logger.info("using Codex arXiv summary module: %s", module_name)
            return module.enqueue_or_replay_arxiv_summary
        except Exception as exc:
            errors.append(exc)
    raise ImportError("Codex arXiv summary module is unavailable") from errors[-1]


def schedule_codex_summary_from_filter_result(
    context: Any,
    *,
    date: str,
    filter_text: str,
    user_id: int | None,
    group_id: int | None,
) -> asyncio.Task | None:
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
        user_id=user_id,
        group_id=group_id,
    )


def schedule_codex_summary(
    context: Any,
    *,
    date: str,
    links: list[str],
    user_id: int | None,
    group_id: int | None,
) -> asyncio.Task | None:
    if not links:
        logger.info("skip Codex arXiv summary enqueue for %s: no links", date)
        return None
    if not hasattr(context, "send_action"):
        logger.debug("skip Codex arXiv summary enqueue: context has no send_action")
        return None

    async def _runner() -> None:
        try:
            enqueue_or_replay_arxiv_summary = _load_codex_summary_entrypoint()

            logger.info(
                "enqueue Codex arXiv summary sidecar: date=%s links=%d",
                date,
                len(links),
            )
            result = await enqueue_or_replay_arxiv_summary(
                context,
                date=date,
                links=links,
                user_id=user_id or getattr(context, "current_user_id", None),
                group_id=group_id or _default_group_id(context),
            )
            logger.info("Codex arXiv summary sidecar enqueue result: %s", result)
        except Exception as exc:
            logger.exception("failed to enqueue Codex arXiv summary for %s: %s", date, exc)

    try:
        return asyncio.create_task(_runner())
    except Exception as exc:
        logger.exception("failed to schedule Codex arXiv summary for %s: %s", date, exc)
        return None
