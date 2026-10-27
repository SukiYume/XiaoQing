"""不依赖模型的本地审查规则：复读、凭空人物经历、身份一致性、纯媒体回复，以及何时需要语义审查。"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence

from ..memory.memory import StoredMessage, is_conversation_reply
from ..message_parts import render_stored_message
from ..persona import persona_subject_pattern
from .check_result import ReplyCheckResult, persona_contract_rejection
from .evidence import (
    dialogue_evidence_fragments,
    direct_evidence_supports_claim,
    evidence_modality_is_preserved,
    normalize_text,
)
from .media_evidence import INBOUND_MEDIA_MARKER_RE, marker_visible_speech


def _last_bot_messages(history: Sequence[StoredMessage], *, limit: int) -> list[str]:
    out: list[str] = []
    for msg in reversed(history[-200:]):
        if not is_conversation_reply(msg):
            continue
        text = render_stored_message(msg)
        if not text:
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _repeat_key(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z一-鿿]+", "", str(text or ""))


_BRACKET_MARKER_RE = re.compile(r"\[[^\]\n]*\]")
# 去掉表情标记和标点后仍有这么高的重合，说明只是把旧句子改了几个字。
_NEAR_COPY_RATIO     = 0.75
_NEAR_COPY_MIN_CHARS = 12
# 短回复碰巧重合几个字更常见，要更高的重合才算照搬；“草”“晚安”这种再短的不比。
_SHORT_COPY_RATIO     = 0.8
_SHORT_COPY_MIN_CHARS = 8
_NEAR_COPY_LOOK_BACK  = 6


def _content_key(text: str) -> str:
    return _repeat_key(_BRACKET_MARKER_RE.sub("", str(text or "")))


def _near_copy_of_recent(reply: str, bot_msgs: Sequence[str]) -> bool:
    key = _content_key(reply)
    if len(key) < _SHORT_COPY_MIN_CHARS:
        return False
    threshold = _NEAR_COPY_RATIO if len(key) >= _NEAR_COPY_MIN_CHARS else _SHORT_COPY_RATIO
    return any(
        difflib.SequenceMatcher(None, key, _content_key(previous)).ratio() >= threshold
        for previous in bot_msgs[:_NEAR_COPY_LOOK_BACK]
    )


_OUTBOUND_MEDIA_ONLY_RE = re.compile(r"\[(?:表情包|QQ表情|图片)[:：][^\]\n]*\]")


def heuristic_check(
    *,
    reply: str,
    history: Sequence[StoredMessage],
    max_repeat_compare: int,
    similarity_threshold: float,
    max_assistant_in_row: int,
) -> ReplyCheckResult | None:
    r = normalize_text(reply)
    if not r:
        return ReplyCheckResult(False, "回复为空", True)

    max_look_back = max(_NEAR_COPY_LOOK_BACK, int(max_repeat_compare))
    bot_msgs = _last_bot_messages(history, limit=max_look_back)

    # 只发一张表情或图片时，被要求再发一次同款很正常，不按文字复读处理。
    media_only = not _OUTBOUND_MEDIA_ONLY_RE.sub("", reply).strip()
    if bot_msgs and max_repeat_compare > 0 and not media_only:
        for prev_msg in bot_msgs[: int(max_repeat_compare)]:
            last = normalize_text(prev_msg)
            if r == last:
                return ReplyCheckResult(False, "回复与之前机器人消息完全相同", True)
            sim = difflib.SequenceMatcher(None, r, last).ratio()
            if sim >= float(similarity_threshold):
                return ReplyCheckResult(False, f"回复与之前机器人消息高度相似({sim:.2f})", True)
        # 整体不相似时，也不能把自己刚说过的一整句原样再说一遍。
        recent = [_repeat_key(message) for message in bot_msgs[: int(max_repeat_compare)]]
        for clause in re.split(r"[。！？!?\n]+", r):
            key = _repeat_key(clause)
            if len(key) >= 8 and any(key in previous for previous in recent):
                return ReplyCheckResult(False, f"回复复述了自己刚说过的话：{clause.strip()}", True)
        if _near_copy_of_recent(reply, bot_msgs):
            return ReplyCheckResult(
                False, "回复几乎照搬了自己前面说过的话，换个说法确认，或补一点新内容", True
            )

    in_row = 0
    for msg in reversed(history[-40:]):
        if is_conversation_reply(msg):
            in_row += 1
            continue
        if msg.role == "assistant":
            continue
        break
    if max_assistant_in_row > 0 and in_row >= int(max_assistant_in_row):
        return ReplyCheckResult(False, "疑似消息轰炸（连续多条机器人发言）", True)

    return None


_FIRST_PERSON_HISTORY_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?:那|不过|其实|反正|说起来)?\s*"
    r"(?P<claim>我(?:自己)?(?:之前|以前|曾经|当时|那次|上次|去年|前年|小时候|"
    r"早些时候|前(?:[一二两三四五六七八九十\d]+|几|些)天)[^，。！？；;\n]{1,48})"
)


_RELATIONSHIP_NOUN = r"(?:室友|朋友|同学|家人|亲戚|同事|老师|导师|对象|伴侣|邻居|队友)"


_FIRST_PERSON_RELATIONSHIP_RE = re.compile(
    rf"(?:^|[，。！？；;\n])\s*(?P<claim>"
    rf"(?:我(?:有|的|跟|和|从|听|问)|跟|和|听|问)"
    rf"[^，。！？；;\n]{{0,10}}{_RELATIONSHIP_NOUN}"
    r"[^，。！？；;\n]{0,48})"
)


_FIRST_PERSON_EXPERIENCE_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?P<claim>"
    r"我(?:自己)?(?:也|还|就|真|确实|曾|曾经|以前|之前|最近|从没|从未|没|没有|并没|"
    r"可没|倒是|好像|大概|\s){0,5}"
    r"(?:去过|做过|学过|参加过|当过|用过|玩过|看过|听过|见过|认识过|"
    r"买过|养过|住过|工作过)[^，。！？；;\n]{0,36})"
)


_FIRST_PERSON_STABLE_HABIT_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?P<claim>"
    r"(?:"
    r"我(?:自己)?(?:一般|平时|平常|日常|通常|经常|总是|老是|一直|"
    r"向来|一向|往往|有时|偶尔|每次|嘴上|习惯)"
    r"|"
    r"(?:一般|平时|平常|日常|通常|经常|总是|老是|一直|向来|一向|"
    r"往往|有时|偶尔|每次)"
    r"[^，。！？；;\n]{0,8}我(?:自己)?"
    r")"
    r"[^，。！？；;\n]{1,48})"
)


_FIRST_PERSON_EXPERIENTIAL_BACKING_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?P<claim>"
    r"(?:按|据|照)?我(?:自己)?的(?:经验|经历|习惯|做法)"
    r"[^，。！？；;\n]{0,48})"
)


_FIRST_PERSON_CURRENT_ACTIVITY_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?P<claim>"
    r"我(?:自己)?(?:还|又|也|正好|刚好)?(?:正|正在|在|刚在|准备|打算)"
    r"[^，。！？；;\n]{1,48})"
)


_FIRST_PERSON_TIME_ANCHORED_STATE_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?P<claim>"
    r"(?:"
    r"我(?:自己)?(?:今天|今早|今晚|明天|明早|明晚|后天|这周|本周|下周|"
    r"这个周末|周末|待会儿?|等会儿?|一会儿?|稍后)"
    r"|"
    r"(?:今天|今早|今晚|明天|明早|明晚|后天|这周|本周|下周|"
    r"这个周末|周末|待会儿?|等会儿?|一会儿?|稍后)"
    r"[^，。！？；;\n]{0,8}我(?:自己)?"
    r")"
    r"[^，。！？；;\n]{1,48})"
)


_OMITTED_PERSONAL_EPISODE_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?:感觉|好像|像是)?\s*(?P<claim>"
    r"(?:今天|昨天|昨晚|最近|这周|上周|周末|刚才|刚刚|刚|"
    r"前(?:[一二两三四五六七八九十\d]+|几|些)天|"
    r"这两天|这阵子|那天)"
    r"[^。！？；;\n]{0,120}"
    r"(?:给我|让我|发现|遇到|碰到|赶上|拿下|"
    r"[^。！？；;\n]{0,48}(?:了|过|完|着)|(?:正?在)[\u4e00-\u9fff])"
    r"[^。！？；;\n]{0,40})"
)


_OMITTED_TIME_ANCHORED_STATE_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?:反正|不过|而且|正好|刚好|毕竟|还好|好在)?\s*"
    r"(?P<claim>"
    r"(?:今天|今早|今晚|早上|中午|下午|晚上|明天|明早|明晚|后天|"
    r"这周|本周|下周|周末|待会儿?|等会儿?|一会儿?|稍后|刚刚?|刚才)"
    r"[^，。！？；;\n]{0,36}"
    r"(?:就|才|再|又|已经|还(?:要|得|没)|准备|打算|不(?:用|必|需要)|无需)"
    r"[^，。！？；;\n]{1,40})"
)


def grounded_self_history_check(
    *,
    reply: str,
    grounding_text: str,
    check_omitted_episode: bool = False,
) -> ReplyCheckResult | None:
    """用通用语法结构拦截没有人物资料依据的第一人称往事。

    这里识别时间锚点、现实关系和完成体经历，不维护活动、商品或话题词表。
    低风险的当下观点、感受和能力不属于人物履历，交给正常对话自然表达。
    """

    normalized         = normalize_text(reply)
    evidence_fragments = dialogue_evidence_fragments(grounding_text)
    for pattern in (
        _FIRST_PERSON_HISTORY_RE,
        _FIRST_PERSON_RELATIONSHIP_RE,
        _FIRST_PERSON_EXPERIENCE_RE,
        _FIRST_PERSON_STABLE_HABIT_RE,
        _FIRST_PERSON_EXPERIENTIAL_BACKING_RE,
        _FIRST_PERSON_CURRENT_ACTIVITY_RE,
        _FIRST_PERSON_TIME_ANCHORED_STATE_RE,
    ):
        for match in pattern.finditer(normalized):
            claim = str(match.group("claim") or "").strip()
            if any(
                evidence_modality_is_preserved(claim, evidence)
                and direct_evidence_supports_claim(claim, evidence)
                for evidence in evidence_fragments
            ):
                continue
            return ReplyCheckResult(
                suitable     = False,
                reason       = "回复对受控人物资料没有支持的具体往事或现实关系作了肯定或否定陈述",
                need_replan  = True,
                severity     = "hard",
                failure_code = "persona_grounding",
            )
    if check_omitted_episode:
        for match in _OMITTED_PERSONAL_EPISODE_RE.finditer(normalized):
            claim = str(match.group("claim") or "").strip()
            if any(
                evidence_modality_is_preserved(claim, evidence)
                and direct_evidence_supports_claim(claim, evidence)
                for evidence in evidence_fragments
            ):
                continue
            return ReplyCheckResult(
                suitable     = False,
                reason       = "主动参与回复省略主语补写了受控人物资料没有支持的近期生活片段",
                need_replan  = True,
                severity     = "hard",
                failure_code = "persona_grounding",
            )
        # 同一条回复已经用“我”表明说话人时，另一个省略主语的时间安排或生活
        # 状态通常仍指向角色自己。这里只依据时间和体貌结构，不维护活动词表。
        if "我" in normalized:
            for match in _OMITTED_TIME_ANCHORED_STATE_RE.finditer(normalized):
                claim = str(match.group("claim") or "").strip()
                if any(
                    evidence_modality_is_preserved(claim, evidence)
                    and direct_evidence_supports_claim(claim, evidence)
                    for evidence in evidence_fragments
                ):
                    continue
                return ReplyCheckResult(
                    suitable     = False,
                    reason       = "主动参与回复用省略主语的时间结构补写了无依据现实状态或安排",
                    need_replan  = True,
                    severity     = "hard",
                    failure_code = "persona_grounding",
                )
    return None


_NAMED_PERSON_REFERENCE = r"(?:(?:小|老|阿)[\u4e00-\u9fff])"


_PERSON_REFERENCE = (
    rf"(?:你|您|他|她|他们|她们|我们|咱们|大家|所有人|群里(?:的人|大家)?|"
    rf"对方|人家|这人|那人|本人|{_NAMED_PERSON_REFERENCE})"
)


_PERSON_FACT_REFERENCE = (
    r"(?:之前|以前|曾经|当时|那次|上次|早些时候|前(?:几|些)天|过去|原先|"
    r"昨晚|昨天|今天|明天|周末|刚才|刚刚|前面|平时|一直|经常|通常|总是|"
    r"从来|已经|还没|没在|没说|没提|说过|提过|表示过|正在|正准备|准备|"
    r"打算|想要|想去|要去|会去)"
)


_PERSON_HISTORY_REFERENCE_RE = re.compile(
    rf"(?:^|[，。！？；;\n])\s*(?P<claim>"
    rf"(?:(?:{_PERSON_REFERENCE})[^，。！？；;\n]{{0,18}}(?:{_PERSON_FACT_REFERENCE})"
    rf"|(?:{_PERSON_FACT_REFERENCE})[^，。！？；;\n]{{0,18}}(?:{_PERSON_REFERENCE}))"
    r"[^，。！？；;\n]{1,48})"
)


_IMPLICIT_GROUP_STATE_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?P<claim>"
    r"(?:可能|也许|大概|或许|估计|看来|看样子)?\s*"
    r"(?:都|全都|一个个)(?:在|正|正在|要|想|准备|忙着)"
    r"[^，。！？；;\n]{1,48})"
)


def grounded_context_history_check(
    *,
    reply: str,
    dialogue_text: str,
) -> ReplyCheckResult | None:
    """拦截凭空添加的人物事实，只依据通用人物与状态结构判断风险。"""

    fragments  = dialogue_evidence_fragments(dialogue_text)
    normalized = normalize_text(reply)
    for pattern in (_PERSON_HISTORY_REFERENCE_RE, _IMPLICIT_GROUP_STATE_RE):
        for match in pattern.finditer(normalized):
            claim = str(match.group("claim") or "").strip()
            if any(
                evidence_modality_is_preserved(claim, evidence)
                and direct_evidence_supports_claim(claim, evidence)
                for evidence in fragments
            ):
                continue
            return ReplyCheckResult(
                suitable     = False,
                reason       = "回复添加了可见对话没有直接支持的人物经历、习惯、状态或打算",
                need_replan  = True,
                severity     = "hard",
                failure_code = "context_grounding",
            )
    return None


_MEDIA_FORM_RE = re.compile(r"(图片|这张图|那张图|表情包|QQ表情|表情|媒体)")


_INTENT_QUESTION_RE = re.compile(r"(啥意思|什么意思|几个意思|什么情况|干嘛|为何|为什么)")


def _current_media_only_anchors(current_text: str) -> tuple[str, ...]:
    text = str(current_text or "").strip()
    if not text:
        return ()
    anchors: list[str] = []
    for match in INBOUND_MEDIA_MARKER_RE.finditer(text):
        label = str(match.group(2) or "").strip()
        label = re.split(r"[；;]", label, maxsplit=1)[0].strip()
        if label:
            anchors.extend(part.strip() for part in re.split(r"[，,、\s]+", label) if part.strip())
        speech = marker_visible_speech(match.group(0))
        if speech:
            anchors.append(speech)
    if not anchors:
        return ()
    remainder = INBOUND_MEDIA_MARKER_RE.sub("", text).strip()
    if remainder:
        return ()
    deduped: list[str] = []
    for anchor in anchors:
        if anchor and anchor not in deduped:
            deduped.append(anchor)
    return tuple(deduped)


def media_meta_reply_check(*, reply: str, current_text: str) -> ReplyCheckResult | None:
    anchors = _current_media_only_anchors(current_text)
    if not anchors:
        return None

    normalized_reply = normalize_text(reply)
    if not normalized_reply:
        return None

    anchor_in_reply = any(
        anchor and anchor in normalized_reply for anchor in anchors if len(anchor) <= 40
    )
    media_form_mentioned = _MEDIA_FORM_RE.search(normalized_reply) is not None
    asks_about_intent    = _INTENT_QUESTION_RE.search(normalized_reply) is not None

    if media_form_mentioned and not anchor_in_reply:
        return ReplyCheckResult(
            False,
            "当前用户只发了媒体消息，回复主要围绕媒体形式本身，没有接住其中的文字、情绪或上下文意图",
            True,
        )

    if asks_about_intent:
        for anchor in anchors:
            if not anchor or len(anchor) > 12 or anchor not in normalized_reply:
                continue
            return ReplyCheckResult(
                False,
                "当前用户只发了媒体消息，回复把媒体摘要标签当作要解释的对象，而不是接住它表达的反应",
                True,
            )
    return None


EXPLICIT_COMMUNICATION_CONSTRAINT_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?:你|请|先|就|只|千万)?\s*"
    r"(?:别|不要|不用|不许|请勿|务必|必须|只能|只说|别再|不要再)"
)


_DURABLE_PERSONA_QUESTION_TAIL = (
    rf"[^，。！？；;\n]{{0,24}}"
    rf"(?:{_RELATIONSHIP_NOUN}|以前|之前|曾经|小时候|去过|做过|学过|当过|"
    r"认识过|住过|工作过|家住|来自|毕业|学校|大学|城市|老家|哪里人|"
    r"住哪|在哪读|专业|职业|生日|年龄|多大|几岁)"
)


# 人设里声明“不透露 / 没有设定”的句子；只有这些句子点名的资料才按保密处理。
_PRIVATE_PROFILE_POLICY_RE = re.compile(
    r"[^。；;\n]*(?:没有设定|未设定|不主动补|不能添加|不能编造|不在群里透露|不透露|保密)[^。；;\n]*"
)
# （保密句里点名的资料，追问这项资料的说法）
_PRIVATE_PROFILE_FIELDS = (
    ("学校", r"哪所(?:学校|大学|学院)|哪个学校|在哪上学"),
    ("城市", r"哪个城市|哪里人|老家|住哪|具体住址"),
    ("生日", r"生日"),
    ("家", r"家庭|父母|家人|家里人"),
    ("对象", r"对象|伴侣"),
)


_FEMALE_PERSONA_RE = re.compile(r"(?:女生|女性|女孩|女大学生)")


_MALE_PERSONA_RE = re.compile(r"(?:男生|男性|男孩|男大学生)")


_MALE_SELF_ASSERTION_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?:我|本人)[^，。！？；;\n]{0,10}"
    r"(?:是|算|这个)(?:男生|男人|男的|男大学生)"
)


_FEMALE_SELF_ASSERTION_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?:我|本人)[^，。！？；;\n]{0,10}"
    r"(?:是|算|这个)(?:女生|女人|女的|女大学生)"
)


_MALE_ADDRESS_IN_SELF_STORY_RE = re.compile(
    r"我[^。！？；;\n]{0,120}(?:说|喊|叫|来一句)[：:]?[“\"]"
    r"(?:小伙子|哥们|兄弟|帅哥|先生)"
)


_FEMALE_ADDRESS_IN_SELF_STORY_RE = re.compile(
    r"我[^。！？；;\n]{0,120}(?:说|喊|叫|来一句)[：:]?[“\"]"
    r"(?:姑娘|妹子|姐妹|美女|女士)"
)


_THIRD_PARTY_REFERENCE = (
    rf"(?:他|她|他们|她们|大家|所有人|群里(?:的人|大家)?|对方|人家|这人|那人|"
    rf"{_NAMED_PERSON_REFERENCE})"
)


_SOCIAL_CONTEXT_QUERY_RE = re.compile(
    rf"(?:{_THIRD_PARTY_REFERENCE})[^，。！？；;\n]{{0,28}}"
    r"(?:是不是|是否|会不会|有没有|为什么|怎么回事|在干嘛|都在|平时|一直|"
    r"经常|通常|总是|状态|情况|原因|忙什么|干什么|做什么|去哪(?:儿)?|"
    r"怎么(?:没|不|这么)|没动静|安静)"
)


_OPEN_GROUP_INVITATION_START_RE = re.compile(r"^(?:大家|各位|你们|有没有人|有人|谁|群里有人)")


_GROUP_INFERENCE_QUESTION_RE = re.compile(r"(?:是不是|为什么|怎么回事|都在|状态|情况|原因)")


_STABLE_PERSONA_REPLY_PREFIX = r"(?:^|[，。！？；;\n])\s*"


_STABLE_PERSONA_REPLY_TAIL = (
    r"[^，。！？；;\n]{0,18}"
    r"(?:一直|平时|经常|通常|总是|从来|习惯|家住|来自|毕业|学校|大学|"
    r"城市|老家|住址|专业|职业|生日|年龄)"
)


def _is_open_group_invitation(text: str) -> bool:
    return bool(
        _OPEN_GROUP_INVITATION_START_RE.search(text)
        and _GROUP_INFERENCE_QUESTION_RE.search(text) is None
    )


def requires_configured_profile_boundary(
    current_text: str,
    grounding_text: str,
    *,
    bot_name: str,
) -> bool:
    """判断用户是否追问了当前人设明确保留为空的可核验资料。

    只看人设保密句里点名的那几项：人设写明了单位或学校时，问学校就照人设答，不算越界。
    """

    policy   = "。".join(_PRIVATE_PROFILE_POLICY_RE.findall(str(grounding_text or "")))
    question = normalize_text(current_text)
    subject  = persona_subject_pattern(bot_name)
    return any(
        keyword in policy and re.search(f"{subject}[^，。！？；;\\n]{{0,30}}(?:{asked})", question)
        for keyword, asked in _PRIVATE_PROFILE_FIELDS
    )


def persona_identity_consistency_check(
    *,
    reply: str,
    grounding_text: str,
) -> ReplyCheckResult | None:
    """拦截日常创作中与稳定性别身份直接冲突的自称或受称。"""

    candidate = normalize_text(reply)
    grounding = str(grounding_text or "")
    if _FEMALE_PERSONA_RE.search(grounding) and (
        _MALE_SELF_ASSERTION_RE.search(candidate)
        or _MALE_ADDRESS_IN_SELF_STORY_RE.search(candidate)
    ):
        return persona_contract_rejection("回复中的自称或他人称呼与已设定的女性身份冲突")
    if _MALE_PERSONA_RE.search(grounding) and (
        _FEMALE_SELF_ASSERTION_RE.search(candidate)
        or _FEMALE_ADDRESS_IN_SELF_STORY_RE.search(candidate)
    ):
        return persona_contract_rejection("回复中的自称或他人称呼与已设定的男性身份冲突")
    return None


def requires_llm_semantic_check(
    *,
    reply: str,
    current_text: str,
    bot_name: str,
    proactive_persona_scan: bool = False,
) -> bool:
    """只把需要语义判断的高风险社交陈述交给远程检查器。

    普通知识问答、即时看法和简短承接由生成模型与确定性门禁负责，避免每条消息
    再串行等待一次远程模型。涉及用户明确表达方式、人物履历、第三方事实或媒体
    交际作用时，仍使用完整语义审查。
    """

    current   = normalize_text(current_text)
    candidate = normalize_text(reply)
    if EXPLICIT_COMMUNICATION_CONSTRAINT_RE.search(current):
        return True
    if re.search(
        persona_subject_pattern(bot_name) + _DURABLE_PERSONA_QUESTION_TAIL,
        current,
    ):
        return True
    # 主动插话中的第一人称陈述很容易把观点写成现实经历或日程。确定性门禁
    # 负责常见句法，语义检查再区分低风险观点与事实性自述。
    if proactive_persona_scan and "我" in candidate:
        return True
    social_current      = current
    normalized_bot_name = str(bot_name or "").strip()
    if normalized_bot_name:
        social_current = re.sub(
            rf"^\s*{re.escape(normalized_bot_name)}\s*[，,:：、]?\s*",
            "",
            social_current,
            count=1,
        )
    if _SOCIAL_CONTEXT_QUERY_RE.search(social_current) and not _is_open_group_invitation(
        social_current
    ):
        return True
    if any(
        pattern.search(candidate)
        for pattern in (
            _PERSON_HISTORY_REFERENCE_RE,
            _IMPLICIT_GROUP_STATE_RE,
            _FIRST_PERSON_RELATIONSHIP_RE,
            _FIRST_PERSON_EXPERIENCE_RE,
            _FIRST_PERSON_STABLE_HABIT_RE,
            _FIRST_PERSON_EXPERIENTIAL_BACKING_RE,
            _FIRST_PERSON_CURRENT_ACTIVITY_RE,
            _FIRST_PERSON_TIME_ANCHORED_STATE_RE,
            re.compile(
                _STABLE_PERSONA_REPLY_PREFIX
                + persona_subject_pattern(bot_name, include_first_person=True)
                + _STABLE_PERSONA_REPLY_TAIL
            ),
        )
    ):
        return True
    return bool(
        INBOUND_MEDIA_MARKER_RE.search(current) or INBOUND_MEDIA_MARKER_RE.search(candidate)
    )
