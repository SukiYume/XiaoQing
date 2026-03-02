# pyright: reportConstantRedefinition=false
"""
消息分发器

负责解析消息并路由到对应的插件命令。

处理流程:
1. 事件过滤 - 仅处理 message 类型事件
2. 消息解析 - 提取文本、user_id、group_id
3. URL 解析 - 全局监听 URL 并路由到 url_parser
4. 触发判断 - 判断是否处理消息
5. 命令路由 - 匹配命令并执行
6. 会话处理 - 多轮对话支持
7. 闲聊处理 - 无命令时进行闲聊
"""

from __future__ import annotations

import asyncio
import logging
import time
import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, TYPE_CHECKING

from . import constants
from .clock import IClock, IRandom, SystemClock, SystemRandom
from .metrics import MetricsCollector
from .interfaces import AdminCheck, ConfigProvider, ContextFactory, PluginRegistry
from .message import compile_bot_name_pattern, normalize_message, parse_text_command_context
from .models import OneBotEvent
from pydantic import ValidationError
from .router import CommandRouter

if TYPE_CHECKING:
    from .session import SessionManager

logger = logging.getLogger(__name__)



# ============================================================
# 数据类
# ============================================================

@dataclass
class MessageContext:
    """消息上下文，封装消息相关的所有信息"""
    request_id: str          # 请求追踪 ID
    text: str                # 原始文本
    clean_text: str          # 去除前缀后的文本
    user_id: int | None   # 用户 ID
    group_id: int | None  # 群 ID (私聊为 None)
    is_private: bool         # 是否私聊
    has_bot_name: bool       # 是否包含 bot_name
    has_prefix: bool         # 是否有命令前缀
    is_only_bot_name: bool   # 是否只叫 bot_name
    is_at_me: bool           # 是否 @ 了机器人
    event: dict[str, Any]    # 原始事件
    cached_session: Any = None  # 缓存的会话对象（避免 TOCTOU 竞争）

@dataclass
class ProcessDecision:
    """处理决策结果"""
    should_process: bool     # 是否处理消息
    smalltalk_mode: bool     # 是否以闲聊模式处理

class MessageParser:
    """解析消息事件并构建 MessageContext"""

    def __init__(self, config_provider: ConfigProvider) -> None:
        self._config_provider = config_provider
        self._prefix_cache_key: tuple[str, tuple[str, ...]] | None = None
        self._bot_name_pattern: re.Pattern[str] | None = None
        self._cached_bot_name: str = ""
        self._cached_prefixes: tuple[str, ...] = tuple()
        self.refresh_prefix_cache()

    def refresh_prefix_cache(self) -> None:
        config = self._config_provider.config
        bot_name = config.get("bot_name", "")
        prefixes = tuple(config.get("command_prefixes", ["/"]))

        cache_key = (bot_name, prefixes)
        if cache_key == self._prefix_cache_key:
            return

        self._prefix_cache_key = cache_key
        self._cached_bot_name = bot_name
        self._cached_prefixes = prefixes
        self._bot_name_pattern = compile_bot_name_pattern(bot_name)

    def parse(self, event: dict[str, Any]) -> MessageContext | None:
        """解析消息事件，构建消息上下文"""
        text, user_id, group_id = normalize_message(event)
        if not text:
            logger.debug(
                "Drop empty message: post_type=%s message_type=%s message_kind=%s",
                event.get("post_type"),
                event.get("message_type"),
                type(event.get("message")).__name__,
            )
            return None

        self.refresh_prefix_cache()
        bot_name = self._cached_bot_name
        prefixes = self._cached_prefixes
        self_id = str(event.get("self_id", ""))

        # 忽略来自自己的消息，防止循环触发
        if self_id and user_id and str(user_id) == self_id:
            return None

        is_at_me, clean_text, has_bot_name, has_prefix, is_only_bot_name = parse_text_command_context(
            text,
            event,
            bot_name=bot_name,
            prefixes=prefixes,
            self_id=self_id,
            bot_name_pattern=self._bot_name_pattern,
        )

        # 构建上下文
        return MessageContext(
            request_id=str(uuid.uuid4())[:8],
            text=text,
            clean_text=clean_text,
            user_id=user_id,
            group_id=group_id,
            is_private=group_id is None,
            has_bot_name=has_bot_name,
            has_prefix=has_prefix,
            is_only_bot_name=is_only_bot_name,
            is_at_me=is_at_me,
            event=event,
        )

class MessageHandler(Protocol):
    async def handle(
        self,
        ctx: MessageContext,
        smalltalk_mode: bool,
    ) -> list[dict[str, Any]] | None:
        ...

class BotNameHandler:
    def __init__(self, dispatcher: "Dispatcher") -> None:
        self._dispatcher = dispatcher

    async def handle(
        self,
        ctx: MessageContext,
        smalltalk_mode: bool,
    ) -> list[dict[str, Any]] | None:
        if not ctx.is_only_bot_name:
            return None
        logger.info("[%s] Handling bot name only", ctx.request_id)
        return await self._dispatcher._handle_bot_name_only(ctx)

class CommandHandler:
    def __init__(self, dispatcher: "Dispatcher") -> None:
        self._dispatcher = dispatcher

    async def handle(
        self,
        ctx: MessageContext,
        smalltalk_mode: bool,
    ) -> list[dict[str, Any]] | None:
        return await self._dispatcher._try_handle_command(ctx)

class SessionHandler:
    def __init__(self, dispatcher: "Dispatcher") -> None:
        self._dispatcher = dispatcher

    async def handle(
        self,
        ctx: MessageContext,
        smalltalk_mode: bool,
    ) -> list[dict[str, Any]] | None:
        return await self._dispatcher._try_handle_session(ctx)

class SmalltalkHandler:
    def __init__(self, dispatcher: "Dispatcher") -> None:
        self._dispatcher = dispatcher

    async def handle(
        self,
        ctx: MessageContext,
        smalltalk_mode: bool,
    ) -> list[dict[str, Any]] | None:
        if not smalltalk_mode:
            return None
        provider = self._dispatcher._get_smalltalk_provider()
        logger.info("[%s] Handling as smalltalk (provider=%s)", ctx.request_id, provider)
        return await self._dispatcher._handle_smalltalk(ctx)

# ============================================================
# Dispatcher 类
# ============================================================

class Dispatcher:
    """
    消息分发器
    
    负责接收 OneBot 消息事件并路由到对应的处理器：
    - 命令处理: 匹配命令触发词并执行对应 handler
    - 会话处理: 多轮对话支持
    - 闲聊处理: 无命令时进行闲聊
    - URL 解析: 自动检测并解析 URL
    """

    def __init__(
        self,
        router: CommandRouter,
        config_provider: ConfigProvider,
        plugin_registry: PluginRegistry,
        admin_check: AdminCheck,
        build_context: ContextFactory,
        semaphore: asyncio.Semaphore | None,
        session_manager: SessionManager | None = None,
        metrics: MetricsCollector | None = None,
        clock: IClock | None = None,
        random_gen: IRandom | None = None,
        parser: MessageParser | None = None,
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
        self.router = router
        self.config_provider = config_provider
        self.plugin_registry = plugin_registry
        self.admin_check = admin_check
        self.build_context = build_context
        self.semaphore = semaphore
        self.session_manager = session_manager
        self.metrics = metrics
        self.clock = clock or SystemClock()
        self.random = random_gen or SystemRandom()
        self.parser = parser or MessageParser(config_provider)
        self._handlers: tuple[MessageHandler, ...] = (
            BotNameHandler(self),
            CommandHandler(self),
            SessionHandler(self),
            SmalltalkHandler(self),
        )
        
        # 静音管理：{group_id: unmute_time}
        self._muted_groups: dict[int, float] = {}
        self.refresh_prefix_cache()

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

        # Use semaphore if available (for concurrency control)
        if self.semaphore:
            async with self.semaphore:
                return await self._process_event(event_data)
        else:
            # No semaphore (e.g., in tests)
            return await self._process_event(event_data)

    @staticmethod
    def _validate_event(event: dict[str, Any]) -> dict[str, Any] | None:
        try:
            validated = OneBotEvent.model_validate(event)
        except ValidationError as exc:
            logger.warning("Invalid OneBot event: %s", exc)
            return None
        return validated.model_dump()

    # ============================================================
    # 静音控制
    # ============================================================

    def mute_group(self, group_id: int, duration_minutes: float) -> None:
        """
        让机器人在指定群静音一段时间
        
        静音期间：
        - 不随机回复
        - 不主动闲聊
        - 但仍响应命令和主动 @ 
        """
        unmute_time = self.clock.now() + duration_minutes * constants.SECONDS_PER_MINUTE
        self._muted_groups[group_id] = unmute_time
        logger.info("Group %s muted for %.1f minutes", group_id, duration_minutes)

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
        
        if group_id not in self._muted_groups:
            return False
        
        # 检查是否过期
        unmute_time = self._muted_groups[group_id]
        if self.clock.now() >= unmute_time:
            del self._muted_groups[group_id]
            logger.info("Group %s mute expired", group_id)
            return False
        
        return True

    def get_mute_remaining(self, group_id: int) -> float:
        """获取剩余静音时间（分钟）"""
        if group_id not in self._muted_groups:
            return 0
        remaining = self._muted_groups[group_id] - self.clock.now()
        return max(0, remaining / constants.SECONDS_PER_MINUTE)

    # ============================================================
    # 核心处理流程
    # ============================================================

    async def _process_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """
        处理事件的核心逻辑
        
        处理流程:
        1. 消息解析
        2. URL 检测
        3. 触发判断
        4. 命令/会话/闲聊 处理
        """
        # Step 1: 解析消息
        msg_ctx = self.parser.parse(event)
        if not msg_ctx:
            return []
        
        logger.info(
            "[%s] Received: user=%s, group=%s, text='%s'",
            msg_ctx.request_id, msg_ctx.user_id, msg_ctx.group_id,
            self._truncate_text(msg_ctx.text, constants.DEFAULT_LOG_TRUNCATE_LEN)
        )

        await self._observe_message(msg_ctx)

        # Step 2: URL 检测（全局监听）
        url_result = await self._try_handle_url(msg_ctx)
        if url_result:
            return url_result

        # Step 3: 判断是否处理
        decision = self._decide_process(msg_ctx)
        should_process = decision.should_process
        
        # 特殊逻辑：如果有活跃会话，始终处理消息（除非是命令前缀的情况，前面已经 covered）
        # 这允许用户在会话中直接输入内容而不必 @机器人
        # M3: 使用单次 get() 代替 exists() + get() 避免 TOCTOU 竞争
        if not should_process and self.session_manager and msg_ctx.user_id:
            # 只有当消息不是纯粹的 bot 名字时才检查会话（防止打断 invoke bot）
            if not msg_ctx.is_only_bot_name:
                session = await self.session_manager.get(msg_ctx.user_id, msg_ctx.group_id)
                if session:
                    should_process = True
                    msg_ctx.cached_session = session
                    # 会话模式下不强制闲聊
        
        if not should_process:
            return []

        # Step 4: 分类处理
        return await self._dispatch(msg_ctx, decision.smalltalk_mode)

    def _decide_process(self, ctx: MessageContext) -> ProcessDecision:
        """
        判断是否处理消息
        
        返回 (should_process, smalltalk_mode)
        """
        config = self.config_provider.config
        
        # 私聊始终处理，可闲聊
        if ctx.is_private:
            return ProcessDecision(True, True)

        # 群聊中...
        require_bot_name = config.get("require_bot_name_in_group", True)
        random_reply_rate = config.get("random_reply_rate", 0.05)
        is_muted = self.is_muted(ctx.group_id)

        # 有命令前缀时始终处理（静音不影响命令）
        if ctx.has_prefix:
            return ProcessDecision(True, False)

        # 有 bot_name 或 @机器人 时处理
        if ctx.has_bot_name or ctx.is_at_me:
            # 静音时不闲聊，只处理命令
            return ProcessDecision(True, not is_muted)

        # 不要求 bot_name 时处理
        if not require_bot_name:
            return ProcessDecision(True, False)

        # 静音模式下不随机回复
        if is_muted:
            return ProcessDecision(False, False)

        # 特殊处理 xiaoqing_chat 提供者
        # xiaoqing_chat 有自己的频率控制和回复概率判断，
        # 所有消息都进入 smalltalk 处理，由插件自己决定是否回复。
        # 因此不使用 random_reply_rate，直接返回处理。
        if self._get_smalltalk_provider() == "xiaoqing_chat":
            return ProcessDecision(True, True)

        # 其他 smalltalk 提供者（如 smalltalk）使用 random_reply_rate 控制随机回复
        if random_reply_rate > 0 and self.random.random() < random_reply_rate:
            return ProcessDecision(True, True)

        return ProcessDecision(False, False)

    async def _dispatch(self, ctx: MessageContext, smalltalk_mode: bool) -> list[dict[str, Any]]:
        """
        分发消息到对应的处理器
        
        处理优先级:
        1. 只叫 bot_name
        2. 命令匹配
        3. 活跃会话
        4. 闲聊
        """
        for handler in self._handlers:
            result = await handler.handle(ctx, smalltalk_mode)
            if result is not None:
                return result

        logger.debug("[%s] No action taken", ctx.request_id)
        return []

    # ============================================================
    # 命令处理
    # ============================================================

    async def _try_handle_command(self, ctx: MessageContext) -> list[dict[str, Any]] | None:
        """
        尝试匹配并执行命令
        
        Returns:
            命令执行结果，如果未匹配返回 None
        """
        resolved = self.router.resolve(ctx.clean_text)
        if not resolved:
            # 如果有命令前缀但没匹配到命令，给出提示
            if ctx.has_prefix and ctx.clean_text:
                # 检查是否像是一个命令（以字母或中文开头）
                first_char = ctx.clean_text[0]
                if first_char.isalpha() or '\u4e00' <= first_char <= '\u9fff':
                    cmd_name = ctx.clean_text.split()[0] if ctx.clean_text else ctx.clean_text
                    logger.info(
                        "[%s] Unknown command: '%s'",
                        ctx.request_id, cmd_name
                    )
                    # 提供更详细的错误消息，包含用户输入的命令
                    safe_cmd = Dispatcher._truncate_text(cmd_name, max_len=20)
                    return [{
                        "type": "text",
                        "data": {"text": f"❓ 未知命令: /{safe_cmd}\n💡 输入 /help 查看可用命令"}
                    }]
            return None
        
        spec, args = resolved
        logger.info(
            "[%s] Command matched: %s.%s (args='%s')",
            ctx.request_id, spec.plugin, spec.name, args
        )

        # 权限检查
        if spec.admin_only and not self.admin_check.is_admin(ctx.user_id):
            logger.warning(
                "[%s] Permission denied for user %s",
                ctx.request_id, ctx.user_id
            )
            return [{"type": "text", "data": {"text": "权限不足"}}]

        # 执行命令
        context = self.build_context(spec.plugin, ctx.user_id, ctx.group_id, ctx.request_id)
        start_time = time.perf_counter()
        try:
            result = await spec.handler(spec.name, args, ctx.event, context)
            logger.info("[%s] Command completed", ctx.request_id)
            if self.metrics:
                await self.metrics.record_plugin_execution(
                    spec.plugin,
                    spec.name,
                    time.perf_counter() - start_time,
                    is_error=False,
                )
            return result
        except Exception as exc:
            logger.exception("[%s] Command failed: %s", ctx.request_id, exc)
            if self.metrics:
                await self.metrics.record_plugin_execution(
                    spec.plugin,
                    spec.name,
                    time.perf_counter() - start_time,
                    is_error=True,
                )
            return [{"type": "text", "data": {"text": "⚠️ 命令执行出错，请联系管理员查看日志"}}]

    # ============================================================
    # 会话处理
    # ============================================================

    async def _try_handle_session(self, ctx: MessageContext) -> list[dict[str, Any]] | None:
        """
        尝试处理活跃会话
        
        Returns:
            会话处理结果，如果没有活跃会话返回 None
        """
        if not self.session_manager:
            return None

        if ctx.user_id is None:
            return None

        user_id = ctx.user_id
        # M3: 优先使用缓存的会话对象，避免重复查询和 TOCTOU 竞争
        session = ctx.cached_session or await self.session_manager.get(user_id, ctx.group_id)
        if not session:
            return None
        
        logger.info(
            "[%s] Session active: plugin=%s",
            ctx.request_id, session.plugin_name
        )

        # 检查退出命令
        if ctx.text.strip().lower() in constants.EXIT_COMMANDS_SET:
            await self.session_manager.delete(user_id, ctx.group_id)
            logger.info("[%s] Session exited by user", ctx.request_id)
            return [{"type": "text", "data": {"text": "已退出当前对话"}}]

        # 路由到会话插件
        context = self.build_context(session.plugin_name, user_id, ctx.group_id, ctx.request_id)
        try:
            plugin = self.plugin_registry.get(session.plugin_name)
            if plugin and hasattr(plugin.module, "handle_session"):
                return await plugin.module.handle_session(
                    ctx.clean_text, ctx.event, context, session
                )
            elif plugin and hasattr(plugin.module, "handle"):
                return await plugin.module.handle(
                    "__session__", ctx.clean_text, ctx.event, context
                )
        except Exception as exc:
            logger.exception("[%s] Session handler failed: %s", ctx.request_id, exc)
            await self.session_manager.delete(user_id, ctx.group_id)
            return [{"type": "text", "data": {"text": "⚠️ 对话处理出错，请联系管理员查看日志"}}]
        
        return None

    # ============================================================
    # URL 处理
    # ============================================================

    async def _try_handle_url(self, ctx: MessageContext) -> list[dict[str, Any]] | None:
        """
        尝试检测并处理 URL
        
        仅当消息中包含 URL 且没有命令前缀时触发
        """
        # 有命令前缀时不处理 URL（避免干扰命令）
        if ctx.has_prefix:
            return None
        
        # 检测 URL
        url_match = re.search(r'https?://[^\s]+', ctx.text)
        if not url_match:
            return None
        
        # 静音时不处理
        if self.is_muted(ctx.group_id):
            return None
        
        url = url_match.group()
        plugin = self.plugin_registry.get("url_parser")
        if not plugin or not hasattr(plugin.module, "handle_url"):
            return None
        
        logger.info("[%s] URL detected: %s", ctx.request_id, url)
        context = self.build_context("url_parser", ctx.user_id, ctx.group_id, ctx.request_id)
        
        try:
            result = await plugin.module.handle_url(url, ctx.event, context)
            if result:
                logger.info("[%s] URL handled", ctx.request_id)
                return result
        except Exception as exc:
            logger.error("[%s] URL handling failed: %s", ctx.request_id, exc)
        
        return None

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
        try:
            await self._call_provider(
                provider,
                "observe_message",
                ctx,
                (ctx.clean_text, ctx.event),
            )
        except Exception:
            return None

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

        if provider != "smalltalk":
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

        try:
            context = self.build_context(provider, ctx.user_id, ctx.group_id, ctx.request_id)
            method = getattr(plugin.module, method_name)
            result = method(*args, context)
            if asyncio.iscoroutine(result):
                result = await result
            return result if result else []
        except Exception as exc:
            label = "Fallback " if fallback else ""
            logger.warning("%s%s failed: %s", label, method_name, exc)
            return None

    # ============================================================
    # 辅助方法
    # ============================================================

    def refresh_prefix_cache(self) -> None:
        self.parser.refresh_prefix_cache()

    def _get_smalltalk_provider(self) -> str:
        """获取配置的闲聊提供者"""
        plugins_config = self.config_provider.config.get("plugins", {})
        return plugins_config.get("smalltalk_provider", "smalltalk")

    @staticmethod
    def _truncate_text(text: str, max_len: int = constants.DEFAULT_LOG_TRUNCATE_LEN) -> str:
        """截断文本用于日志显示"""
        if len(text) > max_len:
            return text[:max_len] + "..."
        return text
