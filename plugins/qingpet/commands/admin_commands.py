import logging
import re
from typing import Tuple

from ..services.admin_service import AdminService
from ..services.economy_service import EconomyService
from ..services.database import Database
from ..models import OperationLog

logger = logging.getLogger(__name__)


def _format_config_text(config) -> str:
    """统一的配置展示文本（Issue #14: 消除重复代码）"""
    text = f"⚙️ **群配置 ({config.group_id})**\n\n"
    text += f"• 插件状态: {'启用' if config.enabled else '禁用'}\n"
    text += f"• 经济倍率: {config.economy_multiplier}x\n"
    text += f"• 衰减倍率: {config.decay_multiplier}x\n"
    text += f"• 交易功能: {'开启' if config.trade_enabled else '关闭'}\n"
    text += f"• 自然触发: {'开启' if config.natural_trigger_enabled else '关闭'}\n"
    text += f"• 活动功能: {'开启' if config.activity_enabled else '关闭'}\n"
    text += f"• 敏感词数量: {len(config.sensitive_words)}\n"
    return text


async def handle_manage_enable(user_id: str, group_id: int, args: str,
                               db: Database, is_admin: bool = False) -> Tuple[bool, str]:
    """开启插件（需要管理员权限 Issue #1）"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    admin_service = AdminService(db)
    success = admin_service.enable_plugin(group_id)

    if success:
        # 记录操作日志
        admin_service.log_admin_operation(group_id, user_id, "ENABLE", "启用插件")
        return True, f"✅ 宠物插件已在群 {group_id} 中启用"
    return False, "启用失败"


async def handle_manage_disable(user_id: str, group_id: int, args: str,
                                db: Database, is_admin: bool = False) -> Tuple[bool, str]:
    """关闭插件（需要管理员权限 Issue #1）"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    admin_service = AdminService(db)
    success = admin_service.disable_plugin(group_id)

    if success:
        admin_service.log_admin_operation(group_id, user_id, "DISABLE", "禁用插件")
        return True, f"✅ 宠物插件已在群 {group_id} 中禁用"
    return False, "禁用失败"


async def handle_manage_config(user_id: str, group_id: int, args: str,
                               db: Database, is_admin: bool = False) -> Tuple[bool, str]:
    """查看/设置配置（需要管理员权限 Issue #1）"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    admin_service = AdminService(db)

    args = args.strip()
    if not args or args == "查看":
        config = admin_service.get_config(group_id)
        return True, _format_config_text(config)

    parts = args.split()
    action = parts[0]

    if action == "设置":
        if len(parts) < 3:
            return False, ("格式错误\n用法: /宠物 管理 配置 设置 <key> <value>\n"
                           "可用key: economy_multiplier, decay_multiplier, "
                           "trade_enabled, natural_trigger_enabled, activity_enabled")

        key = parts[1]
        # value 可能是剩余所有部分（如果 value 包含空格，虽然目前配置项似乎不需要）
        # 但根据原来的逻辑 parts = args.strip().split(maxsplit=3)，value是最后一个。
        # 这里为了安全，对于配置项通常是单值，我们只取 parts[2]，或者重新 split maxsplit=2
        parts = args.split(maxsplit=2)
        if len(parts) < 3:
             return False, "请提供配置值"
        value = parts[2]

        success = admin_service.set_config(group_id, key, value)
        if success:
            admin_service.log_admin_operation(group_id, user_id, "CONFIG_SET", f"{key}={value}")
            return True, f"✅ 配置已更新: {key} = {value}"
        return False, "配置更新失败，请检查key和value是否正确"

    return False, "未知操作，可用操作: 查看, 设置"


async def handle_manage_reset(user_id: str, group_id: int, args: str,
                              db: Database, is_admin: bool = False) -> Tuple[bool, str]:
    """重置用户宠物（需要管理员权限 Issue #1）"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    match = re.match(r"@?(\d+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 管理 重置 @QQ号"

    target_user_id = match.group(1)

    admin_service = AdminService(db)
    success = admin_service.reset_user_pet(target_user_id, group_id)

    if success:
        admin_service.log_admin_operation(group_id, user_id, "RESET", f"重置用户 {target_user_id}", target_user_id)
        return True, f"✅ 用户 {target_user_id} 的宠物已重置"
    return False, "重置失败，用户可能不存在"


async def handle_manage_ban(user_id: str, group_id: int, args: str,
                            db: Database, is_admin: bool = False) -> Tuple[bool, str]:
    """封禁用户（需要管理员权限 Issue #1）"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    match = re.match(r"@?(\d+)\s+(\d+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 管理 封禁 @QQ号 <天数>\n用法: /宠物 管理 解封 @QQ号"

    target_user_id = match.group(1)
    days = int(match.group(2))

    admin_service = AdminService(db)
    success = admin_service.ban_user(target_user_id, group_id, days)

    if success:
        return True, f"✅ 用户 {target_user_id} 已被封禁 {days} 天"
    return False, "封禁失败"


async def handle_manage_unban(user_id: str, group_id: int, args: str,
                              db: Database, is_admin: bool = False) -> Tuple[bool, str]:
    """解封用户（需要管理员权限 Issue #1）"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    match = re.match(r"@?(\d+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 管理 解封 @QQ号"

    target_user_id = match.group(1)

    admin_service = AdminService(db)
    success = admin_service.unban_user(target_user_id, group_id)

    if success:
        return True, f"✅ 用户 {target_user_id} 已解封"
    return False, "解封失败"


async def handle_manage_log(user_id: str, group_id: int, args: str,
                            db: Database, is_admin: bool = False) -> Tuple[bool, str]:
    """查看操作日志"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    limit = 20
    if args.strip() and args.strip().isdigit():
        limit = min(int(args.strip()), 100)

    admin_service = AdminService(db)
    logs = admin_service.get_logs(group_id, limit)

    if not logs:
        return True, "📋 暂无操作日志"

    log_text = f"📋 **操作日志** (最近{len(logs)}条)\n\n"
    for log in logs:
        log_text += f"• {log.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        log_text += f"  操作者: {log.user_id}\n"
        if log.target_user_id:
            log_text += f"  目标: {log.target_user_id}\n"
        log_text += f"  操作: {log.operation_type}\n"
        if log.params:
            log_text += f"  参数: {log.params}\n"
        log_text += f"  结果: {log.result}\n\n"

    return True, log_text


async def handle_manage_stats(user_id: str, group_id: int, args: str,
                              db: Database, is_admin: bool = False) -> Tuple[bool, str]:
    """查看群统计数据（Issue #55: 数据统计）"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    economy_service = EconomyService(db)
    return True, economy_service.format_stats(group_id)