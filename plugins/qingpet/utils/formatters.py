from ..models import Pet, User
from ..utils.constants import PetStage, PetStatus, MAX_STAT_VALUE, DEFAULT_DRESS_ITEMS
from typing import List, Tuple


def _progress_bar(value: int, max_val: int = MAX_STAT_VALUE, length: int = 10) -> str:
    filled = int(value / max_val * length) if max_val > 0 else 0
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {value}/{max_val}"


def format_pet_card(pet: Pet, user: User = None) -> str:
    stage_emoji = {
        PetStage.EGG: "🥚",
        PetStage.YOUNG: "🐣",
        PetStage.GROWTH: "🐥",
        PetStage.MATURE: "🐔",
        PetStage.OLD: "🐦",
    }

    status_emoji = {
        PetStatus.NORMAL: "✅",
        PetStatus.SICK: "🤒",
        PetStatus.SLEEPING: "😴",
        PetStatus.TRAVELING: "✈️",
        PetStatus.DEAD: "💀",
    }

    emoji = stage_emoji.get(pet.stage, "🐾")
    s_emoji = status_emoji.get(pet.status, "❓")

    text = f"🐾 **{pet.name}** {emoji}\n"
    text += f"{'═' * 20}\n"
    text += f"• 阶段: {pet.stage.value} ({pet.form})\n"
    text += f"• 性格: {pet.personality.value}\n"
    text += f"• 状态: {s_emoji} {pet.status.value}\n"
    text += f"• 年龄: {pet.age}天\n"
    text += f"• 亲密度: {pet.intimacy}\n"
    text += f"• 经验值: {pet.experience}\n"
    text += f"• 点赞: {pet.likes}\n\n"
    text += f"📊 **属性**\n"
    text += f"  饥饿: {_progress_bar(pet.hunger)}\n"
    text += f"  心情: {_progress_bar(pet.mood)}\n"
    text += f"  清洁: {_progress_bar(pet.clean)}\n"
    text += f"  精力: {_progress_bar(pet.energy)}\n"
    text += f"  健康: {_progress_bar(pet.health)}\n"
    text += f"  照顾评分: {round(pet.care_score * 100, 1)}%\n"

    # 装扮展示
    dress_slots = pet.get_dress_slots()
    equipped = {k: v for k, v in dress_slots.items() if v}
    if equipped:
        slot_emojis = {
            "帽子": "🎩",
            "衣服": "👕",
            "饰品": "🎀",
            "背景": "🖼️"
        }
        text += f"\n👗 **装扮**\n"
        for slot_name, item_id in equipped.items():
            if item_id in DEFAULT_DRESS_ITEMS:
                emoji = slot_emojis.get(slot_name, "🔸")
                text += f"  {emoji} {slot_name}: {DEFAULT_DRESS_ITEMS[item_id]['name']}\n"
        bonus = pet.get_dress_mood_bonus()
        if bonus > 0:
            text += f"  ✨ 心情加成: +{bonus}\n"

    if user:
        text += f"\n💰 **用户信息**\n"
        text += f"  🪙 金币: {user.coins}\n"
        text += f"  ❤️ 友情点: {user.friendship_points}\n"
        if user.titles:
            text += f"  🏷️ 称号: {'、'.join(user.titles[:3])}"
            if len(user.titles) > 3:
                text += f" 等{len(user.titles)}个"
            text += "\n"
        if user.is_trustee_active():
            text += f"  🛡️ 托管中（衰减减半）\n"

    return text


def format_status_text(pet: Pet) -> str:
    alerts = []
    if pet.hunger < 30:
        alerts.append("🍖 宠物饿了！快去喂食")
    if pet.clean < 30:
        alerts.append("🧹 宠物脏了！快去清洁")
    if pet.mood < 30:
        alerts.append("😢 宠物心情不好！快去玩耍")
    if pet.energy < 20:
        alerts.append("💤 宠物累了！让它休息一下")
    if pet.health < 30:
        alerts.append("💊 宠物健康堪忧！快去治疗")
    if pet.status == PetStatus.SICK:
        alerts.append("🤒 宠物生病了！请使用药品治疗")
    if pet.status == PetStatus.TRAVELING:
        alerts.append("✈️ 宠物正在旅行中，使用 /宠物 召回 提前召回")

    if not alerts:
        return "✨ 宠物状态良好"

    return "\n".join(alerts)


def format_ranking_list(ranking: List[Tuple[str, str, float]], ranking_type: str) -> str:
    type_labels = {
        "care_score": "🏆 照顾评分排行",
        "intimacy": "💕 亲密度排行",
        "experience": "🎯 经验值排行",
        "coins": "💰 金币排行"
    }

    type_units = {
        "care_score": "%",
        "intimacy": "",
        "experience": "",
        "coins": "💰"
    }

    title = type_labels.get(ranking_type, "排行榜")
    unit = type_units.get(ranking_type, "")

    text = f"{title}\n{'═' * 20}\n\n"

    if not ranking:
        return text + "暂无数据"

    medal_emoji = ["🥇", "🥈", "🥉"]
    for i, (user_id, pet_name, value) in enumerate(ranking):
        emoji = medal_emoji[i] if i < 3 else f"#{i + 1}"
        display_value = f"{value}{unit}" if unit else str(value)
        text += f"{emoji} {pet_name} ({user_id}) - {display_value}\n"

    return text


def format_help_text(category: str = "") -> str:
    """
    格式化帮助文档，支持按类别查看
    """
    # 使用英文键名以增强编码兼容性
    categories = {
        "basic": (
            "📌 **基础命令**\n"
            "• /宠物 领养 <名字> - 领养宠物\n"
            "• /宠物 状态 - 查看宠物状态\n"
            "• /宠物 喂食 [道具名] - 喂食\n"
            "• /宠物 清洁 - 清洁\n"
            "• /宠物 玩耍 - 玩耍\n"
            "• /宠物 睡觉 - 让宠物睡觉\n"
            "• /宠物 起床 - 唤醒宠物\n"
            "• /宠物 召回 - 召回旅行中的宠物"
        ),
        "advanced": (
            "🎯 **进阶命令**\n"
            "• /宠物 训练 - 训练\n"
            "• /宠物 探索 - 探索冒险\n"
            "• /宠物 治疗 [药品名] - 治疗宠物\n"
            "• /宠物 改名 <新名字> - 改名"
        ),
        "items": (
            "📦 **道具与装扮**\n"
            "• /宠物 背包 - 查看背包\n"
            "• /宠物 商店 - 查看商店\n"
            "• /宠物 购买 <道具> [数量] - 购买道具\n"
            "• /宠物 使用 <道具> - 使用道具\n"
            "• /宠物 装扮 [查看/商店/购买/穿戴/卸下]"
        ),
        "social": (
            "🤝 **社交互动**\n"
            "• /宠物 互访 @QQ号 - 互访\n"
            "• /宠物 送礼 @QQ号 <道具> [数量]\n"
            "• /宠物 查看 @QQ号 - 看他人宠物\n"
            "• /宠物 摸摸 @QQ号 - 点赞/摸摸\n"
            "• /宠物 留言 [@QQ号 <内容>] - 留言板\n"
            "• /宠物 排行 [类型] - 排行榜\n"
            "• /宠物 交易 [列表/挂单/购买/撤单]"
        ),
        "gameplay": (
            "🎮 **更多玩法**\n"
            "• /宠物 游戏 [猜拳/骰子/赛跑]\n"
            "• /宠物 任务 [领取] - 每日任务\n"
            "• /宠物 称号 - 查看我的称号\n"
            "• /宠物 活动 - 查看群活动\n"
            "• /宠物 展示 [投票] - 宠物展示会"
        ),
        "management": (
            "⚙️ **管理命令** (需管理员)\n"
            "• /宠物 管理 开启/关闭\n"
            "• /宠物 管理 配置 [查看/设置]\n"
            "• /宠物 管理 重置 @QQ号\n"
            "• /宠物 管理 删除 @QQ号\n"
            "• /宠物 管理 封禁/解封 @QQ号\n"
            "• /宠物 管理 [日志/统计/导出]\n"
            "• /宠物 管理 公告 展示会"
        )
    }

    # 统一映射到英文键名
    aliases = {
        # 基础
        "basic": "basic", "base": "basic", "基础": "basic",
        # 进阶
        "advanced": "advanced", "adv": "advanced", "进阶": "advanced",
        # 道具
        "item": "items", "items": "items", "shop": "items", "dress": "items", "道具": "items",
        # 社交
        "social": "social", "visit": "social", "社交": "social",
        # 玩法
        "game": "gameplay", "play": "gameplay", "task": "gameplay", "玩法": "gameplay", "gameplay": "gameplay",
        # 管理
        "admin": "management", "manage": "management", "management": "management", "管理": "management"
    }

    key = category.strip().lower()
    
    # 尝试映射别名
    target_key = aliases.get(key, key)

    if target_key in categories:
        # 这里的 key 可能还是原始输入（如果没有在 aliases 中找到），
        # 但我们用 target_key 来取内容。
        # 为了标题显示友好，我们可以做一个反向映射或者直接用中文标题
        titles = {
            "basic": "基础", "advanced": "进阶", "items": "道具",
            "social": "社交", "gameplay": "玩法", "management": "管理"
        }
        title_chn = titles.get(target_key, target_key)
        return f"🐾 **宠物系统帮助 - {title_chn}篇**\n\n{categories[target_key]}"

    # 默认返回目录
    menu = "🐾 **宠物系统帮助菜单**\n\n请使用 `/宠物 帮助 <类别>` 查看详细指令\n\n"
    menu += "📌 **可用类别**\n"
    # 这里我们手动列出中文名称，保证顺序和可读性
    display_names = ["基础", "进阶", "道具", "社交", "玩法", "管理"]
    for name in display_names:
        menu += f"• {name}\n"
    
    return menu