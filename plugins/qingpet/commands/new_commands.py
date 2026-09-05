"""召回、装扮、交易、展示会及相关管理命令。"""

import re

from core.args import parse_int

from ..services.admin_service import AdminService
from ..services.database import Database
from ..services.item_service import ItemService
from ..services.pet_service import PetService
from ..services.social_service import SocialService
from ..services.user_service import UserService
from ..utils.constants import DEFAULT_DRESS_ITEMS, PET_SHOW_CONFIG, TRADE_CONFIG, DressSlot
from .basic_commands import resolve_pet_for_self_command

_DRESS_SLOT_FIELDS = {
    DressSlot.HAT: "dress_hat",
    DressSlot.CLOTHES: "dress_clothes",
    DressSlot.ACCESSORY: "dress_accessory",
    DressSlot.BACKGROUND: "dress_background",
}
_DRESS_SLOT_NAMES = {slot.value: field_name for slot, field_name in _DRESS_SLOT_FIELDS.items()}
_SHOW_MEDALS      = ("🥇", "🥈", "🥉")


# ──────────────────── 召回 ────────────────────


def handle_recall(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """召回旅行中的宠物。"""
    pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(
        db, user_id, group_id, args, "召回"
    )
    if err:
        return False, err
    if pet is None:
        return False, "你还没有宠物"
    if resolved_args.strip():
        return False, "召回命令不接受额外参数\n用法: /宠物 召回 [群号]"

    user = UserService(db).get_or_create_user(user_id, resolved_group_id)
    return PetService(db).recall_pet(pet, user)


# ──────────────────── 装扮系统 ────────────────────


def handle_dress(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """分发装扮查看、购买、穿戴和卸下操作。"""
    if not args.strip():
        return _show_dress_help()

    parts = args.strip().split(maxsplit=1)
    action = parts[0]
    rest   = parts[1] if len(parts) > 1 else ""

    if action in {"查看", "view", "状态"}:
        if rest.strip():
            return False, "查看装扮不接受额外参数\n用法: /宠物 装扮 查看"
        return _dress_view(user_id, group_id, db)
    if action in {"商店", "shop"}:
        if rest.strip():
            return False, "装扮商店不接受额外参数\n用法: /宠物 装扮 商店"
        return _dress_shop()
    if action in {"购买", "buy"}:
        return _dress_buy(user_id, group_id, rest, db)
    if action in {"穿戴", "equip", "穿"}:
        return _dress_equip(user_id, group_id, rest, db)
    if action in {"卸下", "unequip", "脱"}:
        return _dress_unequip(user_id, group_id, rest, db)
    return False, "未知装扮命令\n用法: /宠物 装扮 <查看|商店|购买|穿戴|卸下>"


def _show_dress_help() -> tuple[bool, str]:
    return True, (
        "👗 **装扮系统**\n\n"
        "• /宠物 装扮 查看 - 查看当前装扮\n"
        "• /宠物 装扮 商店 - 查看装扮商店\n"
        "• /宠物 装扮 购买 <装扮ID> - 购买装扮\n"
        "• /宠物 装扮 穿戴 <装扮ID> - 穿戴装扮\n"
        "• /宠物 装扮 卸下 <槽位> - 卸下装扮"
    )


def _dress_view(user_id: str, group_id: int, db: Database) -> tuple[bool, str]:
    pet = db.get_pet(user_id, group_id)
    if pet is None:
        return False, "你还没有宠物"

    lines = [f"👗 **{pet.name}的装扮**", ""]
    for slot_name, item_id in pet.get_dress_slots().items():
        item = DEFAULT_DRESS_ITEMS.get(item_id) if item_id else None
        if item is None:
            lines.append(f"• {slot_name}: 无")
            continue
        lines.append(f"• {slot_name}: {item['name']} ({item['rarity'].value})")

    bonus = pet.get_dress_mood_bonus()
    lines.extend(("", f"总心情加成: +{bonus}"))

    owned = db.get_dress_inventory(user_id, group_id)
    if owned:
        lines.extend(("", "📦 已拥有的装扮:"))
        for item_id in owned:
            item      = DEFAULT_DRESS_ITEMS.get(item_id)
            item_name = item["name"] if item is not None else "未知道具"
            lines.append(f"  • [{item_id}] {item_name}")

    return True, "\n".join(lines)


def _dress_shop() -> tuple[bool, str]:
    by_slot: dict[str, list[tuple[str, dict]]] = {}
    for item_id, item in DEFAULT_DRESS_ITEMS.items():
        slot_name = item["slot"].value
        by_slot.setdefault(slot_name, []).append((item_id, item))

    lines = ["👗 **装扮商店**", "", "使用 /宠物 装扮 购买 <ID> 购买", ""]
    for slot_name, items in by_slot.items():
        lines.append(f"📌 **{slot_name}**")
        for item_id, item in items:
            currency   = item.get("currency", "coins")
            price_icon = "💰" if currency == "coins" else "❤️"
            price_text = f"{item['price']}金币" if currency == "coins" else f"{item['price']}友情点"
            lines.append(
                f"  • [{item_id}] {item['name']} ({item['rarity'].value}) - "
                f"{price_icon}{price_text} | +{item['mood_bonus']}心情"
            )
        lines.append("")
    return True, "\n".join(lines)


def _dress_buy(user_id: str, group_id: int, item_id: str, db: Database) -> tuple[bool, str]:
    item_id = item_id.strip()
    if not item_id:
        return False, "请指定装扮ID\n用法: /宠物 装扮 购买 <装扮ID>"

    if item_id not in DEFAULT_DRESS_ITEMS:
        return False, f"装扮 '{item_id}' 不存在"

    item     = DEFAULT_DRESS_ITEMS[item_id]
    currency = item.get("currency", "coins")
    price    = item["price"]
    success, reason = db.purchase_dress_atomic(user_id, group_id, item_id, currency, price)
    if not success:
        if reason == "余额不足":
            reason = "友情点不足" if currency == "friendship" else "金币不足"
        return False, reason
    cost_msg = f"{price}友情点" if currency == "friendship" else f"{price}金币"

    return (
        True,
        f"✅ 购买成功！花费{cost_msg}，获得 {item['name']}\n使用 /宠物 装扮 穿戴 {item_id} 穿戴",
    )


def _dress_equip(user_id: str, group_id: int, item_id: str, db: Database) -> tuple[bool, str]:
    item_id = item_id.strip()
    if not item_id:
        return False, "请指定装扮ID"

    if item_id not in DEFAULT_DRESS_ITEMS:
        return False, f"装扮 '{item_id}' 不存在"

    owned = db.get_dress_inventory(user_id, group_id)
    if item_id not in owned:
        return False, "你还没有这个装扮，请先购买"

    pet = db.get_pet(user_id, group_id)
    if pet is None:
        return False, "你还没有宠物"

    item       = DEFAULT_DRESS_ITEMS[item_id]
    slot       = item["slot"]
    field_name = _DRESS_SLOT_FIELDS.get(slot)
    if field_name is None:
        return False, "装扮槽位无效"
    setattr(pet, field_name, item_id)
    if not db.update_pet(pet):
        return False, "穿戴失败，请稍后重试"
    return True, f"✅ 已穿戴 {item['name']}（{slot.value}）"


def _dress_unequip(user_id: str, group_id: int, slot_name: str, db: Database) -> tuple[bool, str]:
    slot_name = slot_name.strip()
    pet       = db.get_pet(user_id, group_id)
    if pet is None:
        return False, "你还没有宠物"

    if slot_name not in _DRESS_SLOT_NAMES:
        return False, f"无效槽位，可用: {', '.join(_DRESS_SLOT_NAMES)}"

    attr = _DRESS_SLOT_NAMES[slot_name]
    if getattr(pet, attr) is None:
        return False, f"{slot_name}槽位没有装扮"

    setattr(pet, attr, None)
    if not db.update_pet(pet):
        return False, "卸下失败，请稍后重试"
    return True, f"✅ 已卸下{slot_name}装扮"


# ──────────────────── 交易市场 ────────────────────


def handle_trade(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """分发交易市场的查询、挂单、购买和撤单操作。"""
    config = db.get_group_config(group_id)
    if not config.trade_enabled:
        return (
            False,
            "⚠️ 本群尚未开启交易功能\n管理员可使用: /宠物 管理 配置 设置 trade_enabled true",
        )

    if not args.strip():
        return True, (
            "🏪 **交易市场**\n\n"
            "• /宠物 交易 列表 - 查看挂单\n"
            "• /宠物 交易 挂单 <道具ID> <数量> <价格> - 上架\n"
            "• /宠物 交易 购买 <订单号> - 购买\n"
            "• /宠物 交易 撤单 <订单号> - 撤销\n"
            f"  交易税率: {int(TRADE_CONFIG['tax_rate'] * 100)}%"
        )

    parts = args.strip().split(maxsplit=1)
    action = parts[0]
    rest   = parts[1] if len(parts) > 1 else ""

    if action in {"列表", "list"}:
        if rest.strip():
            return False, "交易列表不接受额外参数\n用法: /宠物 交易 列表"
        return _trade_list(group_id, db)
    if action in {"挂单", "sell"}:
        return _trade_sell(user_id, group_id, rest, db)
    if action in {"购买", "buy"}:
        return _trade_buy(user_id, group_id, rest, db)
    if action in {"撤单", "cancel"}:
        return _trade_cancel(user_id, group_id, rest, db)
    return False, "未知交易命令"


def _trade_list(group_id: int, db: Database) -> tuple[bool, str]:
    listings = db.get_active_listings(group_id)
    if not listings:
        return True, "🏪 当前没有挂单"

    lines        = ["🏪 **交易市场**", ""]
    item_service = ItemService(db)
    for listing in listings:
        item = item_service.get_item(listing["item_id"])
        name = item.name if item is not None else listing["item_id"]
        lines.append(
            f"📌 #{listing['id']} | {name} x{listing['amount']}"
            f" | {listing['price']}金币 | 卖家: {listing['seller_user_id']}"
        )
    return True, "\n".join(lines)


def _trade_sell(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    match = re.fullmatch(r"(\S+)\s+([1-9]\d*)\s+([1-9]\d*)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 交易 挂单 <道具ID> <数量> <价格>"

    item_id, amount, price = match.group(1), int(match.group(2)), int(match.group(3))

    if price < TRADE_CONFIG["min_price"] or price > TRADE_CONFIG["max_price"]:
        return False, f"价格范围: {TRADE_CONFIG['min_price']} ~ {TRADE_CONFIG['max_price']}"

    if not db.create_trade_listing_atomic(
        user_id,
        group_id,
        item_id,
        amount,
        price,
        TRADE_CONFIG["listing_expire_hours"],
        TRADE_CONFIG["max_listings"],
    ):
        return False, "背包数量不足、挂单已达上限或创建失败"

    # 交易状态改变后写入管理员审计日志，便于追溯资产流转。
    admin_service = AdminService(db)
    admin_service.log_admin_operation(
        group_id, user_id, "TRADE_SELL", f"挂单:{item_id}x{amount} 售价{price}金币"
    )

    return True, f"✅ 挂单成功！{item_id} x{amount} 售价{price}金币"


def _trade_buy(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    listing_id_str = args.strip()
    listing_id = parse_int(listing_id_str, minimum=1)
    if listing_id is None:
        return False, "请指定订单号\n用法: /宠物 交易 购买 <订单号>"

    success, result = db.purchase_trade_listing(
        listing_id, user_id, group_id, TRADE_CONFIG["tax_rate"]
    )
    if not success:
        return False, str(result)
    if not isinstance(result, dict):
        return False, "购买结果异常，请稍后重试"
    listing = result

    # 成交后记录买卖双方与金额，保持经济变更可追溯。
    admin_service = AdminService(db)
    admin_service.log_admin_operation(
        group_id,
        user_id,
        "TRADE_BUY",
        f"购买订单#{listing_id} {listing['item_id']}x{listing['amount']} 花费{listing['price']}金币",
        target_user_id=str(listing["seller_user_id"]),
    )

    return True, (
        f"✅ 购买成功！获得 {listing['item_id']} x{listing['amount']} "
        f"花费{listing['price']}金币（税{listing['tax']}）"
    )


def _trade_cancel(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    listing_id_str = args.strip()
    listing_id = parse_int(listing_id_str, minimum=1)
    if listing_id is None:
        return False, "请指定订单号"

    listing = db.get_listing_by_id(listing_id, group_id)
    if not listing:
        return False, "订单不存在"

    if listing["seller_user_id"] != user_id:
        return False, "只能撤销自己的挂单"
    if not db.cancel_trade_listing(listing_id, user_id, group_id):
        return False, "撤单失败"

    # 撤单成功后记录审计事件；失败操作不写成已完成交易。
    admin_service = AdminService(db)
    admin_service.log_admin_operation(
        listing["group_id"],
        user_id,
        "TRADE_CANCEL",
        f"撤单#{listing_id} {listing['item_id']}x{listing['amount']}",
    )

    return True, "✅ 已撤单，道具已退还"


# ──────────────────── 宠物展示会 ────────────────────


def handle_show(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    """展示当前比赛信息或提交投票。"""
    if not args.strip():
        return _show_info(group_id, db)

    parts = args.strip().split(maxsplit=1)
    action = parts[0]
    rest   = parts[1] if len(parts) > 1 else ""

    if action in {"投票", "vote"}:
        return _show_vote(user_id, group_id, rest, db)
    return False, "未知展示命令\n用法: /宠物 展示 [投票 <@QQ号>]"


def _show_info(group_id: int, db: Database) -> tuple[bool, str]:
    show = db.get_active_pet_show(group_id)
    if show is None:
        return True, (
            "🏆 **宠物展示会**\n\n"
            "当前没有进行中的展示会\n"
            "管理员可使用 /宠物 管理 公告 展示会 来开启"
        )

    votes = db.get_pet_show_votes(show["id"])
    lines = [f"🏆 **{show['title']}**", "", f"截止时间: {show['end_time']}", ""]

    if votes:
        lines.append("当前排名:")
        for index, (target_user_id, count) in enumerate(votes.items()):
            pet   = db.get_pet(target_user_id, group_id)
            name  = pet.name if pet is not None else target_user_id
            medal = _SHOW_MEDALS[index] if index < len(_SHOW_MEDALS) else f"#{index + 1}"
            lines.append(f"{medal} {name} ({target_user_id}) - {count}票")
    else:
        lines.append("暂无投票")

    lines.extend(
        (
            "",
            f"投票: /宠物 展示 投票 @QQ号 (每人最多{PET_SHOW_CONFIG['max_votes_per_user']}票)",
        )
    )
    return True, "\n".join(lines)


def _show_vote(user_id: str, group_id: int, args: str, db: Database) -> tuple[bool, str]:
    match = re.fullmatch(r"@?(\d+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 展示 投票 @QQ号"

    target_id = match.group(1)
    if target_id == user_id:
        return False, "不能给自己投票"

    show = db.get_active_pet_show(group_id)
    if show is None:
        return False, "当前没有进行中的展示会"

    target_pet = db.get_pet(target_id, group_id)
    if target_pet is None:
        return False, "对方没有宠物"

    if not db.vote_pet_show_atomic(
        show["id"], user_id, target_id, PET_SHOW_CONFIG["max_votes_per_user"]
    ):
        return False, f"你已投满{PET_SHOW_CONFIG['max_votes_per_user']}票或投票失败"
    return True, f"✅ 成功为 {target_pet.name} 投票！"


# ──────────────────── 管理命令: 删除、公告 ────────────────────


def handle_manage_delete(
    user_id: str, group_id: int, args: str, db: Database, is_admin: bool = False
) -> tuple[bool, str]:
    """经管理员授权后删除指定用户的宠物。"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    match = re.fullmatch(r"@?(\d+)(?:\s+(确认|confirm))?", args.strip(), re.IGNORECASE)
    if not match:
        return False, "格式错误\n用法: /宠物 管理 删除 @QQ号"

    target_user_id = match.group(1)
    if not match.group(2):
        return False, (
            f"⚠️ 将永久删除用户 {target_user_id} 的宠物。\n"
            f"确认执行请发送：/宠物 管理 删除 @{target_user_id} 确认"
        )
    success = AdminService(db).delete_user_pet(
        target_user_id,
        group_id,
        operator_user_id=user_id,
    )
    if success:
        return True, f"✅ 用户 {target_user_id} 的宠物已删除"
    return False, "删除失败"


def handle_manage_announce(
    user_id: str, group_id: int, args: str, db: Database, is_admin: bool = False
) -> tuple[bool, str]:
    """经管理员授权后开启或结束宠物展示会。"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    if not args.strip():
        return True, (
            "📢 **管理公告**\n\n"
            "• /宠物 管理 公告 展示会 [标题] - 开启宠物展示会\n"
            "• /宠物 管理 公告 结束展示会 - 结束展示会并发放奖励"
        )

    parts = args.strip().split(maxsplit=1)
    action = parts[0]
    rest   = parts[1] if len(parts) > 1 else ""

    if action in {"展示会", "show"}:
        title    = rest if rest else "宠物展示会"
        existing = db.get_active_pet_show(group_id)
        if existing is not None:
            return False, "已有进行中的展示会"

        show_id = db.create_pet_show(group_id, title, PET_SHOW_CONFIG["duration_hours"])
        if show_id is not None:
            return True, (
                f"🏆 **{title}** 已开启！\n\n"
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
