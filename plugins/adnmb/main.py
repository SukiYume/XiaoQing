"""
A岛匿名版插件 (adnmb)

功能：
- 查看时间线
- 浏览板块列表和板块内容
- 查看串和回复
- 订阅管理（添加/删除/查看）

用法：
  /adnmb -h           查看帮助
  /adnmb -t           查看时间线
  /adnmb -f           查看板块列表
  /adnmb -m <板块名>   查看板块内容
  /adnmb -c <串号>     查看串内容
  /adnmb -r <回复号>   查看单条回复
  /adnmb -d           查看订阅
  /adnmb -a <串号>     添加订阅
  /adnmb -e <串号>     删除订阅
  /adnmb -p <页码>     指定页码（配合其他选项使用）

注意：用户登录/回复功能已禁用，仅保留浏览功能。
"""

import logging
import time
import uuid as uuidlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from core.args import FLAG_VALUE, ParsedArgs, parse, parse_int
from core.plugin_base import (
    PluginContextProtocol,
    Segments,
    ensure_dir,
    gather_bounded,
    image,
    segments,
    text,
)
from core.public_errors import public_error_response

# 使用相对导入
from .adapi import AdnmbClient, Post, Thread

logger = logging.getLogger(__name__)

MAX_CACHED_CLIENTS      = 128
CLIENT_IDLE_TTL_SECONDS = 60 * 60


@dataclass
class _ClientEntry:
    client: AdnmbClient
    last_used: float


def _get_plugin_runtime_state(
    context: PluginContextProtocol | None,
    *,
    create: bool = True,
) -> dict[str, Any]:
    """取得当前插件代际的缓存根；不把客户端状态泄漏到全局模块。"""

    state = getattr(context, "state", None)
    if isinstance(state, dict):
        runtime_state = state.get("adnmb_runtime")
        if isinstance(runtime_state, dict):
            return cast(dict[str, Any], runtime_state)
        if create:
            created: dict[str, Any] = {}
            state["adnmb_runtime"]  = created
            return created
    return {}


def _close_client(client: object) -> None:
    """Release plugin-owned state without closing the shared HTTP session."""
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _client_registry(runtime_state: dict[str, Any]) -> OrderedDict[str, _ClientEntry]:
    raw_registry = runtime_state.get("clients")
    if isinstance(raw_registry, OrderedDict):
        return cast(OrderedDict[str, _ClientEntry], raw_registry)

    # Discard clients created by the legacy unbounded flat-key registry.
    for key, value in tuple(runtime_state.items()):
        if isinstance(key, str) and key.startswith("client"):
            _close_client(value)
            runtime_state.pop(key, None)
    registry: OrderedDict[str, _ClientEntry] = OrderedDict()
    runtime_state["clients"]                 = registry
    return registry


def _prune_clients(registry: OrderedDict[str, _ClientEntry], now: float) -> None:
    expired = [
        key for key, entry in registry.items() if now - entry.last_used >= CLIENT_IDLE_TTL_SECONDS
    ]
    for key in expired:
        entry = registry.pop(key)
        _close_client(entry.client)

    while len(registry) > MAX_CACHED_CLIENTS:
        _key, entry = registry.popitem(last=False)
        _close_client(entry.client)


def _get_client(
    context: PluginContextProtocol,
    cache_dir: Path,
    user_id: str | None = None,
) -> AdnmbClient:
    runtime_state = _get_plugin_runtime_state(context)
    owner_key     = str(user_id or getattr(context, "current_user_id", "") or "")
    cache_key     = f"client:{owner_key}" if owner_key else "client"
    registry      = _client_registry(runtime_state)
    now           = time.monotonic()
    _prune_clients(registry, now)
    cached_entry    = registry.get(cache_key)
    plugin_cfg      = context.get_settings_snapshot().plugin_secrets("adnmb")
    configured_uuid = str(plugin_cfg.get("uuid", "") or "")
    if configured_uuid and owner_key:
        effective_uuid = str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"{configured_uuid}:{owner_key}"))
    elif configured_uuid:
        effective_uuid = configured_uuid
    elif owner_key:
        effective_uuid = str(
            uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"{cache_dir.resolve()}:{owner_key}")
        )
    else:
        effective_uuid = str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, str(cache_dir.resolve())))
    if isinstance(cached_entry, _ClientEntry) and isinstance(cached_entry.client, AdnmbClient):
        cached_client = cached_entry.client
        if (
            cached_client.session is context.http_session
            and cached_client.cache_dir == cache_dir
            and cached_client.uuid == effective_uuid
            and not getattr(cached_client, "closed", False)
        ):
            cached_entry.last_used = now
            registry.move_to_end(cache_key)
            return cached_client
        registry.pop(cache_key, None)
        _close_client(cached_client)

    client = AdnmbClient(
        context.http_session,
        cache_dir,
        uuid=effective_uuid,
    )
    registry[cache_key] = _ClientEntry(client=client, last_used=now)
    registry.move_to_end(cache_key)
    _prune_clients(registry, now)
    return client


# ============================================================
# 插件初始化
# ============================================================


def init(context: PluginContextProtocol | None = None) -> None:
    """插件初始化"""
    logger.info("ADnmb 插件已初始化")


async def shutdown(context: PluginContextProtocol | None = None) -> None:
    """Release all cached client wrappers during plugin shutdown/reload."""
    runtime_state = _get_plugin_runtime_state(context, create=False)
    registry = runtime_state.get("clients")
    if isinstance(registry, OrderedDict):
        for entry in tuple(registry.values()):
            if isinstance(entry, _ClientEntry):
                _close_client(entry.client)
        registry.clear()
    runtime_state.clear()
    if context is not None and isinstance(getattr(context, "state", None), dict):
        context.state.pop("adnmb_runtime", None)


# ============================================================
# 消息格式化
# ============================================================


async def format_posts(
    posts: list[Post],
    client: AdnmbClient,
    max_items: int        = 10,
    download_images: bool = True,
) -> Segments:
    """
    将帖子列表格式化为消息段

    参数:
        posts: 帖子列表
        client: API 客户端（用于下载图片）
        max_items: 最大显示数量
        download_images: 是否下载图片

    返回:
        消息段列表
    """
    if not posts:
        return segments("暂无内容")

    selected_posts                 = posts[:max_items]
    image_paths: list[Path | None] = [None] * len(selected_posts)

    async def download_image(index: int, post: object) -> None:
        image_path = getattr(post, "img", "")
        if not download_images or not image_path:
            return
        image_paths[index] = await client.download_image(image_path)

    await gather_bounded(
        (download_image(index, post) for index, post in enumerate(selected_posts)),
        limit=3,
    )

    result: Segments = []
    for index, post in enumerate(selected_posts):
        # 添加帖子文本
        result.append(text(post.format_text()))

        # 图片已在有界并发窗口中下载；这里仍按帖子顺序组装消息。
        img_path = image_paths[index]
        if img_path:
            result.append(image(str(img_path)))

        if index + 1 < len(selected_posts):
            result.append(text("\n\n"))

    return result


async def format_threads(
    threads: list[Thread],
    client: AdnmbClient,
    max_items: int     = 10,
    show_replies: bool = True,
) -> Segments:
    """
    将串列表格式化为消息段

    参数:
        threads: 串列表
        client: API 客户端
        max_items: 最大显示数量
        show_replies: 是否显示回复

    返回:
        消息段列表
    """
    if not threads:
        return segments("暂无内容")

    # 收集所有帖子
    all_posts = []
    for thread in threads[:max_items]:
        all_posts.append(thread.main_post)
        if show_replies:
            all_posts.extend(thread.replies[:3])  # 每串最多显示 3 条回复

    return await format_posts(all_posts, client, max_items=len(all_posts))


# ============================================================
# 命令处理
# ============================================================


_VALUE_OPTION_SPECS = (
    (("m", "showforum"), "m", "请指定板块名称，如: /adnmb -m 综合版1"),
    (("c", "chuan"), "c", "请指定串号，如: /adnmb -c 12345678"),
    (("r", "ref"), "r", "请指定回复号，如: /adnmb -r 12345678"),
    (("a", "addfeed"), "a", "请指定要订阅的串号，如: /adnmb -a 12345678"),
    (("e", "delfeed"), "e", "请指定要取消订阅的串号，如: /adnmb -e 12345678"),
    (("p", "page"), "p", "请指定页码，如: /adnmb -t -p 2"),
)
_ACTIVE_OPTIONS = frozenset(
    {
        "t",
        "timeline",
        "f",
        "forumlist",
        "m",
        "showforum",
        "c",
        "chuan",
        "r",
        "ref",
        "d",
        "feed",
        "a",
        "addfeed",
        "e",
        "delfeed",
    }
)
_ACTIVE_OPTION_GROUPS = {
    "t": ("t", "timeline"),
    "f": ("f", "forumlist"),
    "m": ("m", "showforum"),
    "c": ("c", "chuan"),
    "r": ("r", "ref"),
    "d": ("d", "feed"),
    "a": ("a", "addfeed"),
    "e": ("e", "delfeed"),
}
_HELP_OPTIONS     = frozenset({"h", "help", "l", "list"})
_DISABLED_OPTIONS = frozenset(
    {
        "v",
        "verify",
        "i",
        "login",
        "k",
        "cookie",
        "w",
        "switchcookie",
        "y",
        "reply",
        "o",
        "logout",
    }
)
_PAGE_ACTIONS       = frozenset({"t", "m", "c", "d"})
_NUMERIC_ID_ACTIONS = frozenset({"c", "r", "a", "e"})


def _read_value_options(parsed: ParsedArgs) -> tuple[dict[str, str], str | None]:
    """读取必须带值的选项，并识别通用解析器产生的无值标记。"""
    values: dict[str, str] = {}
    for names, canonical_name, missing_message in _VALUE_OPTION_SPECS:
        present_values = [parsed.opt(name).strip() for name in names if parsed.has(name)]
        if not present_values:
            continue
        if len(present_values) > 1:
            return {}, f"同一选项不能同时使用短名称和长名称: -{names[0]}/--{names[1]}"
        # core.args 为无值选项写入 FLAG_VALUE；该值不能进入业务 API。
        value = next((item for item in present_values if item and item != FLAG_VALUE), "")
        if not value:
            return {}, missing_message
        values[canonical_name] = value
    return values, None


def _has_any_option(parsed: ParsedArgs, names: set[str] | frozenset[str]) -> bool:
    return any(parsed.has(name) for name in names)


def _selected_action(parsed: ParsedArgs) -> tuple[str | None, str | None]:
    """返回唯一操作；重复别名或并列操作都作为语法错误处理。"""

    selected: list[str] = []
    for canonical, aliases in _ACTIVE_OPTION_GROUPS.items():
        present = [alias for alias in aliases if parsed.has(alias)]
        if len(present) > 1:
            return None, f"同一操作不能同时使用 -{aliases[0]} 和 --{aliases[1]}"
        if present:
            selected.append(canonical)
    if len(selected) > 1:
        return None, "一次只能执行一种 A 岛操作"
    return (selected[0] if selected else None), None


def _request_error(parsed: ParsedArgs) -> tuple[str | None, str | None]:
    """验证通用选项组合，并阻止多余参数在业务分支中被静默忽略。"""

    known_options = _ACTIVE_OPTIONS | _HELP_OPTIONS | _DISABLED_OPTIONS | frozenset({"p", "page"})
    unknown       = sorted(set(parsed.options) - known_options)
    if unknown:
        return None, f"未知选项: --{unknown[0]}"
    if parsed.tokens:
        if len(parsed.tokens) == 1 and parsed.first.casefold() in {"help", "帮助"}:
            if parsed.options:
                return None, "帮助命令不接受其他选项"
            return "help", None
        return None, f"未知位置参数: {parsed.first}"

    action, action_error = _selected_action(parsed)
    if action_error:
        return None, action_error

    help_options = [name for name in _HELP_OPTIONS if parsed.has(name)]
    if help_options:
        if len(help_options) > 1 or len(parsed.options) > 1:
            return None, "帮助命令不接受其他选项"
        if parsed.opt(help_options[0]) != FLAG_VALUE:
            return None, "帮助选项不接受参数"
        return "help", None

    page_options = [name for name in ("p", "page") if parsed.has(name)]
    if page_options and all(parsed.opt(name) == FLAG_VALUE for name in page_options):
        return None, "请指定页码，如: /adnmb -t -p 2"

    if action in {"t", "f", "d"}:
        aliases        = _ACTIVE_OPTION_GROUPS[action]
        selected_alias = next(alias for alias in aliases if parsed.has(alias))
        if parsed.opt(selected_alias) != FLAG_VALUE:
            return None, f"-{aliases[0]}/--{aliases[1]} 不接受参数"

    has_page = _has_any_option(parsed, {"p", "page"})
    if has_page and action not in _PAGE_ACTIONS:
        return None, "-p/--page 只能配合时间线、板块、串或订阅列表使用"
    if action is None and not _has_any_option(parsed, _DISABLED_OPTIONS):
        return ("help", None) if not parsed.options else (None, "请指定一个 A 岛操作")
    return action, None


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    """命令处理入口"""
    try:
        logger.info("收到 ADnmb 命令: %s %s", command, args)
        parsed = parse(args)

        selected_action, request_error = _request_error(parsed)
        if request_error:
            return segments(f"❌ {request_error}\n使用 /adnmb -h 查看用法")

        option_values, value_error = _read_value_options(parsed)
        if value_error:
            return segments(value_error)

        page = 1
        if "p" in option_values:
            try:
                page = int(option_values["p"])
            except ValueError:
                return segments("页码必须是正整数，如: /adnmb -t -p 2")
            if page < 1:
                return segments("页码必须是正整数，如: /adnmb -t -p 2")

        # 帮助和已经禁用的用户功能不需要创建缓存目录或 API 客户端。
        if selected_action == "help":
            return segments(_get_help())

        disabled_options = (
            ({"v", "verify"}, "⚠️ 验证码功能已禁用"),
            ({"i", "login"}, "⚠️ 登录功能已禁用"),
            ({"k", "cookie"}, "⚠️ 饼干列表功能已禁用"),
            ({"w", "switchcookie"}, "⚠️ 切换饼干功能已禁用"),
            ({"y", "reply"}, "⚠️ 回复功能已禁用"),
            ({"o", "logout"}, "⚠️ 退出登录功能已禁用"),
        )
        for option_names, message in disabled_options:
            if _has_any_option(parsed, option_names):
                return segments(message)

        if selected_action in _NUMERIC_ID_ACTIONS:
            identifier = option_values[selected_action]
            if len(identifier) > 20 or parse_int(identifier, minimum=1) is None:
                return segments("编号必须是正整数")

        # 初始化缓存目录
        cache_dir = Path(context.data_dir) / "images"
        ensure_dir(cache_dir)
        logger.debug("ADnmb image cache initialized")

        client = _get_client(context, cache_dir, user_id=str(event.get("user_id", "") or ""))
        logger.debug("API 客户端已创建")

        # 时间线
        if selected_action == "t":
            logger.info("获取时间线，页码: %s", page)
            threads = await client.get_timeline(page)
            logger.info("获取到 %s 个串", len(threads))
            return await format_threads(threads, client, show_replies=False)

        # 板块列表
        if selected_action == "f":
            forum_list = await client.get_forum_list()
            lines      = ["A岛板块列表", "=" * 20]
            for name, fid in forum_list.items():
                lines.append(f"  {name} (ID: {fid})")
            return segments("\n".join(lines))

        # 板块内容
        if selected_action == "m":
            forum_name = option_values["m"]
            logger.info("获取板块: %s，页码: %s", forum_name, page)
            threads = await client.get_forum(forum_name, page)
            if not threads:
                logger.warning("板块 %s 不存在或无内容", forum_name)
                return segments("该板块不存在或暂无内容")
            logger.info("获取到 %s 个串", len(threads))
            return await format_threads(threads, client, show_replies=False)

        # 串内容
        if selected_action == "c":
            thread_id = option_values["c"]
            logger.info("获取串: %s，页码: %s", thread_id, page)
            thread = await client.get_thread(thread_id, page)
            if not thread:
                logger.warning("串 %s 不存在", thread_id)
                return segments("该串不存在")
            logger.info("获取到串，回复数: %s", len(thread.replies))
            return await format_threads([thread], client, show_replies=True)

        # 单条回复
        if selected_action == "r":
            ref_id = option_values["r"]
            post   = await client.get_ref(ref_id)
            if not post:
                return segments("该回复不存在")
            return await format_posts([post], client)

        # 查看订阅
        if selected_action == "d":
            posts = await client.get_feed(page)
            if not posts:
                return segments("暂无订阅或订阅列表为空")
            return await format_posts(posts, client)

        # 添加订阅
        if selected_action == "a":
            thread_id = option_values["a"]
            result    = await client.add_feed(thread_id)
            return segments(f"订阅结果: {result}")

        # 删除订阅
        if selected_action == "e":
            thread_id = option_values["e"]
            result    = await client.del_feed(thread_id)
            return segments(f"取消订阅结果: {result}")

        # 默认显示帮助
        return segments(_get_help())

    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="adnmb.handle")


def _get_help() -> str:
    """返回帮助信息"""
    return """A岛匿名版 (adnmb) v2.0.0
══════════════════════════

📖 浏览功能:
  -t, --timeline      查看时间线
  -f, --forumlist     查看板块列表
  -m, --showforum     查看板块内容
  -c, --chuan         查看串内容
  -r, --ref           查看单条回复

📌 订阅功能:
  -d, --feed          查看订阅列表
  -a, --addfeed       添加订阅
  -e, --delfeed       删除订阅

⚙️ 通用选项:
  -p, --page          指定页码 (默认 1)
  -h, --help          显示帮助

📝 使用示例:
  /adnmb -t           查看时间线第一页
  /adnmb -t -p 2      查看时间线第二页
  /adnmb -f           查看所有板块
  /adnmb -m 综合版1   查看综合版1
  /adnmb -c 12345678  查看指定串
  /adnmb -a 12345678  订阅指定串

⚠️ 用户功能 (登录/回复等) 已禁用"""
