"""应用侧插件上下文、服务调用与能力签发边界。"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .ai import (
    AICompletionResult,
    AIModelInfo,
    complete_configured_route,
    list_configured_models,
)
from .capabilities import (
    AIService,
    ChatReplyService,
    CodexArxivSummaryService,
    ConfigSubscriptionService,
    OneBotMediaService,
    SecretAdminService,
    VoiceSynthesisService,
)
from .context import PluginContext, _scoped_plugin_config, _scoped_plugin_secrets
from .interfaces import (
    PluginCapabilities,
    PluginPrincipal,
    PluginSettingsSnapshot,
)
from .plugin_execution import call_plugin_callback, invoke_loaded_plugin

if TYPE_CHECKING:
    import asyncio

    import aiohttp

    from .app_identity import AppIdentityService
    from .config import ConfigManager, ConfigSnapshot
    from .dispatcher import Dispatcher
    from .metrics import MetricsCollector
    from .plugin_manager import PluginManager
    from .router import CommandRouter
    from .session import SessionManager


class AppPluginContextMixin:
    """集中管理插件可见配置、身份能力和声明式跨插件服务。"""

    if TYPE_CHECKING:
        config_manager: ConfigManager
        identity_service: AppIdentityService
        http_session: aiohttp.ClientSession | None
        router: CommandRouter
        plugin_manager: PluginManager
        metrics: MetricsCollector
        session_manager: SessionManager
        dispatcher: Dispatcher
        _plugin_settings_cache: dict[tuple[str, int], PluginSettingsSnapshot]
        _plugin_settings_cache_revision: int | None

        @property
        def config(self) -> Mapping[str, Any]: ...

        @property
        def secrets(self) -> Mapping[str, Any]: ...

        def issue_user_principal(
            self,
            event: dict[str, Any],
            *,
            user_id: int | None,
            group_id: int | None,
            is_private: bool,
        ) -> PluginPrincipal: ...

        def is_admin(self, user_id: int | None) -> bool: ...

        async def _send_action(
            self,
            action: dict[str, Any],
            wait_ws_seconds: float = 0.0,
        ) -> bool | None: ...

        @staticmethod
        def _tag_action_source(
            action: dict[str, Any],
            plugin_name: str,
        ) -> dict[str, Any]: ...

        async def _request_onebot_action(
            self,
            action_name: str,
            params: dict[str, Any],
        ) -> dict[str, Any] | None: ...

        def reload_config(self) -> None: ...

        def _reload_plugins(self) -> asyncio.Task[None] | None: ...

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
        declared_capabilities: frozenset[str] = frozenset(),
        uses_services: frozenset[str] = frozenset(),
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
                principal = self.identity_service.issue(
                    kind="lifecycle",
                    group_id=group_id,
                )
        elif not self.identity_service.owns(principal):
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

        async def send_action(action: dict[str, Any]) -> bool | None:
            return await self._send_action(
                self._tag_action_source(action, plugin_name),
                wait_ws_seconds=2.0,
            )

        plugin_settings = self._plugin_settings_snapshot(plugin_name)
        capabilities = self._build_plugin_capabilities(
            plugin_name,
            principal,
            request_id,
            declared_capabilities=declared_capabilities,
            uses_services=uses_services,
        )
        return PluginContext(
            config=plugin_settings.config,
            secrets=plugin_settings.secrets,
            plugin_name=plugin_name,
            plugin_dir=plugin_dir,
            data_dir=data_dir,
            http_session=self.http_session,
            send_action=send_action,
            reload_config=self.reload_config,
            reload_plugins=self._reload_plugins,
            get_command_catalog=self.router.get_command_catalog,
            list_plugins=self.plugin_manager.list_plugins,
            metrics=self.metrics,
            session_manager=self.session_manager,
            current_user_id=user_id,
            current_group_id=group_id,
            mute_control=self.dispatcher,
            config_manager=None,
            settings_reader=lambda: self._plugin_settings_snapshot(plugin_name),
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

        if not self.identity_service.owns(principal):
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
        if not self.identity_service.owns(principal):
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
        caller_plugin: str,
        principal: PluginPrincipal,
        request_id: str | None,
        date: str,
        links: list[str],
    ) -> str:
        if not self._codex_arxiv_authorized(principal):
            raise PermissionError("Codex arXiv capability is no longer authorized")
        user_id = principal.user_id if principal.kind == "user" else None
        group_id = principal.group_id if principal.kind == "user" else None
        return cast(
            str,
            await self._invoke_declared_service(
                caller_plugin=caller_plugin,
                service_name="codex.enqueue_arxiv_summary",
                principal=principal,
                request_id=request_id,
                args=(date, list(links), user_id, group_id),
                granted_capabilities=frozenset({"codex_arxiv_summary"}),
            ),
        )

    def _build_plugin_capabilities(
        self,
        plugin_name: str,
        principal: PluginPrincipal,
        request_id: str | None = None,
        *,
        declared_capabilities: frozenset[str] = frozenset(),
        uses_services: frozenset[str] = frozenset(),
    ) -> PluginCapabilities:
        is_system = principal.is_system and self.identity_service.owns(principal)
        is_bot_admin = (
            principal.kind == "user"
            and self.identity_service.owns(principal)
            and self.is_admin(principal.user_id)
        )
        secret_admin = None
        if "secret_admin" in declared_capabilities and is_bot_admin and principal.is_private:
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
        if "onebot_media" in declared_capabilities:
            onebot_media = OneBotMediaService(self._request_onebot_action)

        config_subscription = None
        if "config_subscription" in declared_capabilities:

            def subscribe(
                callback: Callable[[Mapping[str, Any]], Any],
            ) -> Callable[[], None]:
                def relay(snapshot: ConfigSnapshot) -> Any:
                    return callback(self._plugin_config_view(plugin_name, snapshot.config))

                return cast(Callable[[], None], self.config_manager.on_reload(relay))

            config_subscription = ConfigSubscriptionService(subscribe)

        codex_arxiv_summary = None
        if "codex.enqueue_arxiv_summary" in uses_services and (is_system or is_bot_admin):
            codex_arxiv_summary = CodexArxivSummaryService(
                _authorized=lambda: self._codex_arxiv_authorized(principal),
                _enqueue=functools.partial(
                    self._enqueue_codex_arxiv_summary,
                    caller_plugin=plugin_name,
                    principal=principal,
                    request_id=request_id,
                ),
            )

        voice_synthesis = None
        chat_reply = None
        if "voice.synthesize_text" in uses_services:

            async def synthesize_text(text: str) -> list[dict[str, Any]] | None:
                return cast(
                    list[dict[str, Any]] | None,
                    await self._invoke_declared_service(
                        caller_plugin=plugin_name,
                        service_name="voice.synthesize_text",
                        principal=principal,
                        request_id=request_id,
                        args=(text,),
                    ),
                )

            voice_synthesis = VoiceSynthesisService(synthesize_text)

        if "chat.reply" in uses_services:

            async def reply_via_chat(
                text: str,
                event: dict[str, Any],
            ) -> list[dict[str, Any]]:
                return cast(
                    list[dict[str, Any]],
                    await self._invoke_declared_service(
                        caller_plugin=plugin_name,
                        service_name="chat.reply",
                        principal=principal,
                        request_id=request_id,
                        args=(text, dict(event)),
                    ),
                )

            chat_reply = ChatReplyService(reply_via_chat)

        async def complete_ai_route(
            *,
            route_name: str,
            messages: list[dict[str, Any]],
            required_modalities: tuple[str, ...] = ("text",),
            pinned_model: str | None = None,
            temperature: float | None = None,
            top_p: float | None = None,
            max_tokens: int | None = None,
            timeout_seconds: float | None = None,
            total_timeout_seconds: float | None = None,
            max_retry: int | None = None,
            retry_interval_seconds: float | None = None,
            tools: list[dict[str, Any]] | None = None,
            tool_choice: Any = None,
            extra_payload: Mapping[str, Any] | None = None,
        ) -> AICompletionResult:
            """用当前原子配置快照执行插件自己的 AI route。"""

            session = self.http_session
            if session is None or session.closed:
                raise RuntimeError("shared HTTP session is unavailable")
            snapshot = self.config_manager.snapshot()
            return await complete_configured_route(
                session=session,
                config=snapshot.config,
                secrets=snapshot.secrets,
                plugin_name=plugin_name,
                route_name=route_name,
                messages=messages,
                required_modalities=required_modalities,
                pinned_model=pinned_model,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                total_timeout_seconds=total_timeout_seconds,
                max_retry=max_retry,
                retry_interval_seconds=retry_interval_seconds,
                tools=tools,
                tool_choice=tool_choice,
                extra_payload=extra_payload,
            )

        def list_ai_models(
            *,
            route_name: str,
            required_modalities: tuple[str, ...] = ("text",),
        ) -> tuple[AIModelInfo, ...]:
            snapshot = self.config_manager.snapshot()
            return cast(
                tuple[AIModelInfo, ...],
                list_configured_models(
                    config=snapshot.config,
                    secrets=snapshot.secrets,
                    plugin_name=plugin_name,
                    route_name=route_name,
                    required_modalities=required_modalities,
                ),
            )

        return PluginCapabilities(
            is_bot_admin=is_bot_admin,
            is_system=is_system,
            secret_admin=secret_admin,
            onebot_media=onebot_media,
            config_subscription=config_subscription,
            codex_arxiv_summary=codex_arxiv_summary,
            voice_synthesis=voice_synthesis,
            chat_reply=chat_reply,
            ai=AIService(complete_ai_route, list_ai_models),
        )

    def _plugin_config_view(
        self,
        plugin_name: str,
        config: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Return the immutable public config and this plugin's config namespace."""
        source = self.config if config is None else config
        return cast(Mapping[str, Any], _scoped_plugin_config(plugin_name, source))

    def _plugin_secrets_view(
        self,
        plugin_name: str,
        secrets: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Return only this plugin's immutable secret namespace."""
        source = self.secrets if secrets is None else secrets
        return cast(Mapping[str, Any], _scoped_plugin_secrets(plugin_name, source))

    def _plugin_settings_snapshot(
        self,
        plugin_name: str,
        snapshot: ConfigSnapshot | None = None,
    ) -> PluginSettingsSnapshot:
        """Atomically scope one ConfigManager generation to a single plugin."""
        source = self.config_manager.snapshot() if snapshot is None else snapshot
        if self._plugin_settings_cache_revision != source.revision:
            self._plugin_settings_cache.clear()
            self._plugin_settings_cache_revision = source.revision
        cache_key = (plugin_name, source.revision)
        cached = self._plugin_settings_cache.get(cache_key)
        if cached is not None:
            return cached
        settings = PluginSettingsSnapshot(
            config=self._plugin_config_view(plugin_name, source.config),
            secrets=self._plugin_secrets_view(plugin_name, source.secrets),
            revision=source.revision,
            config_status=source.config_status.value,
            secrets_status=source.secrets_status.value,
        )
        self._plugin_settings_cache[cache_key] = settings
        return settings
