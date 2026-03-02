from plugins.xiaoqing_chat.config.config import PersonalityConfig
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.llm.prompt_builder import build_prompt_messages


def test_prompt_builder_discourages_repetitive_clarifying_questions() -> None:
    personality = PersonalityConfig(
        polite_guardrail=True,
        identity="你叫小青。",
        states=[],
        state_probability=0.0,
        reply_style="口语化",
    )

    msgs = build_prompt_messages(
        is_private=False,
        bot_name="小青",
        sender_name="测试用户",
        think_level=1,
        history=[],
        current_text="放弃下载了，太大了",
        personality=personality,
        keyword_rules=[],
        regex_rules=[],
        request_id="req-test",
    )

    system_prompt = msgs[0].content
    # Prompt should discourage chasing unanswered questions
    assert "问过的问题没人回答" in system_prompt or "追问" in system_prompt or "放下" in system_prompt


def test_prompt_builder_does_not_include_user_id_in_name() -> None:
    personality = PersonalityConfig(
        polite_guardrail=True,
        identity="你叫小青。",
        states=[],
        state_probability=0.0,
        reply_style="口语化",
    )

    msgs = build_prompt_messages(
        is_private=False,
        bot_name="小青",
        sender_name="测试用户",
        think_level=1,
        history=[
            StoredMessage(
                role="user",
                name="测试用户",
                content="你好",
                ts=1700000000.0,
                user_id=123456,
            )
        ],
        current_text="在吗",
        personality=personality,
        keyword_rules=[],
        regex_rules=[],
        request_id="req-test-2",
    )

    user_prompt = msgs[1].content
    assert "测试用户(123456)" not in user_prompt
