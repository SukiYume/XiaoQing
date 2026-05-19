from __future__ import annotations

from typing import Any

from core.args import parse
from core.plugin_base import segments

from .manager import get_manager


HELP_TEXT = """Codex 会话队列:
/codex create <name> [cwd:<path>]  创建会话，默认目录为 C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex
/codex <name> <任务>              向指定会话追加任务
/codex list                       查看会话
/codex status [name]              查看状态
/codex cancel <name> [job_id]      取消运行中任务，或移除排队任务
/codex clear <name>                清空排队任务
/codex delete <name> [--force] [--protected]  删除会话并归档历史

路径建议统一输入 / 斜杠，例如 C:/Users/xxx/project。插件会按 bot 所在系统解析。"""


def init(context: Any = None) -> None:
    return None


async def shutdown(context: Any = None) -> None:
    if context is None:
        return
    manager = await get_manager(context)
    await manager.shutdown()


def _event_user_group(event: dict[str, Any], context: Any) -> tuple[int | None, int | None]:
    user_id = event.get("user_id") or getattr(context, "current_user_id", None)
    group_id = event.get("group_id") or getattr(context, "current_group_id", None)
    return user_id, group_id


def _cwd_from_args(parsed: Any) -> str | None:
    cwd = parsed.opt("cwd") or parsed.opt("C")
    if cwd:
        return cwd
    for token in parsed.tokens:
        if token.startswith("cwd:"):
            return token[4:]
        if token.startswith("cwd="):
            return token[4:]
    return None


async def handle(command: str, args: str, event: dict[str, Any], context: Any) -> list[dict[str, Any]]:
    manager = await get_manager(context)
    raw = (args or "").strip()
    if not raw or raw.lower() in {"help", "帮助", "?"}:
        return segments(HELP_TEXT)

    lowered = raw.lower()
    parsed = parse(raw)
    subcommand = parsed.first.lower()
    user_id, group_id = _event_user_group(event, context)

    if subcommand in {"create", "new", "创建"}:
        label = parsed.second
        if not label:
            return segments("用法: /codex create <name> [cwd:<path>]")
        message = await manager.create_session(
            label,
            _cwd_from_args(parsed),
            user_id=user_id,
            group_id=group_id,
        )
        return segments(message)

    if subcommand in {"list", "ls", "列表"}:
        return segments(await manager.list_sessions())

    if subcommand in {"status", "状态"}:
        label = parsed.second or None
        return segments(await manager.status(label))

    if subcommand in {"cancel", "stop", "取消", "停止"}:
        label = parsed.second
        if not label:
            return segments("用法: /codex cancel <name> [job_id]")
        job_id = None
        if parsed.get(2):
            try:
                job_id = int(parsed.get(2))
            except ValueError:
                return segments("job_id 必须是数字。")
        return segments(await manager.cancel(label, job_id))

    if subcommand in {"clear", "清空"}:
        label = parsed.second
        if not label:
            return segments("用法: /codex clear <name>")
        return segments(await manager.clear_queue(label))

    if subcommand in {"delete", "del", "remove", "rm", "删除"}:
        label = parsed.second
        if not label:
            return segments("用法: /codex delete <name> [--force] [--protected]")
        return segments(
            await manager.delete_session(
                label,
                force=parsed.has("force") or "--force" in lowered,
                allow_protected=parsed.has("protected") or "--protected" in lowered,
            )
        )

    if " " not in raw:
        return segments(f"缺少任务内容。用法: /codex {raw} <任务>")
    label, prompt = raw.split(maxsplit=1)
    return segments(
        await manager.enqueue(
            label,
            prompt,
            user_id=user_id,
            group_id=group_id,
            context=context,
        )
    )
