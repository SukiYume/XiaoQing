"""训练、道具、社交、任务与小游戏等进阶宠物命令。"""

import re

from core.args import parse_int

from ..services.database import Database
from ..services.item_service import ItemService
from ..services.pet_service import PetService
from ..services.social_service import SocialService
from ..services.user_service import UserService
from ..utils.constants import ItemType
from ..utils.formatters import format_ranking_list, format_status_text
from ..utils.validators import validate_item_amount
from .basic_commands import resolve_pet_for_self_command, with_pet_name

_TRAINING_TYPES = {
    "体力": "strength",
    "strength": "strength",
    "敏捷": "agility",
    "agility": "agility",
    "智力": "intellect",
    "intellect": "intellect",
}
_EXPLORE_LOCATIONS = {
    "森林": "forest",
    "forest": "forest",
    "海边": "beach",
    "beach": "beach",
    "山洞": "cave",
    "cave": "cave",
    "废墟": "ruins",
    "ruins": "ruins",
}
_RANKING_TYPES = ("care_score", "intimacy", "experience", "coins")
_TASK_NAMES    = {
    "feed": "喂食宠物",
    "clean": "清洁宠物",
    "play": "玩耍互动",
    "visit": "访问他人宠物",
}


def _extract_target_user_id(args: str) -> str:
    text = args.strip()
    if not text:
        return ""

    direct = re.match(r"@?(\d+)$", text)
    if direct:
        return direct.group(1)

    cq_at = re.search(r"\[CQ:at,qq=(\d+)\]", text)
    if cq_at:
        return cq_at.group(1)

    return ""


def handle_train(
    user_id: str,
    group_id: int,
    args: str,
    db: Database,
    *,
    spam_decay_factor: float = 1.0,
) -> tuple[bool, str]:
    pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "训练"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    requested_type = resolved_args.strip()
    if requested_type and requested_type not in _TRAINING_TYPES:
        return False, "无效训练类型，可用: 体力、敏捷、智力"

    user_service = UserService(db)
    user         = user_service.get_or_create_user(user_id, resolved_group_id)

    pet_service   = PetService(db)
    training_type = _TRAINING_TYPES.get(requested_type, "strength")
    success, message, _coins = pet_service.train_pet(
        pet,
        user,
        training_type     = training_type,
        spam_decay_factor = spam_decay_factor,
    )

    if success:
        status_text = format_status_text(pet)
        message     = f"{message}\n\n{status_text}"

    return success, with_pet_name(pet, message)


def handle_explore(
    user_id: str,
    group_id: int,
    args: str,
    db: Database,
    *,
    spam_decay_factor: float = 1.0,
) -> tuple[bool, str]:
    pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "探索"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    requested_location = resolved_args.strip()
    if requested_location and requested_location not in _EXPLORE_LOCATIONS:
        return False, "无效探索地点，可用: 森林、海边、山洞、废墟"

    user_service = UserService(db)
    user         = user_service.get_or_create_user(user_id, resolved_group_id)

    pet_service = PetService(db)
    location    = _EXPLORE_LOCATIONS.get(requested_location, "forest")
    success, message, _coins = pet_service.explore(
        pet,
        user,
        location          = location,
        spam_decay_factor = spam_decay_factor,
    )

    if success:
        status_text = format_status_text(pet)
        message     = f"{message}\n\n{status_text}"

    return success, with_pet_name(pet, message)


def handle_treat(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "治疗"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    item_id = resolved_args.strip() or "medicine"

    item_service  = ItemService(db)
    resolved_item = item_service.resolve_item(item_id)
    if resolved_item is None:
        return False, "该药品不存在"
    item_id, item = resolved_item

    if item.item_type != ItemType.MEDICINE:
        return False, "该道具不是药品，不能用于治疗"

    result = db.treat_pet_atomic(
        user_id,
        resolved_group_id,
        pet.id,
        item_id,
        health_gain      = item.health_gain,
        clean_gain       = item.clean_gain,
        daily_limit      = 20,
        cooldown_seconds = 300,
    )
    if result.success and result.pet is not None:
        pet.__dict__.update(result.pet.__dict__)
        status_text = format_status_text(pet)
        return True, with_pet_name(
            pet, f"治疗成功！恢复了{item.health_gain}健康值\n\n{status_text}"
        )
    if result.remaining > 0:
        return False, with_pet_name(pet, f"治疗冷却中，请等待{result.remaining}秒")
    return False, with_pet_name(pet, result.reason or "治疗失败")


def handle_backpack(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    if args.strip():
        return False, "背包命令不接受额外参数\n用法: /宠物 背包"
    item_service = ItemService(db)
    inventory    = item_service.get_inventory(user_id, group_id)

    if not inventory.items:
        return True, "你的背包是空的\n使用 /宠物 商店 查看可购买的道具"

    items_list: list[str] = []
    for item_id, count in inventory.items.items():
        item = item_service.get_item(item_id)
        if item is not None:
            items_list.append(f"• {item.name} x{count} ({item.rarity.value})")
        else:
            # 历史背包可能残留已下架 ID；保留可见性，便于管理员排查数据。
            items_list.append(f"• [{item_id}] 未知道具 x{count}")

    return True, "📦 **背包内容**\n\n" + "\n".join(items_list)


def handle_shop(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    if args.strip():
        return False, "商店命令不接受额外参数\n用法: /宠物 商店"
    item_service = ItemService(db)
    items        = item_service.get_all_items()

    shop_list: list[str] = []
    for item_id, item in items.items():
        effects: list[str] = []
        if item.hunger_gain > 0:
            effects.append(f"+{item.hunger_gain}饥饿")
        if item.mood_gain > 0:
            effects.append(f"+{item.mood_gain}心情")
        if item.health_gain > 0:
            effects.append(f"+{item.health_gain}健康")
        if item.clean_gain > 0:
            effects.append(f"+{item.clean_gain}清洁")
        if item.exp_gain > 0:
            effects.append(f"+{item.exp_gain}经验")
        if item.trustee_hours > 0:
            effects.append(f"托管{item.trustee_hours}h")
        effect_str = " ".join(effects) if effects else "特殊道具"
        shop_list.append(
            f"• [{item_id}] {item.name} ({item.rarity.value}) - {item.price}金币\n  效果: {effect_str}"
        )

    return True, "🛒 **道具商店**\n\n使用 /宠物 购买 <道具ID/名字> [数量] 购买\n\n" + "\n\n".join(
        shop_list
    )


def handle_buy(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    parts = args.strip().split()
    if not parts:
        return False, "请指定要购买的道具\n用法: /宠物 购买 <道具名> [数量]"
    if len(parts) > 2:
        return False, "参数过多\n用法: /宠物 购买 <道具名> [数量]"

    item_id = parts[0]
    amount  = 1
    if len(parts) == 2:
        parsed_amount = parse_int(parts[1])
        if parsed_amount is None:
            return False, "数量必须是整数"
        amount = parsed_amount

    user_service = UserService(db)
    user         = user_service.get_or_create_user(user_id, group_id)
    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    item_service = ItemService(db)
    success, message = item_service.buy_item(user_id, group_id, item_id, amount)

    return success, message


def handle_use(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """使用道具（加速卡、托管券等）"""
    pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "使用"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    item_id = resolved_args.strip()
    if not item_id:
        return False, "请指定要使用的道具\n用法: /宠物 使用 <道具名>"

    user_service = UserService(db)
    user         = user_service.get_or_create_user(user_id, resolved_group_id)

    item_service  = ItemService(db)
    resolved_item = item_service.resolve_item(item_id)
    if resolved_item is None:
        return False, "道具不存在"
    item_id, _item = resolved_item

    pet_service = PetService(db)

    if item_id == "acceleration_card":
        success, message = pet_service.use_acceleration_card(pet, user)
        return success, with_pet_name(pet, message)
    if item_id == "trusteeship_coupon":
        success, message = pet_service.use_trusteeship_coupon(pet, user)
        return success, with_pet_name(pet, message)
    return False, "该道具暂不支持手动使用"


def handle_gift(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    match = re.fullmatch(r"@?(\d+)\s+(\S+)(?:\s+([1-9]\d*))?", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 送礼 @QQ号 <道具名> [数量]"

    target_user_id = match.group(1)
    item_id        = match.group(2)
    amount_text    = match.group(3)
    if amount_text is not None and len(amount_text) > 3:
        return False, "单次数量不能超过99"
    amount = int(amount_text) if amount_text is not None else 1
    valid, message = validate_item_amount(amount)
    if not valid:
        return False, message

    user_service = UserService(db)
    user         = user_service.get_or_create_user(user_id, group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    social_service = SocialService(db)
    success, message = social_service.gift_item(user_id, target_user_id, group_id, item_id, amount)

    return success, message


def handle_visit(
    user_id: str,
    group_id: int,
    args: str,
    db: Database,
    *,
    message_id: str | None = None,
) -> tuple[bool, str]:
    match = re.fullmatch(r"@?(\d+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 互访 @QQ号"

    target_user_id = match.group(1)

    user_service = UserService(db)
    user         = user_service.get_or_create_user(user_id, group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    social_service = SocialService(db)
    success, message = social_service.visit_pet(
        user_id,
        target_user_id,
        group_id,
        message_id=message_id,
    )

    return success, message


def handle_view_pet(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """查看同群另一用户的宠物卡片。"""
    target_user_id = _extract_target_user_id(args)
    if not target_user_id:
        return False, "格式错误\n用法: /宠物 查看 @QQ号 或 /宠物 查看 [CQ:at,qq=QQ号]"

    social_service = SocialService(db)
    return social_service.view_pet_card(target_user_id, group_id)


def handle_like(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """点赞或摸摸同群另一用户的宠物。"""
    match = re.fullmatch(r"@?(\d+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 摸摸 @QQ号"

    target_user_id = match.group(1)

    social_service = SocialService(db)
    return social_service.like_pet(user_id, target_user_id, group_id)


def handle_message(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """执行宠物留言板的查看与写入操作。"""
    if not args.strip():
        # 查看自己收到的留言
        social_service = SocialService(db)
        return social_service.get_messages(user_id, group_id)

    match = re.match(r"@?(\d+)\s+(.+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 留言 @QQ号 <内容>\n用法: /宠物 留言 （查看你的留言）"

    target_user_id = match.group(1)
    message_text   = match.group(2)

    social_service = SocialService(db)
    return social_service.leave_message(user_id, target_user_id, group_id, message_text)


def handle_ranking(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    ranking_type = args.strip() or "care_score"

    if ranking_type not in _RANKING_TYPES:
        return False, f"无效的排行类型\n可用类型: {', '.join(_RANKING_TYPES)}"

    social_service = SocialService(db)
    ranking        = social_service.get_ranking(group_id, ranking_type, 10)

    return True, format_ranking_list(ranking, ranking_type)


def handle_activity(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """读取并执行持久化的群活动状态。"""
    requested = args.strip()
    if requested:
        parts = requested.split()
        if parts[0] not in {"领取", "claim"}:
            return False, "未知活动命令\n用法: /宠物 活动 [领取 <活动ID>]"
        activity_id = parse_int(parts[1], minimum=1) if len(parts) == 2 else None
        if activity_id is None:
            return False, "用法：/宠物 活动 领取 <活动ID>"
        reward = db.claim_activity_reward(activity_id, user_id, group_id)
        if reward is None:
            return False, "活动未完成、已领取或不存在"
        return True, f"🎁 活动奖励领取成功：{reward}金币"

    activities = db.get_active_activities(group_id)
    if not activities:
        return True, "🎉 **群活动**\n\n当前暂无进行中的活动\n敬请期待！"

    lines = ["🎉 **群活动**", ""]
    for act in activities:
        title    = act.get("title", act.get("activity_type", "未知活动"))
        desc     = act.get("description", "")
        current  = act.get("current_value", 0)
        target   = act.get("target_value", 0)
        reward   = act.get("reward_coins", 0)
        progress = min(100, int(current / target * 100)) if target > 0 else 0

        lines.append(f"📌 **#{act['id']} {title}**")
        if desc:
            lines.append(f"  {desc}")
        lines.extend(
            (
                f"  进度: {current}/{target} ({progress}%)",
                f"  奖励: {reward}金币",
                "",
            )
        )

    lines.append("完成后使用 /宠物 活动 领取 <活动ID>")
    return True, "\n".join(lines)


def handle_task(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """读取、更新并展示数据库中的每日任务。"""
    pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "任务"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    requested = resolved_args.strip()
    if requested not in {"", "领取", "claim"}:
        return False, "未知任务命令\n用法: /宠物 任务 [领取]"

    if requested in {"领取", "claim"}:
        claimed_total            = 0
        claimed_tasks: list[str] = []
        for task_type, task_name in _TASK_NAMES.items():
            reward = db.claim_task_reward(user_id, resolved_group_id, task_type)
            if reward is not None:
                claimed_total += reward
                claimed_tasks.append(task_name)

        if claimed_tasks:
            return True, with_pet_name(
                pet,
                f"🎁 成功领取任务奖励！\n\n完成任务: {'、'.join(claimed_tasks)}\n获得: {claimed_total} 金币",
            )
        return True, with_pet_name(pet, "暂无可领取的任务奖励（未完成或已领取）")

    tasks         = db.get_or_create_daily_tasks(user_id, resolved_group_id)
    lines         = ["📋 **每日任务**", ""]
    all_completed = True
    for task in tasks:
        task_type = task["task_type"]
        current   = task["current_value"]
        target    = task["target_value"]
        reward    = task["reward_coins"]
        claimed   = task["claimed"]
        name      = _TASK_NAMES.get(task_type, task_type)

        if claimed:
            status = "✅ 已领取"
        elif current >= target:
            status = "🎁 可领取"
        else:
            status        = f"({current}/{target})"
            all_completed = False

        lines.append(f"• {name} {status} - 奖励 {reward}金币")

    lines.append("")
    if all_completed:
        lines.append("🎉 所有任务已完成！")
    else:
        lines.append("完成任务后使用 /宠物 任务 领取 来领取奖励")

    return True, with_pet_name(pet, "\n".join(lines))


def handle_group_task(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    requested = args.strip()
    if requested not in {"", "领取", "claim"}:
        return False, "未知群任务命令\n用法: /宠物 群任务 [领取]"

    tasks = db.get_or_create_group_tasks(group_id)
    if requested in {"领取", "claim"}:
        claimed: list[str] = []
        total              = 0
        for task in tasks:
            reward = db.claim_group_task_reward(user_id, group_id, str(task["task_type"]))
            if reward is not None:
                claimed.append(str(task.get("description") or task["task_type"]))
                total += reward
        if not claimed:
            return False, "暂无可领取的群任务奖励（未完成或已领取）"
        return True, f"🎁 已领取群任务奖励：{total}金币\n" + "\n".join(claimed)
    lines = ["📋 **今日群任务**"]
    for task in tasks:
        done = int(task["current_value"]) >= int(task["target_value"])
        lines.append(
            f"• {task['description']} {task['current_value']}/{task['target_value']} "
            f"{'🎁 可领取' if done else ''} - {task['reward_coins']}金币"
        )
    lines.append("\n完成后使用 /宠物 群任务 领取")
    return True, "\n".join(lines)


def handle_rename(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    if not args.strip():
        return False, "请提供新名字\n用法: /宠物 改名 <新名字>"

    pet, _, resolved_args, err = resolve_pet_for_self_command(db, user_id, group_id, args, "改名")
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    new_name = resolved_args.strip()
    if not new_name:
        return False, "请提供新名字\n用法: /宠物 改名 <新名字>"

    pet_service = PetService(db)
    success, message = pet_service.rename_pet(pet, new_name)

    return success, with_pet_name(pet, message)


def handle_title(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """查看和管理宠物称号。"""
    if args.strip():
        return False, "称号命令不接受额外参数\n用法: /宠物 称号"
    user_service = UserService(db)
    new_titles   = user_service.check_and_award_titles(user_id, group_id)
    text         = user_service.format_titles(user_id, group_id)
    if new_titles:
        text += f"\n\n🎉 新获得称号: {'、'.join(new_titles)}"
    return True, text


def handle_minigame(
    user_id: str,
    group_id: int,
    args: str,
    db: Database,
    *,
    message_id: str | None = None,
) -> tuple[bool, str]:
    """路由宠物小游戏命令。"""
    if not args.strip():
        return True, (
            "🎮 **小游戏**\n\n"
            "• /宠物 游戏 猜拳 <石头/剪刀/布> - 猜拳\n"
            "• /宠物 游戏 骰子 - 骰子比大小\n"
            "• /宠物 游戏 赛跑 @QQ号 - 宠物赛跑\n"
        )

    parts = args.strip().split(maxsplit=1)
    game_type      = parts[0]
    game_args      = parts[1] if len(parts) > 1 else ""
    social_service = SocialService(db)

    if game_type in {"猜拳", "rps"}:
        if not game_args:
            return False, "请选择出拳\n用法: /宠物 游戏 猜拳 <石头/剪刀/布>"
        return social_service.play_rock_paper_scissors(
            user_id,
            group_id,
            game_args,
            message_id=message_id,
        )

    if game_type in {"骰子", "dice"}:
        if game_args.strip():
            return False, "骰子命令不接受额外参数\n用法: /宠物 游戏 骰子"
        return social_service.play_dice(user_id, group_id, message_id=message_id)

    if game_type in {"赛跑", "race"}:
        match = re.fullmatch(r"@?(\d+)", game_args)
        if not match:
            return False, "请选择对手\n用法: /宠物 游戏 赛跑 @QQ号"
        target_user_id = match.group(1)
        return social_service.race_pet(
            user_id,
            target_user_id,
            group_id,
            message_id=message_id,
        )

    return False, f"未知游戏类型: {game_type}\n可用游戏: 猜拳, 骰子, 赛跑"
