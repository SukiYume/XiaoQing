import logging
import re
from typing import Tuple

from ..models import Pet
from ..services.pet_service import PetService
from ..services.user_service import UserService
from ..services.item_service import ItemService
from ..services.social_service import SocialService
from ..services.database import Database
from ..utils.formatters import format_status_text, format_ranking_list
from ..utils.validators import validate_item_amount
from .basic_commands import resolve_pet_for_self_command, with_pet_name

logger = logging.getLogger(__name__)


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


async def handle_train(user_id: str, group_id: int, args: str, db: Database, **kwargs) -> Tuple[bool, str]:
    pet, resolved_group_id, _, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "训练"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    spam_decay = kwargs.get("spam_decay_factor", 1.0)
    pet_service = PetService(db)
    success, message, coins = pet_service.train_pet(pet, user,
                                                     spam_decay_factor=spam_decay)

    if success:
        status_text = format_status_text(pet)
        message = f"{message}\n\n{status_text}"

    return success, with_pet_name(pet, message)


async def handle_explore(user_id: str, group_id: int, args: str, db: Database, **kwargs) -> Tuple[bool, str]:
    pet, resolved_group_id, _, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "探索"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    spam_decay = kwargs.get("spam_decay_factor", 1.0)
    pet_service = PetService(db)
    success, message, coins = pet_service.explore(pet, user,
                                                   spam_decay_factor=spam_decay)

    if success:
        status_text = format_status_text(pet)
        message = f"{message}\n\n{status_text}"

    return success, with_pet_name(pet, message)


async def handle_treat(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "治疗"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    item_id = resolved_args.strip() if resolved_args.strip() else "medicine"

    item_service = ItemService(db)
    item = item_service.get_item(item_id)
    if not item:
        # 尝试按名称搜索
        found_id, found_item = item_service.get_item_by_name(item_id)
        if found_item:
            item = found_item
            item_id = found_id
        else:
            return False, "该药品不存在"

    inventory = item_service.get_inventory(user_id, resolved_group_id)
    if not inventory.has_item(item_id):
        return False, "背包中没有该药品"

    pet.update_stat("health", item.health_gain)
    pet.update_stat("clean", item.clean_gain)

    from ..utils.constants import PetStatus
    if pet.health >= 50:
        pet.status = PetStatus.NORMAL

    inventory.remove_item(item_id)

    success = db.update_pet(pet) and db.update_inventory(inventory)
    if success:
        status_text = format_status_text(pet)
        return True, with_pet_name(pet, f"治疗成功！恢复了{item.health_gain}健康值\n\n{status_text}")
    return False, with_pet_name(pet, "治疗失败")


async def handle_backpack(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    item_service = ItemService(db)
    inventory = item_service.get_inventory(user_id, group_id)

    if not inventory.items:
        return True, "你的背包是空的\n使用 /宠物 商店 查看可购买的道具"

    items_list = []
    for item_id, count in inventory.items.items():
        item = item_service.get_item(item_id)
        if item:
            items_list.append(f"• {item.name} x{count} ({item.rarity.value})")

    return True, f"📦 **背包内容**\n\n" + "\n".join(items_list)


async def handle_shop(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    item_service = ItemService(db)
    items = item_service.get_all_items()

    shop_list = []
    for item_id, item in items.items():
        effects = []
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
        shop_list.append(f"• [{item_id}] {item.name} ({item.rarity.value}) - {item.price}金币\n  效果: {effect_str}")

    return True, f"🛒 **道具商店**\n\n使用 /宠物 购买 <道具ID/名字> [数量] 购买\n\n" + "\n\n".join(shop_list)


async def handle_buy(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    parts = args.strip().split()
    if not parts:
        return False, "请指定要购买的道具\n用法: /宠物 购买 <道具名> [数量]"

    item_id = parts[0]
    amount = 1
    if len(parts) > 1 and parts[1].isdigit():
        amount = int(parts[1])

    # 校验购买数量（Issue #17）
    valid, msg = validate_item_amount(amount)
    if not valid:
        return False, msg

    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    item_service = ItemService(db)
    success, message = item_service.buy_item(user_id, group_id, item_id, amount)

    return success, message


async def handle_use(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
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
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    item_service = ItemService(db)
    item = item_service.get_item(item_id)
    if not item:
        found_id, found_item = item_service.get_item_by_name(item_id)
        if found_item:
            item = found_item
            item_id = found_id
        else:
            return False, "道具不存在"

    pet_service = PetService(db)

    if item_id == "acceleration_card":
        success, message = pet_service.use_acceleration_card(pet, user)
        return success, with_pet_name(pet, message)
    elif item_id == "trusteeship_coupon":
        success, message = pet_service.use_trusteeship_coupon(pet, user)
        return success, with_pet_name(pet, message)
    else:
        return False, "该道具暂不支持手动使用"


async def handle_gift(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    match = re.match(r"@?(\d+)\s+(\S+)\s*(\d*)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 送礼 @QQ号 <道具名> [数量]"

    target_user_id = match.group(1)
    item_id = match.group(2)
    amount = int(match.group(3)) if match.group(3) and match.group(3).isdigit() else 1

    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    social_service = SocialService(db)
    success, message = social_service.gift_item(user_id, target_user_id, group_id, item_id, amount)

    return success, message


async def handle_visit(user_id: str, group_id: int, args: str, db: Database, **kwargs) -> Tuple[bool, str]:
    match = re.match(r"@?(\d+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 互访 @QQ号"

    target_user_id = match.group(1)

    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    social_service = SocialService(db)
    success, message = social_service.visit_pet(user_id, target_user_id, group_id)

    return success, message


async def handle_view_pet(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    """查看他人宠物卡片（Issue #42）"""
    target_user_id = _extract_target_user_id(args)
    if not target_user_id:
        return False, "格式错误\n用法: /宠物 查看 @QQ号 或 /宠物 查看 [CQ:at,qq=QQ号]"

    social_service = SocialService(db)
    return social_service.view_pet_card(user_id, target_user_id, group_id)


async def handle_like(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    """点赞/摸摸他人宠物（Issue #43）"""
    match = re.match(r"@?(\d+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 摸摸 @QQ号"

    target_user_id = match.group(1)

    social_service = SocialService(db)
    return social_service.like_pet(user_id, target_user_id, group_id)


async def handle_message(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    """留言板操作（Issue #44）"""
    if not args.strip():
        # 查看自己收到的留言
        social_service = SocialService(db)
        return social_service.get_messages(user_id, group_id)

    match = re.match(r"@?(\d+)\s+(.+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 留言 @QQ号 <内容>\n用法: /宠物 留言 （查看你的留言）"

    target_user_id = match.group(1)
    message_text = match.group(2)

    social_service = SocialService(db)
    return social_service.leave_message(user_id, target_user_id, group_id, message_text)


async def handle_ranking(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    ranking_type = args.strip() if args.strip() else "care_score"

    valid_types = ["care_score", "intimacy", "experience", "coins"]
    if ranking_type not in valid_types:
        return False, f"无效的排行类型\n可用类型: {', '.join(valid_types)}"

    social_service = SocialService(db)
    ranking = social_service.get_ranking(group_id, ranking_type, 10)

    return True, format_ranking_list(ranking, ranking_type)


async def handle_activity(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    """群活动系统（Issue #5: 不再是硬编码占位符）"""
    activities = db.get_active_activities(group_id)

    if not activities:
        return True, "🎉 **群活动**\n\n当前暂无进行中的活动\n敬请期待！"

    text = "🎉 **群活动**\n\n"
    for act in activities:
        title = act.get('title', act.get('activity_type', '未知活动'))
        desc = act.get('description', '')
        current = act.get('current_value', 0)
        target = act.get('target_value', 0)
        reward = act.get('reward_coins', 0)
        progress = min(100, int(current / target * 100)) if target > 0 else 0

        text += f"📌 **{title}**\n"
        if desc:
            text += f"  {desc}\n"
        text += f"  进度: {current}/{target} ({progress}%)\n"
        text += f"  奖励: {reward}金币\n\n"

    return True, text


async def handle_task(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    """每日任务系统（Issue #4: 不再是硬编码，真正读写数据库）"""
    pet, resolved_group_id, _, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "任务"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    # 如果有参数 "领取"，尝试领取任务奖励
    if args.strip() in ["领取", "claim"]:
        task_types = ["feed", "clean", "play", "visit"]
        claimed_total = 0
        claimed_tasks = []
        for task_type in task_types:
            reward = db.claim_task_reward(user_id, resolved_group_id, task_type)
            if reward is not None:
                claimed_total += reward
                task_names = {"feed": "喂食", "clean": "清洁", "play": "玩耍", "visit": "互访"}
                claimed_tasks.append(task_names.get(task_type, task_type))

        if claimed_tasks:
            return True, with_pet_name(pet, f"🎁 成功领取任务奖励！\n\n完成任务: {'、'.join(claimed_tasks)}\n获得: {claimed_total} 金币")
        return True, with_pet_name(pet, "暂无可领取的任务奖励（未完成或已领取）")

    # 获取或创建今日任务
    tasks = db.get_or_create_daily_tasks(user_id, resolved_group_id)

    task_names = {"feed": "喂食宠物", "clean": "清洁宠物", "play": "玩耍互动", "visit": "访问他人宠物"}

    text = f"📋 **每日任务**\n\n"
    all_completed = True
    for task in tasks:
        task_type = task['task_type']
        current = task['current_value']
        target = task['target_value']
        reward = task['reward_coins']
        claimed = task['claimed']
        name = task_names.get(task_type, task_type)

        if claimed:
            status = "✅ 已领取"
        elif current >= target:
            status = "🎁 可领取"
        else:
            status = f"({current}/{target})"
            all_completed = False

        text += f"• {name} {status} - 奖励 {reward}金币\n"

    text += "\n"
    if all_completed:
        text += "🎉 所有任务已完成！"
    else:
        text += "完成任务后使用 /宠物 任务 领取 来领取奖励"

    return True, with_pet_name(pet, text)


async def handle_rename(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    if not args.strip():
        return False, "请提供新名字\n用法: /宠物 改名 <新名字>"

    pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "改名"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    new_name = resolved_args.strip()
    if not new_name:
        return False, "请提供新名字\n用法: /宠物 改名 <新名字>"

    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    pet_service = PetService(db)
    success, message = pet_service.rename_pet(pet, new_name)

    return success, with_pet_name(pet, message)


async def handle_title(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    """称号系统（Issue #48）"""
    user_service = UserService(db)
    # 先检查是否有新称号
    new_titles = user_service.check_and_award_titles(user_id, group_id)
    text = user_service.format_titles(user_id, group_id)
    if new_titles:
        text += f"\n\n🎉 新获得称号: {'、'.join(new_titles)}"
    return True, text


async def handle_minigame(user_id: str, group_id: int, args: str, db: Database, **kwargs) -> Tuple[bool, str]:
    """小游戏入口（Issue #46）"""
    if not args.strip():
        return True, ("🎮 **小游戏**\n\n"
                      "• /宠物 游戏 猜拳 <石头/剪刀/布> - 猜拳\n"
                      "• /宠物 游戏 骰子 - 骰子比大小\n"
                      "• /宠物 游戏 赛跑 @QQ号 - 宠物赛跑\n")

    parts = args.strip().split(maxsplit=1)
    game_type = parts[0]
    game_args = parts[1] if len(parts) > 1 else ""

    social_service = SocialService(db)

    if game_type in ["猜拳", "rps"]:
        if not game_args:
            return False, "请选择出拳\n用法: /宠物 游戏 猜拳 <石头/剪刀/布>"
        return social_service.play_rock_paper_scissors(user_id, group_id, game_args)

    elif game_type in ["骰子", "dice"]:
        return social_service.play_dice(user_id, group_id)

    elif game_type in ["赛跑", "race"]:
        match = re.match(r"@?(\d+)", game_args)
        if not match:
            return False, "请选择对手\n用法: /宠物 游戏 赛跑 @QQ号"
        target_user_id = match.group(1)
        return social_service.race_pet(user_id, target_user_id, group_id)

    return False, f"未知游戏类型: {game_type}\n可用游戏: 猜拳, 骰子, 赛跑"
