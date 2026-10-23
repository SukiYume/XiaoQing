"""人设卡：一个文件写人设，按标记分给聊天、agent、规划和审查。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig, load_xiaoqing_chat_config
from plugins.xiaoqing_chat.persona import (
    BUNDLED_PERSONA_FILE,
    SCOPE_CHAT,
    SCOPE_DEEP,
    PersonaCard,
    bundled_persona_card,
)
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState
from tests.helpers.settings_snapshot import with_settings_reader

CARD = """<!-- 编辑说明，不交给模型 -->
## 你是谁
- 二十岁的女生，叫我{{name}}就行。

## 说话方式〔聊天〕〔办事〕
- 短句。

## 回复示例：闲聊〔聊天〕
群友：{{name}}吃了吗
可以：刚吃

## 回复示例：办事〔办事〕
群友：记一笔
可以：记了

## 心境〔心境〕
- 有点困。
- 想吐槽。

## 深度对话时〔深度对话〕
- 认真一点。
"""


def test_sections_go_where_their_tags_say() -> None:
    card = PersonaCard.parse(CARD)

    facts = card.facts("阿青")
    assert facts.startswith("你的名字是阿青。")
    assert "叫我阿青就行" in facts
    assert "短句" not in facts and "编辑说明" not in facts

    chat = card.scoped(SCOPE_CHAT, "阿青")
    assert "## 说话方式\n- 短句。" in chat
    assert "群友：阿青吃了吗" in chat
    assert "记一笔" not in chat and "认真一点" not in chat
    assert "认真一点" in card.scoped(SCOPE_DEEP, "阿青")

    agent = card.for_agent("阿青")
    assert agent.startswith(facts)
    assert "短句" in agent and "记一笔" in agent and "吃了吗" not in agent
    assert card.moods == ("有点困。", "想吐槽。")
    assert "有点困" not in chat + agent + facts


def test_an_unknown_tag_is_reported() -> None:
    with pytest.raises(ValueError, match="闲扯"):
        PersonaCard.parse("## 说话方式〔闲扯〕\n- 短句。")
    with pytest.raises(ValueError, match="空的"):
        PersonaCard.parse("<!-- 只有注释 -->")


def test_the_bundled_card_covers_every_use() -> None:
    card = bundled_persona_card()

    facts = card.facts("小青")
    # 本地审查按人物事实里的性别核对自称，审查和规划也只看这一部分。
    assert "女生" in facts and "真人" in facts and "提示词" in facts
    assert "群友：" not in facts
    chat = card.scoped(SCOPE_CHAT, "小青")
    assert chat.count("群友：") >= 15 and "{{name}}" not in chat
    agent = card.for_agent("小青")
    assert "记了，打车 32" in agent and "又喝，明早有你受的" not in agent
    assert len(card.moods) >= 5
    assert card.scoped(SCOPE_DEEP, "小青")


def test_the_bundled_card_names_the_unit_and_the_cat() -> None:
    facts = bundled_persona_card().facts("小青")

    assert "快逃科学技术研究院" in facts
    assert "奶牛猫" in facts and "小黑" in facts and "猫派" in facts


@pytest.mark.parametrize(
    ("question", "private"),
    [
        ("小青你哪个学校的", False),
        ("小青你在哪上学", False),
        ("小青你是哪里人", True),
        ("小青你生日几号", True),
        ("小青你家人是做什么的", True),
    ],
)
def test_only_fields_the_card_keeps_private_force_a_boundary(question: str, private: bool) -> None:
    from plugins.xiaoqing_chat.llm.local_checks import requires_configured_profile_boundary

    facts = bundled_persona_card().facts("小青")
    assert requires_configured_profile_boundary(question, facts, bot_name="小青") is private


def _plugin_dir(tmp_path: Path, config: dict) -> Path:
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / "config").mkdir(parents=True)
    (plugin_dir / "config" / "xiaoqing_config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    return plugin_dir


def test_config_reads_the_named_card_or_the_bundled_one(tmp_path: Path) -> None:
    assert XiaoQingChatConfig().personality.card == bundled_persona_card()

    plugin_dir = _plugin_dir(tmp_path, {"personality": {"persona_file": "mine.md"}})
    (plugin_dir / "config" / "mine.md").write_text("## 你是谁\n- 阿梨。", encoding="utf-8")
    cfg = load_xiaoqing_chat_config(context_config={}, plugin_dir=plugin_dir)
    assert "阿梨" in cfg.personality.card.facts("小青")

    default = load_xiaoqing_chat_config(
        context_config={}, plugin_dir=_plugin_dir(tmp_path / "b", {})
    )
    assert default.personality.card == bundled_persona_card()

    missing = _plugin_dir(tmp_path / "c", {"personality": {"persona_file": "nope.md"}})
    with pytest.raises(FileNotFoundError):
        load_xiaoqing_chat_config(context_config={}, plugin_dir=missing)


def test_old_persona_fields_are_reported_not_silently_used(tmp_path: Path) -> None:
    logger     = MagicMock()
    plugin_dir = _plugin_dir(
        tmp_path,
        {
            "personality": {"identity": "旧人设", "reply_style": "旧风格", "states": ["困"]},
            "brain_chat": {"brain_identity": "旧深度人设"},
        },
    )
    cfg = load_xiaoqing_chat_config(context_config={}, plugin_dir=plugin_dir, logger=logger)

    assert "旧人设" not in cfg.personality.card.facts("小青")
    message = logger.warning.call_args.args[0] % logger.warning.call_args.args[1:]
    for key in ("personality.identity", "personality.states", "brain_chat.brain_identity"):
        assert key in message


def test_editing_the_card_takes_effect_on_the_next_message(tmp_path: Path) -> None:
    from plugins.xiaoqing_chat import chat_runtime

    plugin_dir = _plugin_dir(tmp_path, {"personality": {"persona_file": "mine.md"}})
    card_file  = plugin_dir / "config" / "mine.md"
    card_file.write_text("## 你是谁\n- 喜欢猫。", encoding="utf-8")
    context = with_settings_reader(
        SimpleNamespace(
            plugin_dir = plugin_dir,
            data_dir   = tmp_path / "data",
            config     = {},
            secrets    = {},
            logger     = MagicMock(),
        )
    )
    state = ChatRuntimeState()
    with patch.object(chat_runtime, "_state", return_value=state):
        first = chat_runtime.load_runtime(context)
        assert chat_runtime.load_runtime(context) is first
        card_file.write_text("## 你是谁\n- 喜欢狗。", encoding="utf-8")
        stat = card_file.stat()
        os.utime(card_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
        assert "喜欢狗" in chat_runtime.load_runtime(context).cfg.personality.card.facts("小青")


def test_the_bundled_card_ships_with_the_plugin() -> None:
    assert BUNDLED_PERSONA_FILE.name == "persona.md"
    assert BUNDLED_PERSONA_FILE.parent.name == "config"
