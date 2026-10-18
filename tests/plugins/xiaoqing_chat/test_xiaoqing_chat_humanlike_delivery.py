"""群聊活人感相关的确定性行为：时区、称呼、分句、复读、旧会话回忆与兜底口吻。"""

import json
import time

import pytest

import plugins.xiaoqing_chat.llm.arithmetic as arithmetic_mod
from plugins.xiaoqing_chat.config.config import ResponsePostProcessConfig
from plugins.xiaoqing_chat.handlers import _chat_text_with_addressee
from plugins.xiaoqing_chat.llm import reply_checker as checker
from plugins.xiaoqing_chat.llm.postprocess import process_llm_response
from plugins.xiaoqing_chat.llm.prompt_builder import (
    build_dialogue_prompt,
    build_earlier_context_block,
)
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.reply_fallbacks import (
    CONTEXT_GROUNDING_FALLBACKS,
    NOT_READY_FALLBACK,
    PERSONA_GROUNDING_FALLBACKS,
    PRESENCE_FALLBACK,
    UNSURE_FALLBACK,
)
from plugins.xiaoqing_chat.reply_generator import _strip_sender_vocative
from plugins.xiaoqing_chat.reply_payload import build_reply_payload_from_parts
from plugins.xiaoqing_chat.utils import wall_clock


def _text_batches(text: str) -> list[str]:
    payload = build_reply_payload_from_parts([{"kind": "text", "text": text}])
    return [
        "".join(segment["data"]["text"] for segment in batch if segment["type"] == "text")
        for batch in payload.outbound_batches
    ]


def test_wall_clock_uses_configured_timezone_instead_of_host_timezone():
    moment = 1_792_130_400.0  # 2026-10-16 06:00 UTC
    try:
        wall_clock.configure_wall_clock("Asia/Shanghai")
        assert wall_clock.describe_now(moment) == "2026-10-16 14:00（周五下午）"
        wall_clock.configure_wall_clock("America/Los_Angeles")
        assert wall_clock.describe_now(moment) == "2026-10-15 23:00（周四深夜）"
        wall_clock.configure_wall_clock("Not/AZone")
        assert wall_clock.format_local("%H:%M", moment) == "14:00"
    finally:
        wall_clock.configure_wall_clock("Asia/Shanghai")


@pytest.mark.parametrize(
    "raw,clean,at_me,expected",
    [
        ("小青你明天有课吗", "你明天有课吗", False, "小青，你明天有课吗"),
        ("[CQ:at,qq=1] 你明天有课吗", "你明天有课吗", True, "小青，你明天有课吗"),
        ("阿杰你明天有课吗", "阿杰你明天有课吗", False, "阿杰你明天有课吗"),
        ("小青，在吗", "小青，在吗", False, "小青，在吗"),
    ],
)
def test_addressee_survives_core_prefix_stripping(raw, clean, at_me, expected):
    event = {"message": raw, "raw_message": raw, "self_id": "1"}
    if at_me:
        event["message"] = [
            {"type": "at", "data": {"qq": "1"}},
            {"type": "text", "data": {"text": " " + clean}},
        ]
    assert _chat_text_with_addressee(clean, event, "小青") == expected


def test_sentence_split_keeps_repeated_marks_with_their_sentence():
    parts = process_llm_response(
        "恭喜阿杰！！！四级过了真的稳。真的假的？！",
        ResponsePostProcessConfig(),
        bot_name="小青",
    )
    assert parts == ["恭喜阿杰！！！", "四级过了真的稳。", "真的假的？！"]


def test_outbound_bubbles_drop_lone_full_stops_but_keep_tone_and_code():
    assert _text_batches("治不了。\n它已经赢了。") == ["治不了\n它已经赢了"]
    assert _text_batches("嗯。。。") == ["嗯。。。"]
    assert _text_batches("```py\nx = 1。\n```") == ["```py\nx = 1。\n```"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("阿杰，那你怎么答的", "那你怎么答的"),
        ("阿杰你这也太惨了", "阿杰你这也太惨了"),
        ("阿杰，", "阿杰，"),
    ],
)
def test_explicit_vocative_to_current_sender_is_removed(text, expected):
    assert _strip_sender_vocative(text, "阿杰") == expected


def test_reply_cannot_repeat_its_own_recent_sentence_verbatim():
    history = [
        StoredMessage(role="user", name="Momo", ts=1.0, content="猫又咬线了"),
        StoredMessage(role="assistant", name="小青", ts=2.0, content="这猫是拿耳机线当磨牙棒了吧"),
        StoredMessage(role="user", name="Momo", ts=3.0, content="小青你说怎么治"),
    ]
    common = {
        "history": history,
        "max_repeat_compare": 2,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 3,
    }
    repeated = checker.heuristic_check(
        reply="这猫是拿耳机线当磨牙棒了吧。收线吧，别跟猫较劲", **common
    )
    assert repeated is not None and "复述" in repeated.reason
    assert checker.heuristic_check(reply="收线吧，别跟猫较劲", **common) is None


def test_earlier_episode_is_offered_as_labelled_recollection():
    now   = time.time()
    block = build_earlier_context_block(
        [
            StoredMessage(
                role="user", name="Momo", ts=now - 30 * 3600, content="明天要去面试了好紧张"
            )
        ],
        bot_name = "小青",
        now      = now,
    )
    assert "约1天前" in block
    assert "明天要去面试了好紧张" in block
    assert build_earlier_context_block([], bot_name="小青") == ""


def test_single_latest_history_message_is_not_marked_as_forgotten():
    description = "[图片：" + "粉色背景上有黑色文字显示闭馆通知" * 6 + "]"
    dialogue    = build_dialogue_prompt(
        [StoredMessage(role="user", name="Momo", ts=time.time(), content=description)],
        bot_name="小青",
    )
    assert "记不清" not in dialogue


def _response(**changes):
    payload = {
        "suitable": True,
        "reason": "",
        "need_replan": False,
        "severity": "soft",
        "context_coherent": True,
        "speaker_correct": True,
        "instruction_followed": True,
        "persona_grounded": True,
        "factually_plausible": True,
        "non_template": True,
        "persona_scan_complete": True,
        "persona_claims": [],
        "context_scan_complete": True,
        "context_claims": [],
    }
    payload.update(changes)
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("kind,expected", [("creative", True), ("fact", False), (None, False)])
def test_context_claim_kind_separates_playful_proposals_from_asserted_facts(kind, expected):
    claim = {"claim": "奶茶的事你自己看着办吧", "source_ids": []}
    if kind:
        claim["kind"] = kind
    result = checker._interpret_checker_response(
        content=_response(context_claims=[claim]),
        reply                            = "牛啊！奶茶的事你自己看着办吧",
        current_text                     = "小青快夸我",
        history_text                     = "阿杰：我四级过了！！！",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
        source_records                   = {"h1": "阿杰：我四级过了！！！", "current": "小青快夸我"},
    )
    assert result.suitable is expected


@pytest.mark.parametrize("allow_fiction,expected", [(True, True), (False, False)])
def test_malformed_persona_evidence_is_not_stricter_than_omitted_evidence(allow_fiction, expected):
    result = checker._interpret_checker_response(
        content=_response(
            persona_claims=[
                {"claim": "有次半夜泡面忘了吃", "evidence": "住校的大二女生；可以即兴补充小片段"}
            ]
        ),
        reply                            = "有次半夜泡面忘了吃，第二天才发现",
        current_text                     = "小青说件笨事",
        history_text                     = "",
        grounding_text                   = "住校的大二女生。校园日常可以即兴补充小片段。",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = allow_fiction,
        source_records                   = {"current": "小青说件笨事"},
    )
    assert result.suitable is expected


def test_exhausted_fallbacks_stay_in_character():
    fallbacks = (
        *PERSONA_GROUNDING_FALLBACKS,
        *CONTEXT_GROUNDING_FALLBACKS,
        NOT_READY_FALLBACK,
        PRESENCE_FALLBACK,
        UNSURE_FALLBACK,
    )
    for text in fallbacks:
        assert not any(word in text for word in ("稍后再试", "人设", "配置", "无法确认", "审查"))


@pytest.mark.parametrize(
    "text,previous,expected",
    [
        ("下次一定", ["这周末爬山吗", "下次一定"], True),
        ("我超，原！", ["我超，原"], True),
        ("下次一定", ["这周末爬山吗"], False),
        ("这" * 40, ["这" * 40], False),
        ("下次一定", [], False),
    ],
)
def test_group_repeat_chain_is_recognized_from_previous_member(text, previous, expected):
    from plugins.xiaoqing_chat.participation import is_group_repeat_chain

    assert is_group_repeat_chain(text, previous) is expected


@pytest.mark.asyncio
async def test_repeat_chain_cue_reads_recent_history_excluding_current_turn():
    from types import SimpleNamespace

    from plugins.xiaoqing_chat.frequency_control import _repeat_chain_cue

    history = [
        StoredMessage(role="user", name="阿杰", ts=1.0, content="这周末要不要去爬山"),
        StoredMessage(role="user", name="Momo", ts=2.0, content="下次一定"),
        StoredMessage(role="user", name="北北", ts=3.0, content="下次一定"),
    ]

    async def get_recent_async(event_chat_id, *, max_items):
        return history[-max_items:]

    state = SimpleNamespace(memory_store=SimpleNamespace(get_recent_async=get_recent_async))
    assert await _repeat_chain_cue(state, "g1", "下次一定") == "repeat_chain"
    assert await _repeat_chain_cue(state, "g1", "周日吧") == ""


@pytest.mark.parametrize(
    "text,expected",
    [
        ("[图片：图片内容读取失败]", True),
        ("[表情包：动画表情]", True),
        ("[图片：一个女孩骑在白老虎上]", False),
        ("[图片：图片内容读取失败] 这个咋样", False),
    ],
)
def test_content_free_media_is_not_a_proactive_cue(text, expected):
    from plugins.xiaoqing_chat.participation import is_low_information_group_turn

    assert is_low_information_group_turn(text) is expected


@pytest.mark.parametrize(
    "changes,suitable,severity",
    [
        ({}, True, "soft"),
        ({"image_evidence_respected": "yes"}, True, "infra"),
        ({"visual_scan_complete": False}, True, "infra"),
    ],
)
def test_omitted_image_summary_flag_defers_to_the_verified_visual_inventory(
    changes, suitable, severity
):
    payload = {"visual_claims": [], "visual_scan_complete": True, **changes}
    result  = checker._interpret_checker_response(
        content                          = _response(**payload),
        reply                            = "刚放下手柄，中午打了两把",
        current_text                     = "有没有人刚打完游戏，来聊两句",
        history_text                     = "",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
        image_evidence_required          = True,
        source_records                   = {"current": "有没有人刚打完游戏，来聊两句"},
    )
    assert (result.suitable, result.severity) == (suitable, severity)


@pytest.mark.parametrize(
    "claim,expected",
    [
        ("一共是9加4.5，13块5", True),
        ("两杯热可可18块", True),
        ("两杯柠檬茶14块", False),
        ("一共20元", False),
    ],
)
def test_prices_computed_from_the_poster_are_supported(claim, expected):
    evidence = "热可可 12元 柠檬茶 9元 第二杯半价·仅同款参与"
    assert arithmetic_mod.measured_values_are_supported(claim, evidence) is expected


_MEME_PART = {
    "kind": "emoji",
    "media_hash": "a6ee",
    "marker": "[表情包：慵懒，放松]",
    "description": "橙色卡通猫趴着，文字内容是“我只是坐一下 一坐就是半小时”",
    "emotion_tags": ["慵懒", "放松"],
}


def test_meme_text_reaches_the_reply_model_in_current_turn_and_history():
    from plugins.xiaoqing_chat.llm.prompt_builder import _media_marker_for_current_turn
    from plugins.xiaoqing_chat.message_parts import render_message_parts

    expected = "[表情包：慵懒，放松；内容：橙色卡通猫趴着；写着“我只是坐一下 一坐就是半小时”]"
    assert _media_marker_for_current_turn(_MEME_PART) == expected
    # 注册表条目没有描述时，消息自己记录的画面摘要仍然生效。
    assert render_message_parts([{"kind": "text", "text": "看看"}, _MEME_PART]) == f"看看{expected}"


def test_older_image_outside_the_window_stays_quotable():
    from plugins.xiaoqing_chat.llm.prompt_builder import build_recent_media_block

    poster = StoredMessage(
        role  = "user",
        name  = "阿梨",
        parts = (
            {"kind": "text", "text": "路过看到的"},
            {
                "kind": "image",
                "marker": "[图片：饮品优惠卡片，文字内容是“地点：东门小广场”]",
                "description": "饮品优惠卡片，文字内容是“地点：东门小广场”",
            },
        ),
        ts=time.time() - 600,
    )
    chatter = [
        StoredMessage(
            role="user", name="阿禾", content=f"闲聊{index}", ts=time.time() - 500 + index
        )
        for index in range(15)
    ]
    history = [poster, *chatter]
    dialogue = build_dialogue_prompt(history, bot_name="小青")
    assert "东门" not in dialogue
    block = build_recent_media_block(history, bot_name="小青", dialogue=dialogue)
    assert "地点：东门小广场" in block
    assert build_recent_media_block(history[-3:], bot_name="小青", dialogue=dialogue) == ""


@pytest.mark.parametrize(
    "description,unreadable",
    [("图片内容读取失败", True), ("", True), ("橙色卡通猫趴着", False)],
)
def test_media_fallback_only_claims_a_loading_failure_when_nothing_was_read(
    description, unreadable
):
    from plugins.xiaoqing_chat.llm.media_evidence import event_media_all_unreadable

    part  = {"kind": "image", "marker": "[图片：x]", "description": description}
    event = {"_xc_effective_user_parts": [{"kind": "text", "text": "看"}, part]}
    assert event_media_all_unreadable(event) is unreadable
    assert event_media_all_unreadable({"_xc_effective_user_parts": []}) is False


@pytest.mark.parametrize(
    "hint,expected",
    [
        ("瘫着的猫", "cat"),
        ("慵懒", "cat"),
        ("猫摊成饼打哈欠", None),
        ("开心跳舞", None),
    ],
)
def test_sticker_hint_matches_by_meaningful_characters(hint, expected):
    from plugins.xiaoqing_chat.media.marker_resolver import find_candidate_by_hint

    entries = [
        ("dog", "柴犬歪头", ("疑惑",)),
        ("cat", "橙色卡通猫趴在蓝色物体上", ("慵懒", "放松")),
    ]
    found = find_candidate_by_hint(entries, hint, key_fn=lambda item: (item[1], *item[2]))
    assert (found[0] if found else None) == expected


@pytest.mark.parametrize("copy", ["name", "whole_label"])
def test_sticker_candidate_copied_into_marker_still_resolves(copy):
    from plugins.xiaoqing_chat.media.emoji_library import EmojiLibraryEntry
    from plugins.xiaoqing_chat.media.marker_resolver import (
        OUTBOUND_HINT_MAX_CHARS,
        _emoji_candidate_label,
        find_candidate_by_hint,
        parse_marker,
    )

    lazy = EmojiLibraryEntry(
        media_hash   = "lazy",
        file_path    = "lazy.png",
        description  = "一只橙色卡通猫软趴趴地瘫在蓝色长方形垫子上，眯着眼睛，神情慵懒放松",
        emotion_tags = ("慵懒", "放松"),
        usage_count  = 0,
        last_used_ts = 0.0,
        marker       = "[表情包：慵懒，放松]",
    )
    dog = EmojiLibraryEntry(
        media_hash   = "dog",
        file_path    = "dog.png",
        description  = "柴犬歪头，文字内容是“啊？”",
        emotion_tags = ("疑惑",),
        usage_count  = 0,
        last_used_ts = 0.0,
        marker       = "[表情包：疑惑]",
    )
    label = _emoji_candidate_label(lazy)
    name  = label.split("（", 1)[0]
    assert len(name) <= OUTBOUND_HINT_MAX_CHARS
    parsed = parse_marker(f"[想发表情:{name if copy == 'name' else label}]")
    assert parsed is not None
    found = find_candidate_by_hint(
        [dog, lazy],
        parsed.hint,
        key_fn=lambda item: (item.marker, item.description, *item.emotion_tags),
    )
    assert found is lazy


def test_qq_face_synonym_finds_catalog_label():
    from plugins.xiaoqing_chat.media.marker_resolver import find_candidate_by_hint

    faces = [("13", "呲牙"), ("14", "微笑"), ("20", "偷笑")]
    found = find_candidate_by_hint(faces, "笑脸", key_fn=lambda item: (item[1],))
    assert found is not None and "笑" in found[1]


def test_resending_the_same_sticker_on_request_is_not_a_verbatim_repeat():
    sticker = "[表情包：慵懒，满足；写着“我只是坐一下”]"
    history = [StoredMessage(role="assistant", name="小青", content=sticker, ts=time.time())]
    kwargs = {
        "history": history,
        "max_repeat_compare": 4,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 0,
    }
    assert checker.heuristic_check(reply=sticker, **kwargs) is None
    text = "这张猫图我笑了好久真的好久"
    history[0] = StoredMessage(role="assistant", name="小青", content=text, ts=time.time())
    assert checker.heuristic_check(reply=text, **kwargs) is not None


def test_visual_evidence_quote_tolerates_ocr_spacing():
    current = "[图片：通知，文字内容是“自习室调整通知\n10月18日（周日）\n09:00—12:00 暂停开放”]"
    result  = checker._interpret_checker_response(
        content=_response(
            visual_claims=[
                {
                    "claim": "周日九点到十二点暂停",
                    "evidence": "10月18日（周日） 09:00—12:00 暂停开放",
                }
            ],
            visual_scan_complete     = True,
            image_evidence_respected = True,
        ),
        reply                            = "周日九点到十二点暂停，想上午去的得改下午了",
        current_text                     = current,
        history_text                     = "",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
        image_evidence_required          = True,
        source_records                   = {"current": current},
    )
    assert result.suitable is True


def test_computed_price_may_rest_on_a_price_outside_the_cited_message():
    records = {
        "h1": "阿梨: [图片：价目表，文字内容是“柠檬茶 9元 第二杯半价”]",
        "h2": "小青(你): 柠檬茶挺划算",
    }
    result = checker._interpret_checker_response(
        content=_response(
            context_claims=[{"claim": "两杯柠檬茶原价18块", "source_ids": ["h2"], "kind": "fact"}]
        ),
        reply                            = "两杯柠檬茶原价18块，半价后13块5",
        current_text                     = "两杯柠檬茶呢",
        history_text                     = "\n".join(records.values()),
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
        source_records                   = {**records, "current": "两杯柠檬茶呢"},
    )
    assert result.suitable is True


def test_meme_summary_keeps_scene_and_caption_together():
    from plugins.xiaoqing_chat.llm.prompt_builder import _current_turn_target_block
    from plugins.xiaoqing_chat.message_parts import render_message_parts

    rendered = render_message_parts([_MEME_PART])
    assert "橙色卡通猫趴着" in rendered and "写着“我只是坐一下 一坐就是半小时”" in rendered
    meme_only = _current_turn_target_block(
        sender="阿禾", current_text="", current_parts=[_MEME_PART], bot_name="小青"
    )
    assert "画面：橙色卡通猫趴着" in meme_only and "我只是坐一下" in meme_only


@pytest.mark.parametrize(
    "evidence,claim,expected",
    [
        ("柠檬茶每杯9元，第二杯七折\n两杯一共多少钱", "两杯一共15.3元", True),
        ("柠檬茶每杯9元，第二杯七折\n两杯一共多少钱", "两杯一共16元", False),
        ("一杯柠檬茶9元\n十一杯一共多少钱", "十一杯一共99元", True),
        ("一杯柠檬茶9元\n十一杯一共多少钱", "十一杯一共100元", False),
        ("奶茶12元，全场八五折", "打完折10.2元", True),
        ("每杯9元，仅第二杯七折，其余原价\n三杯多少钱", "9+6.3+9，三杯24.3元", True),
        ("每杯9元，仅第二杯七折，其余原价\n三杯多少钱", "三杯一共25元", False),
        ("每杯9元，第二杯半价\n四个人四杯平摊", "四杯27元，每人6.75元", True),
    ],
)
def test_prices_follow_counts_and_discounts_stated_in_the_chat(evidence, claim, expected):
    assert arithmetic_mod.measured_values_are_supported(claim, evidence) is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我抽到限定了！！！！十连出金", "excited_share"),
        ("论文终于投出去了!!", "excited_share"),
        ("啊啊！！", ""),
        ("阿白，你抽到了！！", ""),
        ("今天天气不错", ""),
        ("发个狗头", "media_request"),
        ("来张梗图", "media_request"),
        ("阿白发个狗头", ""),
        ("发个链接给我", ""),
    ],
)
def test_excited_self_share_invites_group_reactions(text, expected):
    from plugins.xiaoqing_chat.participation import classify_group_participation_cue

    assert classify_group_participation_cue(text) == expected


@pytest.mark.parametrize(
    "claim,expected",
    [
        ("演出也很有意思，实拍和动画混着来", True),
        ("2026年10月17日", True),
        ("阿杰下周请客", False),
        ("阿杰说他下周请客", False),
        ("你上次也这么说", False),
    ],
)
def test_unsourced_claim_only_counts_as_dialogue_fact_when_it_concerns_people(claim, expected):
    result = checker._interpret_checker_response(
        content=_response(context_claims=[{"claim": claim, "source_ids": [], "kind": "fact"}]),
        reply                            = f"哈哈，{claim}",
        current_text                     = "小青你觉得呢",
        history_text                     = "17:37, 阿杰: 我在补孤独摇滚",
        grounding_text                   = "",
        bot_name                         = "小青",
        allow_low_stakes_persona_fiction = True,
        # 对话行带“时:分”前缀，日期里的“17”不能被当成人名。
        source_records={
            "h1": "17:37, 阿杰: 我在补孤独摇滚",
            "current": "小青你觉得呢",
        },
    )
    assert result.suitable is expected
