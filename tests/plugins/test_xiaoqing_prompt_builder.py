from plugins.xiaoqing_chat.config.config import PersonalityConfig
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.llm.prompt_builder import build_prompt_messages
from plugins.xiaoqing_chat.runtime_state import get_state


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


def test_prompt_builder_treats_media_markers_as_real_content() -> None:
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
        current_text="[QQ表情：微笑]",
        personality=personality,
        keyword_rules=[],
        regex_rules=[],
        request_id="req-test-3",
    )

    system_prompt = msgs[0].content
    assert "[图片：...]" in system_prompt or "[表情包：...]" in system_prompt or "[QQ表情：...]" in system_prompt


def test_prompt_builder_rehydrates_media_marker_from_registry(tmp_path) -> None:
    personality = PersonalityConfig(
        polite_guardrail=True,
        identity="你叫小青。",
        states=[],
        state_probability=0.0,
        reply_style="口语化",
    )
    state = get_state()
    state.media_store.bind(tmp_path)
    state.media_store.upsert_media_items(
        [
            {
                "kind": "emoji",
                "media_hash": "hash-emoji-1",
                "description": "猫猫翻白眼",
                "emotion_tags": ["无语", "嫌弃"],
                "marker": "[表情包：猫猫翻白眼]",
            }
        ]
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
                content="[[xc_media_1]]",
                media_items=(
                    {
                        "kind": "emoji",
                        "media_hash": "hash-emoji-1",
                        "marker": "[表情包：一张表情包]",
                    },
                ),
                ts=1700000001.0,
            )
        ],
        current_text="你看懂没",
        personality=personality,
        keyword_rules=[],
        regex_rules=[],
        request_id="req-test-4",
    )

    user_prompt = msgs[1].content
    assert "猫猫翻白眼" in user_prompt
    assert "无语" in user_prompt
    assert "[表情包：一张表情包]" not in user_prompt
    assert "[[xc_media_1]]" not in user_prompt


def test_prompt_builder_prefers_canonical_parts_when_legacy_fields_are_stale(tmp_path) -> None:
    personality = PersonalityConfig(
        polite_guardrail=True,
        identity="你叫小青。",
        states=[],
        state_probability=0.0,
        reply_style="口语化",
    )
    state = get_state()
    state.media_store.bind(tmp_path)

    msgs = build_prompt_messages(
        is_private=False,
        bot_name="小青",
        sender_name="测试用户",
        think_level=1,
        history=[
            StoredMessage(
                role="user",
                name="测试用户",
                content="[[xc_media_1]]",
                media_items=(
                    {
                        "kind": "emoji",
                        "media_hash": "hash-emoji-1",
                        "marker": "[表情包：一张表情包]",
                    },
                ),
                parts=(
                    {"kind": "text", "text": "先看这个"},
                    {
                        "kind": "emoji",
                        "media_hash": "hash-emoji-1",
                        "marker": "[表情包：猫猫翻白眼]",
                        "description": "猫猫翻白眼",
                        "emotion_tags": ["无语"],
                    },
                    {"kind": "text", "text": "就知道了"},
                ),
                ts=1700000002.0,
            )
        ],
        current_text="在吗",
        personality=personality,
        keyword_rules=[],
        regex_rules=[],
        request_id="req-test-5",
    )

    user_prompt = msgs[1].content
    assert "先看这个[表情包：猫猫翻白眼]就知道了" in user_prompt
    assert "[表情包：一张表情包]" not in user_prompt
