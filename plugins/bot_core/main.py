"""
核心命令插件
提供 Bot 的基础管理功能
"""

import asyncio
import json
import logging
import math
import re
import time
import unicodedata
from collections.abc import Mapping
from typing import Any, NoReturn, cast

from core.args import parse_int
from core.interfaces import (
    ACTION_BYPASS_SINK_KEY,
    PluginContextProtocol,
    SecretAdminCapability,
)
from core.plugin_base import build_action, run_sync, segments
from core.public_errors import public_error_response
from core.router import CommandCatalogNode

logger = logging.getLogger(__name__)

# ============================================================
# 常量配置
# ============================================================

DEFAULT_MUTE_MINUTES      = 10  # 默认静音时长（分钟）
MAX_MUTE_MINUTES          = 1440  # 最长静音时长（分钟，24小时）
SECRET_MASK_CHAR          = "*"  # 密钥遮罩字符
METRICS_SEPARATOR         = "─" * 20  # 指标显示分隔线
MAX_DISPLAYED_SECRET_KEYS = 20
# JSON 导出沿用原分页大小；面向手机的文本目录单独使用更小页面。
HELP_PAGE_SIZE                     = 12
HELP_TEXT_PAGE_SIZE                = 8
HELP_PLUGIN_PAGE_SIZE              = 6
MAX_HELP_QUERY_LENGTH              = 128
MAX_PLUGIN_OVERVIEW_SUMMARY_LENGTH = 32
MAX_HELP_MENU_SUMMARY_LENGTH       = 32
HELP_MOBILE_LINE_WIDTH             = 34
_RELOAD_NOTIFICATION_MARKER        = "_xiaoqing_bot_core_reload_notification_registered"
_NO_ARGUMENT_USAGE                 = {
    "reload": "/reload",
    "plugins": "/plugins",
    "说话": "/说话",
    "metrics": "/metrics",
}

_SECRET_PATH_PATTERN = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")
_DURATION_PATTERN    = re.compile(
    r"(?P<value>(?:\d+(?:\.\d*)?|\.\d+))(?P<unit>h|小时|m|min|分钟)?\Z",
    flags=re.IGNORECASE,
)


# ============================================================
# 通用数据保护与指标格式化
# ============================================================


def mask_secret(value: Any) -> str:
    """遮罩敏感信息的显示

    Args:
        value: 要遮罩的值

    Returns:
        遮罩后的字符串
    """
    try:
        if isinstance(value, str):
            if len(value) < 12:
                return SECRET_MASK_CHAR * 4
            # 固定遮罩长度，避免长密钥制造巨型日志或泄露原始长度。
            return value[:2] + SECRET_MASK_CHAR * 4 + value[-2:]
        if isinstance(value, (int, float)):
            return SECRET_MASK_CHAR * 4
        if isinstance(value, list):
            return f"[<{len(value)} values>]"
        if isinstance(value, dict):
            return f"{{<{len(value)} keys>}}"
        return "[hidden]"
    except Exception as exc:
        logger.error("遮罩密钥失败 error_type=%s", type(exc).__name__)
        return "[error]"


def _secret_admin_capability(
    context: PluginContextProtocol,
) -> SecretAdminCapability | None:
    """仅为可信 Bot 管理员私聊返回全局密钥管理能力。"""

    principal    = getattr(context, "principal", None)
    capabilities = getattr(context, "capabilities", None)
    capability   = getattr(capabilities, "secret_admin", None)
    if (
        principal is None
        or not getattr(capabilities, "is_bot_admin", False)
        or not getattr(principal, "is_private", False)
        or capability is None
    ):
        return None
    # 兼容测试替身和旧插件上下文的结构探测；通过上述门禁后只按窄能力协议使用。
    return cast(SecretAdminCapability, capability)


def _reject_json_constant(constant: str) -> NoReturn:
    """拒绝 JSON 标准之外的 NaN/Infinity。"""

    raise ValueError(f"non-standard JSON constant: {constant}")


def _metric_number(
    values: Mapping[str, Any],
    key: str,
    *,
    maximum: float | None = None,
) -> float | None:
    """从指标映射读取有限非负数；损坏字段显示为未知而非零。"""

    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or (maximum is not None and number > maximum):
        return None
    return number


def _format_metric(value: float | None, spec: str, suffix: str = "") -> str:
    return "n/a" if value is None else f"{format(value, spec)}{suffix}"


# ============================================================
# 命令分发入口
# ============================================================


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    """命令处理入口

    Args:
        command: plugin.json 中定义的 command name
        args: 用户输入的参数字符串
        event: 原始事件数据
        context: 插件上下文

    Returns:
        消息段列表
    """
    try:
        logged_args = (
            "<redacted>" if command in {"set_secret", "get_secret"} else (args[:50] if args else "")
        )
        logger.info("核心命令: %s, 参数: %s", command, logged_args)

        usage = _NO_ARGUMENT_USAGE.get(command)
        if usage is not None and args.strip():
            return segments(f"❌ 该命令不接受参数\n用法: {usage}")

        if command == "help":
            return _handle_help(args, context)

        if command == "reload":
            return await _handle_reload(context)

        if command == "plugins":
            return _handle_plugins(context)

        if command == "闭嘴":
            return _handle_mute(args, event, context)

        if command == "说话":
            return _handle_unmute(event, context)

        if command == "set_secret":
            return await _handle_set_secret(args, context)

        if command == "get_secret":
            return _handle_get_secret(args, context)

        if command == "metrics":
            return await _handle_metrics(context)

        logger.warning("未知命令: %s", command)
        return segments("❌ 未知命令")

    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "bot_core.handle",
        )


# ============================================================
# 子命令处理函数
# ============================================================


def _handle_help(
    keyword: str,
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    """从 Core 的结构化快照查询、分页或导出完整命令目录。"""

    try:
        catalog = context.get_command_catalog()
        if not catalog:
            logger.warning("查询帮助信息为空")
            return segments("❌ 暂无命令")

        output_format, query, page = _parse_help_request(keyword)
        if output_format == "text" and not query:
            plugin_groups, total_pages = _plugin_overview_page(catalog, page)
            return segments(
                _format_plugin_overview(
                    plugin_groups,
                    page          = page,
                    total_pages   = total_pages,
                    total_plugins = len(_group_catalog_by_plugin(catalog)),
                    total_nodes   = len(_flatten_catalog(catalog)),
                )
            )

        if output_format == "json":
            selected = _select_catalog_nodes(catalog, query)
            if not selected:
                return _help_not_found(query)
            page_nodes, total_pages = _catalog_page(selected, page)
            return segments(_format_catalog_json(page_nodes, query, page, total_pages))

        plugin_roots = _find_plugin_roots(catalog, query)
        if plugin_roots:
            return segments(_format_plugin_menu(plugin_roots, page=page))

        exact_nodes = _find_exact_catalog_nodes(catalog, query)
        if len(exact_nodes) == 1:
            node = exact_nodes[0]
            if node.children:
                return segments(_format_branch_menu(node, page=page))
            if page != 1:
                raise ValueError("页码超出范围，共 1 页")
            return segments(_format_command_detail(node))

        selected = _select_catalog_nodes(catalog, query)
        if not selected:
            return _help_not_found(query)
        page_nodes, total_pages = _text_catalog_page(selected, page)
        return segments(
            _format_search_results(
                page_nodes,
                query       = query,
                page        = page,
                total_pages = total_pages,
                total_nodes = len(selected),
            )
        )

    except ValueError as exc:
        return segments(
            f"❌ {exc}\n"
            "用法：/help [page N]\n"
            "      /help <插件名|命令路径|命令码|关键词> [page N]\n"
            "      /help json [查询] [page N]"
        )

    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "bot_core.help",
        )


def _help_not_found(query: str) -> list[dict[str, Any]]:
    logger.info("未找到关键词 %r 相关的命令", query)
    return segments(
        f"❌ 未找到与“{query}”相关的命令\n查看插件：/help <插件名>\n搜索功能：/help search <关键词>"
    )


def _parse_help_request(raw: str) -> tuple[str, str, int]:
    """解析帮助查询；保留 `/help <关键词>` 的兼容入口。"""

    tokens        = raw.strip().split()
    output_format = "text"
    if tokens and tokens[0].casefold() in {"json", "export", "导出"}:
        output_format = "json"
        tokens.pop(0)

    page = 1
    if tokens and tokens[0].casefold() in {"page", "list", "all", "页", "全部"}:
        action = tokens.pop(0).casefold()
        # list/all 是分页目录的兼容别名；其后的单个数字表示页码，而不是搜索词。
        implicit_page = parse_int(tokens[0], minimum=1) if len(tokens) == 1 else None
        if action == "page" or implicit_page is not None:
            if tokens:
                page = implicit_page or _parse_page_number(tokens[0])
                tokens.pop(0)
        if tokens:
            raise ValueError("分页命令只接受一个页码")
        return output_format, "", page

    if tokens and tokens[0].casefold() in {"search", "find", "show", "搜索", "查找"}:
        tokens.pop(0)
        if not tokens:
            raise ValueError("请提供插件名、命令码或关键词")

    if len(tokens) >= 2 and tokens[-2].casefold() in {"page", "页"}:
        page = _parse_page_number(tokens[-1])
        del tokens[-2:]
    query = " ".join(tokens)
    if len(query) > MAX_HELP_QUERY_LENGTH:
        raise ValueError(f"查询词不能超过 {MAX_HELP_QUERY_LENGTH} 个字符")
    return output_format, query, page


def _parse_page_number(raw: str) -> int:
    page = parse_int(raw, minimum=1)
    if page is None:
        raise ValueError("页码必须是正整数")
    return page


def _flatten_catalog(catalog: tuple[CommandCatalogNode, ...]) -> tuple[CommandCatalogNode, ...]:
    return tuple(node for root in catalog for node in root.walk())


def _group_catalog_by_plugin(
    catalog: tuple[CommandCatalogNode, ...],
) -> tuple[tuple[str, tuple[CommandCatalogNode, ...]], ...]:
    """按插件聚合顶层命令；Core 始终放在功能导航最前面。"""

    grouped: dict[str, list[CommandCatalogNode]] = {}
    for root in catalog:
        grouped.setdefault(root.plugin, []).append(root)
    plugin_names = sorted(
        grouped,
        key=lambda plugin: (plugin.casefold() != "bot_core", plugin.casefold()),
    )
    return tuple((plugin, tuple(grouped[plugin])) for plugin in plugin_names)


def _plugin_overview_page(
    catalog: tuple[CommandCatalogNode, ...],
    page: int,
) -> tuple[tuple[tuple[str, tuple[CommandCatalogNode, ...]], ...], int]:
    groups      = _group_catalog_by_plugin(catalog)
    total_pages = max(1, math.ceil(len(groups) / HELP_PLUGIN_PAGE_SIZE))
    if page > total_pages:
        raise ValueError(f"页码超出范围，共 {total_pages} 页")
    start = (page - 1) * HELP_PLUGIN_PAGE_SIZE
    return groups[start : start + HELP_PLUGIN_PAGE_SIZE], total_pages


def _primary_catalog_root(
    roots: tuple[CommandCatalogNode, ...],
) -> CommandCatalogNode:
    """优先选择子命令最完整的规范入口，而不是兼容快捷入口。"""

    return max(
        enumerate(roots),
        key=lambda item: (len(item[1].walk()), -item[0]),
    )[1]


def _overview_usage(root: CommandCatalogNode) -> str:
    usage = root.usage.strip()
    if usage:
        return usage.split(maxsplit=1)[0]
    return f"/{root.name}"


def _display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def _truncate_display_text(value: str, max_width: int) -> str:
    if _display_width(value) <= max_width:
        return value
    retained: list[str] = []
    width               = 0
    budget              = max(1, max_width - 1)
    for char in value:
        char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if width + char_width > budget:
            break
        retained.append(char)
        width += char_width
    return "".join(retained).rstrip() + "…"


def _overview_summary(root: CommandCatalogNode) -> str:
    normalized = re.sub(r"\s+", " ", root.help_text)
    summary = re.split(r"[|；;]", normalized, maxsplit=1)[0].strip().rstrip("。；;")
    if not summary:
        return "查看该插件的命令目录"
    return _truncate_display_text(summary, MAX_PLUGIN_OVERVIEW_SUMMARY_LENGTH)


def _format_plugin_overview_entry(
    plugin: str,
    roots: tuple[CommandCatalogNode, ...],
) -> str:
    primary    = _primary_catalog_root(roots)
    entrypoint = _overview_usage(primary)
    if plugin == "bot_core":
        preferred  = ("/help", "/plugins", "/reload")
        available  = {_overview_usage(root) for root in roots}
        entrypoint = " · ".join(usage for usage in preferred if usage in available)
    elif len(roots) > 1:
        entrypoint += f"（另有{len(roots) - 1}个入口）"
    node_count = sum(len(root.walk()) for root in roots)
    label      = "bot_core（Core）" if plugin == "bot_core" else plugin
    return "\n".join(
        (
            f"• {label} · {node_count}个命令",
            f"  {entrypoint}",
            f"  {_overview_summary(primary)}",
        )
    )


def _format_plugin_overview(
    groups: tuple[tuple[str, tuple[CommandCatalogNode, ...]], ...],
    *,
    page: int,
    total_pages: int,
    total_plugins: int,
    total_nodes: int,
) -> str:
    lines = [
        f"🧭 XiaoQing 功能导航  {page}/{total_pages}",
        f"{total_plugins} 个插件 · {total_nodes} 个命令",
        "",
    ]
    for plugin, roots in groups:
        lines.extend((_format_plugin_overview_entry(plugin, roots), ""))

    if page > 1:
        lines.append(f"⬅️ 上一页：/help page {page - 1}")
    if page < total_pages:
        lines.append(f"➡️ 下一页：/help page {page + 1}")
    lines.extend(
        (
            "查看插件：/help pendo",
            "搜索功能：/help search <关键词>",
            "JSON 导出：/help json page 1",
        )
    )
    return "\n".join(lines).rstrip()


def _normalized_help_query(query: str) -> str:
    return query.casefold().strip().lstrip("/")


def _find_plugin_roots(
    catalog: tuple[CommandCatalogNode, ...],
    query: str,
) -> tuple[CommandCatalogNode, ...]:
    """只在完整插件名命中时返回其顶层入口。"""

    normalized = _normalized_help_query(query)
    return tuple(root for root in catalog if root.plugin.casefold() == normalized)


def _catalog_exact_terms(node: CommandCatalogNode) -> frozenset[str]:
    path  = tuple(part.casefold() for part in node.path)
    terms = {
        node.code.casefold(),
        " ".join(path),
        "/".join(path),
        node.name.casefold(),
        *(alias.casefold() for alias in node.aliases),
    }
    for alias in node.aliases:
        alias_path = (*path[:-1], alias.casefold())
        terms.add(" ".join(alias_path))
        terms.add("/".join(alias_path))
    return frozenset(terms)


def _find_exact_catalog_nodes(
    catalog: tuple[CommandCatalogNode, ...],
    query: str,
) -> tuple[CommandCatalogNode, ...]:
    normalized = _normalized_help_query(query)
    if not normalized:
        return ()
    return tuple(
        node for node in _flatten_catalog(catalog) if normalized in _catalog_exact_terms(node)
    )


def _select_catalog_nodes(
    catalog: tuple[CommandCatalogNode, ...],
    query: str,
) -> tuple[CommandCatalogNode, ...]:
    """精确查询优先；命中父节点时返回其完整子树。"""

    nodes      = _flatten_catalog(catalog)
    normalized = _normalized_help_query(query)
    if not normalized:
        return nodes

    if _find_plugin_roots(catalog, query):
        return tuple(node for node in nodes if node.plugin.casefold() == normalized)

    exact_nodes = _find_exact_catalog_nodes(catalog, query)
    if exact_nodes:
        return _deduplicate_catalog_nodes(
            [descendant for node in exact_nodes for descendant in node.walk()]
        )

    matches = []
    for node in nodes:
        searchable = "\n".join(
            (
                node.code,
                node.plugin,
                " ".join(node.path),
                node.name,
                " ".join(node.aliases),
                node.help_text,
                node.usage,
            )
        ).casefold()
        if normalized in searchable:
            matches.append(node)
    return tuple(matches)


def _deduplicate_catalog_nodes(nodes: list[CommandCatalogNode]) -> tuple[CommandCatalogNode, ...]:
    unique: dict[str, CommandCatalogNode] = {}
    for node in nodes:
        unique.setdefault(node.code, node)
    return tuple(unique.values())


def _catalog_page(
    nodes: tuple[CommandCatalogNode, ...],
    page: int,
) -> tuple[tuple[CommandCatalogNode, ...], int]:
    """保持自动化 JSON 导出的既有分页。"""

    total_pages = max(1, math.ceil(len(nodes) / HELP_PAGE_SIZE))
    if page > total_pages:
        raise ValueError(f"页码超出范围，共 {total_pages} 页")
    start = (page - 1) * HELP_PAGE_SIZE
    return nodes[start : start + HELP_PAGE_SIZE], total_pages


def _text_catalog_page(
    nodes: tuple[CommandCatalogNode, ...],
    page: int,
) -> tuple[tuple[CommandCatalogNode, ...], int]:
    total_pages = max(1, math.ceil(len(nodes) / HELP_TEXT_PAGE_SIZE))
    if page > total_pages:
        raise ValueError(f"页码超出范围，共 {total_pages} 页")
    start = (page - 1) * HELP_TEXT_PAGE_SIZE
    return nodes[start : start + HELP_TEXT_PAGE_SIZE], total_pages


def _compact_help_summary(value: str) -> str:
    summary = re.sub(r"\s+", " ", value).strip().rstrip("。；;")
    if not summary:
        return "查看命令详情"
    return _truncate_display_text(summary, MAX_HELP_MENU_SUMMARY_LENGTH)


def _wrap_help_tokens(
    value: str,
    *,
    first_prefix: str      = "",
    subsequent_prefix: str = "  ",
) -> list[str]:
    """按手机可读宽度在参数边界换行，不拆开命令 token。"""

    tokens = value.split()
    if not tokens:
        return [first_prefix.rstrip()]
    lines: list[str] = []
    current          = first_prefix
    for token in tokens:
        separator = "" if current == first_prefix else " "
        candidate = f"{current}{separator}{token}"
        if current != first_prefix and _display_width(candidate) > HELP_MOBILE_LINE_WIDTH:
            lines.append(current.rstrip())
            current = f"{subsequent_prefix}{token}"
        else:
            current = candidate
    lines.append(current.rstrip())
    return lines


def _menu_navigation_example(nodes: tuple[CommandCatalogNode, ...]) -> CommandCatalogNode | None:
    return next((node for node in nodes if node.children), nodes[0] if nodes else None)


def _help_query_for_node(node: CommandCatalogNode) -> str:
    return " ".join(node.path)


def _format_menu_entries(nodes: tuple[CommandCatalogNode, ...]) -> list[str]:
    lines: list[str] = []
    for index, node in enumerate(nodes):
        marker = "▸" if node.children else "•"
        lines.extend(
            _wrap_help_tokens(
                f"/{' '.join(node.path)}",
                first_prefix=f"{marker} ",
            )
        )
        lines.append(f"  {_compact_help_summary(node.help_text)}")
        if index + 1 < len(nodes):
            lines.append("")
    return lines


def _append_text_page_navigation(
    lines: list[str],
    *,
    query: str,
    page: int,
    total_pages: int,
) -> None:
    if page > 1:
        lines.append(f"⬅️ 上一页：/help {query} page {page - 1}")
    if page < total_pages:
        lines.append(f"➡️ 下一页：/help {query} page {page + 1}")


def _plugin_menu_nodes(
    roots: tuple[CommandCatalogNode, ...],
) -> tuple[CommandCatalogNode, ...]:
    primary             = _primary_catalog_root(roots)
    compatibility_roots = tuple(root for root in roots if root is not primary)
    if primary.children:
        return (*primary.children, *compatibility_roots)
    return roots


def _format_plugin_menu(
    roots: tuple[CommandCatalogNode, ...],
    *,
    page: int,
) -> str:
    primary    = _primary_catalog_root(roots)
    menu_nodes = _plugin_menu_nodes(roots)
    if len(roots) == 1 and not primary.children:
        if page != 1:
            raise ValueError("页码超出范围，共 1 页")
        return _format_command_detail(primary)

    page_nodes, total_pages = _text_catalog_page(menu_nodes, page)
    total_nodes = sum(len(root.walk()) for root in roots)
    lines       = [
        f"📦 {primary.plugin}  {page}/{total_pages}",
        f"{total_nodes} 个命令 · 本层 {len(menu_nodes)} 个入口",
        _overview_usage(primary),
        _overview_summary(primary),
        "",
        *_format_menu_entries(page_nodes),
        "",
    ]
    _append_text_page_navigation(
        lines,
        query       = primary.plugin,
        page        = page,
        total_pages = total_pages,
    )
    example = _menu_navigation_example(page_nodes)
    if example is not None:
        lines.append(f"继续查看：/help {_help_query_for_node(example)}")
    lines.append("返回总览：/help")
    return "\n".join(lines).rstrip()


def _format_branch_menu(node: CommandCatalogNode, *, page: int) -> str:
    page_nodes, total_pages = _text_catalog_page(node.children, page)
    query = _help_query_for_node(node)
    lines = [
        f"📂 /{query}  {page}/{total_pages}",
        f"{len(node.walk())} 个命令 · 本层 {len(node.children)} 个操作",
        _compact_help_summary(node.help_text),
        "",
        *_format_menu_entries(page_nodes),
        "",
    ]
    _append_text_page_navigation(
        lines,
        query       = query,
        page        = page,
        total_pages = total_pages,
    )
    example = _menu_navigation_example(page_nodes)
    if example is not None:
        lines.append(f"继续查看：/help {_help_query_for_node(example)}")
    parent_query = " ".join(node.path[:-1]) or node.plugin
    lines.append(f"返回上级：/help {parent_query}")
    return "\n".join(lines).rstrip()


def _permission_label(permission: str) -> str:
    return {
        "public": "公开",
        "bot_admin": "Bot 管理员",
        "group_admin": "群管理员",
    }.get(permission, permission)


def _context_label(contexts: tuple[str, ...]) -> str:
    labels = {"private": "私聊", "group": "群聊"}
    return "、".join(labels.get(value, value) for value in contexts)


def _format_command_detail(node: CommandCatalogNode) -> str:
    lines = [
        "📘 命令详情",
        "",
        "用法",
        *_wrap_help_tokens(node.usage),
        node.help_text,
    ]
    if node.aliases:
        lines.extend(("", f"别名：{'、'.join(node.aliases)}"))
    if node.examples:
        lines.extend(("", "正确示例"))
        for example in node.examples:
            lines.extend(_wrap_help_tokens(example, first_prefix="✓ "))
    if node.invalid_examples:
        lines.extend(("", "错误示例"))
        for example in node.invalid_examples:
            lines.extend(_wrap_help_tokens(example, first_prefix="✗ "))
    lines.extend(
        (
            "",
            f"适用：{_permission_label(node.permission)} · {_context_label(node.contexts)}",
            f"命令码：{node.code}",
        )
    )
    if len(node.path) > 1:
        lines.append(f"返回上级：/help {' '.join(node.path[:-1])}")
    return "\n".join(lines)


def _format_search_results(
    nodes: tuple[CommandCatalogNode, ...],
    *,
    query: str,
    page: int,
    total_pages: int,
    total_nodes: int,
) -> str:
    lines = [
        f"🔎 “{query}”  {page}/{total_pages}",
        f"{total_nodes} 条结果",
        "",
        *_format_menu_entries(nodes),
        "",
    ]
    _append_text_page_navigation(
        lines,
        query       = query,
        page        = page,
        total_pages = total_pages,
    )
    if nodes:
        lines.append(f"查看详情：/help {_help_query_for_node(nodes[0])}")
    lines.append("返回总览：/help")
    return "\n".join(lines).rstrip()


def _format_catalog_json(
    nodes: tuple[CommandCatalogNode, ...],
    query: str,
    page: int,
    total_pages: int,
) -> str:
    records = []
    for node in nodes:
        record                = node.to_dict()
        record["subcommands"] = [child.code for child in node.children]
        records.append(record)
    payload = {
        "query": query or None,
        "page": page,
        "total_pages": total_pages,
        "commands": records,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _deliver_reload_completion(
    context: PluginContextProtocol,
    *,
    succeeded: bool,
    elapsed_seconds: float,
    user_id: int | None,
    group_id: int | None,
) -> None:
    """向发起重载的管理员会话发送一次最终结果。"""

    if succeeded:
        message = f"✅ 插件重载完成\n⏱️ 耗时 {elapsed_seconds:.1f} 秒"
    else:
        message = (
            "❌ 插件重载失败或中止\n"
            f"⏱️ 耗时 {elapsed_seconds:.1f} 秒\n"
            "💡 部分插件可能仍在使用旧版本，请检查日志或重启服务。"
        )

    action = build_action(segments(message), user_id, group_id)
    if action is None:
        logger.error("插件重载结果无法投递：缺少有效会话目标")
        return

    # 完成回调继承了原事件的 ContextVar。显式绕过事件收集器，确保后台结果
    # 直接进入 OneBot 发送链，而不会落入已经结束的首条回复缓存。
    action[ACTION_BYPASS_SINK_KEY] = True
    try:
        delivered = await context.send_action(action)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("插件重载结果发送失败: error_type=%s", type(exc).__name__)
        return

    if delivered is False:
        logger.warning("插件重载结果未被 OneBot 确认")
    elif delivered is None:
        logger.warning("插件重载结果已提交，但最终投递回执未知")


def _consume_reload_delivery_task(task: asyncio.Task[None]) -> None:
    """消费短生命周期发送任务的结果，避免后台异常无人读取。"""

    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:  # 防御：普通发送异常已在协程内记录。
        logger.error("插件重载通知任务异常退出: error_type=%s", type(exc).__name__)


def _register_reload_completion_notification(
    reload_task: Any,
    *,
    context: PluginContextProtocol,
    started_at: float,
) -> bool:
    """为一个 Core 重载任务登记至多一个跨插件代完成通知。"""

    if not isinstance(reload_task, asyncio.Future):
        logger.error("插件重载任务不支持完成通知: type=%s", type(reload_task).__name__)
        return False
    if getattr(reload_task, _RELOAD_NOTIFICATION_MARKER, False):
        return True

    try:
        setattr(reload_task, _RELOAD_NOTIFICATION_MARKER, True)
    except Exception as exc:
        logger.error("插件重载任务无法登记完成通知: error_type=%s", type(exc).__name__)
        return False

    principal = context.principal
    user_id   = principal.user_id if principal.kind == "user" else None
    group_id  = principal.group_id if principal.kind == "user" else None

    def on_reload_done(done_task: asyncio.Future[Any]) -> None:
        if done_task.cancelled():
            logger.info("插件重载任务已取消，不发送完成通知")
            return
        try:
            # Core 当前返回 bool；把旧式 None 视为成功以保持窄兼容边界。
            succeeded = done_task.result() is not False
        except BaseException as exc:
            succeeded = False
            logger.error("插件重载任务异常结束: error_type=%s", type(exc).__name__)

        elapsed_seconds = max(0.0, time.monotonic() - started_at)
        delivery        = _deliver_reload_completion(
            context,
            succeeded       = succeeded,
            elapsed_seconds = elapsed_seconds,
            user_id         = user_id,
            group_id        = group_id,
        )
        try:
            delivery_task = done_task.get_loop().create_task(
                delivery,
                name="bot-core-reload-completion-delivery",
            )
        except (RuntimeError, TypeError) as exc:
            delivery.close()
            logger.error("无法创建插件重载通知任务: error_type=%s", type(exc).__name__)
            return
        delivery_task.add_done_callback(_consume_reload_delivery_task)

    reload_task.add_done_callback(on_reload_done)
    return True


async def _handle_reload(context: PluginContextProtocol) -> list[dict[str, Any]]:
    """重载配置，并在后台启动全量插件重载。

    Args:
        context: 插件上下文

    Returns:
        消息段列表
    """
    try:
        logger.info("开始重载配置和插件")
        await run_sync(context.reload_config)

        # reload_plugins() 的契约是创建并返回后台任务。这里不能等待该任务：
        # 当前命令仍占用 bot_core 的执行门，而全量重载需要先排空同一执行门；
        # 若在此 await，就会形成“处理器等重载、重载等处理器”的自锁。
        started_at  = time.monotonic()
        reload_task = context.reload_plugins()
        if reload_task is None:
            logger.warning("配置已重载，但插件后台重载未启动")
            return segments("⚠️ 配置已重载，但插件重载未启动")

        notification_ready = _register_reload_completion_notification(
            reload_task,
            context    = context,
            started_at = started_at,
        )
        if not notification_ready:
            logger.warning("配置已重载，但插件重载完成通知未登记")
            return segments("⚠️ 配置已重载，插件正在后台重载，但完成通知不可用")

        logger.info("配置已重载，插件后台重载已启动")
        return segments("✅ 配置已重载，插件正在后台重载")
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "bot_core.reload",
        )


def _handle_plugins(context: PluginContextProtocol) -> list[dict[str, Any]]:
    """列出已加载的插件

    Args:
        context: 插件上下文

    Returns:
        消息段列表
    """
    try:
        plugins = context.list_plugins()
        if not plugins:
            logger.warning("插件列表为空")
            return segments("❌ 暂无插件")

        header = f"🔌 已加载插件 ({len(plugins)}):\n"
        body   = "\n".join(f"  • {name}" for name in plugins)
        logger.info("显示插件列表: %d 个", len(plugins))
        return segments(header + body)
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "bot_core.plugins",
        )


def _handle_mute(
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    """处理闭嘴命令

    用法:
        /闭嘴         - 默认静音 10 分钟
        /闭嘴 30      - 静音 30 分钟
        /闭嘴 1h      - 静音 1 小时

    Args:
        args: 命令参数
        event: 事件对象
        context: 插件上下文

    Returns:
        消息段列表
    """
    try:
        group_id = event.get("group_id")

        # 私聊不支持静音
        if group_id is None:
            logger.info("私聊不支持静音命令")
            return segments("❌ 私聊不支持此命令")

        # 只有空参数使用默认值；畸形、非有限或非正数不能静默变成 10 分钟。
        duration_text = args.strip()
        duration: float
        if not duration_text:
            duration = DEFAULT_MUTE_MINUTES
        else:
            parsed_duration = _parse_duration(duration_text)
            if parsed_duration is None or parsed_duration <= 0:
                return segments("❌ 时长格式错误，请输入有限正数，例如 30m 或 1.5h")
            duration = parsed_duration

        # 限制最大时长
        if duration > MAX_MUTE_MINUTES:
            logger.warning("静音时长超过限制: %s > %s", duration, MAX_MUTE_MINUTES)
            return segments(f"❌ 静音时长过长，最多支持 {MAX_MUTE_MINUTES // 60} 小时")

        # 执行静音
        context.mute_group(group_id, duration)

        # 生成友好的时间显示
        if duration >= 60:
            time_str = f"{duration / 60:g} 小时"
        else:
            time_str = f"{duration:g} 分钟"

        logger.info("群 %s 设置静音: %s 分钟", group_id, duration)
        return segments(f"🤐 好的，我会安静 {time_str}")

    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "bot_core.mute",
        )


def _handle_unmute(
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    """处理说话命令

    Args:
        event: 事件对象
        context: 插件上下文

    Returns:
        消息段列表
    """
    try:
        group_id = event.get("group_id")

        if group_id is None:
            logger.info("私聊不支持说话命令")
            return segments("❌ 私聊不支持此命令")

        # 检查是否在静音中
        remaining = context.get_mute_remaining(group_id)
        if remaining <= 0:
            logger.info("群 %s 未在静音中", group_id)
            return segments("😊 我本来就没闭嘴啊~")

        # 解除静音
        context.unmute_group(group_id)
        logger.info("群 %s 解除静音，剩余 %.1f 分钟", group_id, remaining)
        return segments("😊 好的，我又可以说话啦！")

    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "bot_core.unmute",
        )


async def _handle_set_secret(
    args: str,
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    """设置 secrets 中的某个值

    用法:
        /set_secret plugins.signin.yingshijufeng.sid NEW_VALUE
        /设置密钥 plugins.signin.yingshijufeng.sid NEW_VALUE

    Args:
        args: 命令参数
        context: 插件上下文

    Returns:
        消息段列表
    """
    parts = args.strip().split(maxsplit=1)
    if len(parts) != 2:
        return segments(
            "❌ 用法: /set_secret <路径> <值>\n\n"
            "示例:\n"
            "  /set_secret plugins.signin.yingshijufeng.sid YZ123456\n"
            "  /set_secret admin_user_ids [123456,789012]\n\n"
            "💡 提示: 使用 /get_secret 查看现有配置路径"
        )

    path, value = parts

    try:
        # 验证路径格式
        if _SECRET_PATH_PATTERN.fullmatch(path) is None:
            return segments("❌ 路径格式错误，请使用 . 分隔，如: plugins.signin.sid")

        # 尝试解析为 JSON（支持设置数字、布尔值等）
        try:
            parsed_value = json.loads(value, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError):
            # 如果不是有效 JSON，就作为字符串处理
            parsed_value = value

        capability = _secret_admin_capability(context)
        if capability is None:
            logger.warning("全局密钥更新被拒绝：管理能力不可用")
            return segments("❌ 只有 Bot 全局管理员可在私聊中管理密钥")

        await run_sync(capability.set, path, parsed_value)

        masked_value = mask_secret(parsed_value)
        logger.info("已更新配置: %s = %s", path, masked_value)
        return segments(f"✅ 已更新 {path}\n新值: {masked_value}")
    except KeyError:
        # 路径不存在
        logger.warning("配置路径不存在: %s", path)
        return segments("❌ 配置路径不存在\n\n💡 提示: 使用 /get_secret 查看现有配置路径")
    except ValueError:
        # 路径类型错误
        logger.warning("配置路径类型错误: %s", path)
        return segments("❌ 配置路径或值格式错误")
    except PermissionError:
        logger.warning("全局密钥更新在提交前失去授权")
        return segments("❌ 只有 Bot 全局管理员可在私聊中管理密钥")
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "bot_core.set_secret",
        )


def _handle_get_secret(
    args: str,
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    """查看 secrets 中的某个值

    用法:
        /get_secret plugins.signin.yingshijufeng.sid
        /查看密钥 plugins.signin.yingshijufeng.sid

    Args:
        args: 命令参数
        context: 插件上下文

    Returns:
        消息段列表
    """
    path = args.strip()
    if not path:
        return segments(
            "❌ 用法: /get_secret <路径>\n\n"
            "示例:\n"
            "  /get_secret plugins.signin.yingshijufeng.sid\n"
            "  /get_secret admin_user_ids\n\n"
            "💡 提示: 使用 /get_secret plugins 查看插件配置列表"
        )

    try:
        if _SECRET_PATH_PATTERN.fullmatch(path) is None:
            return segments("❌ 路径格式错误，请使用 . 分隔，如: plugins.signin.sid")

        capability = _secret_admin_capability(context)
        if capability is None:
            logger.warning("全局密钥读取被拒绝：管理能力不可用")
            return segments("❌ 只有 Bot 全局管理员可在私聊中管理密钥")

        current = capability.get(path)

        if isinstance(current, dict):
            keys_list = [str(key) for key in current]
            if len(keys_list) > MAX_DISPLAYED_SECRET_KEYS:
                display_keys = keys_list[:MAX_DISPLAYED_SECRET_KEYS]
                suffix       = f", ... 还有 {len(keys_list) - MAX_DISPLAYED_SECRET_KEYS} 个"
            else:
                display_keys = keys_list
                suffix       = ""
            logger.info("查询配置目录: %s, %d 个键", path, len(keys_list))
            return segments(f"🔑 {path} 包含以下键:\n  {', '.join(display_keys)}{suffix}")

        if isinstance(current, list):
            logger.info("查询配置列表: %s, %d 个元素", path, len(current))
            return segments(f"🔑 {path} = {mask_secret(current)}")

        logger.info("查询配置值: %s", path)
        return segments(f"🔑 {path} = {mask_secret(current)}")
    except KeyError:
        logger.info("配置路径不存在: %s", path)
        return segments(f"❌ 路径 {path} 不存在")
    except PermissionError:
        logger.warning("全局密钥读取在返回前失去授权")
        return segments("❌ 只有 Bot 全局管理员可在私聊中管理密钥")
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "bot_core.get_secret",
        )


async def _handle_metrics(context: PluginContextProtocol) -> list[dict[str, Any]]:
    """查看运行指标

    Args:
        context: 插件上下文

    Returns:
        消息段列表
    """
    try:
        metrics = getattr(context, "metrics", None)
        if metrics is None:
            logger.warning("Metrics 未启用")
            return segments("❌ Metrics 未启用")

        summary = await metrics.get_summary()
        if not isinstance(summary, Mapping) or not summary:
            logger.warning("无法获取 Metrics 数据")
            return segments("❌ 无法获取 Metrics 数据")

        raw_global_stats = summary.get("global", {})
        global_stats     = raw_global_stats if isinstance(raw_global_stats, Mapping) else {}
        uptime_seconds   = _metric_number(summary, "uptime_seconds")
        total_calls      = _metric_number(global_stats, "total_calls")
        success_rate = _metric_number(global_stats, "success_rate", maximum=1.0)
        avg_time   = _metric_number(global_stats, "avg_time")
        slow_calls = _metric_number(global_stats, "slow_calls")
        errors     = _metric_number(global_stats, "errors")
        lines      = [
            "📈 运行指标",
            METRICS_SEPARATOR,
            f"⏱️ 运行时间: {_format_metric(uptime_seconds, '.0f', 's')}",
            f"📦 总调用: {_format_metric(total_calls, '.0f')}",
            ("✅ 成功率: n/a" if success_rate is None else f"✅ 成功率: {success_rate * 100:.1f}%"),
            f"⏳ 平均耗时: {_format_metric(avg_time, '.3f', 's')}",
            f"🐢 慢调用: {_format_metric(slow_calls, '.0f')}",
            f"❌ 错误: {_format_metric(errors, '.0f')}",
        ]

        top_slow = summary.get("top_slow_plugins", [])
        if isinstance(top_slow, list) and top_slow:
            slow_lines = []
            for item in top_slow[:5]:  # 限制显示5个
                if not isinstance(item, Mapping):
                    continue
                raw_name    = item.get("plugin")
                plugin_name = (
                    " ".join(raw_name.split())[:64]
                    if isinstance(raw_name, str) and raw_name.strip()
                    else "-"
                )
                plugin_avg_time = _metric_number(item, "avg_time")
                slow_lines.append(
                    f"  • {plugin_name}: {_format_metric(plugin_avg_time, '.3f', 's')}"
                )
            if slow_lines:
                lines.extend((METRICS_SEPARATOR, "⚠️ 最慢插件:", *slow_lines))

        logger.info("查询运行指标")
        return segments("\n".join(lines))

    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "bot_core.metrics",
        )


def _parse_duration(text: str) -> float | None:
    """解析时长字符串

    支持格式:
        10      -> 10 分钟
        30m     -> 30 分钟
        1h      -> 60 分钟
        1.5h    -> 90 分钟

    Args:
        text: 时长字符串

    Returns:
        时长（分钟）；空值、畸形值或非有限值返回 None
    """
    match = _DURATION_PATTERN.fullmatch(text.strip())
    if match is None:
        return None

    value = float(match.group("value"))
    if not math.isfinite(value):
        return None
    unit = (match.group("unit") or "").casefold()
    return value * 60 if unit in {"h", "小时"} else value
