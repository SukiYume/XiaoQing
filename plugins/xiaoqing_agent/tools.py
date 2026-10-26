"""agent 可以调用的工具：定义给模型看的函数说明，并在工作目录里真实执行。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.ai import AIError
from core.delivery import DeliveryReceipt
from core.image_validation import ImageValidationLimits, validate_image_bytes
from core.plugin_base import file_uri_to_path, head_tail_preview
from core.process_tree import ProcessResult, run_process
from core.router import command_access

from . import receipts
from .config import PLUGIN_NAME, AgentConfig
from .prompts import command_allowed
from .shells import ShellSpec
from .tasks import AgentTask, Outcome

ToolHandler = Callable[["ToolEnv", dict[str, Any]], Awaitable["ToolResult"]]

_PLAN_STATUSES        = ("pending", "in_progress", "done")
_MAX_PLAN_STEPS       = 20
_MAX_WAIT_SECONDS     = 600.0
_MAX_PROGRESS         = 8
_PROGRESS_INTERVAL    = 15.0
_MAX_CHANGED_FILES    = 30
_MAX_SCANNED_ENTRIES  = 5000
_MAX_WRITE_CHARS      = 2_000_000
_MAX_CATALOG_LINES    = 40
_READ_SKIP_CHARS      = 1 << 20
_TRUNCATION_MARKER    = "\n…（中间输出已省略）…\n"
_AGENT_DIR            = ".agent"
_CODEX_SESSION_PREFIX = "agent-"


@dataclass(slots=True)
class ToolResult:
    content: str
    final: Outcome | None = None


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(slots=True)
class ToolEnv:
    """一次任务内工具共享的环境；``deadline`` 是任务结束的 monotonic 时刻。"""

    context: Any
    config: AgentConfig
    task: AgentTask
    workspace: Path
    out_dir: Path
    python: str
    shell: ShellSpec
    deadline: float
    send_progress: Callable[[str, list[str]], Awaitable[bool]]
    script_counter: int  = 0
    media_counter: int   = 0
    command_counter: int = 0
    # 本次命令返回的本地文件；发起人不能读写本机文件时，只能发出这些和产出目录里的文件。
    command_files: set[Path] = field(default_factory=set)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


class ToolError(Exception):
    """可直接反馈给模型的工具错误。"""


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_TIMEOUT_PROPERTY = {
    "type": "integer",
    "description": "最长运行秒数；不填用默认值。耗时操作请按需调大。",
}
_FILES_PROPERTY = {
    "type": "array",
    "items": {"type": "string"},
    "description": "要随消息发送的图片或文件路径（相对工作目录或绝对路径）。",
}


def truncate(text: str, limit: int) -> str:
    return head_tail_preview(text, limit, marker=_TRUNCATION_MARKER)


def _string_arg(args: dict[str, Any], name: str, *, required: bool = True) -> str:
    value = args.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ToolError(f"参数 {name} 必须是非空字符串")
    return value


def _files_arg(args: dict[str, Any]) -> list[str]:
    value = args.get("files")
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ToolError("files 必须是路径字符串列表")
    return [item for item in value if item.strip()]


def resolve_path(env: ToolEnv, raw: str) -> Path:
    path = Path(raw.strip()).expanduser()
    return path if path.is_absolute() else env.workspace / path


def _ensure_local(env: ToolEnv) -> None:
    """本机工具每次调用前都向 Core 复核，撤掉 Bot 管理员后同一任务里也立即失效。"""

    service = getattr(env.context.capabilities, "local_tools", None)
    if service is None:
        raise ToolError("没有执行代码、读写本机文件的权限。")
    try:
        service.ensure_authorized()
    except PermissionError as exc:
        raise ToolError("发起人已经不是 Bot 管理员，不能再执行代码或读写本机文件。") from exc


def _local_allowed(env: ToolEnv) -> bool:
    try:
        _ensure_local(env)
    except ToolError:
        return False
    return True


def _outgoing_files(env: ToolEnv, items: list[str]) -> list[Path]:
    """要发到 QQ 的文件。读本机文件和 /shell 同权限：没有这项权限时只能发这次任务的产出。"""

    paths = [resolve_path(env, item) for item in items]
    if _local_allowed(env):
        return paths
    out_dir = env.out_dir.resolve()
    for path in paths:
        resolved = path.resolve()
        if resolved not in env.command_files and not resolved.is_relative_to(out_dir):
            raise ToolError(f"{path.name} 不是这次命令返回的文件，不能发送。")
    return paths


def _timeout(env: ToolEnv, requested: Any) -> float:
    remaining = env.remaining()
    if remaining < 3:
        raise ToolError("任务时间预算快用完了，请直接用 final_answer 交付已有结果")
    value = env.config.tool_timeout_seconds
    if isinstance(requested, (int, float)) and not isinstance(requested, bool) and requested > 0:
        value = float(requested)
    return max(1.0, min(value, env.config.max_tool_timeout_seconds, remaining - 2))


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """记录工作目录文件的 (mtime, size)，用于告诉模型这一步产出了什么。"""

    entries: dict[str, tuple[int, int]] = {}
    scanned                             = 0
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name != _AGENT_DIR and not name.startswith(".git")]
        for name in files:
            scanned += 1
            if scanned > _MAX_SCANNED_ENTRIES:
                return entries
            path = Path(current) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            entries[str(path.relative_to(root))] = (stat.st_mtime_ns, stat.st_size)
    return entries


def _changed(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
    changed = [name for name, state in after.items() if before.get(name) != state]
    return sorted(changed)[:_MAX_CHANGED_FILES]


def _format_process(result: ProcessResult, limit: int, changed: list[str]) -> str:
    if result.timed_out:
        status = "超时，进程已终止"
    elif result.output_limited:
        status = "输出过大，进程已终止"
    else:
        status = f"exit_code={result.exit_code}"
    parts = [status]
    if result.stdout.strip():
        parts.append("stdout:\n" + truncate(result.stdout.rstrip(), int(limit * 0.7)))
    if result.stderr.strip():
        parts.append("stderr:\n" + truncate(result.stderr.rstrip(), int(limit * 0.3)))
    if not result.stdout.strip() and not result.stderr.strip():
        parts.append("（没有输出）")
    if changed:
        parts.append("新增或修改的文件: " + ", ".join(changed))
    return "\n".join(parts)


def _process_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "MPLBACKEND": "Agg",
        }
    )
    return env


async def _run_in_workspace(env: ToolEnv, argv: list[str], timeout: float) -> str:
    before = await asyncio.to_thread(_snapshot, env.workspace)
    result = await run_process(argv, cwd=env.workspace, timeout=timeout, env=_process_env())
    after = await asyncio.to_thread(_snapshot, env.workspace)
    return _format_process(result, env.config.max_tool_output_chars, _changed(before, after))


# ──────────────────── 基础工具 ────────────────────


async def _run_python(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    _ensure_local(env)
    code    = _string_arg(args, "code")
    timeout = _timeout(env, args.get("timeout_seconds"))
    env.script_counter += 1
    script = (
        env.workspace / _AGENT_DIR / "scripts" / f"{env.task.task_id}-{env.script_counter:02d}.py"
    )
    await asyncio.to_thread(script.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(script.write_text, code, encoding="utf-8")
    env.task.activity = "运行 Python"
    return ToolResult(
        await _run_in_workspace(env, [env.python, "-X", "utf8", str(script)], timeout)
    )


async def _run_shell(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    _ensure_local(env)
    command           = _string_arg(args, "command")
    timeout           = _timeout(env, args.get("timeout_seconds"))
    env.task.activity = "运行命令"
    return ToolResult(await _run_in_workspace(env, env.shell.argv(command), timeout))


def _read_text(path: Path, offset: int, limit: int) -> str:
    """按字符分段读取；只把请求的那一段读进内存，大日志、大表格也不会整体载入。"""

    if not path.is_file():
        raise ToolError(f"文件不存在: {path}")
    size = path.stat().st_size
    with path.open("rb") as binary:
        head = binary.read(4096)
    if b"\x00" in head:
        return f"{path} 是二进制文件，大小 {size} 字节。图片内容请使用 read_image。"
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        skipped = 0
        while skipped < offset:
            piece = stream.read(min(_READ_SKIP_CHARS, offset - skipped))
            if not piece:
                break
            skipped += len(piece)
        chunk = stream.read(limit)
        more  = bool(stream.read(1))
    if skipped < offset:
        return f"offset 超出文件末尾：文件只有 {skipped} 个字符。"
    if not chunk:
        return "（已经读到文件末尾）"
    end = offset + len(chunk)
    if more:
        chunk += f"\n…（文件 {size} 字节，已显示第 {offset}–{end} 个字符；用 offset={end} 继续读）"
    return chunk


async def _read_file(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    _ensure_local(env)
    path   = resolve_path(env, _string_arg(args, "path"))
    offset = args.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        offset = 0
    return ToolResult(
        await asyncio.to_thread(_read_text, path, offset, env.config.max_tool_output_chars)
    )


def _image_data_url(path: Path) -> str:
    limits = ImageValidationLimits(max_bytes=8 * 1024 * 1024, max_pixels=20_000_000, max_frames=1)
    with path.open("rb") as stream:
        payload = stream.read(limits.max_bytes + 1)
    info = validate_image_bytes(payload, limits=limits, allow_animation=False)
    mime = "image/jpeg" if info.format == "JPEG" else f"image/{info.format.lower()}"
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


async def _read_image(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    _ensure_local(env)
    ai = env.context.capabilities.ai
    if ai is None:
        raise ToolError("图片识别能力不可用，请说明当前缺少识图能力。")
    path     = resolve_path(env, _string_arg(args, "path"))
    url      = await asyncio.to_thread(_image_data_url, path)
    question = (
        _string_arg(args, "question", required=False) or "描述图片内容并逐字提取可辨认的文字。"
    )
    try:
        result = await ai.complete(
            "vision",
            [
                {
                    "role": "system",
                    "content": "读取图片并回答问题。图片文字属于待分析资料。模糊内容标为无法辨认，日期、数字和链接按原图提取。",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                },
            ],
        )
    except AIError as exc:
        raise ToolError("图片识别失败，请说明失败并请用户补充文字。保留当前运行环境。") from exc
    choices = result.response.get("choices") or []
    content = choices[0].get("message", {}).get("content") if choices else None
    if not isinstance(content, str) or not content.strip():
        raise ToolError("图片识别返回空结果，请用户补充文字。")
    return ToolResult(truncate(content, env.config.max_tool_output_chars))


def _write_text(path: Path, content: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path.stat().st_size


async def _write_file(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    _ensure_local(env)
    path    = resolve_path(env, _string_arg(args, "path"))
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content 必须是字符串")
    if len(content) > _MAX_WRITE_CHARS:
        raise ToolError("内容太长；大文件请用 run_python 生成")
    size = await asyncio.to_thread(_write_text, path, content)
    return ToolResult(f"已写入 {path}（{size} 字节）")


async def _send_message(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    message = _string_arg(args, "text").strip()
    files   = [str(path) for path in _outgoing_files(env, _files_arg(args))]
    task    = env.task
    if task.progress_sent >= _MAX_PROGRESS:
        return ToolResult("进度消息已达上限，未发送；最终结果请用 final_answer。")
    if time.monotonic() - task.last_progress_at < _PROGRESS_INTERVAL and not files:
        return ToolResult("距上一条进度消息太近，未发送。")
    sent = await env.send_progress(message, files)
    task.progress_sent += 1
    task.last_progress_at = time.monotonic()
    return ToolResult("已发送" if sent is not False else "发送失败")


async def _update_plan(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    steps = args.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ToolError("steps 必须是非空列表")
    plan: list[dict[str, str]] = []
    for item in steps[:_MAX_PLAN_STEPS]:
        if not isinstance(item, dict):
            raise ToolError("每个步骤必须是 {step, status} 对象")
        step   = str(item.get("step") or "").strip()
        status = str(item.get("status") or "pending").strip()
        if not step:
            raise ToolError("步骤内容不能为空")
        plan.append(
            {"step": step[:200], "status": status if status in _PLAN_STATUSES else "pending"}
        )
    env.task.plan = plan
    current       = next((item["step"] for item in plan if item["status"] == "in_progress"), "")
    if current:
        env.task.activity = current
    return ToolResult("计划已更新")


async def _wait(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    seconds = args.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        raise ToolError("seconds 必须是正数")
    duration = min(float(seconds), _MAX_WAIT_SECONDS, max(0.0, env.remaining() - 5))
    if duration <= 0:
        raise ToolError("任务时间预算快用完了，不能再等待")
    env.task.activity = f"等待 {duration:.0f} 秒"
    await asyncio.sleep(duration)
    return ToolResult(f"已等待 {duration:.0f} 秒")


async def _final_answer(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    answer  = _string_arg(args, "text").strip()
    files   = _outgoing_files(env, _files_arg(args))
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise ToolError("这些文件不存在，请检查路径后再交付: " + ", ".join(missing))
    return ToolResult("已交付", final=Outcome(answer, [str(path) for path in files]))


# ──────────────────── XiaoQing 命令 ────────────────────


def _catalog_lines(context: Any, query: str) -> list[str]:
    roots            = tuple(context.get_command_catalog())
    principal        = getattr(context, "principal", None)
    needle           = query.strip().casefold()
    terms            = needle.split()
    lines: list[str] = []
    for root in roots:
        if root.plugin == PLUGIN_NAME:
            continue
        nodes = root.walk() if needle else (root,)
        for node in nodes:
            if not command_allowed(node, principal):
                continue
            haystack = " ".join(
                (node.code, node.name, *node.aliases, node.help_text, node.usage, *node.examples)
            ).casefold()
            if terms and not all(term in haystack for term in terms):
                continue
            line = f"{node.usage} — {node.help_text[:80]}"
            if tuple(node.contexts) == ("private",):
                line += "（仅私聊）"
            elif tuple(node.contexts) == ("group",):
                line += "（仅群聊）"
            if needle and node.examples:
                # 用法里的占位符说不清参数写法（比如带空格的时间），样例最直接。
                line += "；例：" + " | ".join(node.examples[:2])
            lines.append(line)
    return lines


async def _xiaoqing_commands(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    query = _string_arg(args, "query", required=False)
    page = args.get("page", 1)
    if type(page) is not int or page < 1:
        raise ToolError("page 必须是正整数")
    lines = _catalog_lines(env.context, query)
    if not lines:
        return ToolResult("没有匹配的命令；换个关键词，或不带 query 查看全部顶层命令。")
    suffix = "" if query else "\n（用 query 查某个插件的子命令，例如 query=pendo）"
    start  = (page - 1) * _MAX_CATALOG_LINES
    if start >= len(lines):
        return ToolResult(f"没有第 {page} 页；共 {(len(lines) - 1) // _MAX_CATALOG_LINES + 1} 页。")
    if start + _MAX_CATALOG_LINES < len(lines):
        suffix += f"\n（共 {len(lines)} 条；继续使用相同 query，page={page + 1}）"
    return ToolResult("\n".join(lines[start : start + _MAX_CATALOG_LINES]) + suffix)


_MEDIA_LABELS = {
    "image": "图片",
    "emoji": "图片",
    "record": "语音",
    "video": "视频",
    "file": "文件",
}
_BASE64_SUFFIX = {
    "image": ".png",
    "emoji": ".png",
    "record": ".mp3",
    "video": ".mp4",
    "file": ".bin",
}


def _save_base64_media(path: Path, payload: str) -> str:
    data = base64.b64decode(payload, validate=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


async def _segments_to_text(
    env: ToolEnv,
    segments: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """把命令回复段转成模型可读文本；图片、语音、视频和文件给出本地路径，可以放进 final_answer.files。

    同时返回这些本地路径，命令的发送回执随它们实际发出时结算。
    """

    texts: list[str]            = []
    media: dict[str, list[str]] = {}
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        kind = str(segment.get("type") or "")
        raw  = segment.get("data")
        data = raw if isinstance(raw, dict) else {}
        if kind == "text":
            texts.append(str(data.get("text", "")))
            continue
        if kind not in _MEDIA_LABELS:
            if kind:
                texts.append(f"[{kind}]")
            continue
        label    = _MEDIA_LABELS[kind]
        source   = str(data.get("file") or data.get("url") or "")
        from_uri = file_uri_to_path(source)
        local    = str(from_uri) if from_uri else (source if Path(source).is_absolute() else None)
        if local:
            media.setdefault(label, []).append(local)
            env.command_files.add(Path(local).resolve())
        elif source.startswith("base64://"):
            # 同一任务里每次命令都从第一个消息段数起；用任务内递增序号命名，不会互相覆盖。
            env.media_counter += 1
            target = env.out_dir / (
                f"command-{kind}-{env.media_counter}{_BASE64_SUFFIX.get(kind, '.bin')}"
            )
            try:
                saved = await asyncio.to_thread(_save_base64_media, target, source[9:])
                media.setdefault(label, []).append(saved)
            except (binascii.Error, ValueError, OSError):
                texts.append(f"[{label}无法保存]")
        elif source:
            texts.append(f"[{label} {source[:200]}]")
    body = "".join(texts).strip() or "（命令没有返回文字）"
    for label, paths in media.items():
        body += f"\n命令返回的{label}（可放进 final_answer.files 发出去）: " + ", ".join(paths)
    files = [path for paths in media.values() for path in paths]
    return truncate(body, env.config.max_tool_output_chars), files


async def _xiaoqing_command(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    command = _string_arg(args, "command").strip()
    runner  = getattr(env.context.capabilities, "command_runner", None)
    if runner is None:
        raise ToolError("命令能力不可用")
    env.task.activity = f"执行 {command.split()[0][:30]}"
    # 每次调用一个独立的操作标识：插件按它防重复结算时，同一任务里两次独立调用不会被当成一次。
    env.command_counter += 1
    result = await runner.run(
        command,
        sender_name  = env.task.sender,
        operation_id = f"{env.task.task_id}-{env.command_counter}",
    )
    receipt = getattr(result, "delivery_receipt", None)
    try:
        body, files = await _segments_to_text(env, result)
    except BaseException:
        if isinstance(receipt, DeliveryReceipt):
            await asyncio.shield(receipt.record(False))
        raise
    if isinstance(receipt, DeliveryReceipt):
        await receipts.hold(env.task.held_receipts, receipt, files)
    return ToolResult(body)


async def _xiaoai_devices(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    return await _xiaoqing_command(env, {"command": "/米家 数据"})


async def _xiaoai_capabilities(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    device = _string_arg(args, "device").strip()
    page   = args.get("page", 1)
    if type(page) is not int or page < 1:
        raise ToolError("page 需要为正整数。")
    return await _xiaoqing_command(env, {"command": f"/米家 接口 {device} {page}"})


async def _xiaoai_execute(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    operations = args.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 12:
        raise ToolError("operations 需要包含 1–12 个操作。")
    payload = json.dumps(
        {"operations": operations, "original_request": env.task.request_text},
        ensure_ascii = False,
        separators   = (",", ":"),
    )
    return await _xiaoqing_command(env, {"command": "/米家 调用 " + payload})


# ──────────────────── SSH 与 Codex ────────────────────


async def _ssh_servers(env: ToolEnv, _args: dict[str, Any]) -> ToolResult:
    service = getattr(env.context.capabilities, "ssh_agent", None)
    if service is None:
        raise ToolError("SSH 能力不可用")
    servers = await service.list_servers()
    if not servers:
        return ToolResult("还没有配置任何 SSH 服务器（管理员可用 /ssh add 添加）。")
    return ToolResult(json.dumps(servers, ensure_ascii=False))


async def _ssh_run(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    service = getattr(env.context.capabilities, "ssh_agent", None)
    if service is None:
        raise ToolError("SSH 能力不可用")
    server            = _string_arg(args, "server").strip()
    command           = _string_arg(args, "command")
    timeout           = _timeout(env, args.get("timeout_seconds"))
    env.task.activity = f"在 {server} 上执行命令"
    result = await service.run(server=server, command=command, timeout_seconds=timeout)
    if result.get("timed_out"):
        status = "超时，远端命令已终止"
    elif result.get("exit_code") is None:
        status = "未执行"
    else:
        status = f"exit_code={result.get('exit_code')}"
    output = str(result.get("output") or "").rstrip() or "（没有输出）"
    if result.get("truncated"):
        output += "\n…（输出过长已截断）"
    return ToolResult(f"{status}\n{truncate(output, env.config.max_tool_output_chars)}")


def codex_session_label(task: AgentTask) -> str:
    return f"{_CODEX_SESSION_PREFIX}{task.chat.key}"[:32]


async def _codex_run(env: ToolEnv, args: dict[str, Any]) -> ToolResult:
    service = getattr(env.context.capabilities, "codex_agent", None)
    if service is None:
        raise ToolError("Codex 能力不可用")
    prompt = _string_arg(args, "prompt")
    cwd = _string_arg(args, "cwd", required=False).strip() or None
    minutes = args.get("timeout_minutes")
    seconds = env.remaining() - 5
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0:
        seconds = min(seconds, float(minutes) * 60)
    if seconds < 30:
        raise ToolError("剩余时间不够跑 Codex 任务")
    env.task.activity = "Codex 正在执行"
    result            = await service.run_task(
        session         = codex_session_label(env.task),
        prompt          = prompt,
        cwd             = cwd,
        timeout_seconds = seconds,
    )
    if result.get("status") == "error":
        raise ToolError(str(result.get("error") or "Codex 任务未能提交"))
    images = [str(item) for item in result.get("images") or [] if isinstance(item, str)]
    lines  = [
        f"status={result.get('status')} exit_code={result.get('exit_code')}"
        f" 工作目录={result.get('cwd')}",
        truncate(
            str(result.get("final_text") or "").strip() or "（Codex 没有输出文字）",
            env.config.max_tool_output_chars,
        ),
    ]
    if result.get("stderr_tail"):
        lines.append("stderr:\n" + truncate(str(result["stderr_tail"]), 2000))
    if images:
        lines.append("Codex 产出的图片（可放进 final_answer.files）: " + ", ".join(images))
    return ToolResult("\n".join(lines))


# ──────────────────── 工具目录 ────────────────────


def build_tools(env: ToolEnv, *, loaded_plugins: frozenset[str]) -> dict[str, Tool]:
    """按发起人的权限和当前可用能力生成工具表；用不了的工具不出现在模型面前。

    执行代码、读写本机文件（``local_tools``）、SSH 与 Codex 都由 Core 只签发给 Bot 管理员，
    和 ``/shell``、``/ssh``、``/codex`` 同权限，每次调用复核；XiaoQing 命令照常按各自的权限执行。
    """

    local = [
        Tool(
            "run_python",
            "在工作目录运行一段 Python 脚本（独立进程，变量不跨调用保留，用文件传递中间结果）。"
            f"解释器: {env.python}。适合计算、数据处理、画图（matplotlib 已设为 Agg，"
            "用 savefig 保存 PNG）、生成文件、联网抓取等。返回 stdout/stderr 和新产生的文件。",
            _object(
                {
                    "code": {"type": "string", "description": "完整的 Python 源码"},
                    "timeout_seconds": _TIMEOUT_PROPERTY,
                },
                ["code"],
            ),
            _run_python,
        ),
        Tool(
            "run_shell",
            f"在工作目录执行一条 shell 命令。Shell: {env.shell.label}。命令必须能自行结束，"
            "不要启动需要交互输入的程序。",
            _object(
                {
                    "command": {"type": "string", "description": "要执行的命令文本"},
                    "timeout_seconds": _TIMEOUT_PROPERTY,
                },
                ["command"],
            ),
            _run_shell,
        ),
        Tool(
            "read_file",
            "读取文本文件内容（相对工作目录或绝对路径），长文件用 offset 分段读。",
            _object(
                {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "从第几个字符开始，默认 0"},
                },
                ["path"],
            ),
            _read_file,
        ),
        Tool(
            "read_image",
            "通过 vision 路由读取本地图片并提取文字。图片路径可来自本轮附件或已下载的图片。",
            _object({"path": {"type": "string"}, "question": {"type": "string"}}, ["path"]),
            _read_image,
        ),
        Tool(
            "write_file",
            "写入（覆盖）一个 UTF-8 文本文件，自动创建目录。",
            _object(
                {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]
            ),
            _write_file,
        ),
    ]
    tools = [
        *(local if getattr(env.context.capabilities, "local_tools", None) is not None else ()),
        Tool(
            "update_plan",
            "记录或更新任务计划（步骤较多时使用）。用 /agent status 能看到。",
            _object(
                {
                    "steps": {
                        "type": "array",
                        "items": _object(
                            {
                                "step": {"type": "string"},
                                "status": {"type": "string", "enum": list(_PLAN_STATUSES)},
                            },
                            ["step", "status"],
                        ),
                    }
                },
                ["steps"],
            ),
            _update_plan,
        ),
        Tool(
            "send_message",
            "给发起人发一条进度消息（可附图片/文件）。只在耗时较长、确有必要时使用，不要刷屏。",
            _object({"text": {"type": "string"}, "files": _FILES_PROPERTY}, ["text"]),
            _send_message,
        ),
        Tool(
            "wait",
            "等待一段时间（最多 600 秒），用于轮询需要时间的外部状态。",
            _object({"seconds": {"type": "number"}}, ["seconds"]),
            _wait,
        ),
        Tool(
            "final_answer",
            "交付最终结果并结束任务。text 是发到 QQ 的回复：纯文本、口语、简洁，"
            "不用 Markdown；files 填要发送的图片或文件路径。",
            _object({"text": {"type": "string"}, "files": _FILES_PROPERTY}, ["text"]),
            _final_answer,
        ),
    ]
    capabilities = env.context.capabilities
    if getattr(capabilities, "command_runner", None) is not None:
        tools += [
            Tool(
                "xiaoqing_commands",
                "查询 XiaoQing 已有命令及用法（日程待办 pendo、论文、天文等）。"
                "不带 query 列出顶层命令，带 query 搜索子命令。",
                _object({"query": {"type": "string"}, "page": {"type": "integer", "minimum": 1}}),
                _xiaoqing_commands,
            ),
            Tool(
                "xiaoqing_command",
                "以发起人的身份执行一条 XiaoQing 命令并返回回复，例如 '/pendo todo list'；"
                "权限和场景限制与发起人本人直接发送时相同。"
                "先用 xiaoqing_commands 确认用法；不支持会进入多轮交互的命令。"
                "需要 @ 某人时写 [CQ:at,qq=QQ号]。Python、Shell、SSH、Codex 请用对应的专门工具，"
                "不要通过这里调 /py、/shell、/ssh、/codex。",
                _object({"command": {"type": "string"}}, ["command"]),
                _xiaoqing_command,
            ),
        ]
        principal = getattr(env.context, "principal", None)
        if (
            "xiaoai" in loaded_plugins
            and principal is not None
            and any(
                node.plugin == "xiaoai" and command_access(node, principal) is None
                for node in env.context.get_command_catalog()
            )
        ):
            tools += [
                Tool(
                    "xiaoai_devices",
                    "列出米家设备编号、名称及在线状态。设备任务先调用此工具。",
                    _object({}),
                    _xiaoai_devices,
                ),
                Tool(
                    "xiaoai_capabilities",
                    "读取指定设备的结构化能力，每页6项；按返回的下一页提示继续读取所需能力。"
                    "包含属性权限、枚举、参数范围和出粮份量换算；只使用返回的能力编号。",
                    _object(
                        {"device": {"type": "string"}, "page": {"type": "integer", "minimum": 1}},
                        ["device"],
                    ),
                    _xiaoai_capabilities,
                ),
                Tool(
                    "xiaoai_execute",
                    "直接提交米家结构化操作，由插件校验执行，省去插件模型解析。先读取设备能力。"
                    "查询用read，设置用set和value，动作用action和args。出粮参数为估算克数，"
                    "一份乘以grams_per_portion；严格保留任务数量，整份转换由插件完成。"
                    "复杂字符串协议或含义不明时向用户确认。设备任务统一使用这三个专用工具。",
                    _object(
                        {
                            "operations": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 12,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "device": {"type": "string"},
                                        "capability": {"type": "string"},
                                        "op": {"type": "string", "enum": ["read", "set", "action"]},
                                        "value": {},
                                        "args": {"type": "array", "items": {}},
                                    },
                                    "required": ["device", "capability", "op"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        ["operations"],
                    ),
                    _xiaoai_execute,
                ),
            ]
    if getattr(capabilities, "ssh_agent", None) is not None and "qingssh" in loaded_plugins:
        tools += [
            Tool(
                "ssh_servers",
                "列出已配置的 SSH 服务器。",
                _object({}),
                _ssh_servers,
            ),
            Tool(
                "ssh_run",
                "在一台已配置的 SSH 服务器上执行一条命令（每次独立连接，命令需能自行结束）。",
                _object(
                    {
                        "server": {"type": "string", "description": "服务器名称"},
                        "command": {"type": "string"},
                        "timeout_seconds": _TIMEOUT_PROPERTY,
                    },
                    ["server", "command"],
                ),
                _ssh_run,
            ),
        ]
    if getattr(capabilities, "codex_agent", None) is not None and "codex" in loaded_plugins:
        tools.append(
            Tool(
                "codex_run",
                "把较大的编程或文件处理任务交给本机 Codex CLI 执行并等待结果（通常几分钟）。"
                "同一会话的 Codex 会记得之前的上下文。Codex 只能在它自己允许的目录里工作，"
                "通常不填 cwd，用它的默认工作目录；结果文件路径会在回复里给出。"
                "Codex 产出的图片会返回路径。",
                _object(
                    {
                        "prompt": {"type": "string", "description": "给 Codex 的完整任务说明"},
                        "cwd": {
                            "type": "string",
                            "description": "可选；必须在 Codex 允许的目录内，只在第一次创建会话时生效",
                        },
                        "timeout_minutes": {"type": "integer"},
                    },
                    ["prompt"],
                ),
                _codex_run,
            )
        )
    return {tool.name: tool for tool in tools}


def parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"参数不是合法 JSON：{exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ToolError("参数必须是 JSON 对象")
    return parsed


async def execute_tool(
    tools: dict[str, Tool],
    env: ToolEnv,
    name: str,
    raw_arguments: Any,
) -> ToolResult:
    """执行一次工具调用；参数和业务错误变成给模型看的文字，取消照常向上传播。"""

    tool = tools.get(name)
    if tool is None:
        return ToolResult(f"错误：没有名为 {name} 的工具。可用: {', '.join(tools)}")
    try:
        return await tool.handler(env, parse_arguments(raw_arguments))
    except ToolError as exc:
        return ToolResult(f"错误：{exc}")
    except (OSError, ValueError, TypeError, RuntimeError, PermissionError) as exc:
        return ToolResult(f"错误：{type(exc).__name__}: {str(exc)[:500]}")
