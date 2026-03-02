"""
SSH远程控制插件

通过多轮对话管理SSH连接和执行命令，支持 ~/.ssh/config。

功能：
1. 用户发送 /ssh 开始SSH会话
2. 支持保存和管理SSH服务器配置
3. 连接后可直接发送命令执行
4. 支持多服务器管理
5. 支持从 ~/.ssh/config 导入配置

多轮对话实现要点：
1. 使用 context.create_session() 创建会话
2. 实现 handle_session() 函数处理会话消息
3. 使用 session.get()/set() 存取会话数据
4. 使用 context.end_session() 结束会话

安全提示：
- 敏感信息（如密码）应通过 secrets.json 配置
- 建议使用密钥认证而非密码认证
- 命令执行有超时限制

模块结构：
- main.py: 命令入口和路由
- config.py: 配置常量
- ssh_manager.py: SSH 连接管理器
- handlers.py: 命令处理器
- session_handlers.py: 会话处理器
- validators.py: 输入验证工具
- message_formatter.py: 消息格式化工具
- types.py: 类型定义
"""

import logging
from typing import Any

from core.plugin_base import segments
from core.args import parse

# 使用相对导入
from .ssh_manager import get_manager
from .types import Context, Session, OneBotEvent, MessageSegments
from .handlers import (
    handle_ssh_main,
    handle_ssh_disconnect,
    handle_ssh_list,
    handle_ssh_add,
    handle_ssh_remove,
    handle_ssh_import,
    handle_ssh_config_list,
    handle_ssh_status,
)
from .session_handlers import handle_session as _handle_session

logger = logging.getLogger(__name__)

# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    logger.info("QingSSH plugin initialized")

# ============================================================
# 主命令入口
# ============================================================

async def handle(command: str, args: str, event: OneBotEvent, context: Context) -> MessageSegments:
    """
    命令处理入口
    
    根据不同子命令分发到对应的处理函数。
    """
    try:
        manager = await get_manager(context)
        parsed = parse(args)
        
        # 主 SSH 命令使用统一入口
        if command in {"ssh", "SSH", "远程", "ssh连接", "sshconnect"}:
            # 如果没有参数或第一个参数不是子命令，显示帮助或连接
            if not parsed or parsed.first.lower() not in {
                "help", "帮助", "?", "list", "列表", "add", "添加",
                "remove", "删除", "del", "import", "导入", "config", "配置",
                "status", "状态", "disconnect", "断开"
            }:
                # 处理连接或显示帮助
                return await handle_ssh_main(args, event, context, manager)
            
            # 处理子命令
            subcommand = parsed.first.lower()
            rest_args = parsed.rest(1) if parsed else ""
            
            if subcommand in {"help", "帮助", "?"}:
                return segments(_show_help())
            elif subcommand in {"list", "列表"}:
                return await handle_ssh_list(rest_args, event, context, manager)
            elif subcommand in {"add", "添加"}:
                return await handle_ssh_add(rest_args, event, context, manager)
            elif subcommand in {"remove", "删除", "del"}:
                return await handle_ssh_remove(rest_args, event, context, manager)
            elif subcommand in {"import", "导入"}:
                return await handle_ssh_import(rest_args, event, context, manager)
            elif subcommand in {"config", "配置"}:
                return await handle_ssh_config_list(rest_args, event, context, manager)
            elif subcommand in {"status", "状态"}:
                return await handle_ssh_status(rest_args, event, context, manager)
            elif subcommand in {"disconnect", "断开"}:
                return await handle_ssh_disconnect(rest_args, event, context, manager)
            
            return segments(f"未知子命令: {subcommand}\n输入 /ssh help 查看帮助")
        
        # 兼容旧的独立命令（保持向后兼容）
        elif command in {"ssh断开", "sshdisconnect", "ssh退出"}:
            return await handle_ssh_disconnect(args, event, context, manager)
        elif command in {"ssh列表", "sshlist", "ssh服务器"}:
            return await handle_ssh_list(args, event, context, manager)
        elif command in {"ssh添加", "sshadd"}:
            return await handle_ssh_add(args, event, context, manager)
        elif command in {"ssh删除", "sshremove", "sshdel"}:
            return await handle_ssh_remove(args, event, context, manager)
        elif command in {"ssh导入", "sshimport"}:
            return await handle_ssh_import(args, event, context, manager)
        elif command in {"sshconfig", "ssh配置"}:
            return await handle_ssh_config_list(args, event, context, manager)
        elif command in {"ssh状态", "sshstatus", "ssh连接数", "sshactive"}:
            return await handle_ssh_status(args, event, context, manager)
        
        return segments("❓ 未知命令")
        
    except Exception as e:
        logger.exception("QingSSH handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")

# ============================================================
# 辅助函数
# ============================================================

def _show_help() -> str:
    """显示帮助信息"""
    return """
🖥️ **SSH 远程控制**

**基本命令:**
• /ssh - 显示已保存的服务器和帮助
• /ssh help - 显示此帮助
• /ssh <服务器名> - 连接到服务器
• /ssh <用户名>@<服务器名> - 以指定用户连接

**服务器管理:**
• /ssh list - 查看已保存的服务器列表
• /ssh add - 添加服务器（引导式）
• /ssh add <名称> <主机> [端口] [用户名] - 快速添加
• /ssh remove <名称> - 删除服务器
• /ssh import - 从 ~/.ssh/config 导入配置
• /ssh config - 查看 ~/.ssh/config 中的 Host

**连接管理:**
• /ssh status - 查看当前连接状态
• /ssh disconnect - 断开当前连接
• /ssh disconnect <服务器名> - 断开指定连接

**SSH 会话中:**
• 直接输入命令 - 执行 Shell 命令
• cd <目录> - 切换工作目录
• help / 帮助 - 查看会话中的命令
• 输入「退出」/「取消」- 结束会话

**特性:**
• 支持多服务器管理
• 支持密钥和密码认证
• 命令历史记录
• 自动补全工作目录
• 10 分钟无操作自动断开

**安全提示:**
• 建议使用 SSH 密钥而非密码
• 敏感信息请通过配置文件管理
• 命令执行有 30 秒超时限制
""".strip()

# ============================================================
# 会话处理（多轮对话核心）
# ============================================================

async def handle_session(
    text: str,
    event: OneBotEvent,
    context: Context,
    session: Session,
) -> MessageSegments:
    """
    处理会话消息
    
    当用户有活跃会话时，Dispatcher 会调用这个函数处理后续消息。
    框架已自动处理退出命令（退出/取消/exit/quit/q），插件无需再处理。
    
    参数:
        text: 用户发送的原始文本
        event: OneBot 事件
        context: 插件上下文
        session: 当前会话对象
    """
    return await _handle_session(text, event, context, session)

# ============================================================
# 插件生命周期管理
# ============================================================

async def cleanup(context: Context) -> None:
    """
    插件清理函数
    
    在插件卸载或重启时调用，用于释放资源。
    """
    try:
        manager = await get_manager(context)
        manager.close_all()
        logger.info("SSH plugin cleaned up successfully")
    except Exception as e:
        logger.error("Error during SSH plugin cleanup: %s", e)

async def shutdown(context: Context) -> None:
    await cleanup(context)

# ============================================================
# 定时任务
# ============================================================

async def cleanup_orphans(context: Context) -> None:
    """
    清理孤儿连接（定时任务）
    
    检查所有 SSH 连接，如果对应的会话已不存在（过期），则断开连接。
    防止用户直接关掉会话导致连接泄露。
    """
    from .config import SessionKeys
    
    manager = await get_manager(context)
    if not manager.connections:
        return
        
    # 获取所有 qingssh 的活跃会话
    try:
        if not context.session_manager:
            return
            
        # 使用新添加的 get_all_sessions 方法
        sessions = await context.session_manager.get_all_sessions("qingssh")
        
        # 构建活跃连接键集合 {user_id:group_id:server_name}
        active_keys = set()
        for session in sessions:
            server_name = session.get(SessionKeys.SERVER_NAME)
            user_id = str(session.user_id)
            group_id = str(session.group_id)
            
            if server_name:
                key = manager._build_connection_key(user_id, group_id, server_name)
                active_keys.add(key)
        
        # 检查现有的连接
        orphans = []
        for key in list(manager.connections.keys()):
            # 忽略非标准的 key (防卫性)
            if key not in active_keys:
                orphans.append(key)
        
        # 清理孤儿连接
        if orphans:
            count = 0
            for key in orphans:
                try:
                    # key 格式: user_id:group_id:server_name
                    # 直接调用 close，不通过 disconnect (因为不需要发送停止信号给早已不存在的 session)
                    if key in manager.connections:
                        manager.connections[key].close()
                        del manager.connections[key]
                        count += 1
                except Exception as e:
                    logger.warning("Error closing orphan connection %s: %s", key, e)

            if count > 0:
                logger.info("Cleaned up %d orphan SSH connections", count)
                
    except Exception as e:
        logger.error("Error checking orphan connections: %s", e)
