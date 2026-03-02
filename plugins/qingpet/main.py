"""
QQ群宠物养成系统主入口

修复清单:
- Issue #1: 管理命令需权限校验
- Issue #2: scheduled_decay 使用 get_all_pets() 替代 group_id=0
- Issue #5: 架构建议#5 — services 在 init() 中单例化
- Issue #13: daily_reset 使用批量 SQL
- 新增反脚本/群级频率限制中间件
- CR Fix #1: 群级频率限制 bug
- CR Fix #2: scheduled_decay PetService 提到循环外
- CR Fix #3: handle 返回值统一为 str
- CR Fix #8: 年龄递增（定时任务中每日+1）
- CR Fix #12: 命令记录移到限流检查之后
- CR New: 装扮/交易/展示会/数据导出 命令路由
- Refactor: 引入 CommandRouter 优化命令解析与帮助系统
"""

import logging
import math
import re
import asyncio
from typing import Any, Optional

from core.plugin_base import segments

from .services.database import Database
from .services.pet_service import PetService
from .services.user_service import UserService
from .services.item_service import ItemService
from .services.social_service import SocialService
from .services.economy_service import EconomyService
from .services.admin_service import AdminService

from .commands import (
    handle_adopt, handle_status, handle_feed, handle_clean,
    handle_play, handle_sleep, handle_wake,
    handle_train, handle_explore, handle_treat,
    handle_backpack, handle_shop, handle_buy, handle_use,
    handle_gift, handle_visit, handle_ranking,
    handle_activity, handle_task, handle_rename,
    handle_view_pet, handle_like, handle_message,
    handle_title, handle_minigame,
    handle_dress, handle_trade, handle_show, handle_recall,
    handle_manage_enable, handle_manage_disable, handle_manage_config,
    handle_manage_reset, handle_manage_ban, handle_manage_unban,
    handle_manage_log, handle_manage_stats,
    handle_manage_delete, handle_manage_export, handle_manage_announce,
)

from .utils.formatters import format_help_text
from .utils.constants import ANTI_SPAM_CONFIG, GROUP_RATE_LIMIT
from .utils.router import CommandRouter

# ──────────────────── 全局单例（架构建议 #5）────────────────────

_db_instance: Optional[Database] = None

_pet_service: Optional[PetService] = None
_user_service: Optional[UserService] = None
_item_service: Optional[ItemService] = None
_social_service: Optional[SocialService] = None
_economy_service: Optional[EconomyService] = None
_admin_service: Optional[AdminService] = None

_router: Optional[CommandRouter] = None

def _get_logger(context):
    if context and hasattr(context, "logger"):
        return context.logger
    return logging.getLogger(__name__)


class _SimpleParser:
    """简易指令解析器"""
    def __init__(self, raw: str):
        parts = raw.strip().split(maxsplit=1)
        self.first: str = parts[0] if parts else ""
        self._rest: str = parts[1] if len(parts) > 1 else ""

    def rest(self, offset: int = 1) -> str:
        if offset <= 1:
            return self._rest
        parts = self._rest.split(maxsplit=offset - 1)
        return parts[-1] if parts else ""


def parse(raw: str) -> Optional[_SimpleParser]:
    if not raw or not raw.strip():
        return None
    return _SimpleParser(raw.strip())


def _extract_first_at_qq(event: dict[str, Any]) -> str:
    message = event.get("message")
    if not isinstance(message, list):
        return ""

    for segment in message:
        if not isinstance(segment, dict):
            continue
        if segment.get("type") != "at":
            continue
        qq = segment.get("data", {}).get("qq")
        if qq is None:
            continue
        qq_text = str(qq).strip()
        if re.match(r"^\d+$", qq_text):
            return qq_text

    return ""


def _extract_text_after_first_at(event: dict[str, Any]) -> str:
    message = event.get("message")
    if not isinstance(message, list):
        return ""

    at_found = False
    text_parts: list[str] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        seg_type = segment.get("type")
        if seg_type == "at":
            at_found = True
            continue
        if at_found and seg_type == "text":
            part = str(segment.get("data", {}).get("text", ""))
            if part:
                text_parts.append(part)

    return "".join(text_parts).strip()


def _has_leading_qq_target(text: str) -> bool:
    return bool(re.match(r"^@?\d+", text.strip()))


# ──────────────────── Init / Cleanup ────────────────────

async def init(context) -> None:
    global _db_instance
    global _pet_service, _user_service, _item_service
    global _social_service, _economy_service, _admin_service

    log = _get_logger(context)

    try:
        data_dir = context.data_dir if hasattr(context, "data_dir") else "data"
        import os
        db_path = os.path.join(data_dir, "qingpet", "qingpet.db")

        _db_instance = await asyncio.to_thread(Database, db_path)

        # 单例化服务（架构建议 #5）
        _pet_service = PetService(_db_instance)
        _user_service = UserService(_db_instance)
        _item_service = ItemService(_db_instance)
        _social_service = SocialService(_db_instance)
        _economy_service = EconomyService(_db_instance)
        _admin_service = AdminService(_db_instance)

        log.info("Qingpet plugin initialized successfully")
    except Exception as e:
        log.exception(f"Failed to initialize Qingpet plugin: {e}")


async def cleanup(context) -> None:
    global _db_instance
    log = _get_logger(context)

    if _db_instance:
        await asyncio.to_thread(_db_instance.cleanup)
        _db_instance = None
    log.info("Qingpet plugin cleaned up")


# ──────────────────── 反脚本 & 群级限流中间件 ────────────────────

# CR Fix: 反脚本指数衰减实现（之前 exponential_decay_base 未被使用）
def _get_spam_decay_factor(user_id: str, group_id: int) -> float:
    """
    计算指数衰减因子：当用户在时间窗口内操作次数超标时，
    收益乘以 base^(excess_count) 使反复刷屏收益递减。
    """
    if _db_instance is None:
        return 1.0

    recent_count = _db_instance.get_recent_command_count(
        user_id, group_id, int(ANTI_SPAM_CONFIG["window_seconds"])
    )
    max_commands = ANTI_SPAM_CONFIG["max_commands"]
    if recent_count <= max_commands:
        return 1.0

    excess = recent_count - max_commands
    base = ANTI_SPAM_CONFIG["exponential_decay_base"]
    return math.pow(base, excess)


def _check_anti_spam(user_id: str, group_id: int) -> Optional[str]:
    """检查用户指令频率（Issue #50: 反脚本 / 指数衰减）"""
    if _db_instance is None:
        return None

    recent_count = _db_instance.get_recent_command_count(
        user_id, group_id, int(ANTI_SPAM_CONFIG["window_seconds"])
    )

    if recent_count >= ANTI_SPAM_CONFIG["max_commands"]:
        return "⚠️ 操作过于频繁，请稍后再试"

    return None


def _check_group_rate_limit(group_id: int) -> Optional[str]:
    """
    群级响应频率限制（Issue #52）。
    CR Fix #1: 原来两种情况都返回 None，等于完全没有限制。
    现在超限时返回空字符串 "" 作为静默丢弃标记。
    """
    if _db_instance is None:
        return None

    recent_count = _db_instance.get_group_recent_command_count(
        group_id, GROUP_RATE_LIMIT["window_seconds"]
    )

    if recent_count >= GROUP_RATE_LIMIT["max_responses"]:
        return ""  # 静默丢弃：返回空串让 handle() 提前返回但不发消息
    return None


def _record_command(user_id: str, group_id: int):
    """记录命令时间戳"""
    if _db_instance:
        _db_instance.record_command_timestamp(user_id, group_id)


def _extract_message(result: Any) -> str:
    """
    CR Fix #3: 统一子命令返回值。
    子命令可能返回 Tuple[bool, str] 或 str，这里统一提取为 str。
    """
    if isinstance(result, tuple):
        items = list(result)
        if len(items) > 1:
            return str(items[1])
        if len(items) == 1:
            return str(items[0])
        return ""
    return str(result) if result is not None else ""


def _normalize_plugin_output(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return segments(str(result))


# ──────────────────── Router & Handlers ────────────────────

def _get_router() -> CommandRouter:
    global _router
    if _router:
        return _router

    router = CommandRouter()

    # 包装器：统一各 Handler 的参数调用
    
    def _wrap_std(handler):
        """标准包装器: (user_id, group_id, args, db)"""
        async def wrapper(user_id, group_id, args, db, **kwargs):
            return _extract_message(await handler(user_id, group_id, args, db))
        return wrapper

    def _wrap_spam(handler):
        """需要 Decay 的包装器: (user_id, group_id, args, db, spam_decay_factor)"""
        async def wrapper(user_id, group_id, args, db, **kwargs):
            spam = kwargs.get('spam_decay', 1.0)
            return _extract_message(await handler(user_id, group_id, args, db, spam_decay_factor=spam))
        return wrapper

    def _wrap_help(fixed_category=""):
        """帮助菜单包装器"""
        async def wrapper(user_id, group_id, args, db, **kwargs):
            log = _get_logger(None)
            log.info(f"[DEBUG] _wrap_help called. fixed_category='{fixed_category}', args='{args}', user_id={user_id}")
            
            # 如果注册时指定了固定类别（如 /pet basic -> 基础），优先使用
            target = fixed_category
            
            # 如果没有固定类别（如 /pet help），则尝试从用户参数中获取（/pet help 基础）
            if not target and args:
                target = args.strip()
            
            result = format_help_text(target)
            log.info(f"[DEBUG] format_help_text result (first 50 chars): {result[:50]}...")
            return result
        return wrapper

    def _wrap_admin():
        """管理命令包装器"""
        async def wrapper(user_id, group_id, args, db, **kwargs):
            context = kwargs.get('context')
            result = await _handle_admin_command(args, user_id, group_id, context)
            return _extract_message(result)
        return wrapper

    # ── 命令注册 ──

    # 基础
    router.register("adopt", _wrap_std(handle_adopt), ["领养", "adoption"])
    router.register("status", _wrap_std(handle_status), ["状态", "info", "stat"])
    router.register("feed", _wrap_spam(handle_feed), ["喂食", "eat"])
    router.register("clean", _wrap_spam(handle_clean), ["清洁", "bath"])
    router.register("play", _wrap_spam(handle_play), ["玩耍"])
    router.register("sleep", _wrap_std(handle_sleep), ["睡觉"])
    router.register("wake", _wrap_std(handle_wake), ["起床"])
    
    # 进阶
    router.register("train", _wrap_spam(handle_train), ["训练"])
    router.register("explore", _wrap_spam(handle_explore), ["探索", "adventure"])
    router.register("treat", _wrap_std(handle_treat), ["治疗", "heal", "cure"])
    router.register("rename", _wrap_std(handle_rename), ["改名"])
    router.register("recall", _wrap_std(handle_recall), ["召回"])

    # 道具/商店
    router.register("backpack", _wrap_std(handle_backpack), ["背包", "bag", "inventory"])
    router.register("shop", _wrap_std(handle_shop), ["商店", "store"])
    router.register("buy", _wrap_std(handle_buy), ["购买"])
    router.register("use", _wrap_std(handle_use), ["使用"])
    router.register("gift", _wrap_std(handle_gift), ["送礼"])
    router.register("dress", _wrap_std(handle_dress), ["装扮", "outfit"])

    # 社交/互动
    router.register("visit", _wrap_spam(handle_visit), ["互访"])
    router.register("view", _wrap_std(handle_view_pet), ["查看"])
    router.register("like", _wrap_std(handle_like), ["摸摸", "点赞", "pat", "like"])
    router.register("message", _wrap_std(handle_message), ["留言", "msg"])
    router.register("ranking", _wrap_std(handle_ranking), ["排行", "rank", "top"])
    router.register("trade", _wrap_std(handle_trade), ["交易", "market"])
    router.register("show", _wrap_std(handle_show), ["展示", "展示会"])

    # 玩法
    router.register("game", _wrap_spam(handle_minigame), ["游戏", "play_game"])
    router.register("task", _wrap_std(handle_task), ["任务", "daily"])
    router.register("title", _wrap_std(handle_title), ["称号", "titles"])
    router.register("activity", _wrap_std(handle_activity), ["活动"])

    # 管理
    router.register("admin", _wrap_admin(), ["管理", "manage"])

    # 帮助类目（作为命令注册，解决 /pet 基础 等无法响应的问题）
    router.register("help", _wrap_help(""), ["帮助", "h", "?"])
    router.register("basic", _wrap_help("basic"), ["基础", "base"])
    router.register("advanced", _wrap_help("advanced"), ["进阶", "adv"])
    router.register("item", _wrap_help("items"), ["道具", "items"])
    router.register("social", _wrap_help("social"), ["社交"])
    router.register("gameplay", _wrap_help("gameplay"), ["玩法"])
    router.register("management", _wrap_help("management"), ["management"])

    _router = router
    return router


# ──────────────────── 主入口 ────────────────────

async def handle(command: str, args: str, event: dict[str, Any], context, **kwargs) -> list[dict[str, Any]]:
    """
    主命令入口。接收原始参数并路由到子命令。
    CR Fix #3: 统一返回字符串。
    CR Review: 应用反脚本衰减因子到金币产出。
    """
    log = _get_logger(context)

    user_id = event.get("user_id", "")
    group_id = event.get("group_id", 0)

    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        group_id = 0

    user_id = str(user_id)

    if _db_instance is None:
        return segments("宠物系统尚未初始化，请联系管理员")
    db = _db_instance

    async def _execute() -> Any:
        # ── 群级频率限制（CR Fix #1: 现在真正生效）──
        rate_limit_msg = _check_group_rate_limit(group_id)
        if rate_limit_msg is not None:
            # CR Fix #12: 被限流时不记录命令
            return rate_limit_msg if rate_limit_msg else None  # 空串 → 静默丢弃

        # ── 反脚本/反刷屏 ──
        spam_msg = _check_anti_spam(user_id, group_id)
        if spam_msg:
            return spam_msg

        # ── 记录操作频率（CR Fix #12: 移到限流检查之后）──
        _record_command(user_id, group_id)

        # ── 群级开关检查 ──
        group_config = db.get_group_config(group_id)

        # ── 解析 ──
        parsed = parse(args)
        if not parsed:
            return format_help_text()

        action = parsed.first.lower()
        rest_args = parsed._rest

        at_qq = _extract_first_at_qq(event)
        if at_qq:
            if action in ["查看", "view", "互访", "visit", "摸摸", "like"] and not _has_leading_qq_target(rest_args):
                rest_args = at_qq

            if action in ["送礼", "gift", "留言", "message"] and not _has_leading_qq_target(rest_args):
                tail_text = _extract_text_after_first_at(event)
                payload = tail_text if tail_text else rest_args.strip()
                rest_args = f"{at_qq} {payload}".strip()

            if action in ["游戏", "game"]:
                game_text = rest_args.strip().lower()
                has_race = game_text.startswith("赛跑") or game_text.startswith("race")
                if has_race and not _has_leading_qq_target(game_text.replace("赛跑", "", 1).replace("race", "", 1).strip()):
                    rest_args = f"{rest_args.strip()} {at_qq}".strip()

        # ── 兼容性处理：如果第一个参数是命令名本身 (qingpet, pet, 宠物) ──
        # 某些框架可能会把命令别名作为第一个参数传入
        if action in ["qingpet", "pet", "宠物"]:
            if not rest_args.strip():
                return format_help_text()

            # 重新解析剩余参数作为新的命令
            reparsed = parse(rest_args)
            if reparsed:
                action = reparsed.first.lower()
                rest_args = reparsed._rest
            else:
                return format_help_text()

        # ── 路由分发 ──
        router = _get_router()
        handler = router.get_handler(action)

        if not handler:
            return f"未知命令: {action}\n使用 /宠物 帮助 查看所有命令"

        # ── 权限/状态检查 ──

        # 管理命令始终可用
        if action in ["管理", "admin", "manage"]:
            pass  # 管理命令内部做权限检查
        else:
            # 普通命令检查群开关
            if not group_config.enabled:
                return "🚫 宠物系统在本群尚未启用\n管理员可使用: /宠物 管理 开启"

            # 普通命令检查封禁
            user = db.get_user(user_id, group_id)
            if user and user.is_banned_active():
                return "⛔ 你已被封禁，无法使用宠物系统"

        # ── 执行命令 ──

        # CR Review Issue #2: 计算反脚本衰减因子
        spam_decay = _get_spam_decay_factor(user_id, group_id)

        # 传递必要的参数
        # Router 注册时已经包裹了适配器，这里只需传入核心参数和 kwargs
        try:
            return await router.route(
                action,
                user_id,
                group_id,
                rest_args,
                db,
                context=context,
                spam_decay=spam_decay
            )
        except Exception as e:
            log.exception(f"Error executing command '{action}': {e}")
            return f"执行命令时发生错误: {str(e)}"

    try:
        def _run_execute() -> Any:
            return asyncio.run(_execute())

        result = await asyncio.to_thread(_run_execute)
    except Exception as e:
        log.exception(f"Error in qingpet handle thread execution: {e}")
        return segments(f"执行命令时发生错误: {str(e)}")

    return _normalize_plugin_output(result)


# ──────────────────── 管理命令路由 ────────────────────

async def _handle_admin_command(args: str, user_id: str, group_id: int, context) -> Any:
    """
    管理命令路由。
    Issue #1: 添加管理员身份校验
    """
    parsed = parse(args)

    if not parsed:
        return (True, "用法: /宠物 管理 <子命令>\n"
                "可用子命令: 开启, 关闭, 配置, 重置, 删除, 封禁, 解封, 日志, 统计, 导出, 公告")

    action = parsed.first.lower()
    rest_args = parsed.rest(1)
    db = _db_instance
    if db is None:
        return (False, "宠物系统尚未初始化，请联系管理员")

    # ── 管理员权限检查（Issue #1）──
    def _in_admin_list(candidate_ids) -> bool:
        if not candidate_ids:
            return False
        uid = str(user_id)
        for admin_id in candidate_ids:
            if str(admin_id) == uid:
                return True
        return False

    is_admin = False
    if context and hasattr(context, "is_admin"):
        is_admin = context.is_admin(user_id, group_id)

    if not is_admin and context and hasattr(context, "admin_ids"):
        is_admin = _in_admin_list(getattr(context, "admin_ids", []))

    if not is_admin and context and hasattr(context, "secrets"):
        secrets = getattr(context, "secrets", {}) or {}
        is_admin = _in_admin_list(secrets.get("admin_user_ids", []))

    if not is_admin and context and hasattr(context, "check_permission"):
        is_admin = context.check_permission(user_id, "admin")

    if action in ["开启", "enable", "on"]:
        return await handle_manage_enable(user_id, group_id, rest_args, db, is_admin)

    if action in ["关闭", "disable", "off"]:
        return await handle_manage_disable(user_id, group_id, rest_args, db, is_admin)

    if action in ["配置", "config", "设置", "setting"]:
        return await handle_manage_config(user_id, group_id, rest_args, db, is_admin)

    if action in ["重置", "reset"]:
        return await handle_manage_reset(user_id, group_id, rest_args, db, is_admin)

    if action in ["删除", "delete"]:
        return await handle_manage_delete(user_id, group_id, rest_args, db, is_admin)

    if action in ["封禁", "ban"]:
        return await handle_manage_ban(user_id, group_id, rest_args, db, is_admin)

    if action in ["解封", "unban"]:
        return await handle_manage_unban(user_id, group_id, rest_args, db, is_admin)

    if action in ["日志", "log"]:
        return await handle_manage_log(user_id, group_id, rest_args, db, is_admin)

    if action in ["统计", "stats"]:
        return await handle_manage_stats(user_id, group_id, rest_args, db, is_admin)

    if action in ["导出", "export", "backup"]:
        return await handle_manage_export(user_id, group_id, rest_args, db, is_admin)

    if action in ["公告", "announce"]:
        return await handle_manage_announce(user_id, group_id, rest_args, db, is_admin)

    return (True, f"未知管理命令: {action}\n可用命令: 开启, 关闭, 配置, 重置, 删除, 封禁, 解封, 日志, 统计, 导出, 公告")


# ──────────────────── 定时任务 ────────────────────

async def scheduled_decay(context) -> list[dict[str, Any]]:
    """
    定时衰减任务（每分钟执行）。
    CR Review Issue #1: 使用全局单例 _pet_service 替代重新实例化。
    """
    log = _get_logger(context)

    if _db_instance is None or _pet_service is None:
        return []
    db = _db_instance
    pet_service = _pet_service

    def _run_job() -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        pets = db.get_all_pets()

        for pet in pets:
            group_config = db.get_group_config(pet.group_id)
            if not group_config.enabled:
                continue

            decay_multiplier = group_config.decay_multiplier
            alert_msg = pet_service.apply_decay(pet, decay_multiplier)
            if alert_msg:
                messages.append({
                    "group_id": pet.group_id,
                    "message": alert_msg
                })

        log.info(f"Decay applied to {len(pets)} pets")
        db.cleanup_old_timestamps()
        return messages

    try:
        return await asyncio.to_thread(_run_job)
    except Exception as e:
        log.exception(f"Error in scheduled decay: {e}")
        return []


async def scheduled_daily_reset(context) -> list[dict[str, Any]]:
    """
    每日重置（每天00:00）。
    CR Fix #8: 宠物年龄每日递增。
    CR Review Issue #1: 使用全局单例 _user_service。
    """
    log = _get_logger(context)

    if _db_instance is None or _user_service is None:
        return []
    db = _db_instance
    user_service = _user_service

    def _run_job() -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        count = user_service.reset_daily_all()
        log.info(f"Daily reset completed for {count} users")

        db.increment_all_pet_ages()
        log.info("Pet ages incremented")

        db.cleanup_expired_titles()
        log.info("Expired titles cleaned up")
        return messages

    try:
        return await asyncio.to_thread(_run_job)
    except Exception as e:
        log.exception(f"Error in daily reset: {e}")
        return []


async def scheduled_weekly_activity(context) -> list[dict[str, Any]]:
    """
    每周活动结算。
    CR Review Issue #1/#3: 使用全局单例 _social_service，消除冗余导入和重复实例化。
    """
    log = _get_logger(context)

    if _db_instance is None or _social_service is None:
        return []
    db = _db_instance
    social_service = _social_service

    def _run_job() -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        group_ids = db.get_all_group_ids()

        for group_id in group_ids:
            group_config = db.get_group_config(group_id)
            if not group_config.activity_enabled:
                continue

            ranking = social_service.get_ranking(group_id, "care_score", 3)

            if ranking:
                text = "🎉 **本周活动结算**\n\n"
                medals = ["🥇", "🥈", "🥉"]
                reward_coins = [100, 50, 30]
                for i, (uid, name, score) in enumerate(ranking):
                    text += f"{medals[i]} {name} ({uid}) - {score}%"
                    if i < len(reward_coins):
                        text += f" +{reward_coins[i]}金币"
                    text += "\n"

                    user = db.get_user(uid, group_id)
                    if user and i < len(reward_coins):
                        user.coins += reward_coins[i]
                        db.update_user(user)

                messages.append({
                    "group_id": group_id,
                    "message": text
                })

            show_result = social_service.settle_pet_show(group_id)
            if show_result:
                messages.append({
                    "group_id": group_id,
                    "message": show_result
                })

        log.info(f"Weekly activity settled for {len(group_ids)} groups")
        return messages

    try:
        return await asyncio.to_thread(_run_job)
    except Exception as e:
        log.exception(f"Error in weekly activity: {e}")
        return []
