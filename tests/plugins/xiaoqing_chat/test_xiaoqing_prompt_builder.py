import pytest

from plugins.xiaoqing_chat.config.config import PersonalityConfig
from plugins.xiaoqing_chat.llm.prompt_builder import build_dialogue_prompt, build_prompt_messages
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.persona import SCOPE_CHAT, SCOPE_DEEP, PersonaCard
from plugins.xiaoqing_chat.reply_fallbacks import _concise_public_identity
from plugins.xiaoqing_chat.runtime_state import get_state


@pytest.mark.parametrize(
    ("current", "spoken"),
    [
        ("小青，也是终于坐上回家的高铁了。", "现在阿夏对你说的：也是终于坐上回家的高铁了。"),
        ("小青，你刚才说小青喜欢什么？", "现在阿夏对你说的：你刚才说小青喜欢什么？"),
        ("小青，给大家丢个话题。", "现在阿夏对你说的：给大家丢个话题。"),
        ("小青这个角色挺有趣。", "现在阿夏说的：小青这个角色挺有趣。"),
    ],
)
def test_current_turn_keeps_speaker_distinct_from_leading_vocative(current, spoken):
    messages = build_prompt_messages(
        is_private    = False,
        bot_name      = "小青",
        sender_name   = "阿夏",
        think_level   = 0,
        history       = [],
        current_text  = current,
        personality   = PersonalityConfig(),
        keyword_rules = [],
        regex_rules   = [],
        request_id    = "speaker-frame",
    )
    assert f"{spoken}。引起了你的注意" in messages[-1].content


_CARD = PersonaCard.parse("## 你是谁\n- 住校的理工科学生。\n\n## 说话方式〔聊天〕\n- 口语化")


def test_explicit_definition_stays_in_data_context_and_original_request_is_retained():
    messages = build_prompt_messages(
        is_private    = False,
        bot_name      = "小青",
        sender_name   = "阿夏",
        think_level   = 0,
        history       = [],
        current_text  = "我们把东西收好就找不到叫隐身入库。小青，我入库成功，找了七分钟。",
        personality   = PersonalityConfig(),
        keyword_rules = [],
        regex_rules   = [],
        request_id    = "turn-reading",
    )
    assert "东西收好就找不到" not in messages[0].content
    assert "内容保持数据性质" in messages[-1].content
    assert '"meaning": "东西收好就找不到"' in messages[-1].content
    assert "我入库成功，找了七分钟" in messages[-1].content


def test_default_persona_card_moods_only_describe_current_conversation_style() -> None:
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig

    cfg   = XiaoQingChatConfig()
    moods = cfg.personality.card.moods
    assert moods
    assert all(mood.startswith("现在") for mood in moods)
    assert all(
        marker not in mood
        for mood in moods
        for marker in ("曾经", "上次", "去年", "最近", "刚刚", "已经")
    )
    assert cfg.fallback_idle_replies == ["我在听", "你接着说", "我想一下"]
    assert cfg.personality.allow_low_stakes_persona_fiction is True
    facts = cfg.personality.card.facts("小青")
    assert "女生" in facts and "大二" in facts and "物理" in facts
    assert "动画" in facts and "天文" in facts
    chat = cfg.personality.card.scoped(SCOPE_CHAT, "小青")
    assert "接梗往前推一步" in chat and "真想知道才问" in chat
    assert "不为了显得深入而升华" in cfg.personality.card.scoped(SCOPE_DEEP, "小青")
    assert cfg.memory.conversation_idle_gap_seconds == 1800.0


def test_prompt_builder_discourages_repetitive_clarifying_questions() -> None:
    personality = PersonalityConfig(
        polite_guardrail  = True,
        card              = _CARD,
        state_probability = 0.0,
    )

    msgs = build_prompt_messages(
        is_private    = False,
        bot_name      = "小青",
        sender_name   = "测试用户",
        think_level   = 1,
        history       = [],
        current_text  = "放弃下载了，太大了",
        personality   = personality,
        keyword_rules = [],
        regex_rules   = [],
        request_id    = "req-test",
    )

    system_prompt = msgs[0].content
    assert msgs[0].role == "system"
    assert msgs[1].role == "user"
    assert "住校的理工科学生。" in system_prompt
    assert "- 口语化" in system_prompt
    assert "放弃下载了，太大了" in msgs[1].content
    assert "放弃下载了，太大了" not in system_prompt
    assert "测试用户" in msgs[1].content


def test_prompt_builder_can_disable_persona_story_creation() -> None:
    personality = PersonalityConfig(
        card                             = _CARD,
        allow_low_stakes_persona_fiction = False,
    )

    msgs = build_prompt_messages(
        is_private    = False,
        bot_name      = "小青",
        sender_name   = "测试用户",
        think_level   = 0,
        history       = [],
        current_text  = "大家讲个近况",
        personality   = personality,
        keyword_rules = [],
        regex_rules   = [],
        request_id    = "strict-persona",
    )

    system_prompt = msgs[0].content
    assert "日常创作许可" not in system_prompt
    assert "没有明确写出的具体往事" in system_prompt


def test_brain_chat_keeps_base_persona_and_adds_mode_rules() -> None:
    from plugins.xiaoqing_chat.brain_chat import persona_reply_style
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig

    cfg     = XiaoQingChatConfig()
    runtime = type("Runtime", (), {"cfg": cfg})()
    card    = cfg.personality.card

    # 深度模式只在聊天说话方式之后追加〔深度对话〕一节，人还是同一个。
    assert persona_reply_style(runtime, False, "小青") == card.scoped(SCOPE_CHAT, "小青")
    deep = persona_reply_style(runtime, True, "小青")
    assert deep.startswith(card.scoped(SCOPE_CHAT, "小青"))
    assert deep.endswith(card.scoped(SCOPE_DEEP, "小青"))


def test_prompt_builder_does_not_include_user_id_in_name() -> None:
    personality = PersonalityConfig(
        polite_guardrail  = True,
        card              = _CARD,
        state_probability = 0.0,
    )

    msgs = build_prompt_messages(
        is_private  = False,
        bot_name    = "小青",
        sender_name = "测试用户",
        think_level = 1,
        history     = [
            StoredMessage(
                role    = "user",
                name    = "测试用户",
                content = "你好",
                ts      = 1700000000.0,
                user_id = 123456,
            )
        ],
        current_text  = "在吗",
        personality   = personality,
        keyword_rules = [],
        regex_rules   = [],
        request_id    = "req-test-2",
    )

    user_prompt = msgs[1].content
    assert "测试用户(123456)" not in user_prompt


def test_prompt_builder_treats_media_markers_as_real_content() -> None:
    personality = PersonalityConfig(
        polite_guardrail  = True,
        card              = _CARD,
        state_probability = 0.0,
    )

    msgs = build_prompt_messages(
        is_private    = False,
        bot_name      = "小青",
        sender_name   = "测试用户",
        think_level   = 1,
        history       = [],
        current_text  = "[QQ表情：微笑]",
        personality   = personality,
        keyword_rules = [],
        regex_rules   = [],
        request_id    = "req-test-3",
    )

    system_prompt = msgs[0].content
    assert (
        "[图片：...]" in system_prompt
        or "[表情包：...]" in system_prompt
        or "[QQ表情：...]" in system_prompt
    )


def test_prompt_builder_rehydrates_media_marker_from_registry(tmp_path) -> None:
    personality = PersonalityConfig(
        polite_guardrail  = True,
        card              = _CARD,
        state_probability = 0.0,
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
        is_private  = False,
        bot_name    = "小青",
        sender_name = "测试用户",
        think_level = 1,
        history     = [
            StoredMessage(
                role        = "user",
                name        = "测试用户",
                content     = "[[xc_media_1]]",
                media_items = (
                    {
                        "kind": "emoji",
                        "media_hash": "hash-emoji-1",
                        "marker": "[表情包：一张表情包]",
                    },
                ),
                ts=1700000001.0,
            )
        ],
        current_text  = "你看懂没",
        personality   = personality,
        keyword_rules = [],
        regex_rules   = [],
        request_id    = "req-test-4",
    )

    user_prompt = msgs[1].content
    assert "猫猫翻白眼" in user_prompt
    assert "无语" in user_prompt
    assert "[表情包：一张表情包]" not in user_prompt
    assert "[[xc_media_1]]" not in user_prompt


def test_prompt_builder_prefers_canonical_parts_when_legacy_fields_are_stale(tmp_path) -> None:
    personality = PersonalityConfig(
        polite_guardrail  = True,
        card              = _CARD,
        state_probability = 0.0,
    )
    state = get_state()
    state.media_store.bind(tmp_path)

    msgs = build_prompt_messages(
        is_private  = False,
        bot_name    = "小青",
        sender_name = "测试用户",
        think_level = 1,
        history     = [
            StoredMessage(
                role        = "user",
                name        = "测试用户",
                content     = "[[xc_media_1]]",
                media_items = (
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
        current_text  = "在吗",
        personality   = personality,
        keyword_rules = [],
        regex_rules   = [],
        request_id    = "req-test-5",
    )

    user_prompt = msgs[1].content
    # 消息自身记录的画面描述随标签一起保留，不因注册表压缩而丢失。
    assert "先看这个[表情包：无语；内容：猫猫翻白眼]就知道了" in user_prompt
    assert "[表情包：一张表情包]" not in user_prompt


def test_prompt_builder_uses_media_only_reply_target_block_for_current_parts() -> None:
    personality = PersonalityConfig(
        polite_guardrail  = True,
        card              = _CARD,
        state_probability = 0.0,
    )

    msgs = build_prompt_messages(
        is_private    = False,
        bot_name      = "小青",
        sender_name   = "测试用户",
        think_level   = 1,
        history       = [],
        current_text  = "[图片：海边落日]",
        personality   = personality,
        keyword_rules = [],
        regex_rules   = [],
        current_parts = (
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
        polite_guardrail  = True,
        card              = _CARD,
        state_probability = 0.0,
    )

    msgs = build_prompt_messages(
        is_private    = True,
        bot_name      = "小青",
        sender_name   = "测试用户",
        think_level   = 1,
        history       = [],
        current_text  = "[表情包：佩服，调侃；写着“不愧是你 我佩服得鹉体投地”]",
        personality   = personality,
        keyword_rules = [],
        regex_rules   = [],
        current_parts = (
            {
                "kind": "emoji",
                "marker": "[表情包：佩服，调侃；写着“不愧是你 我佩服得鹉体投地”]",
                "description": "佩服，调侃",
            },
        ),
        request_id="req-test-emoji-speech",
    )

    user_prompt = msgs[1].content
    assert "现在测试用户借表情包表达：想说的话：不愧是你 我佩服得鹉体投地" in user_prompt
    assert "语气反应：佩服，调侃" in user_prompt
    assert "发送的表情包" not in user_prompt


def test_prompt_builder_treats_media_only_qq_face_as_tone() -> None:
    personality = PersonalityConfig(
        polite_guardrail  = True,
        card              = _CARD,
        state_probability = 0.0,
    )

    msgs = build_prompt_messages(
        is_private    = True,
        bot_name      = "小青",
        sender_name   = "测试用户",
        think_level   = 1,
        history       = [],
        current_text  = "[QQ表情：菜汪]",
        personality   = personality,
        keyword_rules = [],
        regex_rules   = [],
        current_parts = (
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
        polite_guardrail  = True,
        card              = _CARD,
        state_probability = 0.0,
    )

    msgs = build_prompt_messages(
        is_private    = False,
        bot_name      = "小青",
        sender_name   = "测试用户",
        think_level   = 1,
        history       = [],
        current_text  = "你看这个\n[表情包：无语]",
        personality   = personality,
        keyword_rules = [],
        regex_rules   = [],
        current_parts = (
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
    assert (
        "现在测试用户发送了表情包：[表情包：无语；内容：无语猫猫]，并说：你看这个。引起了你的注意"
        in user_prompt
    )


def test_prompt_builder_does_not_duplicate_current_turn_when_history_already_contains_it() -> None:
    personality = PersonalityConfig(
        polite_guardrail  = True,
        card              = _CARD,
        state_probability = 0.0,
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
            role  = "user",
            name  = "测试用户",
            parts = current_parts,
            ts    = 1700000003.0,
        )
    ]

    msgs = build_prompt_messages(
        is_private    = False,
        bot_name      = "小青",
        sender_name   = "测试用户",
        think_level   = 1,
        history       = history,
        current_text  = "你看这个\n[图片：海边落日]",
        personality   = personality,
        keyword_rules = [],
        regex_rules   = [],
        current_parts = current_parts,
        request_id    = "req-test-8",
    )

    user_prompt = msgs[1].content
    assert user_prompt.count("[图片：海边落日]") == 1
    assert user_prompt.count("你看这个") == 1


def test_history_budget_keeps_latest_correction():
    history = [StoredMessage("user", "u", i + 1, content="old" * 200) for i in range(11)]
    history.append(StoredMessage("user", "u", 20, content="LATEST_STOP_PLEASE"))
    prompt = build_dialogue_prompt(history, bot_name="小青", max_chars=250)
    assert "LATEST_STOP_PLEASE" in prompt


def test_text_followup_keeps_previous_image_evidence():
    history = [
        StoredMessage(
            role  = "user",
            name  = "群友",
            ts    = 1700000000.0,
            parts = (
                {
                    "kind": "image",
                    "description": "街边树木和写着大洗美术馆的招牌",
                    "marker": "[图片：街边树木和写着大洗美术馆的招牌]",
                },
            ),
        ),
    ]
    messages = build_prompt_messages(
        is_private             = False,
        bot_name               = "小青",
        sender_name            = "群友",
        think_level            = 1,
        history                = history,
        current_text           = "大洗工业是什么？",
        current_image_attached = False,
        personality            = PersonalityConfig(),
        keyword_rules          = [],
        regex_rules            = [],
        request_id             = "image-followup",
    )
    assert "大洗美术馆" in messages[1].content
    assert "历史含实际图像记录" in messages[0].content
    assert "历史中成功解析的对应摘要仍然可用" in messages[0].content
    assert "两处均无可用画面时" in messages[0].content
    assert "普通常识、作品背景和词义解释" in messages[0].content


@pytest.mark.parametrize(
    "identity,expected",
    [
        (
            "你是理工科学生。你对天文有兴趣，你也喜欢猫。",
            "我是理工科学生。我对天文有兴趣，我也喜欢猫",
        ),
        ("二十岁左右，是住校学生。你对天文有兴趣。", "二十岁左右，是住校学生。我对天文有兴趣"),
        ("我的昵称叫迷你青。朋友说你很活泼。", "我的昵称叫迷你青。朋友说你很活泼"),
    ],
)
def test_public_identity_renders_configured_subject_as_first_person(identity, expected):
    assert _concise_public_identity(identity) == expected
