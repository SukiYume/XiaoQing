from types import SimpleNamespace
from unittest.mock import patch

from plugins.xiaoqing_chat.config.config import PersonalityConfig, XiaoQingChatConfig
from plugins.xiaoqing_chat.expression.expr_utils import render_dialogue
from plugins.xiaoqing_chat.llm.prompt_builder import build_prompt_messages
from plugins.xiaoqing_chat.llm.reply_checker import (
    _heuristic_check,
    _requires_configured_profile_boundary,
    _requires_llm_semantic_check,
)
from plugins.xiaoqing_chat.main import _help_text
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.persona import compose_persona_identity
from plugins.xiaoqing_chat.planning.pfc_action_planner import _build_persona_text
from plugins.xiaoqing_chat.reply_generator import _is_persona_intro_query


def test_default_personality_keeps_name_out_of_plugin_config() -> None:
    cfg = XiaoQingChatConfig()

    assert "小青" not in cfg.personality.identity
    assert "小青" not in cfg.brain_chat.brain_identity


def test_custom_name_is_injected_into_planner_and_generation_prompt() -> None:
    personality = PersonalityConfig(identity="住校的理工科学生。", states=[])

    persona_text = _build_persona_text("阿澄", personality)
    messages     = build_prompt_messages(
        is_private    = False,
        bot_name      = "阿澄",
        sender_name   = "测试用户",
        think_level   = 1,
        history       = [],
        current_text  = "阿澄，今天怎么样？",
        personality   = personality,
        keyword_rules = [],
        regex_rules   = [],
        request_id    = "bot-name-test",
    )
    system_prompt = messages[0].content

    assert persona_text == "你的名字是阿澄，住校的理工科学生。"
    assert "- 名字：阿澄" in system_prompt
    assert "- 已知人设：住校的理工科学生。" in system_prompt
    assert "小青" not in persona_text
    assert "小青" not in system_prompt


def test_custom_name_drives_persona_queries() -> None:
    grounding = "具体学校和城市没有设定，不主动补成可核验资料。"

    assert _is_persona_intro_query("阿澄是什么样的人", bot_name="阿澄") is True
    assert _is_persona_intro_query("小青是什么样的人", bot_name="阿澄") is False
    assert (
        _requires_configured_profile_boundary(
            "阿澄在哪所大学",
            grounding,
            bot_name="阿澄",
        )
        is True
    )
    assert (
        _requires_configured_profile_boundary(
            "小青在哪所大学",
            grounding,
            bot_name="阿澄",
        )
        is False
    )
    assert (
        _requires_llm_semantic_check(
            reply        = "我没设定具体学校。",
            current_text = "阿澄多大",
            bot_name     = "阿澄",
        )
        is True
    )


def test_history_uses_current_name_and_keeps_assistant_role_after_rename() -> None:
    history = [
        StoredMessage(role="assistant", name="旧名字", content="刚刚说过", ts=1.0),
    ]

    dialogue = render_dialogue(history, bot_name="阿澄")
    repeated = _heuristic_check(
        reply                = "刚刚说过",
        history              = history,
        max_repeat_compare   = 2,
        similarity_threshold = 0.9,
        max_assistant_in_row = 3,
    )

    assert dialogue == "阿澄(阿澄)：刚刚说过"
    assert "旧名字" not in dialogue
    assert repeated is not None
    assert repeated.suitable is False


def test_persona_composer_uses_only_current_name() -> None:
    assert compose_persona_identity("喜欢天文。", "阿澄") == "你的名字是阿澄，喜欢天文。"


def test_plugin_help_uses_current_name() -> None:
    context = SimpleNamespace(
        get_settings_snapshot=lambda: SimpleNamespace(config={"bot_name": "阿澄"})
    )

    with patch("plugins.xiaoqing_chat.main._catalog_root", return_value=None):
        help_text = _help_text(context)

    assert help_text.startswith("💬 阿澄智能对话")
    assert "小青" not in help_text
