"""生成青宠状态卡、提醒、排行榜和帮助文本。"""

from ..models import Pet, User
from .constants import DEFAULT_DRESS_ITEMS, MAX_STAT_VALUE, PetStage, PetStatus
from .time import utc_now

_STAGE_EMOJI = {
    PetStage.EGG: "🥚",
    PetStage.YOUNG: "🐣",
    PetStage.GROWTH: "🐥",
    PetStage.MATURE: "🐔",
    PetStage.OLD: "🐦",
}

_STATUS_EMOJI = {
    PetStatus.NORMAL: "✅",
    PetStatus.SICK: "🤒",
    PetStatus.SLEEPING: "😴",
    PetStatus.TRAVELING: "✈️",
    PetStatus.DEAD: "💀",
}

_DRESS_SLOT_EMOJI = {
    "帽子": "🎩",
    "衣服": "👕",
    "饰品": "🎀",
    "背景": "🖼️",
}

_RANKING_FORMAT = {
    "care_score": ("🏆 照顾评分排行", "%"),
    "intimacy": ("💕 亲密度排行", ""),
    "experience": ("🎯 经验值排行", ""),
    "coins": ("💰 金币排行", "💰"),
}


def _progress_bar(value: int, max_val: int = MAX_STAT_VALUE, length: int = 10) -> str:
    """把属性值转换为固定宽度进度条，并限制异常值造成的绘制溢出。"""
    safe_length = max(0, length)
    filled = int(value / max_val * safe_length) if max_val > 0 else 0
    filled = min(safe_length, max(0, filled))
    bar = "█" * filled + "░" * (safe_length - filled)
    return f"[{bar}] {value}/{max_val}"


def format_pet_card(pet: Pet, user: User | None = None) -> str:
    """生成包含宠物属性、装扮和可选用户资产的状态卡。"""
    lines = [
        f"🐾 **{pet.name}** {_STAGE_EMOJI.get(pet.stage, '🐾')}",
        "═" * 20,
        f"• 阶段: {pet.stage.value} ({pet.form})",
        f"• 性格: {pet.personality.value}",
        f"• 状态: {_STATUS_EMOJI.get(pet.status, '❓')} {pet.status.value}",
    ]

    if pet.status == PetStatus.TRAVELING and pet.status_expire_time:
        remaining_seconds = (pet.status_expire_time - utc_now()).total_seconds()
        if remaining_seconds > 0:
            hours, remainder = divmod(int(remaining_seconds), 3600)
            minutes = remainder // 60
            lines.append(f"• 旅行剩余: {hours}小时{minutes}分钟")
        else:
            lines.append("• 旅行剩余: 即将返回")

    lines.extend(
        [
            f"• 年龄: {pet.age}天",
            f"• 亲密度: {pet.intimacy}",
            f"• 经验值: {pet.experience}",
            f"• 点赞: {pet.likes}",
            "",
            "📊 **属性**",
            f"  饥饿: {_progress_bar(pet.hunger)}",
            f"  心情: {_progress_bar(pet.mood)}",
            f"  清洁: {_progress_bar(pet.clean)}",
            f"  精力: {_progress_bar(pet.energy)}",
            f"  健康: {_progress_bar(pet.health)}",
            f"  照顾评分: {round(pet.care_score * 100, 1)}%",
        ]
    )

    equipped = [
        (slot_name, item_id)
        for slot_name, item_id in pet.get_dress_slots().items()
        if item_id in DEFAULT_DRESS_ITEMS
    ]
    if equipped:
        lines.extend(["", "👗 **装扮**"])
        for slot_name, item_id in equipped:
            slot_emoji = _DRESS_SLOT_EMOJI.get(slot_name, "🔸")
            lines.append(f"  {slot_emoji} {slot_name}: {DEFAULT_DRESS_ITEMS[item_id]['name']}")
        bonus = pet.get_dress_mood_bonus()
        if bonus > 0:
            lines.append(f"  ✨ 心情加成: +{bonus}")

    if user is not None:
        lines.extend(
            [
                "",
                "💰 **用户信息**",
                f"  🪙 金币: {user.coins}",
                f"  ❤️ 友情点: {user.friendship_points}",
            ]
        )
        if user.titles:
            title_line = f"  🏷️ 称号: {'、'.join(user.titles[:3])}"
            if len(user.titles) > 3:
                title_line += f" 等{len(user.titles)}个"
            lines.append(title_line)
        if user.is_trustee_active():
            lines.append("  🛡️ 托管中（衰减减半）")

    return "\n".join(lines) + "\n"


def format_status_text(pet: Pet) -> str:
    """根据宠物的低属性和特殊状态生成照料提醒。"""
    alerts: list[str] = []
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

    return "\n".join(alerts) if alerts else "✨ 宠物状态良好"


def format_ranking_list(ranking: list[tuple[str, str, float]], ranking_type: str) -> str:
    """生成指定指标的排行榜文本。"""
    title, unit = _RANKING_FORMAT.get(ranking_type, ("排行榜", ""))
    header = f"{title}\n{'═' * 20}\n\n"
    if not ranking:
        return header + "暂无数据"

    rows: list[str] = []
    medal_emoji = ("🥇", "🥈", "🥉")
    for index, (user_id, pet_name, value) in enumerate(ranking):
        position = medal_emoji[index] if index < len(medal_emoji) else f"#{index + 1}"
        display_value = f"{value}{unit}" if unit else str(value)
        rows.append(f"{position} {pet_name} ({user_id}) - {display_value}")
    return header + "\n".join(rows) + "\n"
