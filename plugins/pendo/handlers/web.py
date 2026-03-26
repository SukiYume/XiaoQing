"""Handler for /pendo web commands."""
from core.plugin_base import build_action, segments

from ..config import PendoConfig
from ..web.auth import generate_token
from ..web import server as web_server
from ..utils.error_handlers import handle_command_errors


class WebHandler:
    def __init__(self, db):
        self.db = db

    @handle_command_errors
    async def handle(self, user_id: str, args: str, context=None, group_id=None):
        """Handle /pendo web subcommands."""
        parts = args.strip().split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""

        if subcmd == "token":
            return await self._generate_token(user_id, context)
        elif subcmd == "start":
            return await self._start(user_id, context)
        elif subcmd == "stop":
            return await self._stop(user_id, context)
        elif subcmd == "status":
            return await self._status(user_id, context)
        else:
            return self._help()

    async def _generate_token(self, user_id: str, context=None):
        token = generate_token(user_id, expires_hours=PendoConfig.WEB_TOKEN_EXPIRE_HOURS)
        url = web_server.get_url()
        running = web_server.is_running()
        status_text = "运行中" if running else "未启动"
        token_sent = await self._send_private_text(
            context,
            user_id,
            f"🔑 Pendo Web 登录 Token\n{token}",
        )

        lines = [
            "🌐 Pendo Web",
            "✅ 已生成登录令牌",
            "",
            f"🌍 地址: {url}",
            f"⏳ 有效期: {PendoConfig.WEB_TOKEN_EXPIRE_HOURS} 小时",
            f"⚙️ 服务状态: {status_text}",
            "",
        ]
        if token_sent:
            lines.extend(
                [
                    "🔒 Token 已单独私聊发送",
                    "💡 打开网页后直接粘贴即可登录",
                ]
            )
        else:
            lines.extend(
                [
                    "🔑 登录 Token:",
                    token,
                    "",
                    "💡 打开网页后直接粘贴即可登录",
                ]
            )
        return {
            "status": "success",
            "message": "\n".join(lines),
        }

    async def _start(self, user_id: str, context):
        url = web_server.get_url()
        if web_server.is_running():
            return {
                "status": "success",
                "message": (
                    "🌐 Pendo Web\n"
                    "⚡ 服务已在运行\n\n"
                    f"🌍 地址: {url}\n"
                    f"🔌 端口: {PendoConfig.WEB_PORT}\n"
                    "🔑 发送 /pendo web token 获取登录令牌"
                ),
            }
        started = web_server.start(self.db)
        if started:
            return {
                "status": "success",
                "message": (
                    "🌐 Pendo Web\n"
                    "✅ 服务已启动\n\n"
                    f"🌍 地址: {url}\n"
                    f"🔌 端口: {PendoConfig.WEB_PORT}\n"
                    "🔑 下一步: 发送 /pendo web token 获取登录令牌"
                ),
            }
        return {"status": "error", "message": "❌ Web UI 启动失败"}

    async def _stop(self, user_id: str, context):
        if not web_server.is_running():
            return {
                "status": "success",
                "message": "🌐 Pendo Web\nℹ️ 服务当前未在运行",
            }
        stopped = web_server.stop()
        if stopped:
            return {
                "status": "success",
                "message": "🌐 Pendo Web\n🛑 服务已停止",
            }
        return {"status": "error", "message": "❌ Web UI 停止失败"}

    async def _status(self, user_id: str, context):
        running = web_server.is_running()
        status = "🟢 运行中" if running else "🔴 未启动"
        return {
            "status": "success",
            "message": (
                "🌐 Pendo Web\n"
                f"📡 服务状态: {status}\n\n"
                f"🌍 地址: {web_server.get_url()}\n"
                f"🔌 端口: {PendoConfig.WEB_PORT}\n"
                "🔑 登录令牌: /pendo web token"
            ),
        }

    def _help(self):
        return {
            "status": "success",
            "message": (
                "🌐 Pendo Web\n"
                "管理网页入口、登录令牌和服务状态。\n\n"
                "可用命令:\n"
                "• /pendo web token  - 生成登录令牌\n"
                "• /pendo web start  - 启动 Web 服务\n"
                "• /pendo web stop   - 停止 Web 服务\n"
                "• /pendo web status - 查看服务状态"
            ),
        }

    async def _send_private_text(self, context, user_id: str, message: str) -> bool:
        if context is None or not hasattr(context, "send_action"):
            return False
        try:
            action = build_action(segments(message), int(user_id), None)
            if not action:
                return False
            await context.send_action(action)
            return True
        except Exception:
            return False
