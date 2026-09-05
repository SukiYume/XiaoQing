"""处理 QingSSH 的连接、配置和状态等一次性命令。"""

import logging
from contextlib import suppress
from pathlib import Path

from core.args import tokenize
from core.sensitive_audit import summarize_sensitive

from .audit import audit_request_id
from .config import ADD_SERVER_TIMEOUT, SESSION_TIMEOUT, SessionKeys
from .message_formatter import (
    DIVIDER,
    format_error,
    format_section,
)
from .ssh_manager import PARAMIKO_AVAILABLE, SSHManager
from .types import Context, MessageSegments, OneBotEvent, segments
from .validators import validate_hostname, validate_port, validate_server_name, validate_username

logger = logging.getLogger(__name__)


def _show_ssh_help(manager: SSHManager) -> MessageSegments:
    """显示已保存目标、SSH config 目标和常用入口。"""
    servers          = manager.list_servers()
    ssh_config_hosts = manager.get_ssh_config_hosts()

    lines: list[str] = []

    if servers:
        lines.append("📦 已保存的服务器:")
        for name in sorted(servers):
            lines.append(f"  • {name}")

    if ssh_config_hosts:
        if lines:
            lines.append("")
        lines.append("🔧 ~/.ssh/config 中的 Host:")
        for host in ssh_config_hosts[:10]:
            mark = "✓" if host in servers else " "
            lines.append(f"  {mark} {host}")
        if len(ssh_config_hosts) > 10:
            lines.append(f"  ... (共 {len(ssh_config_hosts)} 个)")

    lines.extend(
        [
            "",
            "📝 使用方式:",
            "  /ssh <名称> - 直接连接",
            "  /ssh <用户名>@<名称> - 以指定用户连接",
            "  /ssh导入 - 从 ~/.ssh/config 导入",
            "  /ssh列表 - 查看详细列表",
            "  连接后输入 showimg <路径或通配符> [--page N] - 分页发送图片",
        ]
    )

    if not servers and not ssh_config_hosts:
        lines.append("")
        lines.append("💡 使用 /ssh添加 手动添加服务器")

    return segments(format_section("🖥️ SSH 远程控制", *lines))


async def _connect_to_server(
    server_name: str,
    context: Context,
    manager: SSHManager,
    username_override: str | None = None,
) -> MessageSegments:
    """连接到指定服务器，并在会话创建失败时回收连接。"""
    server = manager.get_server(server_name)

    # 未持久化的名称仍可直接引用显式声明的 OpenSSH Host。
    if server is None:
        ssh_config = manager.get_ssh_config_for_host(server_name)
        if ssh_config is not None:
            server = {
                "host": ssh_config["hostname"],
                "port": ssh_config["port"],
                "username": ssh_config["user"],
            }
        else:
            return segments(
                format_error(
                    f"服务器 '{server_name}' 不存在\n\n使用 /ssh列表 或 /sshconfig 查看可用服务器"
                )
            )

    if username_override:
        server             = server.copy()
        server["username"] = username_override

    user_id  = str(context.current_user_id)
    group_id = str(context.current_group_id)
    success, message = await manager.connect(
        user_id, group_id, server_name, username_override=username_override
    )

    if not success:
        return segments(message)

    try:
        await context.create_session(
            initial_data={
                SessionKeys.SERVER_NAME: server_name,
                SessionKeys.HOST: server["host"],
                SessionKeys.COMMAND_COUNT: 0,
                SessionKeys.STATE: "connected",
                SessionKeys.USERNAME_OVERRIDE: username_override,
            },
            timeout=SESSION_TIMEOUT,
        )
    except BaseException:
        # 会话没有建立时不能留下无法再由用户管理的 SSH 客户端。
        with suppress(Exception):
            manager.disconnect(user_id, group_id, server_name)
        raise

    target_audit = summarize_sensitive(
        "\0".join((server_name, str(server["host"]), str(server["username"])))
    )
    logger.info(
        "SSH audit operation=connect status=success request_id=%s "
        "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s",
        audit_request_id(context),
        target_audit.kind,
        target_audit.length,
        target_audit.byte_length,
        target_audit.fingerprint,
    )

    return segments(
        format_section(
            "🖥️ SSH 会话已开始",
            f"服务器: {server_name}",
            f"主机: {server['host']}",
            f"用户: {server['username']}",
            DIVIDER,
            "🎯 直接发送命令开始执行",
            "💡 输入「退出」/「取消」结束会话",
            "💡 输入「帮助」查看可用命令",
        )
    )


async def handle_ssh_main(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """处理主 SSH 命令。"""

    if not PARAMIKO_AVAILABLE:
        return segments(
            format_section("❌ SSH 功能不可用", "", "请安装 paramiko 库:", "pip install paramiko")
        )

    existing_session = await context.get_session()
    if existing_session is not None:
        if existing_session.plugin_name != "qingssh":
            return segments("❌ 请先结束当前会话后再连接 SSH")
        server_name = existing_session.get(SessionKeys.SERVER_NAME)
        user_id     = str(context.current_user_id)
        group_id    = str(context.current_group_id)
        if server_name and manager.is_connected(user_id, group_id, server_name):
            return segments(
                format_section(
                    "🖥️ 你已在SSH会话中",
                    f"当前连接: {server_name}",
                    "",
                    "直接发送命令执行",
                    "输入「退出」/「取消」结束会话",
                )
            )
        # 清除已经失去底层连接的旧 QingSSH 会话。
        await context.end_session()

    args = args.strip()
    if not args:
        return _show_ssh_help(manager)

    username_override = None
    server_name       = args

    if "@" in args:
        username_override, server_name = args.split("@", 1)
        if not username_override or not server_name or "@" in server_name:
            return segments("❌ 连接格式错误\n用法: /ssh <服务器名> 或 /ssh <用户名>@<服务器名>")
        is_valid, error_msg = validate_username(username_override)
        if not is_valid:
            return segments(format_error(error_msg))

    return await _connect_to_server(
        server_name, context, manager, username_override=username_override
    )


async def handle_ssh_disconnect(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """断开指定连接，未指定名称时处理当前 QingSSH 会话。"""
    session = await context.get_session()

    user_id       = str(context.current_user_id)
    group_id      = str(context.current_group_id)
    target_server = args.strip() or None
    if target_server and any(character.isspace() for character in target_server):
        return segments("❌ 用法: /ssh disconnect [服务器名]")

    if session is not None and session.plugin_name == "qingssh":
        current_server = session.get(SessionKeys.SERVER_NAME)
        server_name    = target_server or current_server

        if server_name and manager.disconnect(user_id, group_id, server_name):
            if current_server == server_name:
                await context.end_session()
            return segments(f"🔌 已断开SSH连接: {server_name}")

        if target_server:
            return segments(f"❌ 服务器 '{target_server}' 未连接")

        if current_server:
            await context.end_session()
            return segments(f"🔌 已断开SSH连接: {current_server}")
        return segments("❌ 当前没有活跃的SSH会话")

    if target_server:
        if manager.disconnect(user_id, group_id, target_server):
            return segments(f"🔌 已断开SSH连接: {target_server}")
        return segments(f"❌ 服务器 '{target_server}' 未连接")

    return segments("❌ 当前没有活跃的SSH会话")


async def handle_ssh_list(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """列出已保存服务器及当前用户的连接状态。"""
    if args.strip():
        return segments("❌ 用法: /ssh list")
    servers = manager.list_servers()

    if not servers:
        return segments(
            "📋 SSH 服务器列表\n━━━━━━━━━━━━━━━━━━\n暂无保存的服务器\n\n使用 /ssh添加 来添加服务器"
        )

    lines = ["📋 SSH 服务器列表", DIVIDER]

    user_id  = str(context.current_user_id)
    group_id = str(context.current_group_id)

    for name, config in sorted(servers.items()):
        status = "🟢" if manager.is_connected(user_id, group_id, name) else "⚪"
        lines.append(f"{status} {name}")
        username = config.get("username", "?")
        host     = config.get("host", "?")
        port     = config.get("port", "?")
        if config.get("proxycommand") or config.get("proxyjump"):
            lines.append(f"   {username}@{host} (跳板机)")
        else:
            lines.append(f"   {username}@{host}:{port}")

    lines.append(DIVIDER)
    lines.append("使用 /ssh <名称> 连接服务器")

    return segments("\n".join(lines))


async def handle_ssh_status(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """显示全部活跃 SSH 连接。"""
    if args.strip():
        return segments("❌ 用法: /ssh status")
    active_conns = manager.get_active_connections()

    if not active_conns:
        return segments("📊 当前没有任何活跃的 SSH 连接")

    lines = [f"📊 当前活跃 SSH 连接: {len(active_conns)} 个", DIVIDER]

    for conn in active_conns:
        s_name = conn["server_name"]
        u_id   = conn["user_id"]
        g_id   = conn["group_id"]

        info = f"🔌 {s_name}"
        if g_id is not None:
            info += f" [群: {g_id}]"
        info += f" [用户: {u_id}]"

        lines.append(info)

    lines.append(DIVIDER)

    return segments("\n".join(lines))


async def handle_ssh_add(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """快速添加服务器，或在无参数时开始引导式会话。"""

    existing_session = await context.get_session()
    if existing_session is not None:
        return segments("❌ 请先结束当前会话后再添加服务器")

    try:
        parts = tokenize(args, strict=True)
    except ValueError:
        return segments("❌ 参数中的引号没有闭合")

    if not parts:
        await context.create_session(
            initial_data={
                SessionKeys.STATE: "adding",
                SessionKeys.STEP: "name",
                SessionKeys.SERVER_CONFIG: {},
            },
            timeout=ADD_SERVER_TIMEOUT,
        )
        return segments(
            f"➕ 添加SSH服务器\n{DIVIDER}\n请输入服务器名称（用于标识）:\n\n💡 发送「取消」退出添加"
        )

    if len(parts) < 2:
        return segments("❌ 参数不足\n用法: /ssh添加 <名称> <主机> [端口] [用户名]")
    if len(parts) > 4:
        return segments("❌ 参数过多\n用法: /ssh添加 <名称> <主机> [端口] [用户名]")

    name = parts[0]
    is_valid, error_msg = validate_server_name(name)
    if not is_valid:
        return segments(format_error(error_msg))

    host = parts[1]

    is_valid, error_msg = validate_hostname(host)
    if not is_valid:
        return segments(format_error(error_msg))

    port = 22
    if len(parts) > 2:
        is_valid, port, error_msg = validate_port(parts[2])
        if not is_valid:
            return segments(format_error(error_msg))

    username = parts[3] if len(parts) > 3 else "root"
    is_valid, error_msg = validate_username(username)
    if not is_valid:
        return segments(format_error(error_msg))

    # 快速模式不接收聊天密码，默认使用 SSH Agent/本机密钥。
    if not await manager.add_server(name, host, port, username, auth_type="agent"):
        return segments(f"❌ 服务器 '{name}' 已存在")

    return segments(
        f"✅ 服务器已添加\n"
        f"{DIVIDER}\n"
        f"名称: {name}\n"
        f"主机: {host}:{port}\n"
        f"用户: {username}\n"
        f"认证: SSH Agent/本机密钥\n"
        f"{DIVIDER}\n"
        f"💡 如需密码认证，请在管理员私聊中使用引导式 /ssh添加"
    )


async def handle_ssh_remove(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """删除服务器配置及相关连接。"""
    name = args.strip()

    if not name:
        return segments("❌ 请指定要删除的服务器名称\n\n用法: /ssh删除 <服务器名>")
    if any(character.isspace() for character in name):
        return segments("❌ 用法: /ssh remove <服务器名>")

    if await manager.remove_server(name):
        return segments(f"✅ 服务器 '{name}' 已删除")
    return segments(f"❌ 服务器 '{name}' 不存在")


async def handle_ssh_import(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """从 ``~/.ssh/config`` 导入服务器。"""

    ssh_config_path = Path.home() / ".ssh" / "config"
    if not ssh_config_path.exists():
        return segments(
            "❌ 未找到 ~/.ssh/config 文件\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "请确保文件存在，或使用 /ssh添加 手动添加服务器"
        )

    await manager.reload_ssh_config()

    args = args.strip()

    if args:
        if any(character.isspace() for character in args):
            return segments("❌ 用法: /ssh import [Host名|all|全部]")
        if args.lower() == "all" or args == "全部":
            count, imported = await manager.import_all_from_ssh_config()
            if count == 0:
                hosts = manager.get_ssh_config_hosts()
                if not hosts:
                    return segments("❌ ~/.ssh/config 中没有找到有效的 Host 配置")
                return segments("✅ 所有 Host 都已导入过，无需重复导入")

            lines = [f"✅ 成功导入 {count} 个服务器", DIVIDER]
            for name in imported[:10]:
                lines.append(f"  ✓ {name}")
            if len(imported) > 10:
                lines.append(f"  ... 及其他 {len(imported) - 10} 个")
            lines.append(DIVIDER)
            lines.append("使用 /ssh <名称> 连接")
            return segments("\n".join(lines))

        success, message = await manager.import_from_ssh_config(args)
        if success:
            return segments(f"{message}\n\n使用 /ssh {args} 连接")
        return segments(f"❌ {message}")

    hosts = manager.get_ssh_config_hosts()
    if not hosts:
        return segments(
            "❌ ~/.ssh/config 中没有找到有效的 Host 配置\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "请检查 ~/.ssh/config 文件格式是否正确"
        )

    servers = manager.list_servers()

    lines = ["📥 从 ~/.ssh/config 导入", DIVIDER]
    lines.append("可导入的 Host:")

    new_hosts      = [host for host in hosts if host not in servers]
    existing_count = len(hosts) - len(new_hosts)

    if new_hosts:
        for host in new_hosts[:15]:
            config = manager.get_ssh_config_for_host(host)
            if config:
                if config.get("proxycommand") or config.get("proxyjump"):
                    lines.append(f"  • {host} → {config['user']}@{config['hostname']} (跳板机)")
                else:
                    lines.append(f"  • {host} → {config['user']}@{config['hostname']}")
            else:
                lines.append(f"  • {host}")
        if len(new_hosts) > 15:
            lines.append(f"  ... (共 {len(new_hosts)} 个可导入)")
    else:
        lines.append("  (所有 Host 都已导入)")

    if existing_count:
        lines.append("")
        lines.append(f"已导入: {existing_count} 个")

    lines.append("")
    lines.append("📝 用法:")
    lines.append("  /ssh导入 <Host名> - 导入单个")
    lines.append("  /ssh导入 all - 导入全部")

    return segments("\n".join(lines))


async def handle_ssh_config_list(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """列出 ``~/.ssh/config`` 中可直接使用的 Host。"""

    if args.strip():
        return segments("❌ 用法: /ssh config")

    await manager.reload_ssh_config()

    hosts = manager.get_ssh_config_hosts()

    if not hosts:
        ssh_config_path = Path.home() / ".ssh" / "config"
        if not ssh_config_path.exists():
            return segments("❌ 未找到 ~/.ssh/config 文件")
        return segments("❌ ~/.ssh/config 中没有找到有效的 Host 配置")

    lines = ["🔧 ~/.ssh/config 配置", DIVIDER]

    servers = manager.list_servers()

    for host in hosts[:20]:
        config   = manager.get_ssh_config_for_host(host)
        imported = "✓" if host in servers else " "

        if config:
            identity = ""
            if config.get("identityfile"):
                key_file = Path(config["identityfile"][0]).name
                identity = f" 🔑{key_file}"
            lines.append(f"{imported} {host}")
            if config.get("proxycommand") or config.get("proxyjump"):
                lines.append(f"    {config['user']}@{config['hostname']} 🔀跳板机{identity}")
            else:
                lines.append(
                    f"    {config['user']}@{config['hostname']}:{config['port']}{identity}"
                )
        else:
            lines.append(f"{imported} {host}")

    if len(hosts) > 20:
        lines.append(f"... (共 {len(hosts)} 个 Host)")

    lines.append(DIVIDER)
    lines.append("✓ = 已导入到插件")
    lines.append("")
    lines.append("💡 可直接使用 /ssh <Host名> 连接")
    lines.append("💡 使用 /ssh导入 <Host名> 保存配置")

    return segments("\n".join(lines))
