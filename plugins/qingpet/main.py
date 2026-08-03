"""QQ群宠物养成系统的命令与定时任务入口。

服务实例在插件初始化时统一创建，命令路由和定时任务复用同一组数据库服务。
群级限流在频率记录之前执行，反脚本衰减因子则随产出型操作向下传递，避免被
拒绝的请求污染计数或不同入口产生不一致的经济状态。
"""

import asyncio
import hashlib
import json
import logging
import math
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from core.models import PluginManifest
from core.plugin_base import run_sync, segments
from core.public_errors import public_error_message, public_error_response
from core.router import (
    CommandCatalogNode,
    CommandInvocation,
    build_command_catalog_node,
    format_command_catalog,
    get_context_command_root,
    resolve_catalog_invocation,
    resolve_context_command_invocation,
)

from .commands.admin_commands import (
    handle_manage_activity,
    handle_manage_ban,
    handle_manage_config,
    handle_manage_disable,
    handle_manage_enable,
    handle_manage_log,
    handle_manage_reset,
    handle_manage_stats,
    handle_manage_unban,
)
from .commands.advanced_commands import (
    handle_activity,
    handle_backpack,
    handle_buy,
    handle_explore,
    handle_gift,
    handle_group_task,
    handle_like,
    handle_message,
    handle_minigame,
    handle_ranking,
    handle_rename,
    handle_shop,
    handle_task,
    handle_title,
    handle_train,
    handle_treat,
    handle_use,
    handle_view_pet,
    handle_visit,
)
from .commands.basic_commands import (
    handle_adopt,
    handle_clean,
    handle_feed,
    handle_play,
    handle_sleep,
    handle_status,
    handle_wake,
)
from .commands.new_commands import (
    handle_dress,
    handle_manage_announce,
    handle_manage_delete,
    handle_recall,
    handle_show,
    handle_trade,
)
from .models import GroupConfig, GroupConfigReadError
from .services.database import Database
from .services.pet_service import PetService
from .services.social_service import SocialService
from .utils.constants import ANTI_SPAM_CONFIG, GROUP_RATE_LIMIT
from .utils.router import CommandRouter
from .utils.time import business_date, business_week

# ──────────────────── 运行期共享实例 ─────────────────────

_db_instance: Database | None = None

_pet_service: PetService | None = None
_social_service: SocialService | None = None

_router: CommandRouter | None = None

_PRIVATE_SELF_SCOPED_COMMANDS = frozenset(
    {
        "status",
        "feed",
        "clean",
        "play",
        "sleep",
        "wake",
        "train",
        "explore",
        "treat",
        "use",
        "task",
        "rename",
        "recall",
    }
)
_PRIVATE_AUTO_SCOPE_COMMANDS = frozenset(
    {
        "backpack",
        "shop",
        "buy",
        "dress",
        "gift",
        "visit",
        "view",
        "like",
        "message",
        "ranking",
        "activity",
        "trade",
        "show",
        "title",
        "game",
    }
)
_PRIVATE_ALWAYS_ALLOWED_COMMANDS = frozenset(
    {"help", "basic", "advanced", "item", "social", "gameplay", "management"}
)
_SPAM_DECAY_COMMANDS = frozenset({"feed", "clean", "play", "train", "explore"})
_MESSAGE_ID_COMMANDS = frozenset({"visit", "game"})
_SHOW_MEDALS = ("🥇", "🥈", "🥉")
_ADMIN_HANDLERS = {
    "enable": handle_manage_enable,
    "disable": handle_manage_disable,
    "config": handle_manage_config,
    "reset": handle_manage_reset,
    "delete": handle_manage_delete,
    "ban": handle_manage_ban,
    "unban": handle_manage_unban,
    "log": handle_manage_log,
    "stats": handle_manage_stats,
    "announce": handle_manage_announce,
    "activity": handle_manage_activity,
}


def _get_logger(context):
    if context and hasattr(context, "logger"):
        return context.logger
    return logging.getLogger(__name__)


def _split_command(raw: str) -> tuple[str, str] | None:
    """把原始参数拆成首个命令词和剩余参数。"""
    text = raw.strip()
    if not text:
        return None
    parts = text.split(maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else ""


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
        if re.fullmatch(r"\d+", qq_text):
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
    return bool(re.match(r"^@?\d+(?:\s|$)", text.strip()))


# ──────────────────── 初始化与清理 ────────────────────


async def init(context) -> None:
    global _db_instance, _pet_service, _social_service, _router

    log = _get_logger(context)
    database: Database | None = None

    try:
        if _db_instance is not None:
            await asyncio.to_thread(_db_instance.cleanup)

        data_dir = context.data_dir if hasattr(context, "data_dir") else "data"
        db_path = Path(data_dir) / "qingpet" / "qingpet.db"

        database = await asyncio.to_thread(Database, str(db_path))

        # 定时任务跨调用复用这两个无请求状态的服务对象。
        pet_service = PetService(database)
        social_service = SocialService(database)

        _db_instance = database
        _pet_service = pet_service
        _social_service = social_service
        _router = None

        log.info("Qingpet plugin initialized successfully")
    except Exception as exc:
        if database is not None:
            await asyncio.to_thread(database.cleanup)
        _db_instance = None
        _pet_service = None
        _social_service = None
        _router = None
        public_error_message(
            context,
            exc,
            logger=log,
            component="qingpet.init",
        )


async def cleanup(context) -> None:
    global _db_instance, _pet_service, _social_service, _router
    log = _get_logger(context)

    database = _db_instance
    _db_instance = None
    _pet_service = None
    _social_service = None
    _router = None
    if database is not None:
        await asyncio.to_thread(database.cleanup)
    log.info("Qingpet plugin cleaned up")


# ──────────────────── 反脚本 & 群级限流中间件 ────────────────────


def _get_anti_spam_state(user_id: str, group_id: int) -> tuple[str | None, float]:
    """一次读取同时计算硬限流结果和当前请求的奖励衰减因子。"""
    if _db_instance is None:
        return None, 1.0

    recent_count = _db_instance.get_recent_command_count(
        user_id, group_id, int(ANTI_SPAM_CONFIG["window_seconds"])
    )
    max_commands = int(ANTI_SPAM_CONFIG["max_commands"])
    hard_limit = max(
        max_commands + 1, int(ANTI_SPAM_CONFIG.get("hard_block_commands", max_commands * 2))
    )
    if recent_count >= hard_limit:
        return "⚠️ 操作过于频繁，请稍后再试", 0.0

    # 当前请求通过检查后会立即记入频率，因此按“已有次数 + 本次”计算收益。
    excess = max(0, recent_count + 1 - max_commands)
    base = float(ANTI_SPAM_CONFIG["exponential_decay_base"])
    return None, math.pow(base, excess)


def _is_group_rate_limited(group_id: int) -> bool:
    """判断当前群是否已达到静默响应上限。"""
    if _db_instance is None:
        return False

    recent_count = _db_instance.get_group_recent_command_count(
        group_id, GROUP_RATE_LIMIT["window_seconds"]
    )

    return recent_count >= GROUP_RATE_LIMIT["max_responses"]


def _record_command(user_id: str, group_id: int) -> None:
    """记录已通过群级和用户级限流的命令时间戳。"""
    if _db_instance is not None:
        _db_instance.record_command_timestamp(user_id, group_id)


def _private_scope_bucket(user_id: str) -> int:
    """为私聊命令生成稳定的限流桶，避免所有私聊共用 group_id=0。"""
    try:
        return -max(1, int(user_id))
    except (TypeError, ValueError):
        digest = hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:8]
        return -(int(digest, 16) or 1)


def _resolve_private_command_group(
    db: Database,
    user_id: str,
) -> tuple[int | None, str | None]:
    pets = db.get_pets_by_user(user_id)
    if not pets:
        return None, "你还没有宠物，请先在群里领养一只"

    group_ids = sorted({int(pet.group_id) for pet in pets})
    if len(group_ids) == 1:
        return group_ids[0], None

    groups = "、".join(str(group_id) for group_id in group_ids)
    return None, f"你在多个群拥有宠物，请在对应群内使用该命令（可用群号：{groups}）"


def _extract_message(result: Any) -> str:
    """
    将子命令的 ``(success, message)`` 或字符串结果统一提取为响应文本。
    """
    if isinstance(result, tuple):
        if len(result) > 1:
            return str(result[1])
        if len(result) == 1:
            return str(result[0])
        return ""
    return str(result) if result is not None else ""


def _normalize_plugin_output(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, list):
        return cast(list[dict[str, Any]], result)
    return cast(list[dict[str, Any]], segments(str(result)))


# ──────────────────── 命令路由 ────────────────────


@lru_cache(maxsize=1)
def _local_catalog_root() -> CommandCatalogNode:
    """为直接单测调用读取同一 manifest；生产请求使用 Core 已发布快照。"""

    manifest_path = Path(__file__).with_name("plugin.json")
    manifest = PluginManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    return build_command_catalog_node(
        manifest.name,
        manifest.commands[0].model_dump(),
        root=True,
    )


def _catalog_root(context: Any) -> CommandCatalogNode:
    return get_context_command_root(context, "qingpet.qingpet") or _local_catalog_root()


def _get_router(context: Any = None) -> CommandRouter:
    global _router
    if _router is not None:
        return _router

    root = _catalog_root(context)

    def _wrap_help(fixed_category: str = ""):
        """帮助内容也从目录生成；固定类别入口拒绝多余参数。"""

        def wrapper(user_id, group_id, args, db, **kwargs):
            requested = args.strip()
            if fixed_category and requested:
                return False, f"用法: /宠物 {fixed_category}"
            if not fixed_category and requested:
                help_node = root.resolve_child("help")
                parts = requested.split(maxsplit=1)
                category = help_node.resolve_child(parts[0]) if help_node is not None else None
                if category is None or len(parts) > 1:
                    return False, "未知帮助类别；请使用 /宠物 help 查看完整目录"
            return True, format_command_catalog(
                root,
                title="🐾 宠物系统完整命令目录",
            )

        return wrapper

    handlers = {
        "adopt": handle_adopt,
        "status": handle_status,
        "feed": handle_feed,
        "clean": handle_clean,
        "play": handle_play,
        "sleep": handle_sleep,
        "wake": handle_wake,
        "train": handle_train,
        "explore": handle_explore,
        "treat": handle_treat,
        "rename": handle_rename,
        "recall": handle_recall,
        "backpack": handle_backpack,
        "shop": handle_shop,
        "buy": handle_buy,
        "use": handle_use,
        "gift": handle_gift,
        "dress": handle_dress,
        "visit": handle_visit,
        "view": handle_view_pet,
        "like": handle_like,
        "message": handle_message,
        "ranking": handle_ranking,
        "trade": handle_trade,
        "show": handle_show,
        "game": handle_minigame,
        "task": handle_task,
        "group_task": handle_group_task,
        "title": handle_title,
        "activity": handle_activity,
        "admin": _handle_admin_command,
        "help": _wrap_help(),
        "basic": _wrap_help("basic"),
        "advanced": _wrap_help("advanced"),
        "item": _wrap_help("item"),
        "social": _wrap_help("social"),
        "gameplay": _wrap_help("gameplay"),
        "management": _wrap_help("management"),
    }
    _router = CommandRouter(root, handlers)
    return _router


# ──────────────────── 主入口 ────────────────────


def _parse_requested_action(
    args: str,
    context: Any,
) -> tuple[str, str, CommandInvocation] | None:
    """由 Core 目录解析子命令，并兼容重复写一次根命令别名。"""

    root = _catalog_root(context)
    invocation = resolve_context_command_invocation(context, root.code, args)
    if invocation is None:
        invocation = resolve_catalog_invocation(root, args)
    if len(invocation.chain) > 1:
        return invocation.chain[1].name, invocation.remainder_after(1), invocation

    parsed = _split_command(args)
    if parsed is None:
        return None
    action, rest_args = parsed
    if action.casefold() in {root.name.casefold(), *(alias.casefold() for alias in root.aliases)}:
        invocation = resolve_catalog_invocation(root, rest_args)
        if len(invocation.chain) > 1:
            return invocation.chain[1].name, invocation.remainder_after(1), invocation
        reparsed = _split_command(rest_args)
        if reparsed is None:
            return None
        action, rest_args = reparsed
    return action.casefold(), rest_args, invocation


def _resolve_command_group(
    db: Database,
    user_id: str,
    group_id: int,
    is_private: bool,
    canonical_action: str,
) -> tuple[int, str | None]:
    """把私聊命令映射到唯一群作用域；无法安全判定时返回用户提示。"""

    if not is_private:
        return group_id, None
    if canonical_action in _PRIVATE_AUTO_SCOPE_COMMANDS:
        resolved_group_id, error = _resolve_private_command_group(db, user_id)
        return resolved_group_id or group_id, error
    if (
        canonical_action not in _PRIVATE_SELF_SCOPED_COMMANDS
        and canonical_action not in _PRIVATE_ALWAYS_ALLOWED_COMMANDS
    ):
        return group_id, "该命令需要在群聊中使用"
    return group_id, None


def _apply_at_target(
    canonical_action: str,
    rest_args: str,
    event: dict[str, Any],
) -> str:
    """仅为支持 @ 目标的命令补齐参数，不覆盖用户显式提供的 QQ 号。"""

    at_qq = _extract_first_at_qq(event)
    if not at_qq:
        return rest_args
    if canonical_action in {"view", "visit", "like"}:
        return rest_args if _has_leading_qq_target(rest_args) else at_qq
    if canonical_action in {"gift", "message"}:
        if _has_leading_qq_target(rest_args):
            return rest_args
        tail_text = _extract_text_after_first_at(event)
        return f"{at_qq} {tail_text or rest_args.strip()}".strip()
    if canonical_action != "game":
        return rest_args
    game_text = rest_args.strip().lower()
    has_race = game_text.startswith("赛跑") or game_text.startswith("race")
    race_target = game_text.replace("赛跑", "", 1).replace("race", "", 1).strip()
    if has_race and not _has_leading_qq_target(race_target):
        return f"{rest_args.strip()} {at_qq}".strip()
    return rest_args


def _read_group_config(
    db: Database,
    group_id: int,
) -> tuple[GroupConfig | None, bool]:
    """区分未配置与配置不可读；后者必须由调用方安全停用。"""

    if group_id == 0:
        return None, False
    try:
        return db.get_group_config(group_id), False
    except GroupConfigReadError:
        return None, True


def _command_access_error(
    db: Database,
    user_id: str,
    group_id: int,
    canonical_action: str,
    group_config: GroupConfig | None,
    config_read_failed: bool,
) -> str | None:
    """集中执行配置开关与用户封禁检查。"""

    if canonical_action == "admin":
        if config_read_failed:
            return (
                "⚠️ 本群宠物配置读取失败，系统已安全停用。"
                "管理员请检查并修复 group_configs 配置行后重试。"
            )
        return None
    if config_read_failed:
        return "🚫 宠物系统配置异常，已安全停用，请联系管理员"
    if group_config is not None and not group_config.enabled:
        return "🚫 宠物系统在本群尚未启用\n管理员可使用: /宠物 管理 开启"
    user = db.get_user(user_id, group_id) if group_id != 0 else None
    if user is not None and user.is_banned_active():
        return "⛔ 你已被封禁，无法使用宠物系统"
    return None


def _build_handler_kwargs(
    canonical_action: str,
    spam_decay: float,
    event: dict[str, Any],
    context: Any,
    request_kwargs: dict[str, Any],
    invocation: CommandInvocation,
) -> dict[str, Any]:
    """只向确实消费附加字段的处理器传参，避免宽泛 kwargs 掩盖契约错误。"""

    handler_kwargs: dict[str, Any] = {}
    if canonical_action in _SPAM_DECAY_COMMANDS:
        handler_kwargs["spam_decay_factor"] = spam_decay
    if canonical_action in _MESSAGE_ID_COMMANDS:
        message_id = (
            event.get("message_id")
            or request_kwargs.get("request_id")
            or getattr(context, "request_id", None)
            or secrets.token_hex(16)
        )
        handler_kwargs["message_id"] = str(message_id)
    if canonical_action == "admin":
        handler_kwargs["context"] = context
        handler_kwargs["command_invocation"] = invocation
    return handler_kwargs


def _execute_qingpet_command(
    args: str,
    event: dict[str, Any],
    context: Any,
    request_kwargs: dict[str, Any],
    db: Database,
    user_id: str,
    group_id: int,
    is_private: bool,
) -> Any:
    """完成一次子命令的作用域解析、限流、授权与分发。"""

    parsed = _parse_requested_action(args, context)
    if parsed is None:
        return format_command_catalog(
            _catalog_root(context),
            title="🐾 宠物系统完整命令目录",
        )
    action, rest_args, invocation = parsed
    router = _get_router(context)
    canonical_action = router.resolve_command(action)
    effective_group_id, group_error = _resolve_command_group(
        db,
        user_id,
        group_id,
        is_private,
        canonical_action,
    )
    if group_error:
        return group_error

    rate_limit_group_id = (
        effective_group_id if effective_group_id != 0 else _private_scope_bucket(user_id)
    )
    if _is_group_rate_limited(rate_limit_group_id):
        # 被拒绝的请求不计入操作频率，避免限流窗口自行延长。
        return None
    spam_message, spam_decay = _get_anti_spam_state(user_id, rate_limit_group_id)
    if spam_message:
        return spam_message
    _record_command(user_id, rate_limit_group_id)

    rest_args = _apply_at_target(canonical_action, rest_args, event)
    group_config, config_read_failed = _read_group_config(db, effective_group_id)
    handler = router.get_handler(canonical_action)
    if handler is None:
        return f"未知命令: {action}\n使用 /宠物 帮助 查看所有命令"
    access_error = _command_access_error(
        db,
        user_id,
        effective_group_id,
        canonical_action,
        group_config,
        config_read_failed,
    )
    if access_error:
        return access_error

    try:
        result = handler(
            user_id,
            effective_group_id,
            rest_args,
            db,
            **_build_handler_kwargs(
                canonical_action,
                spam_decay,
                event,
                context,
                request_kwargs,
                invocation,
            ),
        )
        return _extract_message(result)
    except Exception as exc:
        return public_error_message(
            context,
            exc,
            logger=_get_logger(context),
            component="qingpet.command",
        )


async def handle(
    command: str, args: str, event: dict[str, Any], context, **kwargs
) -> list[dict[str, Any]]:
    """把根命令交给隔离线程执行，并统一规范化插件输出。"""

    log = _get_logger(context)
    raw_group_id = event.get("group_id")
    try:
        group_id = int(raw_group_id) if raw_group_id is not None else 0
    except (TypeError, ValueError):
        group_id = 0
    user_id = str(event.get("user_id", ""))
    is_private = raw_group_id in (None, "", 0, "0")
    if _db_instance is None:
        return cast(list[dict[str, Any]], segments("宠物系统尚未初始化，请联系管理员"))

    try:
        # 子处理器是同步领域代码；统一交给框架有界 worker，避免阻塞主循环，
        # 也避免每条命令在线程里创建并销毁一套事件循环。
        result = await run_sync(
            _execute_qingpet_command,
            args,
            event,
            context,
            kwargs,
            _db_instance,
            user_id,
            group_id,
            is_private,
        )
    except Exception as exc:
        return cast(
            list[dict[str, Any]],
            public_error_response(
                context,
                exc,
                logger=log,
                component="qingpet.handle",
            ),
        )
    return _normalize_plugin_output(result)


# ──────────────────── 管理命令路由 ────────────────────


def _handle_admin_command(
    user_id: str,
    group_id: int,
    args: str,
    db: Database,
    *,
    context=None,
    command_invocation: CommandInvocation | None = None,
) -> tuple[bool, str]:
    """
    管理命令路由。

    所有改变群状态的分支都必须先通过管理员身份校验。
    """
    parsed = _split_command(args)

    if parsed is None:
        return (
            True,
            "用法: /宠物 管理 <子命令>\n"
            "可用子命令: 开启, 关闭, 配置, 重置, 删除, 封禁, 解封, 日志, 统计, 公告, 活动",
        )

    action, rest_args = parsed
    if (
        command_invocation is not None
        and len(command_invocation.chain) > 2
        and command_invocation.chain[1].name == "admin"
    ):
        action = command_invocation.chain[2].name
        rest_args = command_invocation.remainder_after(2)
    else:
        admin_node = _catalog_root(context).resolve_child("admin")
        child = admin_node.resolve_child(action) if admin_node is not None else None
        action = child.name if child is not None else action.casefold()

    # 只信任 core 基于已认证事件签发的主体，不回退到插件私有密钥或测试字段。
    principal = getattr(context, "principal", None) if context is not None else None
    try:
        same_actor = principal is not None and int(principal.user_id) == int(user_id)
    except (TypeError, ValueError):
        same_actor = False
    capabilities = getattr(context, "capabilities", None) if context is not None else None
    is_global_admin = bool(getattr(capabilities, "is_bot_admin", False))
    can_manage_group = getattr(principal, "can_manage_group", None)
    is_group_manager = bool(can_manage_group(group_id)) if callable(can_manage_group) else False
    is_admin = bool(same_actor and (is_global_admin or is_group_manager))

    handler = _ADMIN_HANDLERS.get(action)
    if handler is not None:
        return handler(user_id, group_id, rest_args, db, is_admin)

    return (
        False,
        f"未知管理命令: {action}\n可用命令: 开启, 关闭, 配置, 重置, 删除, 封禁, 解封, 日志, 统计, 公告, 活动",
    )


# ──────────────────── 定时任务 ────────────────────


async def scheduled_decay(context) -> list[dict[str, Any]]:
    """
    定时衰减任务（每分钟执行）。
    复用初始化阶段的 PetService，确保定时任务与命令共享同一数据库状态。
    """
    log = _get_logger(context)

    if _db_instance is None or _pet_service is None:
        return []
    db = _db_instance
    pet_service = _pet_service

    def _run_job() -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        enabled_group_decay = db.get_enabled_group_decay_map()
        if not enabled_group_decay:
            db.cleanup_old_timestamps()
            return messages
        pets = db.get_all_pets()
        trustee_keys = db.get_active_trustee_keys()

        for pet in pets:
            decay_multiplier = enabled_group_decay.get(pet.group_id)
            if decay_multiplier is None:
                continue

            alert_msg = pet_service.apply_decay(
                pet,
                decay_multiplier,
                is_trustee_override=(pet.user_id, pet.group_id) in trustee_keys,
            )
            if alert_msg:
                messages.append({"group_id": pet.group_id, "message": alert_msg})

        log.info("Decay applied to %s pets", len(pets))
        db.cleanup_old_timestamps()
        return messages

    try:
        return await asyncio.to_thread(_run_job)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=log,
            component="qingpet.schedule.decay",
        )
        return []


async def scheduled_trade_expiry(context) -> list[dict[str, Any]]:
    """独立结算已到期交易托管；数据库 claim 使重复/并发执行保持幂等。"""
    log = _get_logger(context)
    if _db_instance is None:
        return []

    try:
        settled = await asyncio.to_thread(_db_instance.settle_expired_trade_listings)
        if settled:
            log.info("Expired trade listings settled count=%s", settled)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=log,
            component="qingpet.schedule.trade_expiry",
        )
    return []


async def scheduled_pet_show_settlement(context) -> list[dict[str, Any]]:
    """在展示会截止后结算有效票；事务 claim 保证并发调度只奖励一次。"""
    log = _get_logger(context)
    if _db_instance is None or _social_service is None:
        return []
    db = _db_instance
    social_service = _social_service

    def _run_job() -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for group_id in db.get_all_group_ids():
            result = social_service.settle_pet_show(group_id, force=False)
            if result:
                messages.append({"group_id": group_id, "message": result})
        return messages

    try:
        return await asyncio.to_thread(_run_job)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=log,
            component="qingpet.schedule.pet_show_settlement",
        )
        return []


async def scheduled_daily_reset(context) -> list[dict[str, Any]]:
    """
    每日重置（每天00:00）。
    按群原子重置每日状态、递增宠物年龄并登记调度完成状态。
    """
    log = _get_logger(context)

    if _db_instance is None:
        return []
    db = _db_instance

    def _run_job() -> None:
        period = business_date()
        reset_count = 0
        age_count = 0
        for group_id in db.get_enabled_group_ids():
            result = db.run_daily_reset_atomic(f"{period}:{group_id}", group_id)
            if result is None:
                continue
            reset_count += result.users_reset
            age_count += result.pets_aged
        log.info("Daily reset completed users=%s pets=%s", reset_count, age_count)

        db.cleanup_expired_titles()
        log.info("Expired titles cleaned up")

    try:
        await asyncio.to_thread(_run_job)
        return []
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=log,
            component="qingpet.schedule.daily_reset",
        )
        return []


async def scheduled_weekly_activity(context) -> list[dict[str, Any]]:
    """
    每周活动结算。
    数据库事务负责排行奖励和称号；展示会由独立调度任务结算。
    """
    log = _get_logger(context)

    if _db_instance is None:
        return []
    db = _db_instance

    def _run_job() -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        group_ids = db.get_enabled_group_ids(require_activity=True)
        period = business_week()
        settled_groups = 0

        for group_id in group_ids:
            period_key = f"{period}:{group_id}"
            settlement = db.settle_weekly_activity_atomic(period_key, group_id)
            if settlement is None:
                continue
            settled_groups += 1

            if settlement.winners:
                lines = ["🎉 **本周活动结算**", ""]
                for index, winner in enumerate(settlement.winners):
                    line = (
                        f"{_SHOW_MEDALS[index]} {winner.pet_name} "
                        f"({winner.user_id}) - {winner.score}%"
                    )
                    if winner.coins_granted > 0:
                        line += f" +{winner.coins_granted}金币"
                    if winner.title_granted:
                        line += " 🏅本周之星"
                    lines.append(line)

                messages.append({"group_id": group_id, "message": "\n".join(lines)})

        log.info("Weekly activity settled groups=%s", settled_groups)
        return messages

    try:
        return await asyncio.to_thread(_run_job)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=log,
            component="qingpet.schedule.weekly_activity",
        )
        return []
