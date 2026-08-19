"""统一解析机器人名称，并将名称与人物设定组合为运行时人格。"""

from __future__ import annotations

import re

from core.constants import DEFAULT_BOT_NAME


def resolve_bot_name(value: object) -> str:
    """返回清理后的运行时名称；空值沿用项目默认名称。"""

    name = str(value or "").strip()
    return name or DEFAULT_BOT_NAME


def compose_persona_identity(identity: object, bot_name: object) -> str:
    """组合只含一个姓名声明的完整人格描述。"""

    name = resolve_bot_name(bot_name)
    details = str(identity or "").strip()
    return f"你的名字是{name}" + (f"，{details}" if details else "")


def persona_subject_pattern(bot_name: object, *, include_first_person: bool = False) -> str:
    """生成只匹配当前名称的人称正则片段。"""

    subjects: list[str] = ["你", resolve_bot_name(bot_name)]
    if include_first_person:
        subjects.insert(0, "我")
    unique = dict.fromkeys(subject for subject in subjects if subject)
    return "(?:" + "|".join(re.escape(subject) for subject in unique) + ")"


def replace_persona_name(text: object, bot_name: object) -> str:
    """把当前机器人名称归一为第一人称，供人物证据比对使用。"""

    return str(text or "").replace(resolve_bot_name(bot_name), "我")
