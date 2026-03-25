"""Handler for /pendo web commands."""
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
            return await self._generate_token(user_id)
        elif subcmd == "start":
            return await self._start(user_id, context)
        elif subcmd == "stop":
            return await self._stop(user_id, context)
        elif subcmd == "status":
            return await self._status(user_id, context)
        else:
            return self._help()

    async def _generate_token(self, user_id: str):
        token = generate_token(user_id, expires_hours=PendoConfig.WEB_TOKEN_EXPIRE_HOURS)
        url = web_server.get_url()
        running = web_server.is_running()
        status_text = "运行中" if running else "未启动"
        return {
            "status": "success",
            "message": (
                f"🔑 Web UI 登录 Token（{PendoConfig.WEB_TOKEN_EXPIRE_HOURS}小时有效）:\n\n"
                f"`{token}`\n\n"
                f"Web 服务状态: {status_text}\n"
                f"地址: {url}"
            ),
        }

    async def _start(self, user_id: str, context):
        if web_server.is_running():
            return {"status": "success", "message": f"⚡ Web UI 已在运行: {web_server.get_url()}"}
        started = web_server.start(self.db)
        if started:
            return {"status": "success", "message": f"✅ Web UI 已启动: {web_server.get_url()}"}
        return {"status": "error", "message": "❌ Web UI 启动失败"}

    async def _stop(self, user_id: str, context):
        if not web_server.is_running():
            return {"status": "success", "message": "Web UI 未在运行"}
        stopped = web_server.stop()
        if stopped:
            return {"status": "success", "message": "✅ Web UI 已停止"}
        return {"status": "error", "message": "❌ Web UI 停止失败"}

    async def _status(self, user_id: str, context):
        running = web_server.is_running()
        status = "🟢 运行中" if running else "🔴 未启动"
        return {
            "status": "success",
            "message": f"Web UI 状态: {status}\n地址: {web_server.get_url()}\n端口: {PendoConfig.WEB_PORT}",
        }

    def _help(self):
        return {
            "status": "success",
            "message": (
                "📡 Web UI 管理:\n"
                "  /pendo web token  - 生成登录 Token\n"
                "  /pendo web start  - 启动 Web 服务\n"
                "  /pendo web stop   - 停止 Web 服务\n"
                "  /pendo web status - 查看服务状态"
            ),
        }
