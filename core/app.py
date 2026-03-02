"""
XiaoQing 主应用

简化后的核心应用模块。
"""

import asyncio
import functools
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from .config import ConfigManager, ConfigSnapshot
from .constants import (
    DEFAULT_INBOUND_WS_QUEUE_SIZE,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_SESSION_TIMEOUT_SEC,
    MAX_MESSAGE_PREVIEW_LENGTH,
    MESSAGE_SPLIT_DELAY,
)
from .context import PluginContext
from .dispatcher import Dispatcher
from .logging_config import LogManager, setup_logging
from .metrics import MetricsCollector
from .onebot import OneBotHttpSender, OneBotWsClient
from .plugin_base import build_action, segments, split_message_segments
from .plugin_manager import PluginManager
from .router import CommandRouter
from .scheduler import SchedulerManager
from .server import InboundManager
from .session import SessionManager


logger = logging.getLogger(__name__)

Action = dict[str, Any]
ActionSink = Callable[[Action], Awaitable[None]]
current_action_sink: ContextVar[ActionSink | None] = ContextVar("current_action_sink", default=None)


class XiaoQingApp:
    """XiaoQing 主应用类"""

    root: Path
    config_manager: ConfigManager
    log_manager: LogManager
    http_session: aiohttp.ClientSession | None
    http_sender: OneBotHttpSender | None
    ws_client: OneBotWsClient | None
    inbound_manager: InboundManager | None
    router: CommandRouter
    plugins_dir: Path
    plugin_manager: PluginManager
    scheduler: SchedulerManager
    metrics: MetricsCollector
    session_manager: SessionManager
    dispatcher: Dispatcher
    _admin_set: set[int]
    _session_cleanup_task: asyncio.Task[None] | None
    _reload_lock: asyncio.Lock
    _reload_task: asyncio.Task[None] | None

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
        concurrency = int(self.config_manager.config.get("max_concurrency", DEFAULT_MAX_CONCURRENCY))
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

        self._admin_set: set[int] = set()
        self._load_admins()

        self._session_cleanup_task: asyncio.Task[None] | None = None
        self._reload_lock: asyncio.Lock | None = None
        self._reload_task: asyncio.Task[None] | None = None

        # 注册回调
        self.plugin_manager.on_change(self._reschedule)
        self.config_manager.on_reload(self._apply_config)
    
    def _ensure_reload_lock(self) -> asyncio.Lock:
        """Ensure reload lock is initialized (requires event loop)"""
        if self._reload_lock is None:
            self._reload_lock = asyncio.Lock()
        return self._reload_lock

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
        # M1: 延迟初始化 Semaphore（确保运行在事件循环中）
        if self.dispatcher.semaphore is None:
            concurrency = int(self.config.get("max_concurrency", DEFAULT_MAX_CONCURRENCY))
            self.dispatcher.semaphore = asyncio.Semaphore(concurrency)

        # 初始化 HTTP 会话
        self.http_session = aiohttp.ClientSession()

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
                asyncio.create_task(
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
            await self.inbound_manager.start()

    async def stop(self) -> None:
        """优雅停止应用"""
        logger.info("Shutting down XiaoQing...")

        if self._session_cleanup_task:
            self._session_cleanup_task.cancel()
            try:
                await self._session_cleanup_task
            except asyncio.CancelledError:
                pass

        # 1. 停止 WebSocket 客户端（不再接收新消息）
        if self.ws_client:
            await self.ws_client.stop()
            logger.info("WebSocket client stopped")

        # 2. 停止定时任务调度器
        try:
            if self.scheduler.scheduler:
                self.scheduler.scheduler.shutdown(wait=True)
            logger.info("Scheduler stopped")
        except Exception as exc:
            logger.warning("Scheduler shutdown error: %s", exc)

        # 3. 卸载所有插件（触发 shutdown 钩子）
        plugin_names = list(self.plugin_manager.list_plugins())
        for name in plugin_names:
            try:
                await self.plugin_manager.unload_plugin(name)
            except Exception as exc:
                logger.warning("Plugin %s unload error: %s", name, exc)
        logger.info("All plugins unloaded (%d total)", len(plugin_names))

        # 4. 关闭 HTTP 会话
        if self.http_session:
            await self.http_session.close()
            logger.info("HTTP session closed")

        # 5. 停止 Inbound Server（如果有）
        if self.inbound_manager:
            try:
                await self.inbound_manager.stop()
                logger.info("Inbound server stopped")
            except Exception as exc:
                logger.warning("Inbound server stop error: %s", exc)
            self.inbound_manager = None

        logger.info("XiaoQing shutdown complete")

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

    async def _process_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """处理事件并返回 action（通用逻辑）"""
        segs = await self.dispatcher.handle_event(event)
        segs = segments(segs)
        return build_action(segs, event.get("user_id"), event.get("group_id"))

    def _http_enabled(self) -> bool:
        return bool(self.http_sender and str(getattr(self.http_sender, "http_base", "")).strip())

    async def _send_action(self, action: dict[str, Any], wait_ws_seconds: float = 0.0) -> None:
        # 自动拆分长文本消息
        actions = self._maybe_split_action(action)
        for i, act in enumerate(actions):
            if i > 0:
                await asyncio.sleep(MESSAGE_SPLIT_DELAY)
            await self._send_single_action(act, wait_ws_seconds=wait_ws_seconds)

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
            len(results), act_name,
        )
        return results

    async def _send_single_action(self, action: dict[str, Any], wait_ws_seconds: float = 0.0) -> None:
        try:
            act = str(action.get("action", "") or "")
            if act in ("send_group_msg", "send_private_msg"):
                params = action.get("params") or {}
                if isinstance(params, dict):
                    msg = params.get("message")
                    preview_parts: list[str] = []
                    if isinstance(msg, list):
                        for seg in msg[:12]:
                            if not isinstance(seg, dict):
                                continue
                            tp = str(seg.get("type", "") or "")
                            data = seg.get("data") or {}
                            if tp == "text" and isinstance(data, dict):
                                preview_parts.append(str(data.get("text", "") or ""))
                            else:
                                preview_parts.append(f"[{tp}]")
                    preview = "".join(preview_parts).replace("\n", "\\n").strip()
                    if len(preview) > MAX_MESSAGE_PREVIEW_LENGTH:
                        preview = preview[:MAX_MESSAGE_PREVIEW_LENGTH - 1] + "…"
                    logger.info(
                        "Sending: action=%s group=%s user=%s message=%s",
                        act,
                        params.get("group_id") or "-",
                        params.get("user_id") or "-",
                        preview,
                    )
        except (KeyError, TypeError, ValueError) as exc:
            # 日志记录失败不影响消息发送，仅记录调试信息
            logger.debug("Failed to generate message preview: %s", exc)
        bypass_sink = action.pop("_bypass_sink", False)
        sink = current_action_sink.get()
        if not bypass_sink and sink is not None and getattr(sink, "is_active", True):
            await sink(action)
            return

        if self.ws_client and self.ws_client.connected():
            await self.ws_client.send_action(action)
            return
            
        # 尝试通过 Inbound WebSocket 广播（如果是 Inbound WS 连接进来的）
        if self.inbound_manager:
            await self.inbound_manager.broadcast(action)
            return

        if wait_ws_seconds > 0 and self.ws_client:
            deadline = asyncio.get_running_loop().time() + float(wait_ws_seconds)
            while asyncio.get_running_loop().time() < deadline:
                if self.ws_client.connected():
                    await self.ws_client.send_action(action)
                    return
                await asyncio.sleep(0.1)

        if self._http_enabled():
            await self.http_sender.send_action(action)  # pyright: ignore[reportOptionalMemberAccess]
            return

        logger.debug("Action dropped: no available sender (ws/http)")

    async def _collect_actions_for_event(
        self,
        event: dict[str, Any],
        *,
        default_source: str,
    ) -> list[dict[str, Any]]:
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
        setattr(_collect, "is_active", True)
        
        token = current_action_sink.set(_collect)
        try:
            action = await self._process_event(event)
            if action:
                collected.append(action)
        finally:
            # 标记 sink 为失效，使后续（后台任务）调用能直通发送逻辑
            setattr(_collect, "is_active", False)
            current_action_sink.reset(token)

        return collected

    async def _handle_upstream_event(self, event: dict[str, Any]) -> None:
        """处理来自 OneBot 上游的事件"""
        actions = await self._collect_actions_for_event(event, default_source="upstream_ws")
        if not actions:
            return
        ws_client = self.ws_client
        if not ws_client or not ws_client.connected():
            logger.warning("Upstream WS reply dropped: ws client not connected")
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
    ) -> Any:
        """构建插件上下文"""
        async def send_action(action: dict[str, Any]) -> None:
            await self._send_action(action, wait_ws_seconds=2.0)
        return PluginContext(
            config=self.config,
            secrets=self.secrets,
            plugin_name=plugin_name,
            plugin_dir=plugin_dir,
            data_dir=data_dir,
            http_session=self.http_session,
            send_action=send_action,
            reload_config=self.reload_config,
            reload_plugins=self._reload_plugins,
            list_commands=self.router.help_messages,
            list_plugins=self.plugin_manager.list_plugins,
            metrics=self.metrics,
            session_manager=self.session_manager,
            current_user_id=user_id,
            current_group_id=group_id,
            mute_control=self.dispatcher,
            config_manager=self.config_manager,
            request_id=request_id,
            state=state,
        )

    def _reload_plugins(self) -> None:
        """
        重载所有插件（非阻塞，创建后台任务）

        注意：此方法立即返回，实际重载在后台进行。
        如需等待重载完成，请检查 _reload_task 或监听日志。
        """
        if self._reload_task and not self._reload_task.done():
            logger.info("Plugin reload already in progress")
            return
        self._reload_task = asyncio.create_task(self._reload_plugins_async_with_logging())

    async def _reload_plugins_async_with_logging(self) -> None:
        """执行插件重载并记录结果"""
        try:
            async with self._ensure_reload_lock():
                logger.info("Starting plugin reload...")
                for name in list(self.plugin_manager.list_plugins()):
                    await self.plugin_manager.unload_plugin(name)

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
        config = snapshot.config
        secrets = snapshot.secrets

        self._load_admins(secrets)
        self.dispatcher.refresh_prefix_cache()

        http_base = str(config.get("onebot_http_base", "") or "").strip()
        if http_base:
            if not self.http_sender and self.http_session:
                self.http_sender = OneBotHttpSender(
                    http_base,
                    secrets.get("onebot_token", ""),
                    self.http_session,
                )
            elif self.http_sender:
                self.http_sender.update(
                    http_base,
                    secrets.get("onebot_token", ""),
                )
        else:
            self.http_sender = None

        if self.ws_client:
            self.ws_client.update(
                config.get("onebot_ws_uri", ""),
                secrets.get("onebot_token", ""),
            )

        if self.inbound_manager:
            self.inbound_manager.update_token(secrets.get("inbound_token", ""))

    def reload_config(self) -> None:
        """重新加载配置并应用变更"""
        self.config_manager.reload()
        snapshot = ConfigSnapshot(self.config_manager.config, self.config_manager.secrets)
        self._apply_config(snapshot)

    # ============================================================
    # 定时任务
    # ============================================================

    def _reschedule(self, plugin_name: str) -> None:
        """重新调度定时任务"""
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
                handler_name = entry.get("handler", "")
                if not handler_name or not hasattr(loaded.module, handler_name):
                    continue

                cron = entry.get("cron", {})
                job_id = f"plugin.{loaded.definition.name}.{entry.get('id', handler_name)}"
                handler = getattr(loaded.module, handler_name)
                # 获取定时任务配置的 group_ids（可选）
                raw_group_ids = entry.get("group_ids")
                group_ids = [int(x) for x in raw_group_ids] if raw_group_ids else None

                self.scheduler.add_job(
                    job_id,
                    functools.partial(self._run_job, handler, loaded.definition.name, group_ids),
                    cron,
                )

    async def _run_job(self, handler, plugin_name: str, group_ids: list[int] | None = None) -> None:
        """执行定时任务"""
        context = self.plugin_manager.build_context(plugin_name)
        try:
            result = handler(context)
            if asyncio.iscoroutine(result):
                result = await result

            segs = segments(result)
            if not segs:
                return

            # 优先使用任务配置的 group_ids，否则使用默认群组
            target_groups = group_ids if group_ids else context.default_groups()
            for group_id in target_groups:
                action = build_action(segs, None, group_id)
                if action:
                    # 使用统一的 _send_action 方法（优先 WS，备选 HTTP）
                    await self._send_action(action)

        except Exception as exc:
            logger.exception("Scheduled job failed: %s", exc)
