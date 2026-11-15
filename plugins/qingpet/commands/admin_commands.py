"""群配置、用户控制、宠物展示会公告、活动创建和审计查询等管理命令。"""

import re

from core.args import parse_int

from ..services.admin_service import log_admin_operation, set_group_config
from ..services.database import Database
from ..services.economy_service import EconomyService
from ..services.social_service import SocialService
from ..utils.constants import PET_ACTION_LABELS, PET_SHOW_CONFIG
from .parsing import parse_target_qq, split_first_word

_ACTIVITY_TYPES   = tuple(PET_ACTION_LABELS)
_CONFIRMED_TARGET = re.compile(r"@?(\d+)(?:\s+(确认|confirm))?", re.IGNORECASE)


def _parse_confirmed_target(args: str) -> tuple[str, bool] | None:
    """解析破坏性命令的 ``@QQ号 [确认]``；返回 (QQ号, 是否带了确认词)，格式不对返回 ``None``。"""
    match = _CONFIRMED_TARGET.fullmatch(args.strip())
    if match is None:
        return None
    return match.group(1), match.group(2) is not None


def handle_manage_enable(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """经管理员授权后为当前群开启插件。"""
    if args.strip():
        return False, "用法: /宠物 管理 开启"

    if db.update_group_config_field(group_id, "enabled", True):
        log_admin_operation(db, group_id, user_id, "ENABLE", "启用插件")
        return True, f"✅ 宠物插件已在群 {group_id} 中启用"
    return False, "启用失败"


def handle_manage_disable(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """经管理员授权后为当前群关闭插件。"""
    if args.strip():
        return False, "用法: /宠物 管理 关闭"

    if db.update_group_config_field(group_id, "enabled", False):
        log_admin_operation(db, group_id, user_id, "DISABLE", "禁用插件")
        return True, f"✅ 宠物插件已在群 {group_id} 中禁用"
    return False, "禁用失败"


def handle_manage_config(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """经管理员授权后查看或修改群配置。"""

    raw_args = args.strip()
    if not raw_args or raw_args == "查看":
        config = db.get_group_config(group_id)
        lines  = [
            f"⚙️ 群配置 ({config.group_id})",
            "",
            f"• 插件状态: {'启用' if config.enabled else '禁用'}",
            f"• 经济倍率: {config.economy_multiplier}x",
            f"• 衰减倍率: {config.decay_multiplier}x",
            f"• 交易功能: {'开启' if config.trade_enabled else '关闭'}",
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
                "trade_enabled, activity_enabled"
            )

        key, value = parts[1], parts[2]
        if set_group_config(db, group_id, key, value):
            log_admin_operation(db, group_id, user_id, "CONFIG_SET", f"{key}={value}")
            return True, f"✅ 配置已更新: {key} = {value}"
        return False, "配置更新失败，请检查key和value是否正确"

    return False, "未知操作，可用操作: 查看, 设置"


def handle_manage_reset(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """经管理员授权后重置指定用户的宠物。"""

    parsed = _parse_confirmed_target(args)
    if parsed is None:
        return False, "格式错误\n用法: /宠物 管理 重置 @QQ号"

    target_user_id, confirmed = parsed
    if not confirmed:
        return False, (
            f"⚠️ 将重置用户 {target_user_id} 的宠物状态。此操作不可自动撤销。\n"
            f"确认执行请发送：/宠物 管理 重置 @{target_user_id} 确认"
        )

    # 原子重置宠物及相关冷却，并在同一事务写入审计日志
    if db.admin_reset_user_pet_atomic(target_user_id, group_id, user_id):
        return True, f"✅ 用户 {target_user_id} 的宠物已重置"
    return False, "重置失败，用户可能不存在"


def handle_manage_ban(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """经管理员授权后在当前群封禁用户。"""

    match = re.fullmatch(r"@?(\d+)\s+([1-9]\d*)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 管理 封禁 @QQ号 <天数>\n用法: /宠物 管理 解封 @QQ号"

    target_user_id = match.group(1)
    days           = int(match.group(2))

    if db.admin_set_ban_atomic(target_user_id, group_id, user_id, days=days):
        return True, f"✅ 用户 {target_user_id} 已被封禁 {days} 天"
    return False, "封禁失败"


def handle_manage_unban(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """经管理员授权后解除当前群的用户封禁。"""

    target_user_id = parse_target_qq(args)
    if target_user_id is None:
        return False, "格式错误\n用法: /宠物 管理 解封 @QQ号"

    if db.admin_set_ban_atomic(target_user_id, group_id, user_id, days=None):
        return True, f"✅ 用户 {target_user_id} 已解封"
    return False, "解封失败"


def handle_manage_log(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """查看当前群最近的管理员操作日志。"""

    limit      = 20
    limit_text = args.strip()
    if limit_text:
        parsed_limit = parse_int(limit_text, minimum=1, maximum=100)
        if parsed_limit is None:
            return False, "日志条数必须是 1～100 的整数"
        limit = parsed_limit

    logs = db.get_operation_logs(group_id, limit)

    if not logs:
        return True, "📋 暂无操作日志"

    lines = [f"📋 操作日志 (最近{len(logs)}条)", ""]
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


def handle_manage_stats(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """查看当前群的聚合宠物统计数据。"""
    if args.strip():
        return False, "用法: /宠物 管理 统计"

    return True, EconomyService(db).format_stats(group_id)


def handle_manage_activity(
    user_id: str, group_id: int, args: str, db: Database
) -> tuple[bool, str]:
    """创建会随宠物行为推进的限时群活动。"""
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
    title       = parts[4] if len(parts) > 4 else activity_type
    activity_id = db.create_activity(group_id, activity_type, title, target, reward_coins)
    if activity_id is None:
        return False, "活动创建失败"
    log_admin_operation(
        db, group_id, user_id, "ACTIVITY_CREATE", f"activity_id={activity_id} type={activity_type}"
    )
    return True, f"✅ 活动 #{activity_id} 已创建；对应宠物行为会推进进度"


def handle_manage_delete(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """经管理员授权后删除指定用户的宠物。"""

    parsed = _parse_confirmed_target(args)
    if parsed is None:
        return False, "格式错误\n用法: /宠物 管理 删除 @QQ号"

    target_user_id, confirmed = parsed
    if not confirmed:
        return False, (
            f"⚠️ 将永久删除用户 {target_user_id} 的宠物。\n"
            f"确认执行请发送：/宠物 管理 删除 @{target_user_id} 确认"
        )
    if db.admin_delete_pet_atomic(target_user_id, group_id, user_id):
        return True, f"✅ 用户 {target_user_id} 的宠物已删除"
    return False, "删除失败"


def handle_manage_announce(
    user_id: str, group_id: int, args: str, db: Database
) -> tuple[bool, str]:
    """经管理员授权后开启或结束宠物展示会。"""

    if not args.strip():
        return True, (
            "📢 管理公告\n\n"
            "• /宠物 管理 公告 展示会 [标题] - 开启宠物展示会\n"
            "• /宠物 管理 公告 结束展示会 - 结束展示会并发放奖励"
        )

    action, rest = split_first_word(args)

    if action in {"展示会", "show"}:
        title    = rest if rest else "宠物展示会"
        existing = db.get_active_pet_show(group_id)
        if existing is not None:
            return False, "已有进行中的展示会"

        show_id = db.create_pet_show(group_id, title, PET_SHOW_CONFIG["duration_hours"])
        if show_id is not None:
            return True, (
                f"🏆 {title} 已开启！\n\n"
                f"持续时间: {PET_SHOW_CONFIG['duration_hours']}小时\n"
                f"投票: /宠物 展示 投票 @QQ号\n"
                f"每人最多{PET_SHOW_CONFIG['max_votes_per_user']}票"
            )
        return False, "开启失败"

    if action in {"结束展示会", "end_show"}:
        show = db.get_active_pet_show(group_id)
        if show is None:
            return False, "没有进行中的展示会"

        social_service = SocialService(db)
        result = social_service.settle_pet_show(group_id, force=True)
        return True, result if result else "展示会已结束（无投票数据）"

    return False, "未知公告命令"
