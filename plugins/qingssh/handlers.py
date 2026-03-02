"""
SSH 命令处理器

处理各种 SSH 命令（非会话状态）
"""

import logging
from pathlib import Path

from core.plugin_base import segments

from .config import SESSION_TIMEOUT, ADD_SERVER_TIMEOUT, SessionKeys
from .ssh_manager import SSHManager, PARAMIKO_AVAILABLE
from .validators import validate_server_name, validate_port, validate_hostname
from .message_formatter import (
    format_section, format_list_item, format_server_info,
    format_error, format_success, format_info, DIVIDER
)
from .types import Context, OneBotEvent, MessageSegments, Session


logger = logging.getLogger(__name__)


def _show_paramiko_error() -> MessageSegments:
    """显示 paramiko 未安装错误信息"""
    return segments(
        format_section(
            "❌ SSH 功能不可用",
            "",
            "请安装 paramiko 库:",
            "pip install paramiko"
        )
    )


def _handle_existing_session(
    session: Session, manager: SSHManager, context: Context
) -> MessageSegments:
    """处理已存在的会话"""
    server_name = session.get(SessionKeys.SERVER_NAME)
    user_id = str(context.current_user_id)
    group_id = str(context.current_group_id)
    
    if server_name and manager.is_connected(user_id, group_id, server_name):
        return segments(
            format_section(
                "🖥️ 你已在SSH会话中",
                f"当前连接: {server_name}",
                "",
                "直接发送命令执行",
                "输入「退出」/「取消」结束会话"
            )
        )
    return None


async def _show_ssh_help(manager: SSHManager) -> MessageSegments:
    """显示 SSH 帮助信息"""
    servers = manager.list_servers()
    ssh_config_hosts = manager.get_ssh_config_hosts()
    
    lines = []
    
    if servers:
        lines.append("📦 已保存的服务器:")
        for name in servers.keys():
            lines.append(format_list_item(name, level=1))
    
    if ssh_config_hosts:
        lines.append("")
        lines.append("🔧 ~/.ssh/config 中的 Host:")
        for host in ssh_config_hosts[:10]:
            # 标记已导入的
            mark = "✓" if host in servers else " "
            lines.append(f"  {mark} {host}")
        if len(ssh_config_hosts) > 10:
            lines.append(f"  ... (共 {len(ssh_config_hosts)} 个)")
    
    lines.extend([
        "",
        "📝 使用方式:",
        "  /ssh <名称> - 直接连接",
        "  /ssh <用户名>@<名称> - 以指定用户连接",
        "  /ssh导入 - 从 ~/.ssh/config 导入",
        "  /ssh列表 - 查看详细列表",
    ])
    
    if not servers and not ssh_config_hosts:
        lines.append("")
        lines.append("💡 使用 /ssh添加 手动添加服务器")
    
    return segments(format_section("🖥️ SSH 远程控制", *lines))


async def _connect_to_server(
    server_name: str, event: OneBotEvent, context: Context, manager: SSHManager,
    username_override: str = None
) -> MessageSegments:
    """连接到指定服务器"""
    server = manager.get_server(server_name)
    
    # 如果没有保存的配置，检查 ~/.ssh/config
    if not server:
        ssh_config = manager.get_ssh_config_for_host(server_name)
        if ssh_config:
            # 使用 ssh_config 中的配置，创建临时 server 对象用于显示
            server = {
                "host": ssh_config['hostname'],
                "port": ssh_config['port'],
                "username": ssh_config['user'],
            }
        else:
            return segments(format_error(
                f"服务器 '{server_name}' 不存在\n\n使用 /ssh列表 或 /sshconfig 查看可用服务器"
            ))
    
    # 如果指定了用户名覆盖，创建一个新的 server 配置副本
    if username_override:
        server = server.copy()
        server['username'] = username_override
    
    # 连接服务器
    user_id = str(context.current_user_id)
    group_id = str(context.current_group_id)
    success, message = await manager.connect(user_id, group_id, server_name, username_override=username_override)
    
    if not success:
        return segments(message)
    
    await context.create_session(
        initial_data={
            SessionKeys.SERVER_NAME: server_name,
            SessionKeys.HOST: server["host"],
            SessionKeys.COMMAND_COUNT: 0,
            SessionKeys.STATE: "connected",
            SessionKeys.USERNAME_OVERRIDE: username_override,  # 保存用户名覆盖信息
        },
        timeout=SESSION_TIMEOUT,
    )
    
    logger.info(
        "SSH session started: server=%s, host=%s, user=%s, requester=%s",
        server_name, server['host'], server['username'], context.current_user_id
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
            "💡 输入「帮助」查看可用命令"
        )
    )


async def handle_ssh_main(
    args: str,
    event: OneBotEvent,
    context: Context,
    manager: SSHManager
) -> MessageSegments:
    """处理主 SSH 命令"""
    
    # 检查 paramiko 是否可用
    if not PARAMIKO_AVAILABLE:
        return _show_paramiko_error()

    # 检查是否已有进行中的会话（只检查 qingssh 的会话）
    existing_session = await context.get_session()
    if existing_session and existing_session.plugin_name == "qingssh":
        result = _handle_existing_session(existing_session, manager, context)
        if result:
            return result
        # 会话存在但连接已断开
        await context.end_session()
    
    # 解析参数
    args = args.strip()
    
    # 如果没有参数，显示帮助
    if not args:
        return await _show_ssh_help(manager)
    
    # 解析 user@server 格式
    username_override = None
    server_name = args
    
    if '@' in args:
        # 分离用户名和服务器名
        parts = args.split('@', 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            username_override = parts[0]
            server_name = parts[1]
    
    # 尝试连接到指定服务器
    return await _connect_to_server(server_name, event, context, manager, username_override=username_override)


async def handle_ssh_disconnect(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """断开连接"""
    session = await context.get_session()
    
    user_id = str(context.current_user_id)
    group_id = str(context.current_group_id)

    # 只处理属于 qingssh 的会话
    if session and session.plugin_name == "qingssh":
        server_name = session.get(SessionKeys.SERVER_NAME)
        if server_name:
            manager.disconnect(user_id, group_id, server_name)
        await context.end_session()
        return segments(f"🔌 已断开SSH连接: {server_name}")
    
    # 如果指定了服务器名
    if args.strip():
        server_name = args.strip()
        if manager.disconnect(user_id, group_id, server_name):
            return segments(f"🔌 已断开SSH连接: {server_name}")
        else:
            return segments(f"❌ 服务器 '{server_name}' 未连接")
    
    return segments("❌ 当前没有活跃的SSH会话")


async def handle_ssh_list(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """列出服务器"""
    servers = manager.list_servers()
    
    if not servers:
        return segments(
            "📋 SSH 服务器列表\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "暂无保存的服务器\n"
            "\n使用 /ssh添加 来添加服务器"
        )
    
    lines = ["📋 SSH 服务器列表", "━━━━━━━━━━━━━━━━━━"]
    
    user_id = str(context.current_user_id)
    group_id = str(context.current_group_id)
    
    for name, config in servers.items():
        status = "🟢" if manager.is_connected(user_id, group_id, name) else "⚪"
        lines.append(f"{status} {name}")
        if config.get('proxycommand'):
            # 显示跳板机信息
            lines.append(f"   {config['username']}@{config['host']} (跳板机)")
        else:
            lines.append(f"   {config['username']}@{config['host']}:{config['port']}")
    
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("使用 /ssh <名称> 连接服务器")
    
    return segments("\n".join(lines))


async def handle_ssh_status(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """显示当前 SSH 连接状态"""
    active_conns = manager.get_active_connections()
    
    if not active_conns:
        return segments("📊 当前没有任何活跃的 SSH 连接")
    
    lines = [
        f"📊 当前活跃 SSH 连接: {len(active_conns)} 个",
        "━━━━━━━━━━━━━━━━━━"
    ]
    
    for conn in active_conns:
        s_name = conn['server_name']
        u_id = conn['user_id']
        g_id = conn['group_id']
        
        info = f"🔌 {s_name}"
        if g_id != 'None':
            info += f" [群: {g_id}]"
        info += f" [用户: {u_id}]"
        
        lines.append(info)
        
    lines.append("━━━━━━━━━━━━━━━━━━")
    
    return segments("\n".join(lines))


async def handle_ssh_add(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """添加服务器 - 开始多轮对话"""
    
    # 检查是否已有会话（只检查 qingssh 的会话）
    existing_session = await context.get_session()
    if existing_session and existing_session.plugin_name == "qingssh":
        return segments("❌ 请先结束当前会话后再添加服务器")
    
    # 解析参数：名称 主机 [端口] [用户名]
    parts = args.strip().split()
    
    if len(parts) < 2:
        # 开始引导式添加
        await context.create_session(
            initial_data={
                "state": "adding",
                "step": "name",
                "server_config": {},
            },
            timeout=ADD_SERVER_TIMEOUT,
        )
        return segments(
            "➕ 添加SSH服务器\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "请输入服务器名称（用于标识）:\n"
            "\n💡 发送「取消」退出添加"
        )
    
    # 快速添加模式
    name = parts[0]
    
    # 验证服务器名称
    is_valid, error_msg = validate_server_name(name)
    if not is_valid:
        return segments(format_error(error_msg))
    
    host = parts[1]
    
    # 验证主机地址
    is_valid, error_msg = validate_hostname(host)
    if not is_valid:
        return segments(format_error(error_msg))
    
    # 解析端口号
    port = 22
    if len(parts) > 2:
        is_valid, port, error_msg = validate_port(parts[2])
        if not is_valid:
            return segments(format_error(error_msg))
    
    username = parts[3] if len(parts) > 3 else "root"
    
    # 检查是否已存在
    if manager.get_server(name):
        return segments(f"❌ 服务器 '{name}' 已存在")
    
    # 添加服务器（无密码，需要后续配置）
    await manager.add_server(name, host, port, username)
    
    return segments(
        f"✅ 服务器已添加\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"名称: {name}\n"
        f"主机: {host}:{port}\n"
        f"用户: {username}\n"
        f"认证: 待配置\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ 请编辑 data/servers.json 添加密码或密钥路径"
    )


async def handle_ssh_remove(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """删除服务器"""
    name = args.strip()
    
    if not name:
        return segments("❌ 请指定要删除的服务器名称\n\n用法: /ssh删除 <服务器名>")
    
    if await manager.remove_server(name):
        # 此时只能尽量断开所有用户的连接(不支持)，目前仅断开请求用户的连接
        manager.disconnect(str(context.current_user_id), str(context.current_group_id), name)
        return segments(f"✅ 服务器 '{name}' 已删除")
    else:
        return segments(f"❌ 服务器 '{name}' 不存在")


async def handle_ssh_import(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """从 ~/.ssh/config 导入服务器"""
    
    # 检查 ~/.ssh/config 是否存在
    ssh_config_path = Path.home() / ".ssh" / "config"
    if not ssh_config_path.exists():
        return segments(
            "❌ 未找到 ~/.ssh/config 文件\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "请确保文件存在，或使用 /ssh添加 手动添加服务器"
        )
    
    # 重新加载 ssh config
    await manager._load_ssh_config()
    
    args = args.strip()
    
    # 如果指定了 Host 名称，导入单个
    if args:
        if args.lower() == "all" or args == "全部":
            # 导入全部
            count, imported = await manager.import_all_from_ssh_config()
            if count == 0:
                hosts = manager.get_ssh_config_hosts()
                if not hosts:
                    return segments("❌ ~/.ssh/config 中没有找到有效的 Host 配置")
                return segments("✅ 所有 Host 都已导入过，无需重复导入")
            
            lines = [f"✅ 成功导入 {count} 个服务器", "━━━━━━━━━━━━━━━━━━"]
            for name in imported[:10]:
                lines.append(f"  ✓ {name}")
            if len(imported) > 10:
                lines.append(f"  ... 及其他 {len(imported) - 10} 个")
            lines.append("━━━━━━━━━━━━━━━━━━")
            lines.append("使用 /ssh <名称> 连接")
            return segments("\n".join(lines))
        
        # 导入单个
        success, message = await manager.import_from_ssh_config(args)
        if success:
            return segments(f"{message}\n\n使用 /ssh {args} 连接")
        else:
            return segments(f"❌ {message}")
    
    # 没有参数，显示可导入的列表
    hosts = manager.get_ssh_config_hosts()
    if not hosts:
        return segments(
            "❌ ~/.ssh/config 中没有找到有效的 Host 配置\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "请检查 ~/.ssh/config 文件格式是否正确"
        )
    
    servers = manager.list_servers()
    
    lines = ["📥 从 ~/.ssh/config 导入", "━━━━━━━━━━━━━━━━━━"]
    lines.append("可导入的 Host:")
    
    new_hosts = []
    existing_hosts = []
    
    for host in hosts:
        if host in servers:
            existing_hosts.append(host)
        else:
            new_hosts.append(host)
    
    if new_hosts:
        for host in new_hosts[:15]:
            config = manager.get_ssh_config_for_host(host)
            if config:
                if config.get('proxycommand'):
                    lines.append(f"  • {host} → {config['user']}@{config['hostname']} (跳板机)")
                else:
                    lines.append(f"  • {host} → {config['user']}@{config['hostname']}")
            else:
                lines.append(f"  • {host}")
        if len(new_hosts) > 15:
            lines.append(f"  ... (共 {len(new_hosts)} 个可导入)")
    else:
        lines.append("  (所有 Host 都已导入)")
    
    if existing_hosts:
        lines.append("")
        lines.append(f"已导入: {len(existing_hosts)} 个")
    
    lines.append("")
    lines.append("📝 用法:")
    lines.append("  /ssh导入 <Host名> - 导入单个")
    lines.append("  /ssh导入 all - 导入全部")
    
    return segments("\n".join(lines))


async def handle_ssh_config_list(
    args: str, event: OneBotEvent, context: Context, manager: SSHManager
) -> MessageSegments:
    """列出 ~/.ssh/config 中的所有 Host"""
    
    # 重新加载
    await manager._load_ssh_config()
    
    hosts = manager.get_ssh_config_hosts()
    
    if not hosts:
        ssh_config_path = Path.home() / ".ssh" / "config"
        if not ssh_config_path.exists():
            return segments("❌ 未找到 ~/.ssh/config 文件")
        return segments("❌ ~/.ssh/config 中没有找到有效的 Host 配置")
    
    lines = ["🔧 ~/.ssh/config 配置", "━━━━━━━━━━━━━━━━━━"]
    
    servers = manager.list_servers()
    
    for host in hosts[:20]:
        config = manager.get_ssh_config_for_host(host)
        imported = "✓" if host in servers else " "
        
        if config:
            identity = ""
            if config.get('identityfile'):
                key_file = Path(config['identityfile'][0]).name
                identity = f" 🔑{key_file}"
            lines.append(f"{imported} {host}")
            if config.get('proxycommand'):
                # 显示跳板机信息
                lines.append(f"    {config['user']}@{config['hostname']} 🔀跳板机{identity}")
            else:
                lines.append(f"    {config['user']}@{config['hostname']}:{config['port']}{identity}")
        else:
            lines.append(f"{imported} {host}")
    
    if len(hosts) > 20:
        lines.append(f"... (共 {len(hosts)} 个 Host)")
    
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("✓ = 已导入到插件")
    lines.append("")
    lines.append("💡 可直接使用 /ssh <Host名> 连接")
    lines.append("💡 使用 /ssh导入 <Host名> 保存配置")
    
    return segments("\n".join(lines))
