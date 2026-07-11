from __future__ import annotations

import asyncio
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


def _resolve_delivery_target(
    context: Any,
    *,
    user_id: int | None,
    group_id: int | None,
) -> tuple[int | None, int | None]:
    """Resolve the sidecar target without confusing a private chat with a scheduler run."""
    capabilities = getattr(context, "capabilities", None)
    if capabilities is not None and getattr(capabilities, "is_system", False):
        if user_id is None and group_id is None:
            return None, _default_group_id(context)
        return user_id, group_id
    effective_user_id = user_id if user_id is not None else getattr(context, "current_user_id", None)
    return effective_user_id, group_id


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
            target_user_id, target_group_id = _resolve_delivery_target(
                context,
                user_id=user_id,
                group_id=group_id,
            )

            logger.info(
                "enqueue Codex arXiv summary sidecar: date=%s links=%d user=%s group=%s",
                date,
                len(links),
                target_user_id,
                target_group_id,
            )
            result = await context.call_plugin(
                "codex",
                "enqueue_or_replay_arxiv_summary",
                date=date,
                links=links,
                user_id=target_user_id,
                group_id=target_group_id,
            )
            logger.info("Codex arXiv summary sidecar enqueue result: %s", result)
        except Exception as exc:
            logger.exception("failed to enqueue Codex arXiv summary for %s: %s", date, exc)

    try:
        task = asyncio.create_task(_runner())
        state = getattr(context, "state", None)
        if isinstance(state, dict):
            tasks = state.setdefault("arxiv_background_tasks", set())
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return task
    except Exception as exc:
        logger.exception("failed to schedule Codex arXiv summary for %s: %s", date, exc)
        return None
