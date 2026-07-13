from plugins.xiaoqing_chat.config.config import PersonalityConfig
from plugins.xiaoqing_chat.llm.prompt_builder import build_prompt_messages
from plugins.xiaoqing_chat.memory.memory import StoredMessage
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


def test_prompt_builder_uses_media_only_reply_target_block_for_current_parts() -> None:
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
        current_text="[图片：海边落日]",
        personality=personality,
        keyword_rules=[],
        regex_rules=[],
        current_parts=(
            {
                "kind": "image",
                "marker": "[图片：海边落日]",
                "description": "海边落日",
            },
        ),
        request_id="req-test-6",
    )

    user_prompt = msgs[1].content
    assert "现在测试用户发送的图片：[图片：海边落日]。引起了你的注意" in user_prompt
    assert "现在 测试用户 说" not in user_prompt


def test_prompt_builder_treats_media_only_emoji_text_as_user_speech() -> None:
    personality = PersonalityConfig(
        polite_guardrail=True,
        identity="你叫小青。",
        states=[],
        state_probability=0.0,
        reply_style="口语化",
    )

    msgs = build_prompt_messages(
        is_private=True,
        bot_name="小青",
        sender_name="测试用户",
        think_level=1,
        history=[],
        current_text="[表情包：佩服，调侃；写着“不愧是你 我佩服得鹉体投地”]",
        personality=personality,
        keyword_rules=[],
        regex_rules=[],
        current_parts=(
            {
                "kind": "emoji",
                "marker": "[表情包：佩服，调侃；写着“不愧是你 我佩服得鹉体投地”]",
                "description": "佩服，调侃",
            },
        ),
        request_id="req-test-emoji-speech",
    )

    system_prompt = msgs[0].content
    user_prompt = msgs[1].content
    assert "媒体消息理解" in system_prompt
    assert "交际作用" in system_prompt
    assert "不要直接输出 `[表情包：...]`" in system_prompt
    assert "现在测试用户借表情包表达：想说的话：不愧是你 我佩服得鹉体投地" in user_prompt
    assert "语气反应：佩服，调侃" in user_prompt
    assert "发送的表情包" not in user_prompt


def test_prompt_builder_treats_media_only_qq_face_as_tone() -> None:
    personality = PersonalityConfig(
        polite_guardrail=True,
        identity="你叫小青。",
        states=[],
        state_probability=0.0,
        reply_style="口语化",
    )

    msgs = build_prompt_messages(
        is_private=True,
        bot_name="小青",
        sender_name="测试用户",
        think_level=1,
        history=[],
        current_text="[QQ表情：菜汪]",
        personality=personality,
        keyword_rules=[],
        regex_rules=[],
        current_parts=(
            {
                "kind": "qq_face",
                "marker": "[QQ表情：菜汪]",
                "label": "菜汪",
            },
        ),
        request_id="req-test-qq-face-tone",
    )

    user_prompt = msgs[1].content
    assert "现在测试用户用 QQ 表情表达一个反应：菜汪" in user_prompt
    assert "请结合上下文接住这个反应" in user_prompt


def test_prompt_builder_uses_mixed_reply_target_block_for_current_parts() -> None:
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
        current_text="你看这个\n[表情包：无语]",
        personality=personality,
        keyword_rules=[],
        regex_rules=[],
        current_parts=(
            {"kind": "text", "text": "你看这个"},
            {
                "kind": "emoji",
                "marker": "[表情包：无语]",
                "description": "无语猫猫",
            },
        ),
        request_id="req-test-7",
    )

    user_prompt = msgs[1].content
    assert "现在测试用户发送了表情包：[表情包：无语]，并说：你看这个。引起了你的注意" in user_prompt


def test_prompt_builder_does_not_duplicate_current_turn_when_history_already_contains_it() -> None:
    personality = PersonalityConfig(
        polite_guardrail=True,
        identity="你叫小青。",
        states=[],
        state_probability=0.0,
        reply_style="口语化",
    )

    current_parts = (
        {"kind": "text", "text": "你看这个"},
        {
            "kind": "image",
            "marker": "[图片：海边落日]",
            "description": "海边落日",
        },
    )
    history = [
        StoredMessage(
            role="user",
            name="测试用户",
            parts=current_parts,
            ts=1700000003.0,
        )
    ]

    msgs = build_prompt_messages(
        is_private=False,
        bot_name="小青",
        sender_name="测试用户",
        think_level=1,
        history=history,
        current_text="你看这个\n[图片：海边落日]",
        personality=personality,
        keyword_rules=[],
        regex_rules=[],
        current_parts=current_parts,
        request_id="req-test-8",
    )

    user_prompt = msgs[1].content
    assert user_prompt.count("[图片：海边落日]") == 1
    assert user_prompt.count("你看这个") == 1
