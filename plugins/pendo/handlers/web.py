"""处理 ``/pendo web`` 命令与敏感凭据的私聊投递。"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Final, Protocol, TypeAlias, cast

from core.plugin_base import build_action, run_sync, segments

from ..config import PendoConfig
from ..core.types import CommandMessage, PendoContext
from ..utils.error_handlers import handle_command_errors

if TYPE_CHECKING:
    from ..services.db import Database


class _LoginCodeIssuer(Protocol):
    """一次性网页登录码生成器接口。"""

    def __call__(
        self,
        owner_id: str,
        expires_seconds: int = PendoConfig.WEB_LOGIN_CODE_EXPIRE_SECONDS,
        *,
        db: Database | None = None,
    ) -> str:
        """为用户签发限时、单次使用的登录码。"""
        ...


class _WidgetTokenGenerator(Protocol):
    """只读小组件令牌生成器接口。"""

    def __call__(
        self,
        owner_id: str,
        expires_seconds: int = PendoConfig.WEB_WIDGET_TOKEN_EXPIRE_SECONDS,
        *,
        db: Database,
    ) -> str:
        """为用户签发有明确期限的只读令牌。"""
        ...


class _WebServer(Protocol):
    """命令层实际使用的 Web 服务最小接口。"""

    def get_url(self) -> str:
        """返回当前配置的 Web 根地址。"""
        ...

    def is_running(self) -> bool:
        """探测 Web 服务是否可访问。"""
        ...

    def is_managed_running(self) -> bool:
        """判断当前进程是否持有 Web 服务线程。"""
        ...

    def start(self, db: Database) -> bool:
        """使用给定数据库启动插件托管服务。"""
        ...

    def stop(self) -> bool:
        """停止插件托管服务。"""
        ...


_WebComponents: TypeAlias = tuple[_LoginCodeIssuer, _WidgetTokenGenerator, _WebServer]
_OPTIONAL_WEB_IMPORT_ROOTS: Final = frozenset(
    {"anyio", "fastapi", "jwt", "multipart", "pydantic", "starlette", "uvicorn"}
)


def _load_web_components() -> _WebComponents | None:
    """按当前插件包名加载可选 Web 组件，避免热重载时导入第二份模块。"""
    package = __package__
    if not package:
        raise RuntimeError("无法确定 Pendo Web 组件的包路径")
    plugin_package = package.rsplit(".", 1)[0]
    try:
        auth_module = import_module(f"{plugin_package}.web.auth")
        server_module = import_module(f"{plugin_package}.web.server")
    except ModuleNotFoundError as exc:
        # 只降级已知可选依赖；本地模块缺失或内部导入错误必须显式暴露。
        missing_root = (exc.name or "").partition(".")[0]
        if missing_root in _OPTIONAL_WEB_IMPORT_ROOTS:
            return None
        raise
    return (
        cast(_LoginCodeIssuer, auth_module.issue_login_code),
        cast(_WidgetTokenGenerator, auth_module.generate_widget_token),
        cast(_WebServer, server_module),
    )


_loaded_components = _load_web_components()
issue_login_code: _LoginCodeIssuer | None
generate_widget_token: _WidgetTokenGenerator | None
web_server: _WebServer | None
if _loaded_components is None:
    issue_login_code = None
    generate_widget_token = None
    web_server = None
else:
    issue_login_code, generate_widget_token, web_server = _loaded_components
del _loaded_components


class WebHandler:
    """管理 Web 服务生命周期并确保凭据只通过私聊发送。"""

    def __init__(self, db: Database):
        """保存启动 Web 服务时需要的数据库实例。"""
        self.db = db

    @staticmethod
    async def _is_managed_running(server: _WebServer) -> bool:
        """判断服务是否由当前插件线程托管。"""
        return bool(server.is_managed_running())

    @handle_command_errors
    async def handle(
        self,
        user_id: str,
        args: str,
        context: PendoContext | None = None,
        group_id: int | None = None,
    ) -> CommandMessage:
        """按精确子命令处理 Web 请求；``group_id`` 仅用于统一 Handler 接口。"""
        server = web_server
        login_code_issuer = issue_login_code
        widget_token_generator = generate_widget_token
        if server is None or login_code_issuer is None or widget_token_generator is None:
            return {
                "status": "error",
                "message": (
                    "❌ 无法使用 Web UI。\n"
                    "请检查是否安装了所需依赖：\n"
                    "python -m pip install -r requirements.txt"
                ),
            }

        command = " ".join(args.casefold().split())
        if command == "token":
            return await self._generate_token(user_id, context, login_code_issuer)
        if command in {"widget-token", "widget_token", "widget token"}:
            return await self._generate_widget_token(user_id, context, widget_token_generator)
        if command in {"widget-revoke", "widget_revoke", "widget revoke"}:
            return await self._revoke_widget_tokens(user_id)
        if command == "start":
            return await self._start(server)
        if command == "stop":
            return await self._stop(server)
        if command == "status":
            return await self._status(server)
        help_result = self._help()
        return {
            "status": "error",
            "message": f"❌ 未知 Web 子命令: {args.strip()}\n\n{help_result['message']}",
        }

    async def _generate_token(
        self,
        user_id: str,
        context: PendoContext | None,
        issuer: _LoginCodeIssuer,
    ) -> CommandMessage:
        """生成一次性登录码，并只在私聊中发送原始 Code。"""
        code = issuer(
            user_id,
            expires_seconds=PendoConfig.WEB_LOGIN_CODE_EXPIRE_SECONDS,
            db=self.db,
        )
        code_days = PendoConfig.WEB_LOGIN_CODE_EXPIRE_SECONDS // (24 * 60 * 60)
        session_days = PendoConfig.WEB_SESSION_EXPIRE_SECONDS // (24 * 60 * 60)
        token_sent = await self._send_private_text(context, user_id, code)

        return self._build_token_result(
            token_sent=token_sent,
            header="🌐 Pendo Web",
            success_line="✅ 已生成一次性登录 Code",
            expiry_text=(
                f"Code {code_days} 天内可兑换一次；登录后浏览器会话保持 {session_days} 天"
            ),
            private_hint="🔒 登录 Code 已单独私聊发送",
            private_copy_hint="💡 复制私聊中的 Code，在 Pendo Web 登录页粘贴使用。",
        )

    async def _generate_widget_token(
        self,
        user_id: str,
        context: PendoContext | None,
        generator: _WidgetTokenGenerator,
    ) -> CommandMessage:
        """生成只读小组件令牌，并确保命令回复不包含令牌正文。"""
        token = await run_sync(
            generator,
            user_id,
            expires_seconds=PendoConfig.WEB_WIDGET_TOKEN_EXPIRE_SECONDS,
            db=self.db,
        )
        expiry_days = PendoConfig.WEB_WIDGET_TOKEN_EXPIRE_SECONDS // (24 * 60 * 60)
        token_sent = await self._send_private_text(
            context,
            user_id,
            "\n".join(
                [
                    "🧩 Pendo Web Widget Token",
                    token,
                    "",
                    "用于 Scriptable 等只读小组件访问。",
                    "📍 接口路径: /api/widget/summary",
                    f"⏳ 有效期: {expiry_days} 天",
                    "💡 首次在 Scriptable App 内运行脚本时粘贴，令牌会存入 Keychain",
                    "💡 Scriptable 可传 section=tasks / ledger / notes / auto",
                ]
            ),
        )
        return self._build_token_result(
            token_sent=token_sent,
            header="🧩 Pendo Web Widget Token",
            success_line="✅ 已生成只读小组件令牌",
            expiry_text=f"{expiry_days} 天",
            private_hint="🔒 Widget Token 已单独私聊发送",
            private_copy_hint="💡 请从私聊复制 token，并在 Scriptable App 内首次运行脚本时粘贴",
            extra_lines=[
                "用于 Scriptable 等只读小组件访问。",
                "📍 接口路径: /api/widget/summary",
                "💡 在 Scriptable 中把 BASE_URL 改成你的 Pendo Web 地址",
                "💡 Scriptable 可传 section=tasks / ledger / notes / auto",
            ],
        )

    async def _revoke_widget_tokens(self, user_id: str) -> CommandMessage:
        """Revoke all unexpired Widget credentials owned by the caller."""

        revoked = await run_sync(self.db.revoke_widget_tokens, user_id)
        if revoked:
            message = f"✅ 已吊销 {revoked} 个 Widget Token；请重新生成并录入 Keychain"
        else:
            message = "ℹ️ 当前没有可吊销的 Widget Token"
        return {"status": "success", "message": message}

    async def _start(self, server: _WebServer) -> CommandMessage:
        """启动插件托管的 Web 服务，并返回可执行的失败原因。"""
        runtime = PendoConfig.runtime()
        url = server.get_url()
        if await run_sync(server.is_running):
            return {
                "status": "success",
                "message": (
                    "🌐 Pendo Web\n"
                    "⚡ 服务已在运行\n\n"
                    f"🌍 地址: {url}\n"
                    f"🔌 端口: {runtime.web_port}\n"
                    "🔑 发送 /pendo web token 获取一次性登录 Code"
                ),
            }
        started = await run_sync(server.start, self.db)
        if started:
            return {
                "status": "success",
                "message": (
                    "🌐 Pendo Web\n"
                    "✅ 服务已启动\n\n"
                    f"🌍 地址: {url}\n"
                    f"🔌 端口: {runtime.web_port}\n"
                    "🔑 下一步: 发送 /pendo web token 获取一次性登录 Code"
                ),
            }
        error_reader = getattr(server, "get_last_error", None)
        raw_detail = error_reader() if callable(error_reader) else None
        detail = " ".join(str(raw_detail).split()) if raw_detail else None
        lines = [
            "🌐 Pendo Web",
            "❌ 服务启动失败",
            "",
            f"🌍 地址: {url}",
            f"🔌 端口: {runtime.web_port}",
        ]
        if detail:
            lines.extend(["", f"🧾 原因: {detail}"])
        lines.extend(
            [
                "",
                "💡 若提示是端口占用，请先停止现有进程；若提示是系统拒绝绑定，请修改 config.json 中的 plugins.pendo.web_port",
            ]
        )
        return {"status": "error", "message": "\n".join(lines)}

    async def _stop(self, server: _WebServer) -> CommandMessage:
        """只停止当前插件托管的服务，不误杀外部同端口服务。"""
        if not await self._is_managed_running(server):
            if await run_sync(server.is_running):
                return {
                    "status": "success",
                    "message": (
                        "🌐 Pendo Web\nℹ️ 服务可访问，但不是由当前插件线程启动；未停止外部服务"
                    ),
                }
            return {
                "status": "success",
                "message": "🌐 Pendo Web\nℹ️ 服务当前未在运行",
            }
        stopped = await run_sync(server.stop)
        if stopped:
            return {
                "status": "success",
                "message": "🌐 Pendo Web\n🛑 服务已停止",
            }
        return {"status": "error", "message": "❌ Web UI 停止失败"}

    @staticmethod
    async def _status(server: _WebServer) -> CommandMessage:
        """返回 Web 服务的可访问状态和入口信息。"""
        running = await run_sync(server.is_running)
        status = "🟢 运行中" if running else "🔴 未启动"
        return {
            "status": "success",
            "message": (
                "🌐 Pendo Web\n"
                f"📡 服务状态: {status}\n\n"
                f"🌍 地址: {server.get_url()}\n"
                f"🔌 端口: {PendoConfig.runtime().web_port}\n"
                "🔑 登录 Code: /pendo web token\n"
                "🧩 Widget Token: /pendo web widget-token"
            ),
        }

    @staticmethod
    def _help() -> CommandMessage:
        """返回 Web 命令帮助。"""
        return {
            "status": "success",
            "message": (
                "🌐 Pendo Web\n"
                "管理网页入口、登录令牌和服务状态。\n\n"
                "可用命令:\n"
                "• /pendo web token  - 生成一次性登录 Code\n"
                "• /pendo web widget-token - 生成 Scriptable 小组件令牌\n"
                "• /pendo web widget-revoke - 吊销自己的全部小组件令牌\n"
                "• /pendo web start  - 启动 Web 服务\n"
                "• /pendo web stop   - 停止 Web 服务\n"
                "• /pendo web status - 查看服务状态"
            ),
        }

    @staticmethod
    async def _send_private_text(
        context: PendoContext | None,
        user_id: str,
        message: str,
    ) -> bool | None:
        """安全发送私聊，并保留已确认、已拒绝与结果未知三种状态。"""
        if context is None:
            return False
        try:
            recipient_id = int(user_id)
        except (TypeError, ValueError):
            return False
        if recipient_id <= 0:
            return False

        action = build_action(segments(message), recipient_id, None)
        if action is None:
            return False
        # 凭据投递错误不能泄露到群聊回复；取消异常继承 BaseException，仍会正常向上传播。
        try:
            accepted = await context.send_action(action)
        except Exception:
            return False
        if accepted is True or accepted is False:
            return accepted
        return None

    @staticmethod
    def _build_token_result(
        *,
        token_sent: bool | None,
        header: str,
        success_line: str,
        expiry_text: str,
        private_hint: str,
        private_copy_hint: str,
        extra_lines: list[str] | None = None,
    ) -> CommandMessage:
        """统一构造不含凭据正文的公开命令结果。"""
        lines = [
            header,
            success_line,
            "",
            *(extra_lines or []),
            f"⏳ 有效期: {expiry_text}",
            "",
        ]
        if token_sent is True:
            lines.extend([private_hint, private_copy_hint])
        elif token_sent is None:
            lines.extend(
                [
                    "⚠️ 已尝试通过私聊发送凭据，但未收到最终投递回执。",
                    "若私聊中没有收到，请检查私聊设置后再重新生成；凭据不会显示在这里。",
                ]
            )
        else:
            lines.extend(
                [
                    "❌ 无法通过私聊安全发送凭据。",
                    "请先允许 Bot 向你发送私聊消息，然后重新执行此命令。",
                ]
            )
        return {
            "status": "success" if token_sent is not False else "error",
            "message": "\n".join(lines),
        }
