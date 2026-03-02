import logging
from typing import Optional, Tuple

from ..models import Pet
from ..services.pet_service import PetService
from ..services.user_service import UserService
from ..services.database import Database
from ..utils.validators import validate_pet_name
from ..utils.formatters import format_pet_card, format_status_text

logger = logging.getLogger(__name__)


def with_pet_name(pet: Pet, message: str) -> str:
    header = f"🐾 {pet.name}"
    if not message:
        return header
    if message.startswith(header):
        return message
    return f"{header}\n{message}"


def _split_group_prefix(args: str) -> Tuple[Optional[int], str]:
    raw = args.strip()
    if not raw:
        return None, ""
    parts = raw.split(maxsplit=1)
    first = parts[0]
    if not first.isdigit():
        return None, raw
    return int(first), parts[1] if len(parts) > 1 else ""


def resolve_pet_for_self_command(
    db: Database,
    user_id: str,
    group_id: int,
    args: str,
    command_name: str,
) -> Tuple[Optional[Pet], int, str, Optional[str]]:
    if group_id != 0:
        pet = db.get_pet(user_id, group_id)
        if not pet:
            return None, group_id, args, "你还没有宠物"
        return pet, group_id, args, None

    pets = db.get_pets_by_user(user_id)
    if not pets:
        return None, 0, args, "你还没有宠物"

    selected_group, remaining = _split_group_prefix(args)
    group_to_pet = {pet.group_id: pet for pet in pets}

    if selected_group is not None:
        selected_pet = group_to_pet.get(selected_group)
        if not selected_pet:
            groups = "、".join(str(gid) for gid in sorted(group_to_pet))
            return None, 0, args, f"未找到群 {selected_group} 的宠物，可用群号: {groups}"
        return selected_pet, selected_group, remaining, None

    if len(pets) == 1:
        only_pet = pets[0]
        return only_pet, only_pet.group_id, args, None

    groups = "、".join(str(gid) for gid in sorted(group_to_pet))
    return None, 0, args, (
        f"私聊下请先指定群号\n"
        f"用法: /宠物 {command_name} <群号> [原参数]\n"
        f"可用群号: {groups}"
    )



def _get_services(db: Database) -> Tuple[PetService, UserService]:
    return PetService(db), UserService(db)


async def handle_adopt(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    if not args.strip():
        return False, "请提供宠物名字\n用法: /宠物 领养 <名字>"

    name = args.strip()

    # 敏感词过滤
    group_config = db.get_group_config(group_id)
    is_valid, error_msg = validate_pet_name(name, group_config.sensitive_words)
    if not is_valid:
        return False, error_msg

    pet_service, _ = _get_services(db)
    # adopt_pet 内部已包含创建 User 记录（Issue #6 已修复）
    success, message = pet_service.adopt_pet(user_id, group_id, name)

    return success, message


async def handle_status(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    if group_id == 0 and args.strip().isdigit():
        group_id = int(args.strip())

    pet = db.get_pet(user_id, group_id)
    if not pet:
        if group_id == 0:
            pets = db.get_pets_by_user(user_id)
            if pets:
                user_service = UserService(db)
                cards = []
                for user_pet in pets:
                    user = user_service.get_or_create_user(user_id, user_pet.group_id)
                    cards.append(f"🏠 群 {user_pet.group_id}\n{format_pet_card(user_pet, user)}")
                return True, "你在这些群里领养了宠物：\n\n" + "\n\n".join(cards)
        return False, "你还没有宠物，使用 /宠物 领养 <名字> 来领养一只吧！"

    # 确保用户存在
    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, group_id)

    # 检查并颁发称号
    new_titles = user_service.check_and_award_titles(user_id, group_id)
    # 重新获取 user 以包含新称号
    if new_titles:
        user = db.get_user(user_id, group_id)
        if user is None:
            user = user_service.get_or_create_user(user_id, group_id)

    result = format_pet_card(pet, user)

    if new_titles:
        result += f"\n\n🎉 获得新称号: {'、'.join(new_titles)}"

    return True, result


async def handle_feed(user_id: str, group_id: int, args: str, db: Database, **kwargs) -> Tuple[bool, str]:
    pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "喂食"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    item_id = resolved_args.strip() if resolved_args.strip() else "apple"
    spam_decay = kwargs.get("spam_decay_factor", 1.0)

    pet_service = PetService(db)
    success, message, coins = pet_service.feed_pet(pet, user, item_id,
                                                    spam_decay_factor=spam_decay)

    if success:
        status_text = format_status_text(pet)
        message = f"{message}\n\n{status_text}"

    return success, with_pet_name(pet, message)


async def handle_clean(user_id: str, group_id: int, args: str, db: Database, **kwargs) -> Tuple[bool, str]:
    pet, resolved_group_id, _, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "清洁"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    pet_service, user_service = _get_services(db)
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    spam_decay = kwargs.get("spam_decay_factor", 1.0)
    success, message, coins = pet_service.clean_pet(pet, user,
                                                     spam_decay_factor=spam_decay)

    if success:
        status_text = format_status_text(pet)
        message = f"{message}\n\n{status_text}"

    return success, with_pet_name(pet, message)


async def handle_play(user_id: str, group_id: int, args: str, db: Database, **kwargs) -> Tuple[bool, str]:
    pet, resolved_group_id, _, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "玩耍"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    pet_service, user_service = _get_services(db)
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    spam_decay = kwargs.get("spam_decay_factor", 1.0)
    success, message, coins = pet_service.play_with_pet(pet, user,
                                                         spam_decay_factor=spam_decay)

    if success:
        status_text = format_status_text(pet)
        message = f"{message}\n\n{status_text}"

    return success, with_pet_name(pet, message)


async def handle_sleep(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    pet, resolved_group_id, _, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "睡觉"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    pet_service, user_service = _get_services(db)
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    success, message = pet_service.sleep_pet(pet)

    return success, with_pet_name(pet, message)


async def handle_wake(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    pet, resolved_group_id, _, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "起床"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    pet_service, user_service = _get_services(db)
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    if user.is_banned_active():
        return False, "你已被封禁，无法操作"

    success, message = pet_service.wake_pet(pet)

    if success:
        status_text = format_status_text(pet)
        message = f"{message}\n\n{status_text}"

    return success, with_pet_name(pet, message)
