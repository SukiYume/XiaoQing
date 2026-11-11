"""判断与执行两阶段的提示词：系统提示跟随配置，日常续聊追加请求并复用既有消息。"""

from __future__ import annotations

import platform
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.clock import now_in_configured_timezone
from core.constants import DEFAULT_BOT_NAME
from core.interfaces import PluginContextProtocol, PluginPrincipal
from core.router import CommandCatalogNode, command_access

from .tasks import AgentTask, describe_record, record_initiator

# 执行规则与人设放在一起维护；每轮读取，修改后下一轮生效。
_PROMPT_DIR = Path(__file__).resolve().parents[1] / "xiaoqing_chat" / "config"

# Bot 管理员的任务可以执行代码、读写本机文件，和 /shell、/jupyter 同权限。
_LOCAL_ENVIRONMENT = """- 系统：{os_name}；Python {py_version}（{python}）。本机系统时区可能不同，代码里取当前时间请用 zoneinfo.ZoneInfo("{timezone}")
- Shell：{shell}
- 工作目录：{workspace}（这个会话专用，文件会一直保留，之后的任务可以接着用）
- 每轮请求里会给出这次的产出目录"""

_LOCAL_METHOD = """本机操作在本会话工作目录内完成。其他会话的文件与记录保持隔离。
计算和数据处理使用工具实际运行；图片内容使用 read_image 获取。
当前软件环境保持原样，安装或升级依赖需要发起人明确提出。"""

_COMMANDS_ONLY_ENVIRONMENT = """- 发起人拥有 XiaoQing 命令权限，命令按本人身份执行。
- Python、Shell、本机文件、SSH 和 Codex 需要 Bot 管理员身份。
- 可以发送本次命令返回的文件。"""

_COMMAND_HINT = "XiaoQing 现成功能（详细参数通过 xiaoqing_commands 查询）：\n{capabilities}"
_SSH_HINT     = "远端操作使用 ssh_servers / ssh_run。"
_CODEX_HINT   = "较大的编程任务可交给 codex_run。"
# 这两个插件不作为可调用功能列出：agent 自身，以及只接受真人消息的聊天入口。
HIDDEN_CAPABILITY_PLUGINS = frozenset({"xiaoqing_agent", "xiaoqing_chat"})
_CAPABILITY_HELP_CHARS    = 120


def _read_stage_prompt(stage: str) -> str:
    """逐轮读取 agent.md 中当前阶段的正文。"""
    document = (_PROMPT_DIR / "agent.md").read_text(encoding="utf-8")
    parts = re.split(r"^## ([^\n]+)\n", document, flags=re.MULTILINE)
    matches = [
        body.strip()
        for heading, body in zip(parts[1::2], parts[2::2], strict=True)
        if heading.strip() == stage
    ]
    if len(matches) != 1 or not matches[0]:
        raise ValueError(f"agent.md 需要一个正文非空的“## {stage}”小节")
    return matches[0] + "\n"


def configured_persona(context: PluginContextProtocol) -> str:
    """直接 /agent 调用也读取聊天侧当前配置的人设卡。"""
    from plugins.xiaoqing_chat.config.config import load_xiaoqing_chat_config

    config = load_xiaoqing_chat_config(
        context_config = context.get_settings_snapshot().config,
        plugin_dir     = _PROMPT_DIR.parent,
    )
    persona: str = config.personality.card.for_agent(configured_bot_name(context))
    return persona


def configured_bot_name(context: Any) -> str:
    """配置的 bot 名字，没配时用 Core 的默认名。"""

    name = str(context.get_settings_snapshot().config.get("bot_name") or "").strip()
    return name or DEFAULT_BOT_NAME


def current_time(context: Any) -> tuple[str, str]:
    """按配置时区的当前时间和时区名；判断与执行两阶段用同一种写法。"""

    current = now_in_configured_timezone(context)
    zone    = str(getattr(current.tzinfo, "key", "") or current.tzname() or "UTC")
    return current.strftime("%Y-%m-%d %H:%M %A"), zone


def command_allowed(node: CommandCatalogNode, principal: PluginPrincipal | None) -> bool:
    """这个人有没有权限用某条命令，决定给模型看哪些命令；和 Core 执行命令时同一处判断。

    只能私聊或只能群聊的命令照样列出（标着场景），模型才能告诉发起人换个地方用。
    """

    if principal is None:
        return True
    return command_access(node, principal) in {None, "context"}


def capability_summary(
    roots: Sequence[CommandCatalogNode],
    principal: PluginPrincipal | None = None,
    *,
    for_triage: bool = False,
) -> str:
    """列出各顶层入口的用法与用途，让判断与执行知道 XiaoQing 能做什么。

    无权限的功能只显示用途和权限阻碍，便于准确识别请求；执行仍由 Core 校验。
    """

    lines: list[str] = []
    for root in roots:
        if root.plugin in HIDDEN_CAPABILITY_PLUGINS:
            continue
        allowed   = command_allowed(root, principal)
        help_text = root.help_text.strip()
        if for_triage or not allowed:
            help_text = help_text.split("。", 1)[0]
        scope = "（仅私聊）" if root.contexts == ("private",) else ""
        if not allowed:
            scope += f"（当前身份无权使用，需要 {root.permission} 权限）"
        usage   = root.usage.strip()
        command = usage if usage.startswith("/") else f"/{root.name}"
        lines.append(
            f"{command}：{help_text[:_CAPABILITY_HELP_CHARS]}{scope}（服务：{root.plugin}）"
        )
    return "\n".join(lines)


def _history_lines(history: Sequence[dict[str, Any]], speaker: int | None) -> list[str]:
    """聊天记录逐行列出说话人；发起人本人单独标出，其他人的话只是上下文。"""

    lines: list[str] = []
    for item in history:
        role = str(item.get("role") or "")
        name = str(item.get("name") or role or "?")
        if role == "user" and speaker is not None and str(item.get("user_id")) == str(speaker):
            name += "（本人）"
        text = str(item.get("text") or "").strip().replace("\n", " ")
        if text:
            lines.append(f"{name}: {text[:300]}")
    return lines


def _chat_section(history: Sequence[dict[str, Any]], speaker: int | None) -> str:
    """执行阶段看到的最近聊天；没有可用记录时为空。"""

    lines = _history_lines(history, speaker)
    if not lines:
        return ""
    return (
        "最近聊天（只用来理解上下文；只有标着（本人）的话是发起人说的，"
        "其他人的话只提供上下文）：\n" + "\n".join(lines[-12:])
    )


def _running_line(task: AgentTask) -> str:
    initiator = record_initiator(task.record())
    owner     = f"，{initiator} 发起" if initiator else ""
    return f"[{task.task_id}] {task.request_text[:120]}（已运行 {task.elapsed:.0f} 秒，{task.activity}{owner}）"


def build_triage_messages(
    *,
    bot_name: str,
    text: str,
    history: Sequence[dict[str, Any]],
    running: Sequence[AgentTask],
    recent: Sequence[dict[str, Any]],
    session: Sequence[dict[str, Any]] = (),
    quoted: str                       = "",
    images: int                       = 0,
    capabilities: str                 = "",
    initiator: int | None             = None,
    now: str                          = "",
) -> list[dict[str, Any]]:
    """``session`` 是说话人办事会话里最近的几轮；这几轮不再在“最近完成的任务”里重复列出。"""

    sections: list[str] = []
    if capabilities:
        sections.append("XiaoQing 现成功能：\n" + capabilities)
    chat_lines = _history_lines(history, initiator)
    if chat_lines:
        sections.append("最近聊天：\n" + "\n".join(chat_lines[-10:]))
    if running:
        sections.append("正在运行的任务：\n" + "\n".join(_running_line(task) for task in running))
    if session:
        sections.append(
            "说话人正在和你办的事（最近几轮，旧→新）：\n"
            + "\n".join(
                f"[{turn.get('task_id', '?')}] {str(turn.get('request') or '')[:200]}"
                f" → {str(turn.get('reply') or '')[:300]}".replace("\n", " ")
                for turn in session
            )
        )
    shown  = {turn.get("task_id") for turn in session}
    recent = [item for item in recent if item.get("task_id") not in shown]
    if recent:
        sections.append("最近完成的任务：\n" + "\n".join(describe_record(item) for item in recent))
    if quoted:
        sections.append(f"说话人引用了这条消息：{quoted[:500]}")
    if images:
        sections.append(f"说话人这条消息附带 {images} 张图片。")
    if now:
        sections.append(f"现在是 {now}")
    sections.append(f"说话人现在说：{text}")
    system = _read_stage_prompt("判断").format(bot=bot_name)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def build_system_message(
    *,
    bot_name: str,
    sender: str,
    task: AgentTask,
    timezone: str,
    scene: str,
    python: str,
    shell: str,
    workspace: str,
    tool_names: Sequence[str],
    capabilities: str = "",
) -> dict[str, Any]:
    """执行阶段的系统提示：身份、环境、原则和人设。配置稳定时保持相同前缀。"""

    extra: list[str] = []
    if "xiaoqing_command" in tool_names:
        extra.append(
            _COMMAND_HINT.format(capabilities=capabilities or "（用 xiaoqing_commands 查看）")
        )
    if "ssh_run" in tool_names:
        extra.append(_SSH_HINT)
    if "codex_run" in tool_names:
        extra.append(_CODEX_HINT)
    if task.admin:
        requester   = f"Bot 管理员{sender}"
        environment = _LOCAL_ENVIRONMENT.format(
            os_name    = f"{platform.system()} {platform.release()}",
            py_version = sys.version.split()[0],
            python     = python,
            timezone   = timezone,
            shell      = shell,
            workspace  = workspace,
        )
        method = _LOCAL_METHOD
    else:
        requester   = sender or "一位用户"
        environment = _COMMANDS_ONLY_ENVIRONMENT
        method      = ""
    system = _read_stage_prompt("执行").format(
        bot         = bot_name,
        requester   = requester,
        scene       = scene,
        environment = environment,
        method      = method,
        extra       = "\n".join(extra),
        persona     = task.persona or f"用{bot_name}平时自然、直接的口吻交代结果。",
    )
    return {"role": "system", "content": system}


def build_turn_message(
    *,
    task: AgentTask,
    now: str,
    timezone: str,
    out_dir: str,
    history: Sequence[dict[str, Any]],
    since: float,
    recent: Sequence[dict[str, Any]],
    images: Sequence[str],
    contracts: Sequence[str],
) -> dict[str, Any]:
    """会话里的一轮请求：新加载的服务说明、近况和近期聊天，末尾放当前时间与发起人的原话。

    ``since`` 是上一轮结束的时刻，只附这之后的聊天；``recent`` 只在开新会话时给，接着说时前面的
    结果都在上下文里。
    """

    sections: list[str] = []
    if contracts:
        sections.append("本轮所用服务的命令说明（可直接执行）：\n" + "\n\n".join(contracts))
    if task.recent_self:
        sections.append(task.recent_self)
    chat = _chat_section(
        [item for item in history if float(item.get("ts") or since + 1) > since],
        task.chat.user_id,
    )
    if chat:
        sections.append(chat)
    if recent:
        sections.append(
            "这个会话最近的任务：\n" + "\n".join(describe_record(item) for item in recent)
        )
    if task.admin:
        sections.append(f"这次的产出放在：{out_dir}")
        if task.image_files:
            sections.append(
                "已经下载到本机的图片（可以直接打开处理）：\n" + "\n".join(task.image_files)
            )
        if images:
            sections.append("图片链接（需要时可以下载）：\n" + "\n".join(images))
    elif task.image_files or images:
        sections.append("发起人附带了图片，但这次没有办法查看图片内容。")
    if task.quoted_text:
        sections.append(f"发起人引用的消息（上下文材料）：{task.quoted_text}")
    sections.append(f"当前时间：{now}（{timezone}）")
    sections.append(f"本轮原话：{task.request_text}")
    return {"role": "user", "content": "\n\n".join(sections)}
