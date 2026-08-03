"""领养、状态、喂食、清洁、玩耍和作息等基础命令。"""

from core.args import parse_int

from ..models import Pet
from ..services.database import Database
from ..services.pet_service import PetService
from ..services.user_service import UserService
from ..utils.formatters import format_pet_card, format_status_text


def with_pet_name(pet: Pet, message: str) -> str:
    header = f"🐾 {pet.name}"
    if not message:
        return header
    if message.startswith(header):
        return message
    return f"{header}\n{message}"


def _split_group_prefix(args: str) -> tuple[int | None, str]:
    raw = args.strip()
    if not raw:
        return None, ""
    parts = raw.split(maxsplit=1)
    first = parts[0]
    group_id = parse_int(first, minimum=1)
    if group_id is None:
        return None, raw
    return group_id, parts[1] if len(parts) > 1 else ""


def _validate_pet_scope(
    db: Database,
    user_id: str,
    pet: Pet,
    resolved_group_id: int,
    resolved_args: str,
) -> tuple[Pet | None, int, str, str | None]:
    """确认目标群已启用宠物系统，且当前用户没有被封禁。"""
    group_config = db.get_group_config(resolved_group_id)
    if not group_config.enabled:
        return None, resolved_group_id, resolved_args, "🚫 宠物系统在该群尚未启用"
    user = db.get_user(user_id, resolved_group_id)
    if user is not None and user.is_banned_active():
        return None, resolved_group_id, resolved_args, "⛔ 你已被封禁，无法使用宠物系统"
    return pet, resolved_group_id, resolved_args, None


def resolve_pet_for_self_command(
    db: Database,
    user_id: str,
    group_id: int,
    args: str,
    command_name: str,
) -> tuple[Pet | None, int, str, str | None]:
    if group_id != 0:
        pet = db.get_pet(user_id, group_id)
        if pet is None:
            return None, group_id, args, "你还没有宠物"
        return _validate_pet_scope(db, user_id, pet, group_id, args)

    pets = db.get_pets_by_user(user_id)
    if not pets:
        return None, 0, args, "你还没有宠物"

    selected_group, remaining = _split_group_prefix(args)
    group_to_pet = {pet.group_id: pet for pet in pets}

    if selected_group is not None:
        selected_pet = group_to_pet.get(selected_group)
        if selected_pet is None:
            groups = "、".join(str(gid) for gid in sorted(group_to_pet))
            return None, 0, args, f"未找到群 {selected_group} 的宠物，可用群号: {groups}"
        return _validate_pet_scope(db, user_id, selected_pet, selected_group, remaining)

    if len(pets) == 1:
        only_pet = pets[0]
        return _validate_pet_scope(db, user_id, only_pet, only_pet.group_id, args)

    groups = "、".join(str(gid) for gid in sorted(group_to_pet))
    return (
        None,
        0,
        args,
        (f"私聊下请先指定群号\n用法: /宠物 {command_name} <群号> [原参数]\n可用群号: {groups}"),
    )


def handle_adopt(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    if not args.strip():
        return False, "请提供宠物名字\n用法: /宠物 领养 <名字>"

    name = args.strip()

    return PetService(db).adopt_pet(user_id, group_id, name)


def handle_status(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    requested_group = args.strip()
    if group_id == 0:
        if requested_group:
            requested_group_id = parse_int(requested_group, minimum=1)
            if requested_group_id is None:
                return False, "群号必须是正整数\n用法: /宠物 状态 [群号]"
            group_id = requested_group_id
    elif requested_group:
        return False, "群聊中不接受额外参数\n用法: /宠物 状态"

    pet = db.get_pet(user_id, group_id)
    if pet is None:
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
        refreshed_user = db.get_user(user_id, group_id)
        user = refreshed_user or user_service.get_or_create_user(user_id, group_id)

    result = format_pet_card(pet, user)

    if new_titles:
        result += f"\n\n🎉 获得新称号: {'、'.join(new_titles)}"

    return True, result


def handle_feed(
    user_id: str,
    group_id: int,
    args: str,
    db: Database,
    *,
    spam_decay_factor: float = 1.0,
) -> tuple[bool, str]:
    pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "喂食"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"

    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    item_id = resolved_args.strip() if resolved_args.strip() else "apple"

    pet_service = PetService(db)
    success, message, _coins = pet_service.feed_pet(
        pet, user, item_id, spam_decay_factor=spam_decay_factor
    )

    if success:
        status_text = format_status_text(pet)
        message = f"{message}\n\n{status_text}"

    return success, with_pet_name(pet, message)


def handle_clean(
    user_id: str,
    group_id: int,
    args: str,
    db: Database,
    *,
    spam_decay_factor: float = 1.0,
) -> tuple[bool, str]:
    pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "清洁"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"
    if resolved_args.strip():
        return False, "清洁命令不接受额外参数\n用法: /宠物 清洁 [群号]"

    pet_service = PetService(db)
    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    success, message, _coins = pet_service.clean_pet(
        pet,
        user,
        spam_decay_factor=spam_decay_factor,
    )

    if success:
        status_text = format_status_text(pet)
        message = f"{message}\n\n{status_text}"

    return success, with_pet_name(pet, message)


def handle_play(
    user_id: str,
    group_id: int,
    args: str,
    db: Database,
    *,
    spam_decay_factor: float = 1.0,
) -> tuple[bool, str]:
    pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "玩耍"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"
    if resolved_args.strip():
        return False, "玩耍命令不接受额外参数\n用法: /宠物 玩耍 [群号]"

    pet_service = PetService(db)
    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, resolved_group_id)

    success, message, _coins = pet_service.play_with_pet(
        pet,
        user,
        spam_decay_factor=spam_decay_factor,
    )

    if success:
        status_text = format_status_text(pet)
        message = f"{message}\n\n{status_text}"

    return success, with_pet_name(pet, message)


def handle_sleep(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    pet, _, resolved_args, err = resolve_pet_for_self_command(db, user_id, group_id, args, "睡觉")
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"
    if resolved_args.strip():
        return False, "睡觉命令不接受额外参数\n用法: /宠物 睡觉 [群号]"

    success, message = PetService(db).sleep_pet(pet)

    return success, with_pet_name(pet, message)


def handle_wake(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    pet, _, resolved_args, err = resolve_pet_for_self_command(db, user_id, group_id, args, "起床")
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"
    if resolved_args.strip():
        return False, "起床命令不接受额外参数\n用法: /宠物 起床 [群号]"

    success, message = PetService(db).wake_pet(pet)

    if success:
        status_text = format_status_text(pet)
        message = f"{message}\n\n{status_text}"

    return success, with_pet_name(pet, message)
