"""
新增命令处理器：装扮、交易、展示会、删除、导出、公告、召回
"""
import logging
import re
import json
from typing import Tuple

from ..models import Pet
from ..services.pet_service import PetService
from ..services.user_service import UserService
from ..services.item_service import ItemService
from ..services.social_service import SocialService
from ..services.database import Database
from ..services.admin_service import AdminService
from ..utils.constants import (
    DEFAULT_DRESS_ITEMS, DressSlot, TRADE_CONFIG, PET_SHOW_CONFIG
)
from ..utils.formatters import format_status_text

logger = logging.getLogger(__name__)


# ──────────────────── 召回 ────────────────────

async def handle_recall(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    """召回旅行中的宠物"""
    pet = db.get_pet(user_id, group_id)
    if not pet:
        return False, "你还没有宠物"

    user_service = UserService(db)
    user = user_service.get_or_create_user(user_id, group_id)

    pet_service = PetService(db)
    return pet_service.recall_pet(pet, user)


# ──────────────────── 装扮系统 ────────────────────

async def handle_dress(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    """装扮系统入口"""
    if not args.strip():
        return _show_dress_help()

    parts = args.strip().split(maxsplit=1)
    action = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    if action in ["查看", "view", "状态"]:
        return _dress_view(user_id, group_id, db)
    elif action in ["商店", "shop"]:
        return _dress_shop()
    elif action in ["购买", "buy"]:
        return _dress_buy(user_id, group_id, rest, db)
    elif action in ["穿戴", "equip", "穿"]:
        return _dress_equip(user_id, group_id, rest, db)
    elif action in ["卸下", "unequip", "脱"]:
        return _dress_unequip(user_id, group_id, rest, db)
    else:
        return _show_dress_help()


def _show_dress_help() -> Tuple[bool, str]:
    return True, ("👗 **装扮系统**\n\n"
                  "• /宠物 装扮 查看 - 查看当前装扮\n"
                  "• /宠物 装扮 商店 - 查看装扮商店\n"
                  "• /宠物 装扮 购买 <装扮ID> - 购买装扮\n"
                  "• /宠物 装扮 穿戴 <装扮ID> - 穿戴装扮\n"
                  "• /宠物 装扮 卸下 <槽位> - 卸下装扮")


def _dress_view(user_id: str, group_id: int, db: Database) -> Tuple[bool, str]:
    pet = db.get_pet(user_id, group_id)
    if not pet:
        return False, "你还没有宠物"

    slots = pet.get_dress_slots()
    text = f"👗 **{pet.name}的装扮**\n\n"
    for slot_name, item_id in slots.items():
        if item_id and item_id in DEFAULT_DRESS_ITEMS:
            item = DEFAULT_DRESS_ITEMS[item_id]
            text += f"• {slot_name}: {item['name']} ({item['rarity'].value})\n"
        else:
            text += f"• {slot_name}: 无\n"

    bonus = pet.get_dress_mood_bonus()
    text += f"\n总心情加成: +{bonus}"

    owned = db.get_dress_inventory(user_id, group_id)
    if owned:
        text += "\n\n📦 已拥有的装扮:\n"
        for did in owned:
            if did in DEFAULT_DRESS_ITEMS:
                text += f"  • [{did}] {DEFAULT_DRESS_ITEMS[did]['name']}\n"

    return True, text


def _dress_shop() -> Tuple[bool, str]:
    text = "👗 **装扮商店**\n\n使用 /宠物 装扮 购买 <ID> 购买\n\n"
    by_slot = {}
    for item_id, item in DEFAULT_DRESS_ITEMS.items():
        slot_name = item["slot"].value
        if slot_name not in by_slot:
            by_slot[slot_name] = []
        by_slot[slot_name].append((item_id, item))

    for slot_name, items in by_slot.items():
        text += f"📌 **{slot_name}**\n"
        for item_id, item in items:
            currency = item.get("currency", "coins")
            price_icon = "💰" if currency == "coins" else "❤️"
            price_text = f"{item['price']}金币" if currency == "coins" else f"{item['price']}友情点"
            
            text += f"  • [{item_id}] {item['name']} ({item['rarity'].value}) - {price_icon}{price_text} | +{item['mood_bonus']}心情\n"
        text += "\n"
    return True, text


def _dress_buy(user_id: str, group_id: int, item_id: str, db: Database) -> Tuple[bool, str]:
    item_id = item_id.strip()
    if not item_id:
        return False, "请指定装扮ID\n用法: /宠物 装扮 购买 <装扮ID>"

    if item_id not in DEFAULT_DRESS_ITEMS:
        return False, f"装扮 '{item_id}' 不存在"

    item = DEFAULT_DRESS_ITEMS[item_id]
    owned = db.get_dress_inventory(user_id, group_id)
    if item_id in owned:
        return False, "你已经拥有该装扮"

    user = db.get_user(user_id, group_id)
    if not user:
        return False, "用户不存在"

    currency = item.get("currency", "coins")
    price = item["price"]

    if currency == "friendship":
        if user.friendship_points < price:
            return False, f"友情点不足，需要{price}友情点，当前{user.friendship_points}"
        user.friendship_points -= price
        cost_msg = f"{price}友情点"
    else:
        if user.coins < price:
            return False, f"金币不足，需要{price}金币，当前{user.coins}金币"
        user.coins -= price
        cost_msg = f"{price}金币"

    db.update_user(user)
    db.add_dress_item(user_id, group_id, item_id)

    return True, f"✅ 购买成功！花费{cost_msg}，获得 {item['name']}\n使用 /宠物 装扮 穿戴 {item_id} 穿戴"


def _dress_equip(user_id: str, group_id: int, item_id: str, db: Database) -> Tuple[bool, str]:
    item_id = item_id.strip()
    if not item_id:
        return False, "请指定装扮ID"

    if item_id not in DEFAULT_DRESS_ITEMS:
        return False, f"装扮 '{item_id}' 不存在"

    owned = db.get_dress_inventory(user_id, group_id)
    if item_id not in owned:
        return False, "你还没有这个装扮，请先购买"

    pet = db.get_pet(user_id, group_id)
    if not pet:
        return False, "你还没有宠物"

    item = DEFAULT_DRESS_ITEMS[item_id]
    slot = item["slot"]
    if slot == DressSlot.HAT:
        pet.dress_hat = item_id
    elif slot == DressSlot.CLOTHES:
        pet.dress_clothes = item_id
    elif slot == DressSlot.ACCESSORY:
        pet.dress_accessory = item_id
    elif slot == DressSlot.BACKGROUND:
        pet.dress_background = item_id

    db.update_pet(pet)
    return True, f"✅ 已穿戴 {item['name']}（{slot.value}）"


def _dress_unequip(user_id: str, group_id: int, slot_name: str, db: Database) -> Tuple[bool, str]:
    slot_name = slot_name.strip()
    pet = db.get_pet(user_id, group_id)
    if not pet:
        return False, "你还没有宠物"

    slot_map = {"帽子": "dress_hat", "衣服": "dress_clothes", "饰品": "dress_accessory", "背景": "dress_background"}
    if slot_name not in slot_map:
        return False, f"无效槽位，可用: {', '.join(slot_map.keys())}"

    attr = slot_map[slot_name]
    if getattr(pet, attr) is None:
        return False, f"{slot_name}槽位没有装扮"

    setattr(pet, attr, None)
    db.update_pet(pet)
    return True, f"✅ 已卸下{slot_name}装扮"


# ──────────────────── 交易市场 ────────────────────

async def handle_trade(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    """交易市场入口"""
    config = db.get_group_config(group_id)
    if not config.trade_enabled:
        return False, "⚠️ 本群尚未开启交易功能\n管理员可使用: /宠物 管理 配置 设置 trade_enabled true"

    if not args.strip():
        return True, ("🏪 **交易市场**\n\n"
                      "• /宠物 交易 列表 - 查看挂单\n"
                      "• /宠物 交易 挂单 <道具ID> <数量> <价格> - 上架\n"
                      "• /宠物 交易 购买 <订单号> - 购买\n"
                      "• /宠物 交易 撤单 <订单号> - 撤销\n"
                      f"  交易税率: {int(TRADE_CONFIG['tax_rate']*100)}%")

    parts = args.strip().split(maxsplit=1)
    action = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    if action in ["列表", "list"]:
        return _trade_list(group_id, db)
    elif action in ["挂单", "sell"]:
        return _trade_sell(user_id, group_id, rest, db)
    elif action in ["购买", "buy"]:
        return _trade_buy(user_id, group_id, rest, db)
    elif action in ["撤单", "cancel"]:
        return _trade_cancel(user_id, group_id, rest, db)
    else:
        return False, "未知交易命令"


def _trade_list(group_id: int, db: Database) -> Tuple[bool, str]:
    listings = db.get_active_listings(group_id)
    if not listings:
        return True, "🏪 当前没有挂单"

    text = "🏪 **交易市场**\n\n"
    from ..services.item_service import ItemService
    item_service = ItemService(db)
    for listing in listings:
        item = item_service.get_item(listing['item_id'])
        name = item.name if item else listing['item_id']
        text += (f"📌 #{listing['id']} | {name} x{listing['amount']}"
                 f" | {listing['price']}金币 | 卖家: {listing['seller_user_id']}\n")
    return True, text


def _trade_sell(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    match = re.match(r"(\S+)\s+(\d+)\s+(\d+)", args)
    if not match:
        return False, "格式错误\n用法: /宠物 交易 挂单 <道具ID> <数量> <价格>"

    item_id, amount, price = match.group(1), int(match.group(2)), int(match.group(3))

    if price < TRADE_CONFIG["min_price"] or price > TRADE_CONFIG["max_price"]:
        return False, f"价格范围: {TRADE_CONFIG['min_price']} ~ {TRADE_CONFIG['max_price']}"

    count = db.get_user_listing_count(user_id, group_id)
    if count >= TRADE_CONFIG["max_listings"]:
        return False, f"挂单数量已达上限({TRADE_CONFIG['max_listings']})"

    inventory = db.get_or_create_inventory(user_id, group_id)
    if not inventory.has_item(item_id, amount):
        return False, "背包中道具数量不足"

    inventory.remove_item(item_id, amount)
    db.update_inventory(inventory)
    db.create_trade_listing(user_id, group_id, item_id, amount, price,
                            TRADE_CONFIG["listing_expire_hours"])

    # CR Review Issue #7: 交易操作记录日志
    admin_service = AdminService(db)
    admin_service.log_admin_operation(
        group_id, user_id, "TRADE_SELL",
        f"挂单:{item_id}x{amount} 售价{price}金币")

    return True, f"✅ 挂单成功！{item_id} x{amount} 售价{price}金币"


def _trade_buy(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    listing_id_str = args.strip()
    if not listing_id_str.isdigit():
        return False, "请指定订单号\n用法: /宠物 交易 购买 <订单号>"

    listing_id = int(listing_id_str)
    listing = db.get_listing_by_id(listing_id)
    if not listing:
        return False, "订单不存在或已过期"

    if listing['seller_user_id'] == user_id:
        return False, "不能购买自己的挂单"

    if listing['group_id'] != group_id:
        return False, "该订单不属于本群"

    buyer = db.get_user(user_id, group_id)
    if not buyer:
        return False, "用户不存在"

    total_cost = listing['price']
    tax = int(total_cost * TRADE_CONFIG["tax_rate"])
    if buyer.coins < total_cost:
        return False, f"金币不足，需要{total_cost}金币"

    buyer.coins -= total_cost
    db.update_user(buyer)

    seller = db.get_user(listing['seller_user_id'], group_id)
    if seller:
        seller.coins += total_cost - tax
        db.update_user(seller)

    inventory = db.get_or_create_inventory(user_id, group_id)
    inventory.add_item(listing['item_id'], listing['amount'])
    db.update_inventory(inventory)

    db.deactivate_listing(listing_id)

    # CR Review Issue #7: 交易操作记录日志
    admin_service = AdminService(db)
    admin_service.log_admin_operation(
        group_id, user_id, "TRADE_BUY",
        f"购买订单#{listing_id} {listing['item_id']}x{listing['amount']} 花费{total_cost}金币",
        target_user_id=listing['seller_user_id'])

    return True, f"✅ 购买成功！获得 {listing['item_id']} x{listing['amount']} 花费{total_cost}金币（税{tax}）"


def _trade_cancel(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    listing_id_str = args.strip()
    if not listing_id_str.isdigit():
        return False, "请指定订单号"

    listing_id = int(listing_id_str)
    listing = db.get_listing_by_id(listing_id)
    if not listing:
        return False, "订单不存在"

    if listing['seller_user_id'] != user_id:
        return False, "只能撤销自己的挂单"

    inventory = db.get_or_create_inventory(user_id, group_id)
    inventory.add_item(listing['item_id'], listing['amount'])
    db.update_inventory(inventory)
    db.deactivate_listing(listing_id)

    # CR Review Issue #7: 交易操作记录日志
    admin_service = AdminService(db)
    admin_service.log_admin_operation(
        group_id, user_id, "TRADE_CANCEL",
        f"撤单#{listing_id} {listing['item_id']}x{listing['amount']}")

    return True, f"✅ 已撤单，道具已退还"


# ──────────────────── 宠物展示会 ────────────────────

async def handle_show(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    """宠物展示会入口"""
    if not args.strip():
        return _show_info(group_id, db)

    parts = args.strip().split(maxsplit=1)
    action = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    if action in ["投票", "vote"]:
        return _show_vote(user_id, group_id, rest, db)
    else:
        return _show_info(group_id, db)


def _show_info(group_id: int, db: Database) -> Tuple[bool, str]:
    show = db.get_active_pet_show(group_id)
    if not show:
        return True, ("🏆 **宠物展示会**\n\n"
                      "当前没有进行中的展示会\n"
                      "管理员可使用 /宠物 管理 公告 展示会 来开启")

    votes = db.get_pet_show_votes(show['id'])
    text = f"🏆 **{show['title']}**\n\n"
    text += f"截止时间: {show['end_time']}\n\n"

    if votes:
        text += "当前排名:\n"
        for i, (uid, count) in enumerate(votes.items()):
            pet = db.get_pet(uid, group_id)
            name = pet.name if pet else uid
            medals = ["🥇", "🥈", "🥉"]
            medal = medals[i] if i < 3 else f"#{i+1}"
            text += f"{medal} {name} ({uid}) - {count}票\n"
    else:
        text += "暂无投票\n"

    text += f"\n投票: /宠物 展示 投票 @QQ号 (每人最多{PET_SHOW_CONFIG['max_votes_per_user']}票)"
    return True, text


def _show_vote(user_id: str, group_id: int, args: str, db: Database) -> Tuple[bool, str]:
    match = re.match(r"@?(\d+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 展示 投票 @QQ号"

    target_id = match.group(1)
    if target_id == user_id:
        return False, "不能给自己投票"

    show = db.get_active_pet_show(group_id)
    if not show:
        return False, "当前没有进行中的展示会"

    vote_count = db.get_user_vote_count(show['id'], user_id)
    if vote_count >= PET_SHOW_CONFIG['max_votes_per_user']:
        return False, f"你已投满{PET_SHOW_CONFIG['max_votes_per_user']}票"

    target_pet = db.get_pet(target_id, group_id)
    if not target_pet:
        return False, "对方没有宠物"

    db.vote_pet_show(show['id'], user_id, target_id)
    return True, f"✅ 成功为 {target_pet.name} 投票！"


# ──────────────────── 管理命令: 删除、导出、公告 ────────────────────

async def handle_manage_delete(user_id: str, group_id: int, args: str,
                                db: Database, is_admin: bool = False) -> Tuple[bool, str]:
    """删除用户宠物（CR Fix #16）"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    match = re.match(r"@?(\d+)", args.strip())
    if not match:
        return False, "格式错误\n用法: /宠物 管理 删除 @QQ号"

    target_user_id = match.group(1)
    success = db.delete_pet(target_user_id, group_id)
    if success:
        return True, f"✅ 用户 {target_user_id} 的宠物已删除"
    return False, "删除失败"


async def handle_manage_export(user_id: str, group_id: int, args: str,
                                db: Database, is_admin: bool = False) -> Tuple[bool, str]:
    """导出群数据"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    data = db.export_group_data(group_id)
    if not data:
        return False, "导出失败"

    summary = (f"📊 **数据导出摘要**\n\n"
               f"• 群ID: {group_id}\n"
               f"• 用户数: {len(data.get('users', []))}\n"
               f"• 宠物数: {len(data.get('pets', []))}\n"
               f"• 导出时间: {data.get('exported_at', '')}\n\n"
               f"完整数据已保存至数据库日志")

    from ..services.admin_service import AdminService
    admin_service = AdminService(db)
    admin_service.log_admin_operation(group_id, user_id, "EXPORT",
                                      json.dumps({"users": len(data.get('users', [])),
                                                   "pets": len(data.get('pets', []))}))

    return True, summary


async def handle_manage_announce(user_id: str, group_id: int, args: str,
                                  db: Database, is_admin: bool = False) -> Tuple[bool, str]:
    """管理公告：开启展示会等"""
    if not is_admin:
        return False, "⚠️ 该操作需要管理员权限"

    if not args.strip():
        return True, ("📢 **管理公告**\n\n"
                      "• /宠物 管理 公告 展示会 [标题] - 开启宠物展示会\n"
                      "• /宠物 管理 公告 结束展示会 - 结束展示会并发放奖励")

    parts = args.strip().split(maxsplit=1)
    action = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    if action in ["展示会", "show"]:
        title = rest if rest else "宠物展示会"
        existing = db.get_active_pet_show(group_id)
        if existing:
            return False, "已有进行中的展示会"

        show_id = db.create_pet_show(group_id, title, PET_SHOW_CONFIG["duration_hours"])
        if show_id:
            return True, (f"🏆 **{title}** 已开启！\n\n"
                          f"持续时间: {PET_SHOW_CONFIG['duration_hours']}小时\n"
                          f"投票: /宠物 展示 投票 @QQ号\n"
                          f"每人最多{PET_SHOW_CONFIG['max_votes_per_user']}票")
        return False, "开启失败"

    elif action in ["结束展示会", "end_show"]:
        show = db.get_active_pet_show(group_id)
        if not show:
            return False, "没有进行中的展示会"

        social_service = SocialService(db)
        result = social_service.settle_pet_show(group_id)
        return True, result if result else "展示会已结束（无投票数据）"

    return False, "未知公告命令"
