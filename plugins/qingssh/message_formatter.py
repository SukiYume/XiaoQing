"""QingSSH 用户消息中复用的少量格式化组件。"""

DIVIDER = "━" * 20


def format_section(title: str, *lines: str) -> str:
    """组合标题、分隔线和正文。"""
    return "\n".join((title, DIVIDER, *lines))


def format_error(message: str) -> str:
    """为错误消息添加统一图标。"""
    return f"❌ {message}"


def format_server_added(name: str, host: str, port: int, username: str, auth_type: str) -> str:
    """生成包含连接信息和后续用法的添加成功消息。"""
    auth_display = {"password": "密码", "key": "密钥", "agent": "SSH Agent"}.get(
        auth_type, auth_type
    )

    return (
        f"✅ 服务器添加成功！\n"
        f"{DIVIDER}\n"
        f"名称: {name}\n"
        f"主机: {host}:{port}\n"
        f"用户: {username}\n"
        f"认证: {auth_display}\n"
        f"{DIVIDER}\n"
        f"使用 /ssh {name} 连接"
    )
