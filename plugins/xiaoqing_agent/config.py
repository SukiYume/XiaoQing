"""读取 ``config.plugins.xiaoqing_agent``；越界或类型错误的字段回退到默认值。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PLUGIN_NAME = "xiaoqing_agent"


@dataclass(frozen=True, slots=True)
class AgentConfig:
    # 总开关；关闭时 xiaoqing_chat 完全保持原有聊天行为。
    enabled: bool = False
    # 单个任务最多调用模型的轮数。
    max_steps: int = 40
    # 单个任务从开始到交付的总时长上限。
    task_timeout_seconds: float = 1800.0
    # 工具未指定超时时的默认值，以及模型可以申请的上限。
    tool_timeout_seconds: float     = 120.0
    max_tool_timeout_seconds: float = 900.0
    # 收到任务后最多等这么久；做完就直接作为这一轮的回复，否则先回一句再后台继续。
    # 上限 25 秒：加上判断的 20 秒仍短于聊天侧 50 秒的等待和插件 60 秒的执行预算。
    inline_wait_seconds: float = 12.0
    # 名额分两份：Bot 管理员的任务和其他人的任务各算各的，别人再多也挤不掉管理员。
    # Bot 管理员同时运行的任务数。
    max_admin_tasks: int = 3
    # 其他人合计同时运行的任务数，以及每个人同时运行的任务数（不同会话合计）。
    max_member_tasks: int     = 4
    max_tasks_per_member: int = 1
    # 每次工具输出交给模型前的字符上限。
    max_tool_output_chars: int = 12000
    # 判断与执行时附带的本会话最近任务数。
    history_tasks: int = 5
    # 留空时：Python 用 Bot 自身解释器；Shell 在 Windows 优先 Git Bash，否则 PowerShell。
    python: str = ""
    shell: str  = ""


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(maximum, max(minimum, value))


def _float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number):
        return default
    return min(maximum, max(minimum, number))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_agent_config(raw: Any) -> AgentConfig:
    options  = raw if isinstance(raw, Mapping) else {}
    defaults = AgentConfig()
    tool_max = _float(
        options.get("max_tool_timeout_seconds"),
        defaults.max_tool_timeout_seconds,
        minimum = 5.0,
        maximum = 7200.0,
    )
    return AgentConfig(
        enabled=_bool(options.get("enabled"), defaults.enabled),
        max_steps=_int(options.get("max_steps"), defaults.max_steps, minimum=1, maximum=200),
        task_timeout_seconds=_float(
            options.get("task_timeout_seconds"),
            defaults.task_timeout_seconds,
            minimum = 30.0,
            maximum = 86400.0,
        ),
        tool_timeout_seconds=_float(
            options.get("tool_timeout_seconds"),
            defaults.tool_timeout_seconds,
            minimum = 5.0,
            maximum = tool_max,
        ),
        max_tool_timeout_seconds = tool_max,
        inline_wait_seconds      = _float(
            options.get("inline_wait_seconds"),
            defaults.inline_wait_seconds,
            minimum = 0.0,
            maximum = 25.0,
        ),
        max_admin_tasks=_int(
            options.get("max_admin_tasks"),
            defaults.max_admin_tasks,
            minimum = 1,
            maximum = 20,
        ),
        max_member_tasks=_int(
            options.get("max_member_tasks"),
            defaults.max_member_tasks,
            minimum = 0,
            maximum = 50,
        ),
        max_tasks_per_member=_int(
            options.get("max_tasks_per_member"),
            defaults.max_tasks_per_member,
            minimum = 1,
            maximum = 20,
        ),
        max_tool_output_chars=_int(
            options.get("max_tool_output_chars"),
            defaults.max_tool_output_chars,
            minimum = 1000,
            maximum = 100000,
        ),
        history_tasks=_int(
            options.get("history_tasks"),
            defaults.history_tasks,
            minimum = 0,
            maximum = 20,
        ),
        python = _text(options.get("python")),
        shell  = _text(options.get("shell")),
    )


def load_agent_config(context: Any) -> AgentConfig:
    """每次调用都读当前配置快照，改 ``enabled`` 后下一条消息即生效。"""

    settings = context.get_settings_snapshot()
    plugins  = settings.config.get("plugins", {})
    raw      = plugins.get(PLUGIN_NAME, {}) if isinstance(plugins, Mapping) else {}
    return parse_agent_config(raw)
