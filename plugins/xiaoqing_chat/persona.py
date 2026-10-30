"""统一解析机器人名称，读取人设卡，并按用途取出人设文字。

人设只写在人设卡（默认是插件自带的 ``config/persona.md``）里。标题末尾的〔…〕标记
决定一节交给谁：不带标记的是人物事实，聊天、agent、行为规划和回复审查都用；
〔聊天〕只给聊天回复，〔办事〕只给 agent，〔深度对话〕在私聊深度模式下追加，
〔心境〕下的每一条是一种随机心境。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.constants import DEFAULT_BOT_NAME

BUNDLED_PERSONA_FILE = Path(__file__).resolve().parent / "config" / "persona.md"

SCOPE_CHAT  = "聊天"
SCOPE_AGENT = "办事"
SCOPE_DEEP  = "深度对话"
SCOPE_MOODS = "心境"
_SCOPES     = frozenset({SCOPE_CHAT, SCOPE_AGENT, SCOPE_DEEP, SCOPE_MOODS})

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_TAG_RE     = re.compile(r"〔([^〕]*)〕")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BULLET_RE  = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
_NAME_SLOT  = "{{name}}"


def resolve_bot_name(value: object) -> str:
    """返回清理后的运行时名称；空值沿用项目默认名称。"""

    name = str(value or "").strip()
    return name or DEFAULT_BOT_NAME


@dataclass(frozen=True, slots=True)
class PersonaSection:
    title : str
    scopes: frozenset[str]
    body  : str


@dataclass(frozen=True, slots=True)
class PersonaCard:
    """解析好的人设卡；按用途取文字时把 ``{{name}}`` 换成当前名字。"""

    sections: tuple[PersonaSection, ...] = ()

    @classmethod
    def parse(cls, text: str) -> PersonaCard:
        sections: list[PersonaSection] = []
        title, scopes, lines = "", frozenset[str](), list[str]()

        def flush() -> None:
            body = "\n".join(lines).strip()
            if body:
                sections.append(PersonaSection(title, scopes, body))

        for line in _COMMENT_RE.sub("", text).splitlines():
            heading = _HEADING_RE.match(line)
            if heading is None:
                lines.append(line)
                continue
            flush()
            raw     = heading.group(1)
            scopes  = frozenset(tag.strip() for tag in _TAG_RE.findall(raw))
            title   = _TAG_RE.sub("", raw).strip()
            lines   = []
            unknown = scopes - _SCOPES
            if unknown:
                raise ValueError(
                    f"人设卡「{title}」的标记不认识：{'、'.join(sorted(unknown))}；"
                    f"可用的有 {'、'.join(sorted(_SCOPES))}"
                )
        flush()
        if not sections:
            raise ValueError("人设卡是空的")
        return cls(tuple(sections))

    def facts(self, bot_name: object) -> str:
        """人物事实：名字加上不带标记的各节。"""

        name = resolve_bot_name(bot_name)
        body = self._render(lambda section: not section.scopes, name)
        return f"你的名字是{name}。\n\n{body}".strip()

    def scoped(self, scope: str, bot_name: object) -> str:
        """带某个标记的各节，比如聊天时的说话方式和示例。"""

        return self._render(lambda section: scope in section.scopes, resolve_bot_name(bot_name))

    def for_agent(self, bot_name: object) -> str:
        """agent 帮忙办事时用的人设：人物事实加上〔办事〕各节。"""

        parts = (self.facts(bot_name), self.scoped(SCOPE_AGENT, bot_name))
        return "\n\n".join(part for part in parts if part)

    @property
    def moods(self) -> tuple[str, ...]:
        return tuple(
            match.group(1)
            for section in self.sections
            if SCOPE_MOODS in section.scopes
            for match in map(_BULLET_RE.match, section.body.splitlines())
            if match
        )

    def _render(self, wanted: Callable[[PersonaSection], bool], name: str) -> str:
        blocks: list[str] = []
        for section in self.sections:
            if SCOPE_MOODS in section.scopes or not wanted(section):
                continue
            if not section.title:
                blocks.append(section.body)
                continue
            title = section.title.rstrip()
            label = title if ("：" in title or ":" in title) else f"{title}："
            blocks.append(f"{label}\n{section.body}")
        return "\n\n".join(blocks).replace(_NAME_SLOT, name)


def load_persona_card(path: Path) -> PersonaCard:
    return PersonaCard.parse(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def bundled_persona_card() -> PersonaCard:
    """插件自带的人设卡；配置没指定别的文件时用它。"""

    return load_persona_card(BUNDLED_PERSONA_FILE)


def persona_card_path(plugin_dir: Path, persona_file: str) -> Path:
    """配置里的人设卡路径：留空用插件自带的，相对路径从插件的 config/ 目录算起。"""

    if not persona_file.strip():
        return BUNDLED_PERSONA_FILE
    path = Path(persona_file.strip())
    return path if path.is_absolute() else plugin_dir / "config" / path


def persona_subject_pattern(bot_name: object, *, include_first_person: bool = False) -> str:
    """生成只匹配当前名称的人称正则片段。"""

    subjects: list[str] = ["你", resolve_bot_name(bot_name)]
    if include_first_person:
        subjects.insert(0, "我")
    unique = dict.fromkeys(subject for subject in subjects if subject)
    return "(?:" + "|".join(re.escape(subject) for subject in unique) + ")"
