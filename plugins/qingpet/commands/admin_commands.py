"""群配置、用户控制、活动创建和审计查询等管理命令。"""

import re

from core.args import parse_int

from ..services.admin_service import AdminService
from ..services.database import Database
from ..services.economy_service import EconomyService

_ACTIVITY_TYPES = ("feed", "clean", "play", "train", "explore")


def handle_manage_enable(
    user_id: str, group_id: int, args: str, db: Database, is_admin: bool = False
) -> tuple[bool, str]:
    """经管理员授权后为当前群开启插件。"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"
    if args.strip():
        return False, "用法: /宠物 管理 开启"

    admin_service = AdminService(db)
    success = admin_service.enable_plugin(group_id)

    if success:
        admin_service.log_admin_operation(group_id, user_id, "ENABLE", "启用插件")
        return True, f"✅ 宠物插件已在群 {group_id} 中启用"
    return False, "启用失败"


def handle_manage_disable(
    user_id: str, group_id: int, args: str, db: Database, is_admin: bool = False
) -> tuple[bool, str]:
    """经管理员授权后为当前群关闭插件。"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"
    if args.strip():
        return False, "用法: /宠物 管理 关闭"

    admin_service = AdminService(db)
    success = admin_service.disable_plugin(group_id)

    if success:
        admin_service.log_admin_operation(group_id, user_id, "DISABLE", "禁用插件")
        return True, f"✅ 宠物插件已在群 {group_id} 中禁用"
    return False, "禁用失败"


def handle_manage_config(
    user_id: str, group_id: int, args: str, db: Database, is_admin: bool = False
) -> tuple[bool, str]:
    """经管理员授权后查看或修改群配置。"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    admin_service = AdminService(db)

    raw_args = args.strip()
    if not raw_args or raw_args == "查看":
        config = admin_service.get_config(group_id)
        lines = [
            f"⚙️ **群配置 ({config.group_id})**",
            "",
            f"• 插件状态: {'启用' if config.enabled else '禁用'}",
            f"• 经济倍率: {config.economy_multiplier}x",
            f"• 衰减倍率: {config.decay_multiplier}x",
            f"• 交易功能: {'开启' if config.trade_enabled else '关闭'}",
            f"• 自然触发: {'开启' if config.natural_trigger_enabled else '关闭'}",
            f"• 活动功能: {'开启' if config.activity_enabled else '关闭'}",
            f"• 敏感词数量: {len(config.sensitive_words)}",
        ]
        return True, "\n".join(lines)

    parts = raw_args.split(maxsplit=2)
    action = parts[0]

    if action == "设置":
        if len(parts) != 3:
            return False, (
                "格式错误\n用法: /宠物 管理 配置 设置 <key> <value>\n"
                "可用key: economy_multiplier, decay_multiplier, "
                "trade_enabled, natural_trigger_enabled, activity_enabled"
            )

        key = parts[1]
        value = parts[2]

        success = admin_service.set_config(group_id, key, value)
        if success:
            admin_service.log_admin_operation(group_id, user_id, "CONFIG_SET", f"{key}={value}")
            return True, f"✅ 配置已更新: {key} = {value}"
        return False, "配置更新失败，请检查key和value是否正确"

    return False, "未知操作，可用操作: 查看, 设置"


def handle_manage_reset(
    user_id: str, group_id: int, args: str, db: Database, is_admin: bool = False
) -> tuple[bool, str]:
    """经管理员授权后重置指定用户的宠物。"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    match = re.fullmatch(r"@?(\d+)(?:\s+(确认|confirm))?", args.strip(), re.IGNORECASE)
    if not match:
        return False, "格式错误\n用法: /宠物 管理 重置 @QQ号"

    target_user_id = match.group(1)
    if not match.group(2):
        return False, (
            f"⚠️ 将重置用户 {target_user_id} 的宠物状态。此操作不可自动撤销。\n"
            f"确认执行请发送：/宠物 管理 重置 @{target_user_id} 确认"
        )

    admin_service = AdminService(db)
    success = admin_service.reset_user_pet(
        target_user_id,
        group_id,
        operator_user_id=user_id,
    )

    if success:
        return True, f"✅ 用户 {target_user_id} 的宠物已重置"
    return False, "重置失败，用户可能不存在"


def handle_manage_ban(
    user_id: str, group_id: int, args: str, db: Database, is_admin: bool = False
) -> tuple[bool, str]:
    """经管理员授权后在当前群封禁用户。"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    match = re.fullmatch(r"@?(\d+)\s+([1-9]\d*)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 管理 封禁 @QQ号 <天数>\n用法: /宠物 管理 解封 @QQ号"

    target_user_id = match.group(1)
    days = int(match.group(2))

    admin_service = AdminService(db)
    success = admin_service.ban_user(target_user_id, group_id, days, operator_user_id=user_id)

    if success:
        return True, f"✅ 用户 {target_user_id} 已被封禁 {days} 天"
    return False, "封禁失败"


def handle_manage_unban(
    user_id: str, group_id: int, args: str, db: Database, is_admin: bool = False
) -> tuple[bool, str]:
    """经管理员授权后解除当前群的用户封禁。"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    match = re.fullmatch(r"@?(\d+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 管理 解封 @QQ号"

    target_user_id = match.group(1)

    admin_service = AdminService(db)
    success = admin_service.unban_user(target_user_id, group_id, operator_user_id=user_id)

    if success:
        return True, f"✅ 用户 {target_user_id} 已解封"
    return False, "解封失败"


def handle_manage_log(
    user_id: str, group_id: int, args: str, db: Database, is_admin: bool = False
) -> tuple[bool, str]:
    """查看当前群最近的管理员操作日志。"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    limit = 20
    limit_text = args.strip()
    if limit_text:
        parsed_limit = parse_int(limit_text, minimum=1, maximum=100)
        if parsed_limit is None:
            return False, "日志条数必须是 1～100 的整数"
        limit = parsed_limit

    admin_service = AdminService(db)
    logs = admin_service.get_logs(group_id, limit)

    if not logs:
        return True, "📋 暂无操作日志"

    lines = [f"📋 **操作日志** (最近{len(logs)}条)", ""]
    for log in logs:
        lines.append(f"• {log.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"  操作者: {log.user_id}")
        if log.target_user_id:
            lines.append(f"  目标: {log.target_user_id}")
        lines.append(f"  操作: {log.operation_type}")
        if log.params:
            lines.append(f"  参数: {log.params}")
        lines.extend((f"  结果: {log.result}", ""))

    return True, "\n".join(lines)


def handle_manage_stats(
    user_id: str, group_id: int, args: str, db: Database, is_admin: bool = False
) -> tuple[bool, str]:
    """查看当前群的聚合宠物统计数据。"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"
    if args.strip():
        return False, "用法: /宠物 管理 统计"

    economy_service = EconomyService(db)
    return True, economy_service.format_stats(group_id)


def handle_manage_activity(
    user_id: str, group_id: int, args: str, db: Database, is_admin: bool = False
) -> tuple[bool, str]:
    """创建会随宠物行为推进的限时群活动。"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"
    parts = args.strip().split(maxsplit=4)
    if len(parts) < 4 or parts[0] not in {"创建", "create"}:
        return False, (
            "用法：/宠物 管理 活动 创建 <类型> <目标数> <奖励金币> [标题]\n"
            f"可用类型：{', '.join(_ACTIVITY_TYPES)}"
        )
    activity_type = parts[1]
    if activity_type not in _ACTIVITY_TYPES:
        return False, f"无效活动类型，可用类型：{', '.join(_ACTIVITY_TYPES)}"
    target = parse_int(parts[2], minimum=1)
    if target is None:
        return False, "目标数必须是正整数"
    reward_coins = parse_int(parts[3], minimum=0)
    if reward_coins is None:
        return False, "奖励金币必须是非负整数"
    title = parts[4] if len(parts) > 4 else activity_type
    activity_id = db.create_activity(group_id, activity_type, title, target, reward_coins)
    if activity_id is None:
        return False, "活动创建失败"
    AdminService(db).log_admin_operation(
        group_id, user_id, "ACTIVITY_CREATE", f"activity_id={activity_id} type={activity_type}"
    )
    return True, f"✅ 活动 #{activity_id} 已创建；对应宠物行为会推进进度"
