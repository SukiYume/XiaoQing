from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from core.interfaces import DeliveryTarget
from core.plugin_base import build_action, segments, split_message_segments

from .paths import normalize_cwd

logger = logging.getLogger(__name__)

ARXIV_SUMMARY_SOURCE = "arxiv_filter"
ARXIV_SUMMARY_KIND = "arxiv_daily_summary"
ARXIV_SUMMARY_INIT_KIND = "arxiv_summary_init"


def is_arxiv_summary_metadata(metadata: dict[str, Any]) -> bool:
    return (
        metadata.get("source") == ARXIV_SUMMARY_SOURCE
        and metadata.get("kind") == ARXIV_SUMMARY_KIND
    )


def codex_context_from(context: Any) -> Any:
    if getattr(context, "plugin_name", None) != "codex":
        raise PermissionError("Codex arXiv entrypoint requires a Codex-scoped context")
    return context


async def enqueue_or_replay_arxiv_summary(
    context: Any,
    *,
    date: str,
    links: list[str],
    user_id: int | None = None,
    group_id: int | None = None,
) -> str:
    from .manager import get_manager

    effective_user_id = user_id if user_id is not None else getattr(context, "current_user_id", None)
    principal = getattr(context, "principal", None)
    capabilities = getattr(context, "capabilities", None)
    is_system = bool(capabilities is not None and getattr(capabilities, "is_system", False))
    is_current_admin = False
    if principal is not None and getattr(capabilities, "is_bot_admin", False):
        try:
            is_current_admin = int(principal.user_id) == int(effective_user_id)
        except (TypeError, ValueError):
            is_current_admin = False
    if not is_system and not is_current_admin:
        raise PermissionError("Codex arXiv sidecar requires current admin authorization")
    principal_targets = tuple(getattr(principal, "delivery_targets", ()))
    if is_system:
        delivery_targets = principal_targets
    elif principal_targets:
        delivery_targets = principal_targets
    else:
        delivery_targets = _targets_from_ids(effective_user_id, group_id)
    codex_context = codex_context_from(context)
    manager = await get_manager(codex_context)
    addon = ArxivSummaryAddon(manager)
    return await addon.enqueue_or_replay(
        date=date,
        links=links,
        user_id=effective_user_id,
        group_id=group_id if group_id is not None else getattr(context, "current_group_id", None),
        context=context,
        delivery_targets=delivery_targets,
    )


def _targets_from_ids(
    user_id: int | None,
    group_id: int | None,
) -> tuple[DeliveryTarget, ...]:
    if group_id is not None:
        return (DeliveryTarget("group", int(group_id)),)
    if user_id is not None:
        return (DeliveryTarget("private", int(user_id)),)
    return ()


class ArxivSummaryAddon:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def enqueue_or_replay(
        self,
        *,
        date: str,
        links: list[str],
        user_id: int | None,
        group_id: int | None,
        context: Any,
        delivery_targets: tuple[DeliveryTarget, ...] | None = None,
    ) -> str:
        date = date.strip()
        normalized_links = self._normalize_links(links)
        if not date or not normalized_links:
            return "arXiv 总结任务缺少日期或链接。"

        label = self.manager.config.arxiv_summary_label
        explicit_targets = (
            delivery_targets
            if delivery_targets is not None
            else _targets_from_ids(user_id, group_id)
        )
        logger.info(
            "arXiv summary request: label=%s date=%s links=%d",
            label,
            date,
            len(normalized_links),
        )
        replay_message: str | None = None
        inflight_message: str | None = None

        async with self.manager.lock:
            session = self._ensure_session_locked(user_id=user_id, group_id=group_id)
            needs_init = session.thread_id is None and self._find_inflight_init_locked(label) is None
            inflight = self._find_inflight_job_locked(label, date)
            if inflight is not None:
                inflight_message = (
                    f"[codex:{label} #{inflight.job_id}] {date} arXiv 总结任务"
                    f"已在{'运行' if inflight.status == 'running' else '队列'}中。"
                )
            else:
                latest_success = self._latest_successful_summary(label, date)
                if latest_success is not None:
                    job_id = latest_success.get("job_id", "?")
                    content = str(latest_success.get("content") or "").strip()
                    replay_message = f"[codex:{label} #{job_id}] 完成:\n{content}"
                else:
                    init_job = None
                    try:
                        if needs_init:
                            init_job, _init_tasks_ahead = self.manager._enqueue_job_locked(
                                session,
                                self._build_init_prompt(),
                                user_id=user_id,
                                group_id=group_id,
                                context=context,
                                metadata=self._init_metadata(),
                                delivery_targets=explicit_targets,
                            )
                        job, _tasks_ahead = self.manager._enqueue_job_locked(
                            session,
                            self._build_link_prompt(date, normalized_links, session=session),
                            user_id=user_id,
                            group_id=group_id,
                            context=context,
                            metadata=self._metadata(date, normalized_links),
                            delivery_targets=explicit_targets,
                        )
                    except RuntimeError as exc:
                        return str(exc)
                    if init_job is not None:
                        logger.info(
                            "arXiv summary queued with init: label=%s date=%s init_job=%s summary_job=%s links=%d",
                            label,
                            date,
                            init_job.job_id,
                            job.job_id,
                            len(normalized_links),
                        )
                        return (
                            f"已投递 {date} arXiv 初始化和总结任务: "
                            f"`{label}` #{init_job.job_id} -> #{job.job_id}"
                        )
                    logger.info(
                        "arXiv summary queued: label=%s date=%s job=%s links=%d",
                        label,
                        date,
                        job.job_id,
                        len(normalized_links),
                    )
                    return f"已投递 {date} arXiv 总结任务: `{label}` #{job.job_id}"

        if replay_message is not None:
            await self._send_text_to_target(
                replay_message,
                user_id=user_id,
                group_id=group_id,
                context=context,
                delivery_targets=explicit_targets,
            )
            return f"已重发 {date} arXiv 历史总结。"
        if inflight_message is not None:
            await self._send_text_to_target(
                inflight_message,
                user_id=user_id,
                group_id=group_id,
                context=context,
                delivery_targets=explicit_targets,
            )
            return f"{date} arXiv 总结任务已在队列或运行中。"
        return "未执行 arXiv 总结任务。"

    async def _send_text_to_target(
        self,
        content: str,
        *,
        user_id: int | None,
        group_id: int | None,
        context: Any,
        delivery_targets: tuple[DeliveryTarget, ...] | None = None,
    ) -> None:
        targets = (
            delivery_targets
            if delivery_targets is not None
            else _targets_from_ids(user_id, group_id)
        )
        for target in targets:
            for batch in split_message_segments(segments(content)):
                action = build_action(batch, target.user_id, target.group_id)
                if action and hasattr(context, "send_action"):
                    action["_bypass_sink"] = True
                    await context.send_action(action)

    def _metadata(self, date: str, links: list[str]) -> dict[str, Any]:
        return {
            "source": ARXIV_SUMMARY_SOURCE,
            "kind": ARXIV_SUMMARY_KIND,
            "date": date,
            "links": links,
            "failure_title": f"{date} arXiv 总结",
        }

    def _init_metadata(self) -> dict[str, Any]:
        return {
            "source": ARXIV_SUMMARY_SOURCE,
            "kind": ARXIV_SUMMARY_INIT_KIND,
            "suppress_delivery": True,
            "queue_overhead": True,
        }

    def _is_summary_job(self, job: Any, date: str) -> bool:
        metadata = job.metadata or {}
        return is_arxiv_summary_metadata(metadata) and metadata.get("date") == date

    def _is_init_job(self, job: Any) -> bool:
        metadata = job.metadata or {}
        return (
            metadata.get("source") == ARXIV_SUMMARY_SOURCE
            and metadata.get("kind") == ARXIV_SUMMARY_INIT_KIND
        )

    def _find_inflight_job_locked(self, label: str, date: str) -> Any | None:
        running = self.manager.running.get(label)
        if running and self._is_summary_job(running, date):
            return running
        for queued in self.manager.queues.get(label, ()):
            if self._is_summary_job(queued, date):
                return queued
        return None

    def _find_inflight_init_locked(self, label: str) -> Any | None:
        running = self.manager.running.get(label)
        if running and self._is_init_job(running):
            return running
        for queued in self.manager.queues.get(label, ()):
            if self._is_init_job(queued):
                return queued
        return None

    def _conversation_events(self, label: str) -> list[dict[str, Any]]:
        path = self.manager._conversation_path(label)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _latest_successful_summary(self, label: str, date: str) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None
        for event in self._conversation_events(label):
            metadata = event.get("metadata") or {}
            if (
                event.get("role") != "assistant"
                or not is_arxiv_summary_metadata(metadata)
                or metadata.get("date") != date
            ):
                continue
            if event.get("cancelled") or event.get("timed_out"):
                continue
            if event.get("exit_code") not in (0, None):
                continue
            content = str(event.get("content") or "").strip()
            if content:
                latest = event
        return latest

    def _normalize_links(self, links: list[str]) -> list[str]:
        normalized: list[str] = []
        for link in links:
            value = str(link).strip()
            if not value:
                continue
            match = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", value)
            if match:
                value = f"https://arxiv.org/abs/{match.group(1)}"
            normalized.append(value)
        return list(dict.fromkeys(normalized))

    def _build_link_prompt(self, date: str, links: list[str], *, session: Any) -> str:
        methodology = self.manager.config.arxiv_summary_methodology
        methodology_path = Path(session.cwd) / methodology
        link_block = f"## {date}\n" + "\n".join(links)
        return (
            f"请先读取当前工作目录下的 `{methodology}`，并严格遵守该文件中的输出格式要求。\n"
            f"方法论文件路径: {methodology_path}\n"
            "不要把方法论文件内容复述出来，只输出最终 Markdown 摘要。\n\n"
            "硬性输出格式如下，必须全部满足：\n"
            f"1. 第一行必须且只能是 `## {date}`，不能追加任何标题文字。\n"
            "2. 禁止输出确认语、导语、今日重点、总评、`###` 小标题、`为什么重要` 小节或过程日志。\n"
            "3. 每篇论文必须使用 `N. [完整论文标题](https://arxiv.org/abs/...)` 编号列表。\n"
            "4. 每篇论文标题下一行必须是 `   > English Keywords`，关键词用英文，短语中不要使用连字符。\n"
            "5. 每篇论文写 2 段中文摘要，覆盖目的、方法、主要结果和意义；不要额外加小标题。\n"
            "6. 必须总结下面所有链接，不能只记录日期，不能要求我继续发送链接。\n\n"
            "本次要总结的 arXiv 链接：\n"
            f"{link_block}"
        )

    def _build_init_prompt(self) -> str:
        methodology = self.manager.config.arxiv_summary_methodology
        return (
            "你是我的 astro-ph 每日论文摘要会话。请记住：后续我会发送形如 "
            "`## YYYY-MM-DD` 加 arXiv links 的消息。\n"
            f"每次收到这种消息时，请立即读取当前工作目录下的 `{methodology}`，"
            "严格按照其中格式输出 Markdown 摘要。"
            "只总结输入中的所有论文，不输出过程日志、检索日志、确认语、寒暄或额外前言。\n"
            "这条消息只用于初始化会话规则，请简短确认。"
        )

    def _ensure_session_locked(self, *, user_id: int | None, group_id: int | None) -> Any:
        label = self.manager.config.arxiv_summary_label
        session = self.manager.sessions.get(label)
        if session is not None:
            return session

        cwd_text = (
            None
            if self.manager.config.arxiv_summary_cwd == self.manager.config.default_cwd
            else self.manager.config.arxiv_summary_cwd
        )
        cwd = normalize_cwd(cwd_text, self.manager.config)
        return self.manager._create_session_record_locked(
            label,
            cwd,
            user_id=user_id,
            group_id=group_id,
            metadata={"source": ARXIV_SUMMARY_SOURCE, "kind": "session"},
        )
