"""提供 Codex 管理命令入口和受限的 arXiv sidecar 服务。"""

from __future__ import annotations

import re
from typing import Any, cast

from core.args import FLAG_VALUE, ParsedArgs, parse
from core.interfaces import PluginContextProtocol
from core.plugin_base import segments

from .arxiv_summary import enqueue_or_replay_arxiv_summary
from .manager import CodexQueueManager, get_manager, shutdown_existing_manager

HELP_TEXT = """Codex 会话队列:
/codex create <name> [cwd:<path>]  创建会话，默认目录由插件配置决定
/codex <name> <任务>              向指定会话追加任务
/codex list                       查看会话
/codex status [name]              查看状态
/codex cancel <name> [job_id]      取消运行中任务，或移除排队任务
/codex clear <name>                清空排队任务
/codex delete <name> [--force] [--protected]  删除会话并归档历史

路径建议统一输入 / 斜杠，例如 C:/workspace/project。插件会按 bot 所在系统解析。"""

_SUBCOMMANDS = {
    **dict.fromkeys(("create", "new", "创建"), "create"),
    **dict.fromkeys(("list", "ls", "列表"), "list"),
    **dict.fromkeys(("status", "状态"), "status"),
    **dict.fromkeys(("cancel", "stop", "取消", "停止"), "cancel"),
    **dict.fromkeys(("clear", "清空"), "clear"),
    **dict.fromkeys(("delete", "del", "remove", "rm", "删除"), "delete"),
}
_POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]{0,18}")
_MAX_JOB_ID          = 2**63 - 1


def _message(text: str) -> list[dict[str, Any]]:
    """收口 core 消息构造器的动态返回类型。"""

    return cast(list[dict[str, Any]], segments(text))


async def shutdown(_context: PluginContextProtocol | None = None) -> None:
    await shutdown_existing_manager()


async def enqueue_arxiv_summary(
    context: PluginContextProtocol,
    *,
    date: str,
    links: list[str],
    user_id: int | None  = None,
    group_id: int | None = None,
) -> str:
    """供核心能力调用的唯一 Codex sidecar 操作。"""
    return await enqueue_or_replay_arxiv_summary(
        context,
        date     = date,
        links    = links,
        user_id  = user_id,
        group_id = group_id,
    )


async def enqueue_arxiv_summary_service(
    date: str,
    links: list[str],
    user_id: int | None,
    group_id: int | None,
    context: PluginContextProtocol,
) -> str:
    """适配 manifest 声明的 context-last 通用服务契约。"""

    return await enqueue_arxiv_summary(
        context,
        date     = date,
        links    = links,
        user_id  = user_id,
        group_id = group_id,
    )


def _positive_id(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _event_user_group(
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> tuple[int | None, int | None]:
    """优先使用本次事件身份；畸形字段回退到 core 已校验的上下文身份。"""

    user_id = _positive_id(event.get("user_id")) or _positive_id(
        getattr(context, "current_user_id", None)
    )
    group_id = _positive_id(event.get("group_id")) or _positive_id(
        getattr(context, "current_group_id", None)
    )
    return user_id, group_id


def _shape_error(
    parsed: ParsedArgs,
    *,
    minimum_tokens: int,
    maximum_tokens: int,
    allowed_options: frozenset[str] = frozenset(),
) -> str | None:
    unknown = sorted(set(parsed.options) - allowed_options)
    if unknown:
        return f"不支持的选项: {', '.join('--' + item for item in unknown)}"
    if not minimum_tokens <= len(parsed.tokens) <= maximum_tokens:
        return "参数数量不正确。"
    return None


def _cwd_from_args(parsed: ParsedArgs) -> tuple[str | None, str | None]:
    """解析 create 的唯一 cwd 来源，拒绝互相覆盖的多种写法。"""

    error = _shape_error(
        parsed,
        minimum_tokens  = 2,
        maximum_tokens  = 3,
        allowed_options = frozenset({"cwd", "C"}),
    )
    if error:
        return None, error

    values = [parsed.opt(key) for key in ("cwd", "C") if parsed.has(key)]
    if len(parsed.tokens) == 3:
        token   = parsed.tokens[2]
        lowered = token.casefold()
        if lowered.startswith("cwd:") or lowered.startswith("cwd="):
            values.append(token[4:])
        else:
            return None, "工作目录必须使用 cwd:<绝对路径>、--cwd 或 -C。"
    if len(values) > 1:
        return None, "工作目录只能指定一次。"
    if values and (not values[0].strip() or values[0] == FLAG_VALUE):
        return None, "工作目录选项缺少路径。"
    return (values[0] if values else None), None


def _parse_job_id(value: str) -> int | None:
    if _POSITIVE_INTEGER_RE.fullmatch(value) is None:
        return None
    job_id = int(value)
    return job_id if job_id <= _MAX_JOB_ID else None


async def _handle_create(
    manager: CodexQueueManager,
    parsed: ParsedArgs,
    *,
    user_id: int | None,
    group_id: int | None,
) -> str:
    cwd, error = _cwd_from_args(parsed)
    if error:
        return f"{error}\n用法: /codex create <name> [cwd:<path>]"
    return await manager.create_session(
        parsed.second,
        cwd,
        user_id  = user_id,
        group_id = group_id,
    )


async def _handle_list(manager: CodexQueueManager, parsed: ParsedArgs) -> str:
    error = _shape_error(parsed, minimum_tokens=1, maximum_tokens=1)
    return error or await manager.list_sessions()


async def _handle_status(manager: CodexQueueManager, parsed: ParsedArgs) -> str:
    error = _shape_error(parsed, minimum_tokens=1, maximum_tokens=2)
    return error or await manager.status(parsed.second or None)


async def _handle_cancel(manager: CodexQueueManager, parsed: ParsedArgs) -> str:
    error = _shape_error(parsed, minimum_tokens=2, maximum_tokens=3)
    if error:
        return f"{error}\n用法: /codex cancel <name> [job_id]"
    job_id = None
    if len(parsed.tokens) == 3:
        job_id = _parse_job_id(parsed.get(2))
        if job_id is None:
            return "job_id 必须是 1 到 9223372036854775807 的 ASCII 十进制整数。"
    return await manager.cancel(parsed.second, job_id)


async def _handle_clear(manager: CodexQueueManager, parsed: ParsedArgs) -> str:
    error = _shape_error(parsed, minimum_tokens=2, maximum_tokens=2)
    return error or await manager.clear_queue(parsed.second)


async def _handle_delete(manager: CodexQueueManager, parsed: ParsedArgs) -> str:
    error = _shape_error(
        parsed,
        minimum_tokens  = 2,
        maximum_tokens  = 2,
        allowed_options = frozenset({"force", "protected"}),
    )
    if error:
        return f"{error}\n用法: /codex delete <name> [--force] [--protected]"
    if any(value != FLAG_VALUE for value in parsed.options.values()):
        return "--force 和 --protected 是无值标志。"
    return await manager.delete_session(
        parsed.second,
        force           = parsed.has("force"),
        allow_protected = parsed.has("protected"),
    )


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    # 未预期异常统一交给 Dispatcher 生成脱敏公开错误，插件入口不重复包装。
    del command
    raw = (args or "").strip()
    if not raw or raw.lower() in {"help", "帮助", "?"}:
        return _message(HELP_TEXT)

    parsed     = parse(raw)
    subcommand = _SUBCOMMANDS.get(parsed.first.casefold())
    user_id, group_id = _event_user_group(event, context)
    manager = await get_manager(context)

    if subcommand == "create":
        message = await _handle_create(
            manager,
            parsed,
            user_id  = user_id,
            group_id = group_id,
        )
        return _message(message)
    if subcommand == "list":
        return _message(await _handle_list(manager, parsed))
    if subcommand == "status":
        return _message(await _handle_status(manager, parsed))
    if subcommand == "cancel":
        return _message(await _handle_cancel(manager, parsed))
    if subcommand == "clear":
        return _message(await _handle_clear(manager, parsed))
    if subcommand == "delete":
        return _message(await _handle_delete(manager, parsed))

    task_parts = raw.split(maxsplit=1)
    if len(task_parts) != 2:
        return _message(f"缺少任务内容。用法: /codex {raw} <任务>")
    label, prompt = task_parts
    return _message(
        await manager.enqueue(
            label,
            prompt,
            user_id  = user_id,
            group_id = group_id,
            context  = context,
        )
    )
