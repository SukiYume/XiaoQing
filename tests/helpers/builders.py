"""测试数据构建器"""

import time
from typing import Any


class MessageBuilder:
    """构建测试消息的辅助类"""

    @staticmethod
    def text(content: str, **kwargs) -> dict[str, Any]:
        """构建纯文本消息"""
        return {
            "message_type": kwargs.get("type", "private"),
            "user_id": kwargs.get("user_id", 10001),
            "group_id": kwargs.get("group_id"),
            "raw_message": content,
            "message": [{"type": "text", "data": {"text": content}}],
            "time": int(time.time()),
            "self_id": 9999,
            "message_id": kwargs.get("message_id", 1),
            "sender": {
                "user_id": kwargs.get("user_id", 10001),
                "nickname": kwargs.get("nickname", "TestUser"),
                "card": "",
                "sex": "unknown",
                "age": 0,
                "area": "",
                "level": "",
                "role": "member",
                "title": "",
            },
        }

    @staticmethod
    def group_message(content: str, **kwargs) -> dict[str, Any]:
        """构建群消息"""
        return MessageBuilder.text(
            content,
            type="group",
            group_id=kwargs.get("group_id", 50001),
            **kwargs
        )

    @staticmethod
    def at_message(qq: int, text: str = "", **kwargs) -> dict[str, Any]:
        """构建@消息"""
        segments = [
            {"type": "at", "data": {"qq": str(qq)}},
        ]
        if text:
            segments.append({"type": "text", "data": {"text": f" {text}"}})

        msg = MessageBuilder.group_message(text or f"@{qq}", **kwargs)
        msg["message"] = segments
        msg["raw_message"] = f"[@{qq}] {text}".strip()
        return msg


class PluginBuilder:
    """构建测试插件的辅助类"""

    @staticmethod
    def create_mock_plugin(name: str, commands: list | None = None):
        """创建mock插件对象"""
        from unittest.mock import AsyncMock, MagicMock

        plugin = MagicMock()
        plugin.name = name
        plugin.commands = commands or []
        plugin.on_load = AsyncMock()
        plugin.on_unload = AsyncMock()
        plugin.handle_message = AsyncMock(return_value=None)
        return plugin
