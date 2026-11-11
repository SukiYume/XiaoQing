"""她发消息的样子：装饰十条里最多一条、长短跟着自己最近几条换、讲过的往事前后一致。"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.life import extract as life_extract
from plugins.xiaoqing_chat.life.self_timeline import (
    SelfTimelineStore,
    new_entry,
    self_timeline_block,
)
from plugins.xiaoqing_chat.llm.decorations import has_decoration, tame_decorations
from plugins.xiaoqing_chat.llm.prompt_builder import (
    build_dialogue_prompt,
    recent_length_note,
    recent_style_note,
)
from plugins.xiaoqing_chat.media.marker_resolver import COMMON_QQ_FACE_LABELS
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

NOW = 1_800_000_000.0


def _user(text: str, *, ts: float = NOW) -> StoredMessage:
    return StoredMessage(role="user", name="阿梨", ts=ts, user_id=7, content=text)


def _bot(text: str, *, ts: float = NOW) -> StoredMessage:
    return StoredMessage(role="assistant", name="小青", ts=ts, content=text)


def _tame(text: str, recent: list[str]) -> str:
    return tame_decorations(text, recent, COMMON_QQ_FACE_LABELS)


# ---------------------------------------------------------------- 装饰


@pytest.mark.parametrize(
    ("candidate", "recent", "expected"),
    [
        # 最近没用过：留着。
        ("懂不懂啊🙄", ["你才胖了", "想得美"], "懂不懂啊🙄"),
        ("好耶～", [], "好耶～"),
        # 最近用过：emoji、句尾波浪号、括号动作和 QQ 表情标记都去掉。
        ("懂不懂啊🙄", ["你才胖了🙄", "想得美"], "懂不懂啊"),
        ("好耶～", ["来了~"], "好耶"),
        ("有点不敢碰了（笑）", ["行吧（笑）"], "有点不敢碰了"),
        ("行吧 [想发QQ表情:流汗]", ["嗯🙄"], "行吧"),
        ("行吧", ["刚到[QQ表情：捂脸]"], "行吧"),
        # 数字之间的波浪号是内容。
        ("1~3 个都行", ["来了~"], "1~3 个都行"),
    ],
)
def test_decorations_follow_her_recent_replies(candidate, recent, expected) -> None:
    assert _tame(candidate, recent) == expected


def test_face_name_typed_as_text_becomes_a_real_face_or_is_dropped() -> None:
    assert _tame("别被卡池骗着吃保底了捂脸", []) == "别被卡池骗着吃保底了 [想发QQ表情:捂脸]"
    assert _tame("别被卡池骗着吃保底了捂脸", ["懂不懂啊🙄"]) == "别被卡池骗着吃保底了"
    # 已经带了别的素材标记，不再多挂一个表情。
    assert _tame("[想发表情:猫猫叹气] 笑死了捂脸", []) == "[想发表情:猫猫叹气] 笑死了"
    # 正常的话里出现表情名不动。
    for sentence in ("我只能捂脸", "今天又在摸鱼", "我直接裂开", "给你比心"):
        assert _tame(sentence, []) == sentence


def test_content_is_never_emptied_or_reformatted() -> None:
    # 对方就是要一个表情：去掉以后什么都不剩时保持原样。
    assert _tame("🙄", ["你才胖了🙄"]) == "🙄"
    assert _tame("[想发QQ表情:摸鱼]", ["嗯🙄"]) == "[想发QQ表情:摸鱼]"
    # 清单、代码和长文里的符号是内容。
    listing = "- ✅ 第一步\n- ✅ 第二步\n- ✅ 第三步\n- 第四步"
    assert _tame(listing, ["嗯🙄"]) == listing
    code = "```python\nprint('~')\n```"
    assert _tame(code, ["嗯~"]) == code


def test_only_the_recent_window_counts() -> None:
    old = ["好耶～", *["嗯"] * 6]
    assert _tame("来了～", old) == "来了～"
    assert has_decoration("好耶～") and not has_decoration("1~3 个")


def test_style_note_mentions_recent_decorations() -> None:
    history = [_bot("你才胖了🙄"), _user("哈哈"), _bot("想得美"), _user("略"), _bot("行吧")]
    assert "这条不带" in recent_style_note(history)


# ---------------------------------------------------------------- 长短


def test_length_note_after_two_long_replies_only() -> None:
    long_a  = "我那叫稳定给食堂捧场，胖不胖得看数据，不能靠你一张嘴判案啊"
    long_b  = "那推《比宇宙更远的地方》吧，四个女生一起去南极，细节回收特别好"
    history = [_bot(long_a), _user("哈哈"), _bot(long_b), _user("然后呢")]
    note    = recent_length_note(history, "然后呢")
    assert "十来个字" in note and "认真求助" in note
    # 上一条本来就短，或者对方明说了篇幅，就不提醒。
    assert recent_length_note([_bot(long_a), _user("哈"), _bot("想得美")], "哈") == ""
    assert recent_length_note(history, "写个200字的介绍") == ""
    assert recent_length_note(history, "用一句话说说") == ""
    assert recent_length_note([_bot(long_a)], "嗯") == ""


def test_dialogue_window_keeps_twenty_recent_messages() -> None:
    history = [_user(f"第{index}条", ts=NOW + index) for index in range(30)]
    dialogue = build_dialogue_prompt(history, bot_name="小青")
    assert "第10条" in dialogue and "第9条" not in dialogue
    assert dialogue.startswith("（前面的有点记不清了…）")
    assert "第25条" not in build_dialogue_prompt(history[:26], bot_name="小青", max_items=5)[:40]


# ---------------------------------------------------------------- 讲过的往事


def test_stories_outlive_states_and_are_listed_separately(tmp_path) -> None:
    store = SelfTimelineStore()
    store.bind(tmp_path)
    story = new_entry(
        chat_id = "g1",
        kind    = "story",
        text    = "宿舍里找充电线翻了半天，其实压在自己胳膊底下",
        hours   = 1,
        source  = "chat",
        now     = NOW,
    )
    assert story is not None and story.expires_at - story.ts >= 90 * 86400
    states = [
        new_entry(
            chat_id="g1", kind="state", text=f"在忙第{index}件事", hours=1, source="chat", now=NOW
        )
        for index in range(12)
    ]
    store.apply(added=[story, *states], chat_id="g1", max_entries=5, now=NOW)
    # 近况按条数裁，往事不和它们抢名额；别的群里也记得。
    assert [entry.kind for entry in store.entries()].count("state") == 5
    later = NOW + 30 * 86400
    assert store.visible("g2", now=later) == []
    assert [entry.text for entry in store.stories("g2", now=later)] == [story.text]
    assert store.visible("g1", now=NOW) and all(e.kind != "story" for e in store.visible("g1"))

    block = self_timeline_block([], stories=store.stories("g2", now=later), now=later)
    assert "你以前讲过的自己的事" in block and story.text in block
    assert self_timeline_block([], stories=[]) == ""


@pytest.mark.asyncio
async def test_a_told_story_is_recorded_and_cannot_be_resolved(tmp_path) -> None:
    life_extract._last_state_read.clear()
    state = ChatRuntimeState()
    state.self_timeline.bind(tmp_path)
    state.open_loops.bind(tmp_path)
    history = [
        _user("说件你在宿舍干过的笨事", ts=NOW - 30),
        _bot("有次我找充电线翻了半天，结果压在自己胳膊底下", ts=NOW),
    ]
    told   = {"kind": "story", "text": "宿舍里找充电线翻了半天，其实压在胳膊底下", "hours": 4000}
    moment = datetime(2027, 1, 14, 20, 0).timestamp()

    async def run(output: dict) -> None:
        with (
            patch.object(
                life_extract,
                "chat_completions",
                AsyncMock(return_value=json.dumps(output, ensure_ascii=False)),
            ),
            patch.object(life_extract.time, "time", return_value=moment),
        ):
            assert await life_extract.extract_life_records(
                state    = state,
                life_cfg = XiaoQingChatConfig().life,
                secrets  = {"_ai": object()},
                llm      = XiaoQingChatConfig().llm_config("reasoning"),
                chat_id  = "g1",
                bot_name = "小青",
                history  = history,
                source   = "chat",
            )

    await run({"self": [told]})
    stories = state.self_timeline.stories("g1", now=moment)
    assert [entry.text for entry in stories] == [told["text"]]
    # 同一件事再讲一遍不重复记；模型把它标成“办完了”也不会让它消失。
    await run({"self": [told], "resolved_self": [{"id": stories[0].entry_id, "status": "done"}]})
    assert len(state.self_timeline.stories("g1", now=moment)) == 1
    life_extract._last_state_read.clear()


# ---------------------------------------------------------------- 办事的回复


def test_inline_agent_reply_follows_the_same_decoration_budget() -> None:
    from plugins.xiaoqing_chat.agent_bridge import _tamed_segments

    reply   = [{"type": "text", "data": {"text": "奶茶那笔改成 15 元了～"}}]
    history = [
        {"role": "user", "text": "记两笔"},
        {"role": "assistant", "text": "两笔都记了，地铁那笔我先按现金记的～"},
    ]
    assert _tamed_segments(reply, history)[0]["data"]["text"] == "奶茶那笔改成 15 元了"
    assert _tamed_segments(reply, [])[0]["data"]["text"] == "奶茶那笔改成 15 元了～"
    # agent 这边没有表情素材：当文字打出来的表情名直接去掉；图片等其他段原样保留。
    mixed = [
        {"type": "text", "data": {"text": "灯关了捂脸"}},
        {"type": "image", "data": {"file": "file:///tmp/a.png"}},
    ]
    tamed = _tamed_segments(mixed, [])
    assert tamed[0]["data"]["text"] == "灯关了" and tamed[1] == mixed[1]
    assert reply[0]["data"]["text"].endswith("～"), "不改动 agent 交来的原对象"


# ---------------------------------------------------------------- 今天的安排


def test_day_plan_lists_todays_routine_in_time_order() -> None:
    from datetime import datetime as moment_type

    from plugins.xiaoqing_chat.life.schedule import (
        asks_about_routine,
        day_plan_text,
        parse_routine,
        routine_day_plan,
    )
    from plugins.xiaoqing_chat.persona import PersonaCard

    lines = (
        "周一至周五 00:30-07:20 睡觉（睡）",
        "周二、四 14:00-15:40 上课（忙）",
        "周一至周五 08:00-11:50 上课（忙）",
        "周六、周日 01:30-10:30 睡觉（睡）",
    )
    tuesday = moment_type(2026, 11, 10, 9, 0)
    plan    = day_plan_text(parse_routine(lines), tuesday)
    assert plan.startswith(
        "按作息你今天（周二）的安排：00:30-07:20 睡觉；08:00-11:50 上课；14:00-15:40 上课。"
    )
    assert "周六" not in plan and "（忙）" not in plan
    # 这一天没有条目，或人设卡没写作息：不给。
    assert day_plan_text(parse_routine(lines[1:2]), moment_type(2026, 11, 11, 9, 0)) == ""
    assert routine_day_plan(PersonaCard.parse("## 你是谁\n\n- 你叫 {{name}}。\n"), tuesday) == ""
    assert asks_about_routine("你明天有课吗") and not asks_about_routine("今天吃了啥")


# ---------------------------------------------------------------- 接她的话 / 放宽后的审查


@pytest.mark.asyncio
async def test_message_right_after_her_reply_skips_the_dice() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock, Mock

    from plugins.xiaoqing_chat.frequency_control import decide_reply

    runtime                                          = MagicMock()
    runtime.cfg.reply_probability_base               = 0.1
    runtime.cfg.min_reply_interval_seconds           = 0.0
    runtime.cfg.max_replies_per_minute               = 100
    runtime.cfg.heartflow.enable_heartflow           = False
    runtime.cfg.goal.enable_goal                     = False
    runtime.cfg.active_topic_min_reply_interval      = 0.0
    state                                            = MagicMock()
    state.get_last_reply_ts.return_value             = 0.0
    state.get_reply_timestamps.return_value          = []
    state.get_continuous_cooldown_until.return_value = 0.0
    state.goal_store.get_async = AsyncMock(return_value=None)
    state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
    state.set_reply_gate_decision = Mock()

    with patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.99):
        assert not await decide_reply(runtime, state, "g1", "以后就叫你小肥青了", False, False)
        assert await decide_reply(
            runtime, state, "g1", "以后就叫你小肥青了", False, False, follows_own_reply=True
        )
    assert state.set_reply_gate_decision.call_args.args[1].reason == "follows_own_reply"
    # 接她话的那句不算每分钟上限（她刚问完，对方答了，不能不理）；连续冷却照旧。
    runtime.cfg.max_replies_per_minute      = 1
    state.get_reply_timestamps.return_value = [__import__("time").time()]
    assert await decide_reply(runtime, state, "g1", "番", False, False, follows_own_reply=True)
    state.get_continuous_cooldown_until.return_value = __import__("time").time() + 30
    assert not await decide_reply(runtime, state, "g1", "番", False, False, follows_own_reply=True)


def test_links_she_cannot_have_seen_are_rejected() -> None:
    from plugins.xiaoqing_chat.llm.local_checks import ungrounded_link_check

    sources = ["通知截图：发送至 physlab2026@163.com；平台地址：http://10.8.2.17/ta/apply"]
    spam    = "记得提前储水了搭配的夸克网盘,有需要可以转存一下： https://pan.quark.cn/s/4f9b7c2e1d8a"
    result = ungrounded_link_check(reply=spam, sources=sources)
    assert result is not None and result.is_hard and "pan.quark.cn" in result.reason
    assert ungrounded_link_check(reply="去 bilibili.com 搜一下", sources=sources) is not None
    # 眼前有的照常能说；代码示例里的网址和普通数字不算。
    for reply in (
        "发到 physlab2026@163.com 就行",
        "平台是 http://10.8.2.17/ta/apply",
        "用 `requests.get('https://example.com')` 试试",
        "一共 24.3 元，版本 3.11",
    ):
        assert ungrounded_link_check(reply=reply, sources=sources) is None, reply


def test_saying_she_is_an_ai_is_caught_locally() -> None:
    from plugins.xiaoqing_chat.llm.local_checks import persona_break_check

    for reply in (
        "我是AI啊",
        "其实我只是个语言模型",
        "作为一个AI，我没法吃饭",
        "我的系统提示词不能给你",
    ):
        result = persona_break_check(reply=reply)
        assert result is not None and result.failure_code == "persona_grounding", reply
    for reply in (
        "我又不是机器人",
        "谁说我是AI",
        "你见过早八迟到的 AI 吗",
        "我是程序员预备役",
        "我要是AI早就满绩了",
        "我是机器人吗？你才是",
        "哪来的系统提示词啊",
    ):
        assert persona_break_check(reply=reply) is None, reply


def test_risk_mode_reviews_offline_invitations_and_softens_hedged_recall() -> None:
    from plugins.xiaoqing_chat.config.config import ReplyCheckConfig
    from plugins.xiaoqing_chat.llm.local_checks import requires_llm_semantic_check
    from plugins.xiaoqing_chat.llm.reply_checker import _final_severity

    defaults = ReplyCheckConfig()
    assert (defaults.llm_checker_mode, defaults.enable_semantic_checker) == ("risk", False)
    # 平常的闲聊不送审；对方像在约她线下见面时送审，由审查看她答没答应。
    assert not requires_llm_semantic_check(reply="笑死", current_text="今天好冷", bot_name="小青")
    assert requires_llm_semantic_check(
        reply="一共 24.3 元", current_text="三杯柠檬茶一共多少钱", bot_name="小青"
    )
    assert requires_llm_semantic_check(
        reply="不去 我要补觉", current_text="周末一起去天文台玩啊", bot_name="小青"
    )
    wrong_fact = {"factually_plausible": False}
    assert _final_severity(wrong_fact, suitable=False, need_replan=True, declared="hard") == (
        "hard",
        True,
    )
    assert _final_severity(
        wrong_fact, suitable=False, need_replan=True, declared="hard", hedged_recall=True
    ) == ("soft", True)


# ---------------------------------------------------------------- 心里的盘算不说出口


def test_explaining_why_she_speaks_is_rewritten() -> None:
    from plugins.xiaoqing_chat.llm.local_checks import narrated_motive_check

    leaked = "因为阿梨发的拉面图写着“深夜”，但现在才六点，这个反差挺好接话的"
    result = narrated_motive_check(reply=leaked, current_text="深夜放毒")
    assert result is not None and not result.is_hard and result.failure_code == "narrated_motive"
    # 对方问了原因，“因为……”就是正常回答；不以“因为”起头的不管。
    assert narrated_motive_check(reply="因为攒着看过瘾", current_text="为啥") is None
    assert narrated_motive_check(reply="因为早八啊", current_text="你怎么起这么早") is None
    assert narrated_motive_check(reply="才六点就深夜放毒啦", current_text="深夜放毒") is None


def test_planner_thought_is_not_posed_as_a_question_and_forced_turns_carry_none() -> None:
    from plugins.xiaoqing_chat.llm.prompt_builder import build_prompt_messages

    def prompt(reasoning: str) -> str:
        messages = build_prompt_messages(
            is_private        = False,
            bot_name          = "小青",
            sender_name       = "阿梨",
            think_level       = 0,
            history           = [_user("深夜放毒")],
            current_text      = "深夜放毒",
            personality       = XiaoQingChatConfig().personality,
            keyword_rules     = (),
            regex_rules       = (),
            planner_reasoning = reasoning,
            request_id        = "",
        )
        return messages[-1].content

    with_thought = prompt("可以接时间和价格的梗")
    assert "你刚才心里的盘算" in with_thought and "可以接时间和价格的梗" in with_thought
    assert "你为什么要回复这条消息" not in with_thought
    assert "心里的盘算" not in prompt("")


# ---------------------------------------------------------------- 表情不是每条都想带


def test_stickers_are_offered_on_request_and_only_sometimes_otherwise() -> None:
    from plugins.xiaoqing_chat.llm.prompt_builder import build_prompt_messages
    from plugins.xiaoqing_chat.reply_plan import offers_outbound_media

    plain = [_user("今天好冷"), _bot("确实 我都不想出被窝")]
    assert offers_outbound_media("小青发个狗头", plain, roll=0.99)
    assert offers_outbound_media("你也发个表情包表达一下心情", plain, roll=0.99)
    assert offers_outbound_media("今天好冷", plain, roll=0.0)
    assert not offers_outbound_media("今天好冷", plain, roll=0.9)
    # 她最近带过表情或发过表情包，这几条就不再给。
    for recent in ("笑死[QQ表情：捂脸]", "[表情包：猫猫瘫坐]", "好耶～"):
        assert not offers_outbound_media("今天好冷", [*plain, _bot(recent)], roll=0.0), recent

    def system(candidates: str) -> str:
        messages = build_prompt_messages(
            is_private       = False,
            bot_name         = "小青",
            sender_name      = "阿梨",
            think_level      = 0,
            history          = plain,
            current_text     = "今天好冷",
            personality      = XiaoQingChatConfig().personality,
            keyword_rules    = (),
            regex_rules      = (),
            media_candidates = candidates,
            request_id       = "",
        )
        return messages[0].content

    offered = system("可发素材：\n- QQ表情：捂脸")
    assert "[想发表情:" in offered and "可发素材" in offered
    withheld = system("")
    assert "想发" not in withheld and "这一条只发文字" in withheld


# ---------------------------------------------------------------- 长内容怎么分条


def _bubbles(text: str, **kwargs) -> list[str]:
    from plugins.xiaoqing_chat.config.config import ResponsePostProcessConfig
    from plugins.xiaoqing_chat.llm.postprocess import process_llm_response
    from plugins.xiaoqing_chat.message_parts import build_text_message_parts
    from plugins.xiaoqing_chat.reply_draft import build_reply_draft
    from plugins.xiaoqing_chat.reply_payload import build_reply_payload_from_parts

    parts = process_llm_response(text, ResponsePostProcessConfig(), bot_name="小青", **kwargs)
    draft = build_reply_draft(parts)
    assert draft is not None
    payload = build_reply_payload_from_parts(build_text_message_parts(draft.text))
    return [
        "".join(segment["data"]["text"] for segment in batch if segment["type"] == "text")
        for batch in payload.outbound_batches
    ]


def test_her_own_paragraphs_become_the_bubbles() -> None:
    explained = (
        '一般是访问字典时，指定的键不存在，比如 d["name"] 但 d 里没有 "name"\n\n'
        '先检查键名有没有拼错；如果键可能不存在，可以用 d.get("name")'
    )
    assert _bubbles(explained) == [
        '一般是访问字典时，指定的键不存在，比如 d["name"] 但 d 里没有 "name"',
        '先检查键名有没有拼错；如果键可能不存在，可以用 d.get("name")',
    ]
    # 段数超过气泡上限：最短的相邻两段并成一条，一段都不丢。
    assert _bubbles("第一段\n\n第二段\n\n第三段\n\n第四段比较长一点点\n\n五") == [
        "第一段\n第二段\n第三段",
        "第四段比较长一点点",
        "五",
    ]
    # 没分段的短回复照旧按句、按逗号拆。
    assert _bubbles("食堂天天去是因为人天天要吃饭啊，你这是破了什么大案？") == [
        "食堂天天去是因为人天天要吃饭啊",
        "你这是破了什么大案？",
    ]
    # 句子比气泡多：后面的几句连着写进最后一条，不一句一行。
    assert _bubbles("猫！家里有只叫小黑的奶牛猫。住校只能靠照片解馋了。想它。") == [
        "猫！",
        "家里有只叫小黑的奶牛猫",
        "住校只能靠照片解馋了。想它",
    ]


def test_a_long_single_paragraph_keeps_its_sentences_together() -> None:
    sentence = "傅里叶变换能告诉你一段波形里面有哪些频率、各有多强、相位又是怎样"
    bubbles  = _bubbles("。".join([sentence] * 6) + "。")
    assert len(bubbles) == 3
    assert "".join(bubbles).count(sentence) == 6
    assert all("\n" not in bubble for bubble in bubbles)
    # 长气泡是讲解：句子之间的句号照原样留着。
    assert any(bubble.count("。") >= 1 for bubble in bubbles)


def test_a_requested_document_is_sent_whole_with_its_punctuation() -> None:
    from plugins.xiaoqing_chat.reply_generator import _wants_whole_document

    assert _wants_whole_document("小青，帮我写个800字的检讨书")
    assert not _wants_whole_document("用一句话安利一部老番")
    assert not _wants_whole_document("五十字以内说说傅里叶变换")
    assert not _wants_whole_document("今天好冷")
    document = (
        "检讨书\n\n尊敬的老师：\n\n您好！\n\n"
        "我怀着认真反省的心情写下这份检讨。对于这次发生的事情，我认识到自己在态度上存在不足。\n\n"
        "回想事情的经过，我发现自己考虑得不够周全。\n\n检讨人：________"
    )
    assert _bubbles(document, enable_splitter=False) == [
        "检讨书\n尊敬的老师：\n您好！\n"
        "我怀着认真反省的心情写下这份检讨。对于这次发生的事情，我认识到自己在态度上存在不足。\n"
        "回想事情的经过，我发现自己考虑得不够周全。\n检讨人：________"
    ]


# ---------------------------------------------------------------- 句尾的垃圾词


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("今天干啥了，累成这样 白小姐", "今天干啥了，累成这样"),
        ("因为它困鸭 亚洲AV", "因为它困鸭"),
        ("你们玩得开心俺也去", "你们玩得开心"),
        ("这么累人的组会俺也去不了一点", "这么累人的组会"),
        ("怎么啦，今天忙什么了 久久精品", "怎么啦，今天忙什么了"),
        ("好，回聊，吃饭去吧助赢", "好，回聊，吃饭去吧"),
        # 正常的话不动；只剩垃圾词时也不清成空的，交给后面的检查。
        ("今天干啥了，累成这样", "今天干啥了，累成这样"),
        ("俺也去", "俺也去"),
    ],
)
def test_junk_words_dragged_onto_the_end_are_cut(reply, expected) -> None:
    from plugins.xiaoqing_chat.llm.decorations import strip_junk_tail

    assert strip_junk_tail(reply, "今天好累啊") == expected


def test_junk_filter_leaves_words_the_chat_is_actually_about() -> None:
    from plugins.xiaoqing_chat.llm.decorations import strip_junk_tail

    reply = "资料我放夸克网盘了"
    assert strip_junk_tail(reply, "你那份资料放哪了，夸克网盘吗") == reply
    # 垃圾词后面还有一整句，说明是在正经说这个词。
    long_tail = "白小姐是谁啊 我没听说过这个人欸"
    assert strip_junk_tail(long_tail, "") == long_tail


def test_chat_route_samples_conservatively() -> None:
    """top_p 放开后句尾会带出语料里的垃圾词；改这两个默认值前先重跑采样对比。"""

    from core.ai import resolve_route_config

    config = {
        "ai"     : {"models": {"m": {"provider": "p", "model": "m"}}},
        "plugins": {"xiaoqing_chat": {"ai": {"routes": {"chat": ["m"]}}}},
    }
    chat = resolve_route_config(config, "xiaoqing_chat", "chat")
    assert (chat.temperature, chat.top_p) == (0.8, 0.6)
