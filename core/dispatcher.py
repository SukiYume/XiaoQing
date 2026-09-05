# pyright: reportConstantRedefinition=false
"""
消息分发器

负责解析消息并路由到对应的插件命令。

处理流程:
1. 事件过滤 - 仅处理 message 类型事件
2. 消息解析 - 提取文本、user_id、group_id
3. 触发门控 - 私聊、配置放行、has_prefix 或活跃会话
4. URL-only 路由 - 通过门控后才交给 url_parser
5. 会话处理 - 活跃多轮对话优先消费输入
6. 命令路由 - 无会话消费时匹配命令并执行
7. 闲聊处理 - 无命令时进行闲聊
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from . import constants
from .clock import IClock, IRandom, SystemClock, SystemRandom
from .interfaces import (
    AdminCheck,
    ConfigProvider,
    ContextFactory,
    PluginGroupRole,
    PluginPrincipal,
    PluginRegistry,
)
from .message import (
    ValidatedInboundEvent,
    compile_bot_name_pattern,
    parse_text_command_context,
    scan_message,
)
from .metrics import MetricsCollector
from .models import OneBotEvent
from .plugin_execution import (
    PluginExecutionClosed,
    PluginExecutionTimeout,
    PluginExecutionUnavailable,
    call_plugin_callback,
    invoke_loaded_plugin,
)
from .public_errors import public_error_message, public_error_response
from .router import CommandRouter, resolve_catalog_invocation
from .safe_http import UnsafeUrlError, redact_url_for_log, validate_public_url

if TYPE_CHECKING:
    from .session import Session, SessionManager

logger = logging.getLogger(__name__)


class AdjustableSemaphore:
    """Event-loop-owned capacity limiter whose identity survives config reloads.

    Lowering ``capacity`` never revokes holders already inside the dispatcher.
    New work remains blocked until the holder count falls below the new limit,
    so an old and a new limiter generation cannot accidentally add their slots.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity                             = self._validate_capacity(capacity)
        self._in_use                               = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    @staticmethod
    def _validate_capacity(capacity: int) -> int:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("dispatcher capacity must be a positive integer")
        return capacity

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def in_use(self) -> int:
        return self._in_use

    def locked(self) -> bool:
        return self._in_use >= self._capacity

    def over_capacity(self) -> bool:
        """Return whether shrinking left more holders than the new capacity."""

        return self._in_use > self._capacity

    def resize(self, capacity: int) -> None:
        capacity = self._validate_capacity(capacity)
        if capacity == self._capacity:
            return
        increased      = capacity > self._capacity
        self._capacity = capacity
        if increased:
            self._wake_waiters()

    async def acquire(self) -> bool:
        if not self._waiters and self._in_use < self._capacity:
            self._in_use += 1
            return True

        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        self._wake_waiters()
        try:
            await waiter
        except BaseException:
            reserved = waiter.done() and not waiter.cancelled()
            if not waiter.done():
                waiter.cancel()
            try:
                self._waiters.remove(waiter)
            except ValueError:
                pass
            if reserved:
                self._in_use -= 1
            self._wake_waiters()
            raise
        return True

    def release(self) -> None:
        if self._in_use <= 0:
            raise ValueError("AdjustableSemaphore released too many times")
        self._in_use -= 1
        self._wake_waiters()

    def _wake_waiters(self) -> None:
        while self._waiters and self._in_use < self._capacity:
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            self._in_use += 1
            waiter.set_result(None)

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *_exc_info: Any) -> None:
        self.release()


class _SessionGenerationChanged(Exception):
    """Abort a no-longer-current session transaction without publishing it."""


def _build_failed_session_transactions(
    session_manager: SessionManager,
    *,
    user_id: int,
    group_id: int | None,
    expected_generation: tuple[str, str],
    close_callback: Callable[[Session], Awaitable[None]],
) -> tuple[
    Callable[[], Awaitable[bool | None]],
    Callable[[], Awaitable[bool | None]],
]:
    """为一次失败会话冻结代际，并构造带/不带插件钩子的清理事务。"""

    async def cleanup(session: Session) -> bool:
        if (session.plugin_name, session.session_id) != expected_generation:
            raise _SessionGenerationChanged
        await close_callback(session)
        return await session_manager.delete(user_id, group_id)

    async def delete_without_hook(session: Session) -> bool:
        if (session.plugin_name, session.session_id) != expected_generation:
            raise _SessionGenerationChanged
        return await session_manager.delete(user_id, group_id)

    async def run_cleanup() -> bool | None:
        return await session_manager.update(user_id, group_id, cleanup)

    async def run_delete_without_hook() -> bool | None:
        return await session_manager.update(user_id, group_id, delete_without_hook)

    return run_cleanup, run_delete_without_hook


# ============================================================
# 数据类
# ============================================================


@dataclass
class MessageContext:
    """消息上下文，封装消息相关的所有信息"""

    request_id: str  # 请求追踪 ID
    text: str  # 原始文本
    clean_text: str  # 去除前缀后的文本
    user_id: int | None  # 用户 ID
    group_id: int | None  # 群 ID (私聊为 None)
    is_private: bool  # 是否私聊
    has_bot_name: bool  # 是否包含 bot_name（任意位置）
    has_prefix: bool  # 是否"指向 bot"：/开头 OR bot_name OR @me（任意位置）
    has_command_prefix: bool  # 是否以命令前缀（默认 "/"）开头
    is_only_bot_name: bool  # 是否只叫 bot_name
    is_at_me: bool  # 是否 @ 了机器人
    is_url_only: bool  # clean_text 严格匹配 ^https?://\S+$
    event: dict[str, Any]  # 原始事件
    is_empty: bool = False  # 无文本、媒体或 @；仅允许活跃会话消费
    # Read-only routing hint.  The continuation path always revalidates the
    # session generation inside SessionManager.update() before doing work.
    cached_session: Session | None = None


class MessageParser:
    """解析消息事件并构建 MessageContext"""

    def __init__(self, config_provider: ConfigProvider) -> None:
        self._config_provider                                      = config_provider
        self._prefix_cache_key: tuple[str, tuple[str, ...]] | None = None
        self._bot_name_pattern: re.Pattern[str] | None             = None
        self._cached_bot_name: str                                 = ""
        self._cached_prefixes: tuple[str, ...]                     = ()
        self.refresh_prefix_cache()

    def refresh_prefix_cache(self) -> None:
        config   = self._config_provider.config
        bot_name = config.get("bot_name", "")
        prefixes = tuple(config.get("command_prefixes", ["/"]))

        cache_key = (bot_name, prefixes)
        if cache_key == self._prefix_cache_key:
            return

        self._prefix_cache_key = cache_key
        self._cached_bot_name  = bot_name
        self._cached_prefixes  = prefixes
        self._bot_name_pattern = compile_bot_name_pattern(bot_name)

    def parse(
        self,
        event: dict[str, Any],
        *,
        allow_empty_session_input: bool = False,
    ) -> MessageContext | None:
        """解析消息事件，构建消息上下文"""
        bot_name     = self._cached_bot_name
        prefixes     = self._cached_prefixes
        self_id      = str(event.get("self_id", "") or "")
        message_scan = scan_message(
            event.get("message"),
            self_id     = self_id,
            raw_message = str(event.get("raw_message", "") or ""),
        )
        text     = message_scan.text.strip()
        user_id  = event.get("user_id")
        group_id = event.get("group_id")

        is_empty = not text and not message_scan.has_media and not message_scan.is_at_me
        if is_empty and not allow_empty_session_input:
            logger.debug(
                "Drop empty message: post_type=%s message_type=%s message_kind=%s",
                event.get("post_type"),
                event.get("message_type"),
                type(event.get("message")).__name__,
            )
            return None

        # 忽略来自自己的消息，防止循环触发
        if self_id and user_id and str(user_id) == self_id:
            return None

        parsed = parse_text_command_context(
            text,
            event,
            bot_name         = bot_name,
            prefixes         = prefixes,
            self_id          = self_id,
            bot_name_pattern = self._bot_name_pattern,
            message_scan     = message_scan,
        )

        return MessageContext(
            request_id         = str(uuid.uuid4())[:8],
            text               = text,
            clean_text         = parsed.clean_text,
            user_id            = user_id,
            group_id           = group_id,
            is_private         = group_id is None,
            has_bot_name       = parsed.has_bot_name,
            has_prefix         = parsed.has_prefix,
            has_command_prefix = parsed.has_command_prefix,
            is_only_bot_name   = parsed.is_only_bot_name,
            is_at_me           = parsed.is_at_me,
            is_url_only        = parsed.is_url_only,
            event              = event,
            is_empty           = is_empty,
        )


# ============================================================
# Dispatcher 类
# ============================================================


class Dispatcher:
    """
    消息分发器

    负责接收 OneBot 消息事件并路由到对应的处理器：
    - 命令处理: 匹配命令触发词并执行对应 handler
    - 会话处理: 多轮对话支持
    - 闲聊处理: 无会话或命令消费时进行闲聊回落
    - URL 解析: clean_text 为单个 URL 时解析 URL
    """

    def __init__(
        self,
        router: CommandRouter,
        config_provider: ConfigProvider,
        plugin_registry: PluginRegistry,
        admin_check: AdminCheck,
        build_context: ContextFactory,
        semaphore: AdjustableSemaphore | asyncio.Semaphore | None,
        session_manager: SessionManager | None = None,
        metrics: MetricsCollector | None       = None,
        clock: IClock | None                   = None,
        random_gen: IRandom | None             = None,
        parser: MessageParser | None           = None,
    ) -> None:
        """
        初始化分发器

        Args:
            router: 命令路由器
            config_provider: 配置提供者
            plugin_registry: 插件注册表
            admin_check: 管理员权限检查
            build_context: 构建插件上下文的函数
            semaphore: 并发控制信号量（可选，测试时可为 None）
            session_manager: 会话管理器（可选）
        """
        self.router          = router
        self.config_provider = config_provider
        self.plugin_registry = plugin_registry
        self.admin_check     = admin_check
        self.build_context   = build_context
        self.semaphore       = semaphore
        self.session_manager = session_manager
        self.metrics         = metrics
        self.clock           = clock or SystemClock()
        self.random          = random_gen or SystemRandom()
        if parser is None:
            self.parser = MessageParser(config_provider)
        else:
            self.parser = parser
            self.refresh_prefix_cache()

        # 静音管理：{group_id: unmute_time}
        self._muted_groups: dict[int, float] = {}

    # ============================================================
    # 公开 API
    # ============================================================

    async def handle_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """
        处理事件（入口方法，带并发控制）

        Args:
            event: OneBot 事件

        Returns:
            OneBot 消息段列表
        """
        if event.get("post_type") != "message":
            logger.debug(
                "Drop non-message event: post_type=%s message_type=%s",
                event.get("post_type"),
                event.get("message_type"),
            )
            return []

        event_data = self._validate_event(event)
        if event_data is None:
            return []
        # 任意后续消息都顺带回收所有过期静音项，避免只在原群再次发言时
        # 才清理对应条目。
        self._prune_expired_mutes()

        # Use semaphore if available (for concurrency control)
        if self.semaphore:
            async with self.semaphore:
                return await self._process_event(event_data)
        else:
            # No semaphore (e.g., in tests)
            return await self._process_event(event_data)

    @staticmethod
    def _validate_event(event: dict[str, Any]) -> dict[str, Any] | None:
        if isinstance(event, ValidatedInboundEvent):
            return event
        try:
            validated = OneBotEvent.model_validate(event)
        except ValidationError as exc:
            logger.warning("Invalid OneBot event: %s", exc)
            return None
        return ValidatedInboundEvent(validated.model_dump())

    # ============================================================
    # 静音控制
    # ============================================================

    def mute_group(self, group_id: int, duration_minutes: float) -> None:
        """
        让机器人在指定群静音一段时间

        静音期间：
        - 跳过 smalltalk 回落
        - 仍响应命令、只喊名字、主动 @ 和活跃会话
        """
        if type(group_id) is not int or group_id <= 0:
            raise ValueError("group_id must be a positive integer")
        if (
            isinstance(duration_minutes, bool)
            or not isinstance(duration_minutes, (int, float))
            or not math.isfinite(float(duration_minutes))
            or duration_minutes <= 0
        ):
            raise ValueError("duration_minutes must be a positive finite number")
        duration = float(duration_minutes)
        now      = self.clock.now()
        self._prune_expired_mutes(now)
        unmute_time                  = now + duration * constants.SECONDS_PER_MINUTE
        self._muted_groups[group_id] = unmute_time
        logger.info("Group %s muted for %.1f minutes", group_id, duration)

    def unmute_group(self, group_id: int) -> bool:
        """解除群静音"""
        if group_id in self._muted_groups:
            del self._muted_groups[group_id]
            logger.info("Group %s unmuted", group_id)
            return True
        return False

    def is_muted(self, group_id: int | None) -> bool:
        """检查群是否在静音中"""
        if group_id is None:
            return False  # 私聊不受静音影响
        self._prune_expired_mutes()
        return group_id in self._muted_groups

    def get_mute_remaining(self, group_id: int) -> float:
        """获取剩余静音时间（分钟）"""
        if not self._muted_groups:
            return 0
        now = self.clock.now()
        self._prune_expired_mutes(now)
        if group_id not in self._muted_groups:
            return 0
        remaining = self._muted_groups[group_id] - now
        return max(0, remaining / constants.SECONDS_PER_MINUTE)

    def _prune_expired_mutes(self, now: float | None = None) -> None:
        """回收所有已到期静音项；调用点均无 await，字典更新不会交错。"""

        if not self._muted_groups:
            return
        current = self.clock.now() if now is None else now
        expired = [
            group_id
            for group_id, unmute_time in self._muted_groups.items()
            if current >= unmute_time
        ]
        for group_id in expired:
            del self._muted_groups[group_id]
            logger.info("Group %s mute expired", group_id)

    # ============================================================
    # 核心处理流程
    # ============================================================

    async def _process_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Process a validated message event through the linear A-G flow."""
        # Step 0: parse
        # 空白回复在普通消息流中仍会被丢弃，但引导式会话需要它来接受
        # “直接回车使用默认值”。因此只在 dispatcher 内保留空白上下文，
        # 并在确认存在活跃会话后才允许继续。
        ctx = self.parser.parse(event, allow_empty_session_input=True)
        if ctx is None:
            return []

        logger.info(
            "[%s] Received: user=%s, group=%s, text_length=%s, command_prefix=%s, url_only=%s",
            ctx.request_id,
            ctx.user_id,
            ctx.group_id,
            len(ctx.text),
            ctx.has_command_prefix,
            ctx.is_url_only,
        )

        # Step A: process gate
        config           = self.config_provider.config
        require_bot_name = config.get("require_bot_name_in_group", True)
        should_process   = ctx.is_private or (not require_bot_name) or ctx.has_prefix
        session_checked  = False
        if (
            self.session_manager is not None
            and ctx.user_id is not None
            and not ctx.is_only_bot_name
        ):
            session         = await self.session_manager.peek(ctx.user_id, ctx.group_id)
            session_checked = True
            if session is not None:
                should_process     = True
                ctx.cached_session = session

        if ctx.is_empty and ctx.cached_session is None:
            logger.debug("[%s] Empty message has no active session", ctx.request_id)
            return []

        # Resolve commands before observation so command bodies never become
        # Xiaoqing chat memory.  Unaddressed group chatter is intentionally not
        # resolved and remains observable.
        resolved = None
        if should_process and not ctx.is_url_only and not ctx.is_only_bot_name:
            resolved = self.router.resolve(ctx.clean_text)

        unknown_command_short_circuit = should_process and self._is_unknown_command(
            ctx,
            resolved,
        )
        if (
            should_process
            and resolved is None
            and not unknown_command_short_circuit
            and not ctx.is_url_only
            and not ctx.is_only_bot_name
            and not session_checked
            and self.session_manager is not None
            and ctx.user_id is not None
        ):
            ctx.cached_session = await self.session_manager.peek(ctx.user_id, ctx.group_id)
            session_checked    = True

        observation_skip_reason = self._observation_skip_reason(ctx, resolved)
        if observation_skip_reason is None:
            await self._observe_message(ctx)
        else:
            logger.info(
                "[%s] Observer skipped sensitive input category=%s",
                ctx.request_id,
                observation_skip_reason,
            )

        if not should_process and ctx.is_url_only:
            logger.debug("[%s] URL skipped by group process gate", ctx.request_id)
            return []

        if not should_process and self._allow_plain_group_smalltalk(ctx):
            if self.is_muted(ctx.group_id):
                logger.debug("[%s] Group muted; skip smalltalk", ctx.request_id)
                return []
            provider = self._get_smalltalk_provider()
            logger.info("[%s] Handling as smalltalk (provider=%s)", ctx.request_id, provider)
            return await self._handle_smalltalk(ctx)

        if not should_process:
            return []

        # Step B: URL parser.  This must stay after the normal process and
        # mute gates so URL messages cannot create an unauthorised network path.
        if ctx.is_url_only:
            if self.is_muted(ctx.group_id):
                logger.debug("[%s] Group muted; skip URL parser", ctx.request_id)
                return []
            url = ctx.clean_text.strip()
            if self._is_blocked_url_target(url):
                logger.warning("[%s] Blocked suspicious URL", ctx.request_id)
                return []
            return await self._invoke_url_parser(ctx, url) or []

        # Step C: only bot_name
        if ctx.is_only_bot_name:
            logger.info("[%s] Handling bot name only", ctx.request_id)
            return await self._handle_bot_name_only(ctx)

        # Step D: session continuation. Active sessions are modal: their input
        # vocabulary can legitimately overlap global commands (for example
        # Jupyter ``help`` or an SSH username/command named ``codex``/``echo``).
        # A handler may return None explicitly to fall through to global routing.
        if (
            ctx.cached_session is None
            and not session_checked
            and self.session_manager is not None
            and ctx.user_id is not None
        ):
            ctx.cached_session = await self.session_manager.peek(ctx.user_id, ctx.group_id)
        if ctx.cached_session is not None:
            session_result = await self._try_handle_session(ctx)
            if session_result is not None:
                return session_result

        # Step E: command match
        if resolved:
            return await self._execute_command(resolved, ctx) or []

        # Step F: strict-/ unknown-command hint
        if self._is_unknown_command(ctx, resolved):
            cmd_name = ctx.clean_text.split()[0]
            safe_cmd = Dispatcher._truncate_text(cmd_name, max_len=20)
            logger.info("[%s] Unknown command: '%s'", ctx.request_id, cmd_name)
            return [
                {
                    "type": "text",
                    "data": {"text": f"❓ 未知命令: /{safe_cmd}\n💡 输入 /help 查看可用命令"},
                }
            ]

        # Step G: smalltalk fallback (mute blocks this step only)
        if self.is_muted(ctx.group_id):
            logger.debug("[%s] Group muted; skip smalltalk", ctx.request_id)
            return []
        provider = self._get_smalltalk_provider()
        logger.info("[%s] Handling as smalltalk (provider=%s)", ctx.request_id, provider)
        return await self._handle_smalltalk(ctx)

    # ============================================================
    # 命令处理
    # ============================================================

    def _principal_for_event(self, ctx: MessageContext) -> PluginPrincipal:
        # getattr_static 防止 Mock/动态代理凭空制造一个看似可调用的签发器属性。
        declared_issuer = inspect.getattr_static(
            self.admin_check,
            "issue_user_principal",
            None,
        )
        issuer = (
            getattr(self.admin_check, "issue_user_principal", None)
            if declared_issuer is not None
            else None
        )
        if callable(issuer):
            principal = issuer(
                ctx.event,
                user_id    = ctx.user_id,
                group_id   = ctx.group_id,
                is_private = ctx.is_private,
            )
            if not isinstance(principal, PluginPrincipal):
                raise TypeError("issue_user_principal must return PluginPrincipal")
            return principal
        role: PluginGroupRole = "unknown"
        sender                = ctx.event.get("sender")
        if ctx.group_id is not None and isinstance(sender, dict):
            sender_user_id = sender.get("user_id")
            try:
                sender_matches = (
                    sender_user_id is not None
                    and ctx.user_id is not None
                    and int(sender_user_id) == int(ctx.user_id)
                )
            except (TypeError, ValueError):
                sender_matches = False
            candidate_role = str(sender.get("role", "") or "").strip().lower()
            if sender_matches:
                if candidate_role == "owner":
                    role = "owner"
                elif candidate_role == "admin":
                    role = "admin"
                elif candidate_role == "member":
                    role = "member"
        return PluginPrincipal(
            kind         = "user" if ctx.user_id is not None else "lifecycle",
            user_id      = ctx.user_id,
            group_id     = ctx.group_id,
            is_bot_admin = self.admin_check.is_admin(ctx.user_id),
            is_private   = ctx.is_private,
            group_role   = role,
        )

    def _build_event_context(self, plugin_name: str, ctx: MessageContext) -> Any:
        return self.build_context(
            plugin_name,
            ctx.user_id,
            ctx.group_id,
            ctx.request_id,
            self._principal_for_event(ctx),
        )

    async def _execute_command(
        self,
        resolved: tuple[Any, str],
        ctx: MessageContext,
    ) -> list[dict[str, Any]] | None:
        """Execute a matched command (router-resolved)."""
        spec, args = resolved
        logger.info(
            "[%s] Command matched: %s.%s (args_length=%s)",
            ctx.request_id,
            spec.plugin,
            spec.name,
            len(args),
        )

        if spec.admin_only and not self.admin_check.is_admin(ctx.user_id):
            logger.warning("[%s] Permission denied for user %s", ctx.request_id, ctx.user_id)
            return [{"type": "text", "data": {"text": "权限不足"}}]

        context = self._build_event_context(spec.plugin, ctx)
        if spec.catalog is not None:
            invocation                 = resolve_catalog_invocation(spec.catalog, args)
            context.command_invocation = invocation
            required_context           = "private" if ctx.is_private else "group"
            if required_context not in invocation.node.contexts:
                return [{"type": "text", "data": {"text": "当前会话类型不支持此命令"}}]
            principal = context.principal
            if invocation.node.permission == "bot_admin" and not principal.is_bot_admin:
                return [{"type": "text", "data": {"text": "权限不足"}}]
            if invocation.node.permission == "group_admin" and not (
                principal.is_bot_admin or principal.can_manage_group(ctx.group_id)
            ):
                return [{"type": "text", "data": {"text": "需要当前群管理员或群主权限"}}]
        start_time = time.perf_counter()
        try:

            async def run_handler() -> list[dict[str, Any]]:
                return await call_plugin_callback(spec.handler, spec.name, args, ctx.event, context)

            # Command resolution captures one immutable plugin generation.
            # Looking up the registry again here could run an old handler
            # through a newer generation's open gate (or without a gate after
            # unload).  The spec itself therefore owns admission.
            result = await invoke_loaded_plugin(spec, run_handler)
            logger.info("[%s] Command completed", ctx.request_id)
            if self.metrics:
                await self.metrics.record_plugin_execution(
                    spec.plugin,
                    spec.name,
                    time.perf_counter() - start_time,
                    is_error=False,
                )
            return result
        except (PluginExecutionClosed, PluginExecutionTimeout, PluginExecutionUnavailable) as exc:
            logger.info("[%s] Command unavailable: %s", ctx.request_id, exc)
            return [{"type": "text", "data": {"text": "⚠️ 插件暂时不可用，请稍后重试"}}]
        except Exception as exc:
            if self.metrics:
                await self.metrics.record_plugin_execution(
                    spec.plugin,
                    spec.name,
                    time.perf_counter() - start_time,
                    is_error=True,
                )
            return public_error_response(
                context,
                exc,
                logger    = logger,
                component = f"dispatcher.command.{spec.plugin}.{spec.name}",
            )

    # ============================================================
    # 会话处理
    # ============================================================

    def _session_context_is_allowed(self, plugin_name: str, *, is_private: bool) -> bool:
        """Apply the plugin's published root-command contexts to continuations."""

        required_context = "private" if is_private else "group"
        roots            = tuple(
            root for root in self.router.get_command_catalog() if root.plugin == plugin_name
        )
        return not roots or any(required_context in root.contexts for root in roots)

    async def _try_handle_session(self, ctx: MessageContext) -> list[dict[str, Any]] | None:
        """
        尝试处理活跃会话

        Returns:
            会话处理结果，如果没有活跃会话返回 None
        """
        session_manager = self.session_manager
        if not session_manager:
            return None

        if ctx.user_id is None:
            return None

        user_id = ctx.user_id

        # A session can be replaced between this routing snapshot and the
        # transaction. Retry once instead of invoking it through a stale gate.
        for _attempt in range(2):
            observed = await session_manager.peek(user_id, ctx.group_id)
            if observed is None:
                return None
            expected_plugin_name = observed.plugin_name
            expected_session_id  = observed.session_id
            plugin               = self.plugin_registry.get(expected_plugin_name)
            context              = self._build_event_context(expected_plugin_name, ctx)

            async def close_plugin_session(
                session: Session,
                _plugin: Any               = plugin,
                _context: Any              = context,
                _expected_plugin_name: str = expected_plugin_name,
            ) -> None:
                if not _plugin or not hasattr(_plugin.module, "close_session"):
                    return
                try:
                    await call_plugin_callback(
                        _plugin.module.close_session,
                        ctx.event,
                        _context,
                        session,
                    )
                except Exception as exc:
                    public_error_message(
                        _context,
                        exc,
                        logger    = logger,
                        component = f"dispatcher.session_close.{_expected_plugin_name}",
                    )

            async def handle_active_session(
                session: Session,
                _expected_plugin_name: str = expected_plugin_name,
                _expected_session_id: str  = expected_session_id,
                _plugin: Any               = plugin,
                _context: Any              = context,
            ) -> list[dict[str, Any]] | None:
                """Run one continuation under gate -> per-key-lock ordering."""

                if (
                    session.plugin_name != _expected_plugin_name
                    or session.session_id != _expected_session_id
                ):
                    raise _SessionGenerationChanged

                logger.info(
                    "[%s] Session active: plugin=%s",
                    ctx.request_id,
                    _expected_plugin_name,
                )

                if not self._session_context_is_allowed(
                    _expected_plugin_name,
                    is_private=ctx.is_private,
                ):
                    await close_plugin_session(session)
                    await session_manager.delete(user_id, ctx.group_id)
                    logger.warning(
                        "[%s] Closed session outside published context plugin=%s context=%s",
                        ctx.request_id,
                        _expected_plugin_name,
                        "private" if ctx.is_private else "group",
                    )
                    return [
                        {
                            "type": "text",
                            "data": {"text": "当前会话类型不再受支持，会话已关闭"},
                        }
                    ]

                if self.plugin_registry.has_capability(
                    _expected_plugin_name,
                    "admin_sessions",
                ) is True and not self.admin_check.is_admin(user_id):
                    await close_plugin_session(session)
                    await session_manager.delete(user_id, ctx.group_id)
                    logger.warning(
                        "[%s] Revoked privileged session plugin=%s user=%s",
                        ctx.request_id,
                        _expected_plugin_name,
                        user_id,
                    )
                    return [{"type": "text", "data": {"text": "权限已变更，高权限会话已关闭"}}]

                # end_session()/delete() re-enters this same key from the same
                # operation task, so it cannot race another continuation.
                if ctx.text.strip().lower() in constants.EXIT_COMMANDS_SET:
                    await close_plugin_session(session)
                    await session_manager.delete(user_id, ctx.group_id)
                    logger.info("[%s] Session exited by user", ctx.request_id)
                    return [{"type": "text", "data": {"text": "已退出当前对话"}}]

                if _plugin and hasattr(_plugin.module, "handle_session"):
                    return await call_plugin_callback(
                        _plugin.module.handle_session,
                        ctx.clean_text,
                        ctx.event,
                        _context,
                        session,
                    )
                if _plugin and hasattr(_plugin.module, "handle"):
                    return await call_plugin_callback(
                        _plugin.module.handle,
                        "__session__",
                        ctx.clean_text,
                        ctx.event,
                        _context,
                    )
                return None

            async def run_transaction() -> list[dict[str, Any]] | None:
                return await session_manager.update(
                    user_id,
                    ctx.group_id,
                    handle_active_session,
                )

            try:
                if plugin is not None:
                    result = await invoke_loaded_plugin(plugin, run_transaction)
                else:
                    result = await run_transaction()
            except _SessionGenerationChanged:
                continue
            except (
                PluginExecutionClosed,
                PluginExecutionTimeout,
                PluginExecutionUnavailable,
            ) as exc:
                logger.info("[%s] Session unavailable: %s", ctx.request_id, exc)
                return [{"type": "text", "data": {"text": "⚠️ 会话插件暂时不可用，请稍后重试"}}]
            except Exception as exc:
                # The exception must escape SessionManager.update() first so
                # its isolated working copy is discarded.  Turning it into a
                # public response inside the callback would commit any partial
                # session mutations made before the failure.
                response = public_error_response(
                    context,
                    exc,
                    logger    = logger,
                    component = f"dispatcher.session.{expected_plugin_name}",
                )

                run_cleanup, run_delete_without_hook = _build_failed_session_transactions(
                    session_manager,
                    user_id             = user_id,
                    group_id            = ctx.group_id,
                    expected_generation = (expected_plugin_name, expected_session_id),
                    close_callback      = close_plugin_session,
                )

                try:
                    if plugin is not None:
                        await invoke_loaded_plugin(plugin, run_cleanup)
                    else:
                        await run_cleanup()
                except _SessionGenerationChanged:
                    pass
                except (
                    PluginExecutionClosed,
                    PluginExecutionTimeout,
                    PluginExecutionUnavailable,
                ):
                    # A closed/poisoned plugin gate cannot run its close hook,
                    # but the same-generation session still must be removed.
                    try:
                        await run_delete_without_hook()
                    except _SessionGenerationChanged:
                        pass
                return response
            if result is None or isinstance(result, list):
                return result
            logger.warning("[%s] Session handler returned an invalid result", ctx.request_id)
            return None
        return None

    # ============================================================
    # URL 处理
    # ============================================================

    async def _invoke_url_parser(
        self,
        ctx: MessageContext,
        url: str,
    ) -> list[dict[str, Any]] | None:
        """Invoke url_parser for a clean URL message after dispatcher gates."""
        plugin = self.plugin_registry.get("url_parser")
        if not plugin or not hasattr(plugin.module, "handle_url"):
            return None

        logger.info("[%s] URL detected: %s", ctx.request_id, redact_url_for_log(url))
        context = self._build_event_context("url_parser", ctx)

        try:

            async def run_url_parser() -> list[dict[str, Any]]:
                return await call_plugin_callback(plugin.module.handle_url, url, ctx.event, context)

            result = await invoke_loaded_plugin(plugin, run_url_parser)
            if result:
                logger.info("[%s] URL handled", ctx.request_id)
                return result
        except (PluginExecutionClosed, PluginExecutionTimeout, PluginExecutionUnavailable) as exc:
            logger.info("[%s] URL parser unavailable: %s", ctx.request_id, exc)
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger    = logger,
                component = "dispatcher.url_parser",
            )

        return None

    @staticmethod
    def _is_blocked_url_target(url: str) -> bool:
        try:
            validate_public_url(url)
        except UnsafeUrlError:
            return True
        return False

    # ============================================================
    # 闲聊处理
    # ============================================================

    async def _handle_bot_name_only(self, ctx: MessageContext) -> list[dict[str, Any]]:
        """
        处理只叫 bot_name 的情况
        """
        default_response = self.random.choice(constants.DEFAULT_BOT_NAME_RESPONSES_LIST)
        return await self._dispatch_to_provider(
            ctx,
            "call_bot_name_only",
            (),
            default_response=default_response,
        )

    async def _handle_smalltalk(self, ctx: MessageContext) -> list[dict[str, Any]]:
        """
        处理闲聊
        """
        return await self._dispatch_to_provider(
            ctx,
            "handle_smalltalk",
            (ctx.clean_text, ctx.event),
        )

    async def _observe_message(self, ctx: MessageContext) -> None:
        provider = self._get_smalltalk_provider()
        await self._call_provider(
            provider,
            "observe_message",
            ctx,
            (ctx.clean_text, ctx.event),
        )

    @staticmethod
    def _observation_skip_reason(
        ctx: MessageContext,
        resolved: tuple[Any, str] | None,
    ) -> str | None:
        """Classify structured inputs that must not enter chat memory."""
        if ctx.is_url_only:
            return "url"
        if resolved is not None:
            spec, _args = resolved
            return f"command:{getattr(spec, 'plugin', 'unknown')}"
        if ctx.has_command_prefix:
            return "command-prefix"
        if ctx.cached_session is not None:
            return f"session:{ctx.cached_session.plugin_name}"
        return None

    @staticmethod
    def _is_unknown_command(
        ctx: MessageContext,
        resolved: tuple[Any, str] | None,
    ) -> bool:
        """统一未知前缀命令的短路与提示条件，避免两个阶段发生漂移。"""

        return bool(
            resolved is None
            and ctx.has_command_prefix
            and ctx.clean_text
            and ctx.clean_text[0].isalpha()
        )

    async def _dispatch_to_provider(
        self,
        ctx: MessageContext,
        method_name: str,
        args: tuple[Any, ...],
        default_response: str | None = None,
    ) -> list[dict[str, Any]]:
        provider = self._get_smalltalk_provider()
        logger.debug("%s: provider=%s", method_name, provider)

        result = await self._call_provider(provider, method_name, ctx, args)
        if result is not None:
            return result

        # xiaoqing_chat 会维护人物、对话、图片和规划状态。它失败时切换到无状态的
        # 通用闲聊会绕过这些约束，甚至把错误回复当成小青发出，因此宁可本轮静默。
        if provider not in {"smalltalk", "xiaoqing_chat"}:
            result = await self._call_provider("smalltalk", method_name, ctx, args, fallback=True)
            if result is not None:
                return result

        if default_response:
            return [{"type": "text", "data": {"text": default_response}}]

        logger.debug("No %s handler available", method_name)
        return []

    async def _call_provider(
        self,
        provider: str,
        method_name: str,
        ctx: MessageContext,
        args: tuple[Any, ...],
        fallback: bool = False,
    ) -> list[dict[str, Any]] | None:
        plugin = self.plugin_registry.get(provider)
        if not plugin or not hasattr(plugin.module, method_name):
            return None

        context = None
        try:
            context = self._build_event_context(provider, ctx)
            method  = getattr(plugin.module, method_name)

            async def run_provider() -> Any:
                return await call_plugin_callback(method, *args, context)

            result = await invoke_loaded_plugin(plugin, run_provider)
            return result if result else []
        except (PluginExecutionClosed, PluginExecutionTimeout, PluginExecutionUnavailable) as exc:
            logger.info(
                "%s unavailable for plugin %s error_type=%s",
                method_name,
                provider,
                type(exc).__name__,
            )
            return None
        except Exception as exc:
            public_error_message(
                context or ctx,
                exc,
                logger    = logger,
                component = ("dispatcher.fallback_provider" if fallback else "dispatcher.provider"),
            )
            return None

    # ============================================================
    # 辅助方法
    # ============================================================

    def refresh_prefix_cache(self) -> None:
        self.parser.refresh_prefix_cache()

    def _get_smalltalk_provider(self) -> str:
        """获取配置的闲聊提供者"""
        plugins_config = self.config_provider.config.get("plugins", {})
        if not isinstance(plugins_config, Mapping):
            return "smalltalk"
        provider = plugins_config.get("smalltalk_provider", "smalltalk")
        return provider if isinstance(provider, str) and provider else "smalltalk"

    def _allow_plain_group_smalltalk(self, ctx: MessageContext) -> bool:
        if ctx.is_private or ctx.has_prefix or ctx.is_only_bot_name:
            return False
        return self._get_smalltalk_provider() == "xiaoqing_chat"

    @staticmethod
    def _truncate_text(text: str, max_len: int = constants.DEFAULT_LOG_TRUNCATE_LEN) -> str:
        """截断文本用于日志显示"""
        if len(text) > max_len:
            return text[:max_len] + "..."
        return text
