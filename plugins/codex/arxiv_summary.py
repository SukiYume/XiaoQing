"""把 arXiv 筛选结果安全接入固定的 Codex 日报会话。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import stat
from collections.abc import Mapping
from datetime import date as calendar_date
from typing import TYPE_CHECKING, Any

from core.interfaces import ACTION_BYPASS_SINK_KEY, DeliveryTarget, PluginContextProtocol
from core.plugin_base import build_action, segments, split_message_segments

from .paths import normalize_cwd

if TYPE_CHECKING:
    from .manager import CodexQueueManager, CodexSession, RuntimeJob

logger = logging.getLogger(__name__)

ARXIV_SUMMARY_SOURCE = "arxiv_filter"
ARXIV_SUMMARY_KIND = "arxiv_daily_summary"
ARXIV_SUMMARY_INIT_KIND = "arxiv_summary_init"
MAX_ARXIV_LINKS = 512
MAX_ARXIV_LINK_CHARS = 2_048
MAX_CONVERSATION_READ_BYTES = 8 * 1024 * 1024
MAX_CONVERSATION_LINE_BYTES = 1024 * 1024
ARXIV_LINK_RE = re.compile(
    r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/"
    r"(?P<identifier>[0-9]{4}\.[0-9]{4,5})(?:v[0-9]+)?(?:\.pdf)?/?(?:[?#].*)?",
    re.IGNORECASE,
)


def is_arxiv_summary_metadata(metadata: Mapping[str, Any]) -> bool:
    return (
        metadata.get("source") == ARXIV_SUMMARY_SOURCE
        and metadata.get("kind") == ARXIV_SUMMARY_KIND
    )


def _optional_positive_id(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer or None")
    return value


def _normalize_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    try:
        parsed = calendar_date.fromisoformat(cleaned)
    except ValueError:
        return None
    return cleaned if parsed.isoformat() == cleaned else None


def _normalize_arxiv_links(links: Any) -> tuple[list[str], str | None]:
    """把受限 arXiv URL 规范化为无版本的 HTTPS abs 链接。"""

    if not isinstance(links, list) or any(not isinstance(link, str) for link in links):
        return [], "arXiv 链接必须是字符串列表。"
    if len(links) > MAX_ARXIV_LINKS:
        return [], f"单次 arXiv 总结最多接受 {MAX_ARXIV_LINKS} 个链接。"

    normalized: list[str] = []
    seen: set[str] = set()
    for link in links:
        value = link.strip()
        if not value or len(value) > MAX_ARXIV_LINK_CHARS or any(ord(char) < 32 for char in value):
            return [], "arXiv 链接为空、含控制字符或超过长度上限。"
        match = ARXIV_LINK_RE.fullmatch(value)
        if match is None:
            return [], f"仅支持 arxiv.org 的 abs/pdf 链接: {value[:80]}"
        canonical = f"https://arxiv.org/abs/{match.group('identifier')}"
        if canonical not in seen:
            seen.add(canonical)
            normalized.append(canonical)
    return normalized, None


def _summary_identity_matches(
    metadata: Mapping[str, Any],
    *,
    date: str,
    links: list[str],
) -> bool:
    """同一摘要必须同时匹配源日期和规范化后的论文集合。"""

    if not is_arxiv_summary_metadata(metadata) or metadata.get("date") != date:
        return False
    stored_links, error = _normalize_arxiv_links(metadata.get("links"))
    if error is not None:
        # 旧历史若没有记录链接身份，不能仅凭日期复用。
        return False
    return frozenset(stored_links) == frozenset(links)


async def enqueue_or_replay_arxiv_summary(
    context: PluginContextProtocol,
    *,
    date: str,
    links: list[str],
    user_id: int | None = None,
    group_id: int | None = None,
) -> str:
    from .manager import get_manager

    effective_user_id = _optional_positive_id(
        user_id if user_id is not None else getattr(context, "current_user_id", None),
        field_name="user_id",
    )
    effective_group_id = _optional_positive_id(
        group_id if group_id is not None else getattr(context, "current_group_id", None),
        field_name="group_id",
    )
    principal = getattr(context, "principal", None)
    capabilities = getattr(context, "capabilities", None)
    is_system = bool(capabilities is not None and getattr(capabilities, "is_system", False))
    is_current_admin = False
    if principal is not None and getattr(capabilities, "is_bot_admin", False):
        is_current_admin = principal.user_id is not None and principal.user_id == effective_user_id
    if not is_system and not is_current_admin:
        raise PermissionError("Codex arXiv sidecar requires current admin authorization")
    principal_targets = tuple(getattr(principal, "delivery_targets", ()))
    if is_system:
        delivery_targets = principal_targets
    elif principal_targets:
        delivery_targets = principal_targets
    else:
        delivery_targets = _targets_from_ids(effective_user_id, effective_group_id)
    if getattr(context, "plugin_name", None) != "codex":
        raise PermissionError("Codex arXiv entrypoint requires a Codex-scoped context")
    manager = await get_manager(context)
    addon = ArxivSummaryAddon(manager)
    return await addon.enqueue_or_replay(
        date=date,
        links=links,
        user_id=effective_user_id,
        group_id=effective_group_id,
        context=context,
        delivery_targets=delivery_targets,
    )


def _targets_from_ids(
    user_id: int | None,
    group_id: int | None,
) -> tuple[DeliveryTarget, ...]:
    if group_id is not None:
        return (DeliveryTarget("group", group_id),)
    if user_id is not None:
        return (DeliveryTarget("private", user_id),)
    return ()


class ArxivSummaryAddon:
    def __init__(self, manager: CodexQueueManager) -> None:
        self.manager = manager

    async def enqueue_or_replay(
        self,
        *,
        date: str,
        links: list[str],
        user_id: int | None,
        group_id: int | None,
        context: PluginContextProtocol,
        delivery_targets: tuple[DeliveryTarget, ...] | None = None,
    ) -> str:
        normalized_date = _normalize_date(date)
        if normalized_date is None:
            return "arXiv 总结日期必须是有效的 YYYY-MM-DD。"
        normalized_links, link_error = _normalize_arxiv_links(links)
        if link_error:
            return link_error
        if not normalized_links:
            return "arXiv 总结任务缺少链接。"
        date = normalized_date

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
        disk_usage_bytes = await self.manager._measure_disk_usage_bytes(
            max_bytes=self.manager.config.emergency_disk_bytes
        )

        async with self.manager.lock:
            session = self._ensure_session_locked(user_id=user_id, group_id=group_id)
            needs_init = (
                session.thread_id is None and self._find_inflight_init_locked(label) is None
            )
            inflight = self._find_inflight_job_locked(label, date, normalized_links)
            if inflight is not None:
                inflight_message = (
                    f"[codex:{label} #{inflight.job_id}] {date} arXiv 总结任务"
                    f"已在{'运行' if inflight.status == 'running' else '队列'}中。"
                )
            else:
                latest_success = await asyncio.to_thread(
                    self._latest_successful_summary,
                    label,
                    date,
                    normalized_links,
                )
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
                                metadata={
                                    "source": ARXIV_SUMMARY_SOURCE,
                                    "kind": ARXIV_SUMMARY_INIT_KIND,
                                    "suppress_delivery": True,
                                    "queue_overhead": True,
                                },
                                delivery_targets=explicit_targets,
                                disk_usage_bytes=disk_usage_bytes,
                            )
                        job, _tasks_ahead = self.manager._enqueue_job_locked(
                            session,
                            self._build_link_prompt(date, normalized_links),
                            user_id=user_id,
                            group_id=group_id,
                            context=context,
                            metadata={
                                "source": ARXIV_SUMMARY_SOURCE,
                                "kind": ARXIV_SUMMARY_KIND,
                                "date": date,
                                "links": normalized_links,
                                "failure_title": f"{date} arXiv 总结",
                            },
                            delivery_targets=explicit_targets,
                            disk_usage_bytes=disk_usage_bytes,
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

        existing_message = replay_message or inflight_message
        if existing_message is None:
            return "未执行 arXiv 总结任务。"
        await self._send_text_to_target(
            existing_message,
            user_id=user_id,
            group_id=group_id,
            context=context,
            delivery_targets=explicit_targets,
        )
        return (
            f"已重发 {date} arXiv 历史总结。"
            if replay_message is not None
            else f"{date} arXiv 总结任务已在队列或运行中。"
        )

    async def _send_text_to_target(
        self,
        content: str,
        *,
        user_id: int | None,
        group_id: int | None,
        context: PluginContextProtocol,
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
                    action[ACTION_BYPASS_SINK_KEY] = True
                    await context.send_action(action)

    def _is_summary_job(self, job: RuntimeJob, date: str, links: list[str]) -> bool:
        metadata = job.metadata or {}
        return isinstance(metadata, Mapping) and _summary_identity_matches(
            metadata,
            date=date,
            links=links,
        )

    def _is_init_job(self, job: RuntimeJob) -> bool:
        metadata = job.metadata or {}
        return (
            metadata.get("source") == ARXIV_SUMMARY_SOURCE
            and metadata.get("kind") == ARXIV_SUMMARY_INIT_KIND
        )

    def _find_inflight_job_locked(
        self,
        label: str,
        date: str,
        links: list[str],
    ) -> RuntimeJob | None:
        running = self.manager.running.get(label)
        if running and self._is_summary_job(running, date, links):
            return running
        for queued in self.manager.queues.get(label, ()):
            if self._is_summary_job(queued, date, links):
                return queued
        return None

    def _find_inflight_init_locked(self, label: str) -> RuntimeJob | None:
        running = self.manager.running.get(label)
        if running and self._is_init_job(running):
            return running
        for queued in self.manager.queues.get(label, ()):
            if self._is_init_job(queued):
                return queued
        return None

    def _conversation_events(self, label: str) -> list[dict[str, Any]]:
        path = self.manager._conversation_path(label)
        try:
            info = path.lstat()
        except OSError:
            return []
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return []

        events: list[dict[str, Any]] = []
        try:
            with path.open("rb") as handle:
                if info.st_size > MAX_CONVERSATION_READ_BYTES:
                    handle.seek(info.st_size - MAX_CONVERSATION_READ_BYTES)
                    handle.readline()  # 丢弃从文件中部截到的半行。
                for raw_line in handle:
                    if len(raw_line) > MAX_CONVERSATION_LINE_BYTES:
                        continue
                    try:
                        event = json.loads(raw_line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(event, dict):
                        events.append(event)
        except OSError:
            return []
        return events

    def _latest_successful_summary(
        self,
        label: str,
        date: str,
        links: list[str],
    ) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None
        for event in self._conversation_events(label):
            metadata = event.get("metadata")
            if (
                event.get("role") != "assistant"
                or not isinstance(metadata, Mapping)
                or not _summary_identity_matches(
                    metadata,
                    date=date,
                    links=links,
                )
            ):
                continue
            if event.get("cancelled") or event.get("timed_out"):
                continue
            if event.get("exit_code") not in (0, None):
                continue
            raw_content = event.get("content")
            content = raw_content.strip() if isinstance(raw_content, str) else ""
            if content:
                latest = event
        return latest

    def _build_link_prompt(self, date: str, links: list[str]) -> str:
        methodology = self.manager.config.arxiv_summary_methodology
        link_block = f"## {date}\n" + "\n".join(links)
        return (
            f"请先读取当前工作目录下的 `{methodology}`，并严格遵守该文件中的输出格式要求。\n"
            "不要把方法论文件内容复述出来，只输出最终 Markdown 摘要。\n\n"
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

    def _ensure_session_locked(
        self,
        *,
        user_id: int | None,
        group_id: int | None,
    ) -> CodexSession:
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
