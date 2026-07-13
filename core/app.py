"""
XiaoQing 主应用

简化后的核心应用模块。
"""

import asyncio
import functools
import logging
import time
import weakref
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import aiohttp

from .capabilities import (
    ChatReplyService,
    CodexArxivSummaryService,
    ConfigSubscriptionService,
    OneBotMediaService,
    SecretAdminService,
    VoiceSynthesisService,
)
from .config import ConfigManager, ConfigSnapshot, _freeze_config_mapping
from .constants import (
    DEFAULT_HTTP_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_INBOUND_WS_QUEUE_SIZE,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_SESSION_TIMEOUT_SEC,
    INBOUND_EVENT_DEDUP_TTL_SECONDS,
    MAX_INBOUND_EVENT_DEDUP_KEYS,
    MAX_MESSAGE_PREVIEW_LENGTH,
    MESSAGE_SPLIT_DELAY,
)
from .context import PluginContext
from .dispatcher import Dispatcher
from .interfaces import DeliveryTarget, PluginCapabilities, PluginPrincipal
from .logging_config import LogManager, setup_logging
from .metrics import MetricsCollector
from .onebot import OneBotHttpSender, OneBotWsClient, _extract_message_preview
from .plugin_base import build_action, segments, split_message_segments
from .plugin_execution import (
    PluginExecutionClosed,
    PluginExecutionTimeout,
    PluginExecutionUnavailable,
    call_plugin_callback,
    invoke_loaded_plugin,
)
from .plugin_manager import PluginManager
from .router import CommandRouter
from .scheduler import SchedulerManager
from .server import InboundManager
from .session import SessionManager

logger = logging.getLogger(__name__)

Action = dict[str, Any]
ActionSink = Callable[[Action], Awaitable[None]]
current_action_sink: ContextVar[ActionSink | None] = ContextVar("current_action_sink", default=None)


def _build_shared_http_timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(
        total=DEFAULT_HTTP_TIMEOUT_SECONDS,
        connect=DEFAULT_HTTP_CONNECT_TIMEOUT_SECONDS,
    )


class _PrincipalAuthority:
    """Track principals minted by one Application instance by object identity."""

    def __init__(self) -> None:
        self._issued: weakref.WeakKeyDictionary[PluginPrincipal, str] = weakref.WeakKeyDictionary()

    def issue(
        self,
        *,
        kind: str,
        user_id: int | None = None,
        group_id: int | None = None,
        is_bot_admin: bool = False,
        is_private: bool = False,
        group_role: str = "unknown",
        delivery_targets: tuple[DeliveryTarget, ...] | None = None,
    ) -> PluginPrincipal:
        if delivery_targets is None:
            if kind == "user" and group_id is not None:
                delivery_targets = (DeliveryTarget("group", int(group_id)),)
            elif kind == "user" and user_id is not None:
                delivery_targets = (DeliveryTarget("private", int(user_id)),)
            else:
                delivery_targets = ()
        principal = PluginPrincipal(
            kind=kind,  # type: ignore[arg-type]
            user_id=user_id,
            group_id=group_id,
            is_bot_admin=is_bot_admin,
            is_private=is_private,
            group_role=group_role,  # type: ignore[arg-type]
            delivery_targets=delivery_targets,
        )
        self._issued[principal] = kind
        return principal

    def owns(self, principal: PluginPrincipal) -> bool:
        return self._issued.get(principal) == principal.kind


class XiaoQingApp:
    """XiaoQing 主应用类"""

    def __init__(
        self,
        root: Path,
        config_manager: ConfigManager | None = None,
        router: CommandRouter | None = None,
        plugin_manager: PluginManager | None = None,
        dispatcher: Dispatcher | None = None,
        scheduler: SchedulerManager | None = None,
        session_manager: SessionManager | None = None,
    ) -> None:
        self.root: Path = root

        # 加载配置
        self.config_manager: ConfigManager = config_manager or ConfigManager(
            root / "config" / "config.json",
            root / "config" / "secrets.json",
        )

        # 初始化日志系统（使用新的日志模块）
        self.log_manager: LogManager = setup_logging(
            self.config_manager.config,
            log_dir=root / "logs",
        )

        # HTTP 会话（共享）
        self.http_session: aiohttp.ClientSession | None = None

        # OneBot 通信
        self.http_sender: OneBotHttpSender | None = None
        self.ws_client: OneBotWsClient | None = None
        self.inbound_manager: InboundManager | None = None
        self._admin_set: set[int] = set()
        self._principal_authority = _PrincipalAuthority()

        # 核心组件
        self.router: CommandRouter = router or CommandRouter()
        self.plugins_dir: Path = root / "plugins"
        poll_interval = float(self.config_manager.config.get("plugin_poll_interval", 3600))
        context_factory = self._build_plugin_context
        manager_factory = PluginManager
        self.plugin_manager: PluginManager = plugin_manager or manager_factory(
            self.plugins_dir,
            self.router,
            context_factory,
            poll_interval=poll_interval,
        )
        self._configure_plugin_execution(self.config_manager.config)
        self.scheduler: SchedulerManager = scheduler or SchedulerManager(
            self.config_manager.config.get("timezone", "Asia/Shanghai")
        )
        self.metrics: MetricsCollector = MetricsCollector()

        # 会话管理器（用于多轮对话）
        session_timeout = float(
            self.config_manager.config.get("session_timeout", DEFAULT_SESSION_TIMEOUT_SEC)
        )
        self.session_manager: SessionManager = session_manager or SessionManager(
            default_timeout=session_timeout
        )

        # 消息分发器
        concurrency = int(
            self.config_manager.config.get("max_concurrency", DEFAULT_MAX_CONCURRENCY)
        )
        self._dispatcher_concurrency = concurrency
        # Create Semaphore - if no event loop, defer creation
        try:
            semaphore = asyncio.Semaphore(concurrency)
        except RuntimeError:
            # No event loop running - will be created later or mocked in tests
            semaphore = None  # type: ignore

        self.dispatcher: Dispatcher = dispatcher or Dispatcher(
            self.router,
            self,
            self.plugin_manager,
            self,
            self.plugin_manager.build_context,
            semaphore,
            self.session_manager,
            self.metrics,
        )

        self._load_admins()

        self._session_cleanup_task: asyncio.Task[None] | None = None
        self._reload_lock: asyncio.Lock | None = None
        self._reload_task: asyncio.Task[None] | None = None
        self._ws_client_task: asyncio.Task[None] | None = None
        self._config_watch_task: asyncio.Task[None] | None = None
        self._plugin_watch_task: asyncio.Task[None] | None = None
        self._config_apply_task: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_shutdown_errors: tuple[str, ...] = ()
        self._last_connect_notification_ts: float = 0.0
        self._recent_event_ids: dict[str, float] = {}
        self._event_dedupe_lock: asyncio.Lock | None = None

        # 注册回调
        self.plugin_manager.on_change(self._reschedule)
        self.config_manager.on_reload(self._apply_config)

    def _ensure_reload_lock(self) -> asyncio.Lock:
        """Ensure reload lock is initialized (requires event loop)"""
        if self._reload_lock is None:
            self._reload_lock = asyncio.Lock()
        return self._reload_lock

    def _configure_plugin_execution(self, config: dict[str, Any]) -> None:
        configure = getattr(self.plugin_manager, "configure_execution", None)
        if callable(configure):
            configure(config.get("plugin_execution", {}))

    def _plugin_watch_enabled(self, config: dict[str, Any] | None = None) -> bool:
        source = config if config is not None else self.config
        return bool(source.get("enable_plugin_watcher", False))

    def _plugin_watch_poll_interval(self, config: dict[str, Any] | None = None) -> float:
        source = config if config is not None else self.config
        try:
            return float(source.get("plugin_poll_interval", 3600))
        except (TypeError, ValueError):
            return 3600.0

    def _watch_runtime_active(self) -> bool:
        return self._config_watch_task is not None

    def _configure_plugin_watch(self, config: dict[str, Any] | None = None) -> None:
        if self._stopping:
            logger.debug("Ignoring plugin watcher configuration while stopping")
            return
        self.plugin_manager.update_poll_interval(self._plugin_watch_poll_interval(config))
        if not self._watch_runtime_active():
            return

        if self._plugin_watch_enabled(config):
            if self._plugin_watch_task is None or self._plugin_watch_task.done():
                self._plugin_watch_task = asyncio.create_task(self.plugin_manager.watch())
            return

        task = self._plugin_watch_task
        if task is not None and not task.done():
            task.cancel()
        self._plugin_watch_task = None

    # ============================================================
    # 属性代理（供 Dispatcher 使用）
    # ============================================================

    @property
    def config(self) -> dict[str, Any]:
        return self.config_manager.config

    @property
    def secrets(self) -> dict[str, Any]:
        return self.config_manager.secrets

    def is_admin(self, user_id: int | None) -> bool:
        if not user_id:
            return False
        return int(user_id) in self._admin_set

    def issue_user_principal(
        self,
        event: dict[str, Any],
        *,
        user_id: int | None,
        group_id: int | None,
        is_private: bool,
    ) -> PluginPrincipal:
        """Mint a user principal from one authenticated OneBot event."""

        role = "unknown"
        sender = event.get("sender")
        if group_id is not None and isinstance(sender, dict):
            sender_user_id = sender.get("user_id")
            try:
                sender_matches = (
                    sender_user_id is not None
                    and user_id is not None
                    and int(sender_user_id) == int(user_id)
                )
            except (TypeError, ValueError):
                sender_matches = False
            candidate_role = str(sender.get("role", "") or "").strip().lower()
            if sender_matches and candidate_role in {"owner", "admin", "member"}:
                role = candidate_role
        return self._principal_authority.issue(
            kind="user",
            user_id=user_id,
            group_id=group_id,
            is_bot_admin=self.is_admin(user_id),
            is_private=is_private,
            group_role=role,
        )

    def _load_admins(self, secrets: dict[str, Any] | None = None) -> None:
        source = secrets if secrets is not None else self.secrets
        raw_ids = source.get("admin_user_ids", [])
        try:
            self._admin_set = {int(x) for x in raw_ids}
        except (TypeError, ValueError):
            logger.warning("Invalid admin_user_ids in secrets")
            self._admin_set = set()

    # ============================================================
    # 生命周期
    # ============================================================

    async def start(self) -> None:
        """启动应用"""
        if self._stopping:
            raise RuntimeError("Cannot start an application that is stopping or stopped")

        # M1: 延迟初始化 Semaphore（确保运行在事件循环中）
        if self.dispatcher.semaphore is None:
            concurrency = int(self.config.get("max_concurrency", DEFAULT_MAX_CONCURRENCY))
            self.dispatcher.semaphore = asyncio.Semaphore(concurrency)

        # 初始化 HTTP 会话
        self.http_session = aiohttp.ClientSession(timeout=_build_shared_http_timeout())

        # 初始化 HTTP 发送器（可选）
        http_base = str(self.config.get("onebot_http_base", "") or "").strip()
        if http_base:
            self.http_sender = OneBotHttpSender(
                http_base,
                self.secrets.get("onebot_token", ""),
                self.http_session,
            )
        else:
            self.http_sender = None
            logger.info("HTTP sender disabled (onebot_http_base is empty)")

        # 加载插件
        self.plugin_manager.load_all()
        await self.plugin_manager.wait_inits()
        self._reschedule("startup")

        self._session_cleanup_task = asyncio.create_task(self._cleanup_sessions_loop())
        self._config_watch_task = asyncio.create_task(self.config_manager.watch())
        self._configure_plugin_watch(self.config)

        # 可选：启动 WebSocket 客户端（连接到 OneBot 服务端）
        if self.config.get("enable_ws_client", True):
            ws_uri = self.config.get("onebot_ws_uri", "")
            if ws_uri:
                ws_queue_size_raw = self.config.get("ws_queue_size", DEFAULT_INBOUND_WS_QUEUE_SIZE)
                try:
                    ws_queue_size = int(ws_queue_size_raw)
                except (TypeError, ValueError):
                    ws_queue_size = DEFAULT_INBOUND_WS_QUEUE_SIZE
                self.ws_client = OneBotWsClient(
                    ws_uri,
                    self.secrets.get("onebot_token", ""),
                    queue_size=ws_queue_size,
                )
                self.ws_client.set_on_connect(self._on_ws_connected)
                self._ws_client_task = asyncio.create_task(
                    self.ws_client.connect_and_listen(self._handle_upstream_event)
                )
                logger.info("WebSocket client enabled, connecting to %s", ws_uri)
            else:
                logger.warning("WebSocket client enabled but onebot_ws_uri is empty")
        else:
            logger.info("WebSocket client disabled")

        # 可选：启动 HTTP/WS 服务端（接收外部请求）
        self.inbound_manager = InboundManager.from_config(
            config=self.config,
            token=self.secrets.get("inbound_token", ""),
            handler=self._handle_inbound_event,
        )
        if self.inbound_manager:
            self._bind_inbound_status_providers(self.inbound_manager)
            await self.inbound_manager.start()

    async def stop(self) -> None:
        """优雅停止应用，并让并发调用共享同一次关停。"""
        if self._shutdown_task is None:
            # 先冻结所有会重建运行时组件的入口，再异步执行逐阶段清理。
            self._stopping = True
            self._shutdown_task = asyncio.create_task(self._stop_async())

        if self._shutdown_task is asyncio.current_task():
            return
        await asyncio.shield(self._shutdown_task)

    async def _stop_async(self) -> None:
        """按固定顺序关闭资源；一个阶段失败不影响其余阶段。"""
        logger.info("Shutting down XiaoQing...")
        errors: list[str] = []

        # 1. 先关闭所有入站入口，避免关停过程中接收新的请求。
        await self._run_shutdown_step("inbound server", self._stop_inbound_manager, errors)
        await self._run_shutdown_step("WebSocket client", self._stop_ws_client, errors)
        await self._run_shutdown_step(
            "WebSocket listener task",
            lambda: self._cancel_task("_ws_client_task"),
            errors,
        )

        # 2. 冻结配置、重载和后台维护任务，防止它们重建已关闭的组件。
        for attr_name in (
            "_config_watch_task",
            "_plugin_watch_task",
            "_config_apply_task",
            "_reload_task",
            "_session_cleanup_task",
        ):
            await self._run_shutdown_step(
                f"background task {attr_name}",
                lambda attr_name=attr_name: self._cancel_task(attr_name),
                errors,
            )

        # 3. 先停止调度，之后再卸载可能被任务引用的插件。
        await self._run_shutdown_step("scheduler", self._stop_scheduler, errors)
        await self._unload_plugins_for_shutdown(errors)

        # 4. 最后释放共享连接。引用先清空，防止并发回调继续复用半关闭对象。
        await self._run_shutdown_step("HTTP session", self._close_http_session, errors)

        self._last_shutdown_errors = tuple(errors)
        if errors:
            logger.warning(
                "XiaoQing shutdown completed with %d cleanup error(s): %s",
                len(errors),
                "; ".join(errors),
            )
        else:
            logger.info("XiaoQing shutdown complete")

    async def _run_shutdown_step(
        self,
        name: str,
        operation: Callable[[], Awaitable[None]],
        errors: list[str],
    ) -> None:
        """运行一个关停阶段，并把错误记入汇总而不是中断关停。"""
        try:
            await operation()
        except BaseException as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            logger.exception("Shutdown step %s failed", name)

    async def _stop_inbound_manager(self) -> None:
        manager = self.inbound_manager
        self.inbound_manager = None
        if manager:
            await manager.stop()
            logger.info("Inbound server stopped")

    async def _stop_ws_client(self) -> None:
        client = self.ws_client
        self.ws_client = None
        if client:
            await client.stop()
            logger.info("WebSocket client stopped")

    async def _stop_scheduler(self) -> None:
        if self.scheduler.scheduler:
            self.scheduler.scheduler.shutdown(wait=True)
        logger.info("Scheduler stopped")

    async def _unload_plugins_for_shutdown(self, errors: list[str]) -> None:
        try:
            plugin_names = list(self.plugin_manager.list_plugins())
        except BaseException as exc:
            errors.append(f"plugin list: {type(exc).__name__}: {exc}")
            logger.exception("Could not enumerate plugins during shutdown")
            return

        for name in plugin_names:
            await self._run_shutdown_step(
                f"plugin {name}",
                lambda name=name: self.plugin_manager.unload_plugin(name),
                errors,
            )
        remaining = list(self.plugin_manager.list_plugins())
        if remaining:
            message = "plugin drain incomplete; quarantined callbacks still running: " + ", ".join(
                remaining
            )
            errors.append(message)
            logger.warning(message)
        else:
            logger.info("All plugins unloaded (%d total)", len(plugin_names))

    async def _close_http_session(self) -> None:
        session = self.http_session
        self.http_session = None
        self.http_sender = None
        if session:
            await session.close()
            logger.info("HTTP session closed")

    async def _cancel_task(self, attr_name: str) -> None:
        task = getattr(self, attr_name, None)
        if not task:
            return
        if task is asyncio.current_task():
            logger.warning("Skipped cancelling current task %s during shutdown", attr_name)
            setattr(self, attr_name, None)
            return
        try:
            task.cancel()
            await task
        except asyncio.CancelledError:
            pass
        finally:
            setattr(self, attr_name, None)

    async def _cleanup_sessions_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.session_manager.cleanup_expired()
            except Exception as exc:
                logger.warning("Session cleanup failed: %s", exc)

    # ============================================================
    # 事件处理
    # ============================================================

    async def _on_ws_connected(self) -> None:
        """WebSocket 连接成功回调"""
        ws_client = self.ws_client
        if not ws_client:
            return
        # 获取 default 群列表
        default_groups = self.config.get("default_group_ids", [])
        if not default_groups:
            logger.info("No default groups configured, skipping connect notification")
            return

        # 发送上线通知（可通过 config 配置）
        connect_msg = self.config.get("connect_notification", "🟢 小青已上线~")
        if not connect_msg:
            return
        now = time.monotonic()
        min_interval = self._connect_notification_min_interval()
        if (
            min_interval > 0
            and self._last_connect_notification_ts > 0
            and now - self._last_connect_notification_ts < min_interval
        ):
            logger.info("Connect notification suppressed by min interval")
            return
        self._last_connect_notification_ts = now
        message = [{"type": "text", "data": {"text": connect_msg}}]
        for group_id in default_groups:
            action = {
                "action": "send_group_msg",
                "params": {
                    "group_id": int(group_id),
                    "message": message,
                },
            }
            await self._send_action(action)

    def _connect_notification_min_interval(self) -> float:
        try:
            return max(
                0.0,
                float(self.config.get("connect_notification_min_interval_seconds", 300)),
            )
        except (TypeError, ValueError):
            return 300.0

    async def _process_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """处理事件并返回 action（通用逻辑）"""
        if self._stopping:
            logger.debug("Dropping event while XiaoQing is stopping")
            return None
        segs = await self.dispatcher.handle_event(event)
        segs = segments(segs)
        return build_action(segs, event.get("user_id"), event.get("group_id"))

    def _http_enabled(self) -> bool:
        return bool(self.http_sender and str(getattr(self.http_sender, "http_base", "")).strip())

    async def _request_onebot_action(
        self,
        action_name: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Request a correlated OneBot response without using action sinks/broadcast."""

        if self._stopping:
            return None
        action = {"action": action_name, "params": dict(params)}
        ws_client = self.ws_client
        if ws_client is not None and ws_client.connected():
            response = await ws_client.request_action(action)
            if response is not None:
                return response
        http_sender = self.http_sender
        if http_sender is not None and str(getattr(http_sender, "http_base", "")).strip():
            return await http_sender.request_action(action)
        return None

    async def _send_action(self, action: dict[str, Any], wait_ws_seconds: float = 0.0) -> bool:
        # 自动拆分长文本消息
        actions = self._maybe_split_action(action)
        sent_all = True
        for i, act in enumerate(actions):
            if i > 0:
                await asyncio.sleep(MESSAGE_SPLIT_DELAY)
            sent = await self._send_single_action(
                act,
                wait_ws_seconds=wait_ws_seconds,
            )
            if sent:
                await self._notify_outgoing_action_observers(act)
            sent_all = sent and sent_all
        return sent_all

    @staticmethod
    def _tag_action_source(action: dict[str, Any], plugin_name: str) -> dict[str, Any]:
        if not isinstance(action, dict):
            return action
        tagged = dict(action)
        tagged.setdefault("_source_plugin", str(plugin_name or "").strip())
        return tagged

    async def _notify_outgoing_action_observers(self, action: dict[str, Any]) -> None:
        source_plugin = str(action.get("_source_plugin", "") or "").strip()
        if not source_plugin:
            return
        if str(action.get("action", "") or "").strip() not in (
            "send_group_msg",
            "send_private_msg",
        ):
            return

        loaded = self.plugin_manager.get("xiaoqing_chat")
        observer = getattr(getattr(loaded, "module", None), "observe_outgoing_action", None)
        if observer is None:
            return

        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        group_id = params.get("group_id")
        user_id = params.get("user_id")
        try:
            context = self.plugin_manager.build_context(
                "xiaoqing_chat",
                user_id=user_id if group_id in (None, "") else None,
                group_id=group_id,
                principal=self._principal_authority.issue(
                    kind="lifecycle",
                    group_id=group_id,
                ),
            )

            async def run_observer() -> None:
                await call_plugin_callback(observer, action, context, source_plugin=source_plugin)

            await invoke_loaded_plugin(loaded, run_observer)
        except (PluginExecutionClosed, PluginExecutionTimeout, PluginExecutionUnavailable):
            logger.debug("Outgoing action observer skipped during plugin unload")
        except Exception as exc:
            logger.debug("Outgoing action observer failed: %s", exc, exc_info=True)

    def _maybe_split_action(self, action: dict[str, Any]) -> list[dict[str, Any]]:
        """将包含过长文本的 action 拆分为多个 action"""
        act_name = action.get("action", "")
        if act_name not in ("send_group_msg", "send_private_msg"):
            return [action]

        params = action.get("params")
        if not isinstance(params, dict):
            return [action]

        message = params.get("message")
        if not isinstance(message, list):
            return [action]

        chunks = split_message_segments(message)
        if len(chunks) <= 1:
            return [action]

        # 保留 action 上的额外字段（如 _bypass_sink）
        results = []
        for chunk in chunks:
            new_action = {
                "action": act_name,
                "params": {**params, "message": chunk},
            }
            # 复制非标准字段
            for key in action:
                if key not in ("action", "params"):
                    new_action[key] = action[key]
            results.append(new_action)

        logger.debug(
            "Split long message into %d chunks (action=%s)",
            len(results),
            act_name,
        )
        return results

    async def _send_single_action(
        self, action: dict[str, Any], wait_ws_seconds: float = 0.0
    ) -> bool:
        try:
            act = str(action.get("action", "") or "")
            if act in ("send_group_msg", "send_private_msg"):
                params = action.get("params") or {}
                if isinstance(params, dict):
                    msg = params.get("message")
                    preview = ""
                    if isinstance(msg, list):
                        preview = _extract_message_preview(msg[:12]).replace("\n", "\\n").strip()
                    if len(preview) > MAX_MESSAGE_PREVIEW_LENGTH:
                        preview = preview[: MAX_MESSAGE_PREVIEW_LENGTH - 1] + "…"
                    logger.info(
                        "Sending: action=%s group=%s user=%s message_length=%s",
                        act,
                        params.get("group_id") or "-",
                        params.get("user_id") or "-",
                        len(preview),
                    )
        except (KeyError, TypeError, ValueError) as exc:
            # 日志记录失败不影响消息发送，仅记录调试信息
            logger.debug("Failed to generate message preview: %s", exc)
        bypass_sink = bool(action.get("_bypass_sink", False))
        delivery_action = {key: value for key, value in action.items() if key != "_bypass_sink"}

        def copy_delivery_result() -> None:
            if "_result_message_id" in delivery_action:
                action["_result_message_id"] = delivery_action["_result_message_id"]

        sink = current_action_sink.get()
        if not bypass_sink and sink is not None and getattr(sink, "is_active", True):
            await sink(delivery_action)
            return True

        if self.ws_client and self.ws_client.connected():
            sent = await self.ws_client.send_action(delivery_action)
            if sent:
                copy_delivery_result()
                return True

        # 尝试通过 Inbound WebSocket 广播（如果存在活跃连接）
        if self.inbound_manager and self.inbound_manager.has_active_ws_clients():
            await self.inbound_manager.broadcast(delivery_action)
            return True

        if wait_ws_seconds > 0 and self.ws_client:
            deadline = asyncio.get_running_loop().time() + float(wait_ws_seconds)
            while asyncio.get_running_loop().time() < deadline:
                if self.ws_client.connected():
                    sent = await self.ws_client.send_action(delivery_action)
                    if sent:
                        copy_delivery_result()
                        return True
                await asyncio.sleep(0.1)

        if self._http_enabled():
            sent = await self.http_sender.send_action(delivery_action)  # pyright: ignore[reportOptionalMemberAccess]
            copy_delivery_result()
            if not sent:
                logger.warning("Action was rejected or not acknowledged by OneBot HTTP")
            return bool(sent)

        logger.debug("Action dropped: no available sender (ws/http)")
        return False

    async def _collect_actions_for_event(
        self,
        event: dict[str, Any],
        *,
        default_source: str,
    ) -> list[dict[str, Any]]:
        if not await self._claim_inbound_event(event):
            return []
        sink = current_action_sink.get()
        event = dict(event)
        event.setdefault("_source", default_source)

        if sink is not None:
            action = await self._process_event(event)
            return [action] if action else []

        collected: list[dict[str, Any]] = []

        async def _collect(action: dict[str, Any]) -> None:
            collected.append(action)

        # 标记 sink 为活动状态
        _collect.is_active = True

        token = current_action_sink.set(_collect)
        try:
            action = await self._process_event(event)
            if action:
                collected.append(action)
        finally:
            # 标记 sink 为失效，使后续（后台任务）调用能直通发送逻辑
            _collect.is_active = False
            current_action_sink.reset(token)

        return collected

    async def _claim_inbound_event(self, event: dict[str, Any]) -> bool:
        """Claim a OneBot message id once across inbound HTTP and WS channels."""
        message_id = event.get("message_id")
        if message_id is None or isinstance(message_id, bool):
            return True
        key = ":".join(
            str(event.get(field, ""))
            for field in ("self_id", "post_type", "message_type", "message_id")
        )
        if self._event_dedupe_lock is None:
            self._event_dedupe_lock = asyncio.Lock()
        now = time.monotonic()
        async with self._event_dedupe_lock:
            expired = [
                event_key
                for event_key, seen_at in self._recent_event_ids.items()
                if now - seen_at >= INBOUND_EVENT_DEDUP_TTL_SECONDS
            ]
            for event_key in expired:
                self._recent_event_ids.pop(event_key, None)
            if key in self._recent_event_ids:
                logger.info("Dropped duplicate inbound OneBot event %s", key)
                return False
            if len(self._recent_event_ids) >= MAX_INBOUND_EVENT_DEDUP_KEYS:
                oldest = min(self._recent_event_ids, key=self._recent_event_ids.get)
                self._recent_event_ids.pop(oldest, None)
            self._recent_event_ids[key] = now
            return True

    async def _handle_upstream_event(self, event: dict[str, Any]) -> None:
        """处理来自 OneBot 上游的事件"""
        actions = await self._collect_actions_for_event(event, default_source="upstream_ws")
        if not actions:
            return
        for action in actions:
            await self._send_action(action)

    async def _handle_inbound_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """处理来自 Inbound Server 的事件"""
        return await self._collect_actions_for_event(event, default_source="inbound_http")

    # ============================================================
    # 插件上下文构建
    # ============================================================

    def _build_plugin_context(
        self,
        plugin_name: str,
        plugin_dir: Path,
        data_dir: Path,
        state: dict[str, Any],
        user_id: int | None = None,
        group_id: int | None = None,
        request_id: str | None = None,
        principal: PluginPrincipal | None = None,
    ) -> Any:
        """构建插件上下文"""
        if principal is None:
            if user_id is not None:
                principal = self.issue_user_principal(
                    {},
                    user_id=user_id,
                    group_id=group_id,
                    is_private=group_id is None,
                )
            else:
                principal = self._principal_authority.issue(
                    kind="lifecycle",
                    group_id=group_id,
                )
        elif not self._principal_authority.owns(principal):
            raise PermissionError("plugin context principal was not issued by this application")
        if principal.kind == "user":
            try:
                context_user_id = int(user_id) if user_id is not None else None
                principal_user_id = (
                    int(principal.user_id) if principal.user_id is not None else None
                )
                context_group_id = int(group_id) if group_id is not None else None
                principal_group_id = (
                    int(principal.group_id) if principal.group_id is not None else None
                )
            except (TypeError, ValueError) as exc:
                raise PermissionError("invalid user principal identifiers") from exc
            if (context_user_id, context_group_id) != (
                principal_user_id,
                principal_group_id,
            ):
                raise PermissionError("plugin context identifiers do not match its principal")

        async def send_action(action: dict[str, Any]) -> bool:
            return await self._send_action(
                self._tag_action_source(action, plugin_name),
                wait_ws_seconds=2.0,
            )

        plugin_config = self._plugin_config_view(plugin_name)
        plugin_secrets = self._plugin_secrets_view(plugin_name)
        capabilities = self._build_plugin_capabilities(plugin_name, principal, request_id)
        return PluginContext(
            config=plugin_config,
            secrets=plugin_secrets,
            plugin_name=plugin_name,
            plugin_dir=plugin_dir,
            data_dir=data_dir,
            http_session=self.http_session,
            send_action=send_action,
            reload_config=self.reload_config,
            reload_plugins=self._reload_plugins,
            list_commands=self.router.list_commands,
            list_plugins=self.plugin_manager.list_plugins,
            metrics=self.metrics,
            session_manager=self.session_manager,
            current_user_id=user_id,
            current_group_id=group_id,
            mute_control=self.dispatcher,
            config_manager=None,
            secret_reader=lambda path: self.config_manager.get_plugin_secret(plugin_name, path),
            secret_writer=lambda path, value: self.config_manager.set_plugin_secret(
                plugin_name, path, value
            ),
            secret_deleter=lambda path: self.config_manager.delete_plugin_secret(plugin_name, path),
            principal=principal,
            capabilities=capabilities,
            request_id=request_id,
            state=state,
        )

    async def _invoke_declared_service(
        self,
        *,
        caller_plugin: str,
        service_name: str,
        principal: PluginPrincipal,
        request_id: str | None,
        args: tuple[Any, ...],
        granted_capabilities: frozenset[str] = frozenset(),
    ) -> Any:
        """Invoke one current manifest binding selected by a core capability."""

        if not self._principal_authority.owns(principal):
            raise PermissionError("plugin service principal was not issued by this application")
        loaded, service = self.plugin_manager.resolve_service(
            caller_plugin=caller_plugin,
            service_name=service_name,
            granted_capabilities=granted_capabilities,
        )
        user_id = principal.user_id if principal.kind == "user" else None
        group_id = principal.group_id if principal.kind == "user" else None
        target_context = self.plugin_manager.build_context(
            service.owner,
            user_id,
            group_id,
            request_id,
            principal,
        )

        async def operation() -> Any:
            return await call_plugin_callback(service.callback, *args, target_context)

        return await invoke_loaded_plugin(loaded, operation)

    def _codex_arxiv_authorized(self, principal: PluginPrincipal) -> bool:
        if not self._principal_authority.owns(principal):
            return False
        if principal.is_system:
            return True
        return (
            principal.kind == "user"
            and principal.user_id is not None
            and principal.is_bot_admin
            and self.is_admin(principal.user_id)
        )

    async def _enqueue_codex_arxiv_summary(
        self,
        *,
        principal: PluginPrincipal,
        request_id: str | None,
        date: str,
        links: list[str],
    ) -> str:
        if not self._codex_arxiv_authorized(principal):
            raise PermissionError("Codex arXiv capability is no longer authorized")
        user_id = principal.user_id if principal.kind == "user" else None
        group_id = principal.group_id if principal.kind == "user" else None
        return await self._invoke_declared_service(
            caller_plugin="arxiv_filter",
            service_name="codex.enqueue_arxiv_summary",
            principal=principal,
            request_id=request_id,
            args=(date, list(links), user_id, group_id),
            granted_capabilities=frozenset({"codex_arxiv_summary"}),
        )

    def _build_plugin_capabilities(
        self,
        plugin_name: str,
        principal: PluginPrincipal,
        request_id: str | None = None,
    ) -> PluginCapabilities:
        is_system = principal.is_system and self._principal_authority.owns(principal)
        is_bot_admin = (
            principal.kind == "user"
            and self._principal_authority.owns(principal)
            and self.is_admin(principal.user_id)
        )
        secret_admin = None
        if plugin_name == "bot_core" and is_bot_admin and principal.is_private:
            secret_admin = SecretAdminService(
                _authorized=lambda: (
                    principal.user_id is not None
                    and principal.is_private
                    and self.is_admin(principal.user_id)
                ),
                _snapshot=lambda: self.config_manager.snapshot().secrets,
                _writer=self.config_manager.update_secret,
            )

        onebot_media = None
        if plugin_name == "xiaoqing_chat":
            onebot_media = OneBotMediaService(self._request_onebot_action)

        config_subscription = None
        if plugin_name == "pendo":

            def subscribe(callback: Callable[[dict[str, Any]], Any]) -> Callable[[], None]:
                def relay(_snapshot: ConfigSnapshot) -> Any:
                    return callback(self._plugin_config_view(plugin_name))

                return self.config_manager.on_reload(relay)

            config_subscription = ConfigSubscriptionService(subscribe)

        codex_arxiv_summary = None
        if plugin_name == "arxiv_filter" and (is_system or is_bot_admin):
            codex_arxiv_summary = CodexArxivSummaryService(
                _authorized=lambda: self._codex_arxiv_authorized(principal),
                _enqueue=functools.partial(
                    self._enqueue_codex_arxiv_summary,
                    principal=principal,
                    request_id=request_id,
                ),
            )

        voice_synthesis = None
        chat_reply = None
        if plugin_name == "smalltalk":

            async def synthesize_text(text: str) -> list[dict[str, Any]] | None:
                return await self._invoke_declared_service(
                    caller_plugin="smalltalk",
                    service_name="voice.synthesize_text",
                    principal=principal,
                    request_id=request_id,
                    args=(text,),
                )

            async def reply_via_chat(
                text: str,
                event: dict[str, Any],
            ) -> list[dict[str, Any]]:
                return await self._invoke_declared_service(
                    caller_plugin="smalltalk",
                    service_name="chat.reply",
                    principal=principal,
                    request_id=request_id,
                    args=(text, dict(event)),
                )

            voice_synthesis = VoiceSynthesisService(synthesize_text)
            chat_reply = ChatReplyService(reply_via_chat)

        return PluginCapabilities(
            is_bot_admin=is_bot_admin,
            is_system=is_system,
            secret_admin=secret_admin,
            onebot_media=onebot_media,
            config_subscription=config_subscription,
            codex_arxiv_summary=codex_arxiv_summary,
            voice_synthesis=voice_synthesis,
            chat_reply=chat_reply,
        )

    def _plugin_config_view(self, plugin_name: str) -> dict[str, Any]:
        """Return the immutable public config and this plugin's config namespace."""
        public_keys = (
            "bot_name",
            "command_prefixes",
            "default_group_ids",
            "timezone",
            "require_bot_name_in_group",
            "random_reply_rate",
        )
        config = self.config
        plugin_options = config.get("plugins", {}).get(plugin_name, {})
        view = {key: config[key] for key in public_keys if key in config}
        view["plugins"] = {plugin_name: plugin_options}
        return _freeze_config_mapping(view)

    def _plugin_secrets_view(self, plugin_name: str) -> dict[str, Any]:
        """Return only this plugin's immutable secret namespace."""
        plugin_secrets = self.secrets.get("plugins", {}).get(plugin_name, {})
        return _freeze_config_mapping({"plugins": {plugin_name: plugin_secrets}})

    def _reload_plugins(self) -> asyncio.Task[None] | None:
        """
        重载所有插件（非阻塞，创建后台任务）

        注意：此方法立即返回，实际重载在后台进行。
        如需等待重载完成，请检查 _reload_task 或监听日志。
        """
        if self._stopping:
            logger.debug("Ignoring plugin reload while stopping")
            return None
        if self._reload_task and not self._reload_task.done():
            logger.info("Plugin reload already in progress")
            return self._reload_task
        self._reload_task = asyncio.create_task(self._reload_plugins_async_with_logging())
        return self._reload_task

    async def _reload_plugins_async_with_logging(self) -> None:
        """执行插件重载并记录结果"""
        try:
            async with self._ensure_reload_lock():
                if self._stopping:
                    return
                logger.info("Starting plugin reload...")
                for name in list(self.plugin_manager.list_plugins()):
                    if self._stopping:
                        return
                    # 清理该插件的所有活跃 session，避免 reload 后残留旧状态
                    await self.session_manager.clear_plugin_sessions(name)
                    await self.plugin_manager.unload_plugin(name)

                if self._stopping:
                    return
                self.plugin_manager.load_all()
                await self.plugin_manager.wait_inits()
                logger.info("Plugin reload completed successfully")
        except Exception as exc:
            logger.exception("Plugin reload failed: %s", exc)

    # ============================================================
    # 配置热更新
    # ============================================================

    def _apply_config(self, snapshot: ConfigSnapshot) -> None:
        """应用配置变更"""
        if self._stopping:
            logger.debug("Ignoring configuration update while stopping")
            return
        config = snapshot.config
        secrets = snapshot.secrets

        self._load_admins(secrets)
        self.dispatcher.refresh_prefix_cache()
        self._configure_plugin_execution(config)
        self._configure_plugin_watch(config)
        session_timeout = float(config.get("session_timeout", DEFAULT_SESSION_TIMEOUT_SEC))
        self.session_manager.set_default_timeout(session_timeout)

        concurrency = int(config.get("max_concurrency", DEFAULT_MAX_CONCURRENCY))
        if concurrency != self._dispatcher_concurrency:
            self.dispatcher.semaphore = asyncio.Semaphore(concurrency)
            self._dispatcher_concurrency = concurrency

        timezone = str(config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai")
        if timezone != self.scheduler.timezone:
            self.scheduler.reset(timezone)
            self._reschedule("startup")

        if self.http_session is not None:
            current_task = self._config_apply_task
            if current_task and not current_task.done():
                current_task.cancel()
            self._config_apply_task = asyncio.create_task(self._apply_runtime_config(snapshot))

    def reload_config(self) -> None:
        """重新加载配置并应用变更"""
        if self._stopping:
            logger.debug("Ignoring configuration reload while stopping")
            return
        self.config_manager.reload()
        snapshot = ConfigSnapshot(self.config_manager.config, self.config_manager.secrets)
        self._apply_config(snapshot)

    async def _apply_runtime_config(self, snapshot: ConfigSnapshot) -> None:
        config = snapshot.config
        secrets = snapshot.secrets
        if self._stopping or not self.http_session:
            return

        http_base = str(config.get("onebot_http_base", "") or "").strip()
        if http_base:
            if not self.http_sender:
                self.http_sender = OneBotHttpSender(
                    http_base,
                    secrets.get("onebot_token", ""),
                    self.http_session,
                )
            else:
                self.http_sender.update(http_base, secrets.get("onebot_token", ""))
        else:
            self.http_sender = None

        enable_ws = bool(config.get("enable_ws_client", True))
        ws_uri = str(config.get("onebot_ws_uri", "") or "").strip()
        ws_queue_size = self._parse_ws_queue_size(config)
        await self._reconcile_ws_client(
            enable_ws=enable_ws,
            ws_uri=ws_uri,
            token=secrets.get("onebot_token", ""),
            queue_size=ws_queue_size,
        )

        if self._stopping:
            return
        await self._reconcile_inbound_manager(config, secrets)

    def _parse_ws_queue_size(self, config: dict[str, Any]) -> int:
        ws_queue_size_raw = config.get("ws_queue_size", DEFAULT_INBOUND_WS_QUEUE_SIZE)
        try:
            return int(ws_queue_size_raw)
        except (TypeError, ValueError):
            return DEFAULT_INBOUND_WS_QUEUE_SIZE

    async def _reconcile_ws_client(
        self,
        *,
        enable_ws: bool,
        ws_uri: str,
        token: str,
        queue_size: int,
    ) -> None:
        if self._stopping:
            return
        if not enable_ws or not ws_uri:
            if self.ws_client:
                await self.ws_client.stop()
                await self._cancel_task("_ws_client_task")
                self.ws_client = None
            return

        needs_restart = (
            self.ws_client is None
            or self.ws_client.ws_uri != ws_uri
            or self.ws_client.auth_token != token
            or getattr(self.ws_client, "_queue_size", queue_size) != queue_size
            or self._ws_client_task is None
            or self._ws_client_task.done()
        )
        if not needs_restart:
            return

        if self.ws_client:
            await self.ws_client.stop()
            await self._cancel_task("_ws_client_task")

        if self._stopping:
            return
        self.ws_client = OneBotWsClient(
            ws_uri,
            token,
            queue_size=queue_size,
        )
        self.ws_client.set_on_connect(self._on_ws_connected)
        self._ws_client_task = asyncio.create_task(
            self.ws_client.connect_and_listen(self._handle_upstream_event)
        )

    async def _reconcile_inbound_manager(
        self,
        config: dict[str, Any],
        secrets: dict[str, Any],
    ) -> None:
        if self._stopping:
            return
        desired = InboundManager.from_config(
            config=config,
            token=secrets.get("inbound_token", ""),
            handler=self._handle_inbound_event,
        )
        desired_key = self._inbound_manager_key(desired)
        current_key = self._inbound_manager_key(self.inbound_manager)

        if desired is None:
            if self.inbound_manager:
                await self.inbound_manager.stop()
                self.inbound_manager = None
            return

        if self.inbound_manager is None or current_key != desired_key:
            if self.inbound_manager:
                await self.inbound_manager.stop()
            if self._stopping:
                return
            self.inbound_manager = desired
            self._bind_inbound_status_providers(self.inbound_manager)
            await self.inbound_manager.start()
            return

        self.inbound_manager.update_token(secrets.get("inbound_token", ""))
        self._bind_inbound_status_providers(self.inbound_manager)

    @staticmethod
    def _inbound_manager_key(manager: InboundManager | None) -> tuple[Any, ...] | None:
        if manager is None:
            return None
        return manager.config_key

    def _bind_inbound_status_providers(self, manager: InboundManager) -> None:
        def _plugins_count() -> int:
            return len(self.plugin_manager.list_plugins())

        def _sessions_count() -> int:
            return self.session_manager.active_count

        def _pending_jobs() -> int:
            scheduler = self.scheduler.scheduler
            if not scheduler:
                return 0
            return len(scheduler.get_jobs())

        def _metrics() -> dict[str, Any]:
            return self.metrics.summary_snapshot()

        manager.set_status_providers(
            plugins_count=_plugins_count,
            sessions_count=_sessions_count,
            pending_jobs=_pending_jobs,
            metrics=_metrics,
        )

    # ============================================================
    # 定时任务
    # ============================================================

    def _reschedule(self, plugin_name: str) -> None:
        """重新调度定时任务"""
        if self._stopping:
            logger.debug("Skipping schedule update for %s while stopping", plugin_name)
            return
        if plugin_name == "startup":
            # 启动时全量加载
            self.scheduler.clear_prefix("plugin.")
            target_plugins = self.plugin_manager.schedule_definitions()
        else:
            # 单个插件更新
            self.scheduler.clear_prefix(f"plugin.{plugin_name}.")
            loaded = self.plugin_manager.get(plugin_name)
            target_plugins = [loaded] if loaded else []

        for loaded in target_plugins:
            if not loaded:
                continue
            for entry in loaded.definition.schedule:
                if not entry.get("enabled", True):
                    logger.info(
                        "Manifest-disabled schedule skipped: plugin=%s id=%s",
                        loaded.definition.name,
                        entry.get("id", entry.get("handler", "<unknown>")),
                    )
                    continue
                handler_name = entry.get("handler", "")
                if not handler_name or not hasattr(loaded.module, handler_name):
                    continue

                cron = entry.get("cron", {})
                job_id = f"plugin.{loaded.definition.name}.{entry.get('id', handler_name)}"
                handler = getattr(loaded.module, handler_name)
                # 获取定时任务配置的 group_ids（可选）
                raw_group_ids = entry.get("group_ids")
                group_ids = (
                    tuple(int(x) for x in raw_group_ids) if raw_group_ids is not None else None
                )

                self.scheduler.add_job(
                    job_id,
                    functools.partial(
                        self._run_job,
                        handler,
                        loaded.definition.name,
                        group_ids,
                        loaded_plugin=loaded,
                    ),
                    cron,
                    description=entry.get("description"),
                )

    async def _run_job(
        self,
        handler,
        plugin_name: str,
        group_ids: tuple[int, ...] | list[int] | None = None,
        *,
        loaded_plugin: Any | None = None,
    ) -> None:
        """执行定时任务"""
        if self._stopping:
            logger.debug("Scheduled job skipped while stopping: %s", plugin_name)
            return
        raw_target_groups = (
            group_ids if group_ids is not None else list(self.config.get("default_group_ids", []))
        )
        delivery_targets = tuple(DeliveryTarget("group", int(value)) for value in raw_target_groups)
        principal = self._principal_authority.issue(
            kind="scheduled_system",
            is_private=False,
            delivery_targets=delivery_targets,
        )
        context = self.plugin_manager.build_context(
            plugin_name,
            principal=principal,
        )
        try:
            loaded = (
                loaded_plugin if loaded_plugin is not None else self.plugin_manager.get(plugin_name)
            )

            async def run_job_handler() -> Any:
                return await call_plugin_callback(handler, context)

            result = await invoke_loaded_plugin(loaded, run_job_handler)

            segs = segments(result)
            if not segs:
                return

            for target in principal.delivery_targets:
                action = build_action(segs, target.user_id, target.group_id)
                if action:
                    action = self._tag_action_source(action, plugin_name)
                    # 使用统一的 _send_action 方法（优先 WS，备选 HTTP）
                    await self._send_action(action)

        except asyncio.CancelledError:
            logger.info("Scheduled job cancelled during shutdown: %s", plugin_name)
        except (PluginExecutionClosed, PluginExecutionTimeout, PluginExecutionUnavailable):
            logger.debug("Scheduled job skipped during plugin unload: %s", plugin_name)
        except Exception as exc:
            logger.exception("Scheduled job failed: %s", exc)
