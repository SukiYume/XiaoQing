from __future__ import annotations

import difflib
import logging as _logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from core.ai import AIError as LLMError

from ..memory.memory import StoredMessage
from ..message_parts import render_stored_message
from ..utils.json_parsing import parse_first_json_object, strict_json_bool
from . import llm_client
from .gateway import chat_completions_raw_with_fallback_paths

_log = _logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplyCheckResult:
    suitable: bool
    reason: str
    need_replan: bool
    # hard：上下文、说话人、人物经历、事实或结构错误，任何情况下都不能发送。
    # soft：口癖、措辞、节奏等风格问题，重生成耗尽后可在强制回复场景谨慎采用。
    # infra：远程检查器超时、不可用或返回了无效协议；确定性检查已通过，可受控放行。
    severity: str = "hard"
    # 只标识调用方能够安全恢复的通用失败类别，不传递具体话题或个案。
    failure_code: str = ""
    persona_claim_count: int = 0
    context_claim_count: int = 0

    @property
    def is_hard(self) -> bool:
        return not self.suitable and self.severity == "hard"


@dataclass(frozen=True)
class _LLMCheckInput:
    """一次语义审查的不可变内容，避免把提示词字段误当成 transport 配置。"""

    bot_name: str
    reply: str
    goal: str
    current_text: str
    policy_text: str
    grounding_text: str
    history_text: str
    allow_low_stakes_persona_fiction: bool


@dataclass(frozen=True)
class _LLMRequestPolicy:
    """远端检查器的预算与重试策略；不参与内容证据判定。"""

    max_tokens: int
    timeout_seconds: float
    max_retry: int
    retry_interval_seconds: float


def _checker_unavailable(reason: str) -> ReplyCheckResult:
    """检查服务故障时保留确定性门禁，但不把基础设施故障误判成内容错误。"""

    return ReplyCheckResult(
        suitable=True,
        reason=reason,
        need_replan=False,
        severity="infra",
    )


class ReplyRejected(RuntimeError):
    def __init__(self, reason: str, need_replan: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.need_replan = need_replan


def _last_bot_messages(history: Sequence[StoredMessage], *, bot_name: str, limit: int) -> list[str]:
    out: list[str] = []
    for msg in reversed(history[-200:]):
        if msg.role != "assistant":
            continue
        name = (msg.name or "").strip()
        if name and bot_name and name != bot_name:
            continue
        text = render_stored_message(msg)
        if not text:
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _normalize_text(s: str) -> str:
    t = (s or "").strip()
    # 中文省略号也承担分句作用。先归一化，避免人物事实藏在“……”后绕过
    # 只从句首扫描的确定性门禁。
    t = re.sub(r"…+", "。", t)
    t = re.sub(r"\s+", " ", t)
    return t


def _heuristic_check(
    *,
    reply: str,
    history: Sequence[StoredMessage],
    bot_name: str,
    max_repeat_compare: int,
    similarity_threshold: float,
    max_assistant_in_row: int,
) -> ReplyCheckResult | None:
    r = _normalize_text(reply)
    if not r:
        return ReplyCheckResult(False, "回复为空", True)

    max_look_back = max(4, int(max_repeat_compare))
    bot_msgs = _last_bot_messages(history, bot_name=bot_name, limit=max_look_back)

    if bot_msgs and max_repeat_compare > 0:
        for prev_msg in bot_msgs[: int(max_repeat_compare)]:
            last = _normalize_text(prev_msg)
            if r == last:
                return ReplyCheckResult(False, "回复与之前机器人消息完全相同", True)
            sim = difflib.SequenceMatcher(None, r, last).ratio()
            if sim >= float(similarity_threshold):
                return ReplyCheckResult(False, f"回复与之前机器人消息高度相似({sim:.2f})", True)

    in_row = 0
    for msg in reversed(history[-40:]):
        if msg.role == "assistant":
            in_row += 1
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


def _normalize_evidence_text(text: str) -> str:
    """去掉表面格式，便于判断同一段经历是否真的出现在受控资料中。"""

    normalized = re.sub(r"[\s，。！？；;：:'\"“”‘’（）()【】\[\]]+", "", str(text or ""))
    return normalized.replace("小青", "我")


def _grounded_self_history_check(
    *,
    reply: str,
    grounding_text: str,
    check_omitted_episode: bool = False,
) -> ReplyCheckResult | None:
    """用通用语法结构拦截没有人物资料依据的第一人称往事。

    这里识别时间锚点、现实关系和完成体经历，不维护活动、商品或话题词表。
    低风险的当下观点、感受和能力不属于人物履历，交给正常对话自然表达。
    """

    normalized = _normalize_text(reply)
    evidence_fragments = _dialogue_evidence_fragments(grounding_text)
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
                _evidence_modality_is_preserved(claim, evidence)
                and _direct_evidence_supports_claim(claim, evidence)
                for evidence in evidence_fragments
            ):
                continue
            return ReplyCheckResult(
                suitable=False,
                reason="回复对受控人物资料没有支持的具体往事或现实关系作了肯定或否定陈述",
                need_replan=True,
                severity="hard",
                failure_code="persona_grounding",
            )
    if check_omitted_episode:
        for match in _OMITTED_PERSONAL_EPISODE_RE.finditer(normalized):
            claim = str(match.group("claim") or "").strip()
            if any(
                _evidence_modality_is_preserved(claim, evidence)
                and _direct_evidence_supports_claim(claim, evidence)
                for evidence in evidence_fragments
            ):
                continue
            return ReplyCheckResult(
                suitable=False,
                reason="主动参与回复省略主语补写了受控人物资料没有支持的近期生活片段",
                need_replan=True,
                severity="hard",
                failure_code="persona_grounding",
            )
        # 同一条回复已经用“我”表明说话人时，另一个省略主语的时间安排或生活
        # 状态通常仍指向角色自己。这里只依据时间和体貌结构，不维护活动词表。
        if "我" in normalized:
            for match in _OMITTED_TIME_ANCHORED_STATE_RE.finditer(normalized):
                claim = str(match.group("claim") or "").strip()
                if any(
                    _evidence_modality_is_preserved(claim, evidence)
                    and _direct_evidence_supports_claim(claim, evidence)
                    for evidence in evidence_fragments
                ):
                    continue
                return ReplyCheckResult(
                    suitable=False,
                    reason="主动参与回复用省略主语的时间结构补写了无依据现实状态或安排",
                    need_replan=True,
                    severity="hard",
                    failure_code="persona_grounding",
                )
    return None


_EVIDENCE_NEGATION_RE = re.compile(r"(?:不|没|无|未|非|否)")
_QUESTION_EVIDENCE_RE = re.compile(r"[?？]|(?:吗|是否|有没有|是不是|能否|会不会|该不该)")
_QUESTION_CLAIM_RE = re.compile(
    r"[?？]|(?:问|询问|想知道|想确认|确认一下|吗|是否|有没有|是不是|能否|会不会|该不该)"
)
_HYPOTHETICAL_RE = re.compile(r"(?:如果|假设|假如|要是|倘若|若是)")
_UNCERTAINTY_RE = re.compile(r"(?:可能|也许|大概|或许|似乎|好像|不一定|未必)")
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
_CLAIM_SCAFFOLD_RE = re.compile(
    r"^(?:我|你|她|他|本人|自己|角色)(?:自己)?|(?:是一个|是一名|是一位|就是|属于|是)"
)


def _claim_evidence_core(text: str) -> str:
    """只去掉人称和系词脚手架，保留时间、否定、数量和事实内容。"""

    normalized = re.sub(
        r"[^0-9A-Za-z\u4e00-\u9fff]+",
        "",
        str(text or ""),
    )
    previous = ""
    while normalized and normalized != previous:
        previous = normalized
        normalized = _CLAIM_SCAFFOLD_RE.sub("", normalized)
    return normalized


def _direct_evidence_supports_claim(claim: str, evidence: str) -> bool:
    """核对两段原文是否至少表达同一个明示事实。

    这不做领域语义推断：先要求 evidence 是受控资料原文，再核对极性和文字
    覆盖。宽松的双字组覆盖只用于容纳人称、系词和轻微语序差异，不允许用一段
    相关但不同义的身份资料支持新经历。
    """

    claim_core = _claim_evidence_core(claim)
    evidence_core = _claim_evidence_core(evidence)
    if not claim_core or not evidence_core:
        return False
    if bool(_EVIDENCE_NEGATION_RE.search(claim_core)) != bool(
        _EVIDENCE_NEGATION_RE.search(evidence_core)
    ):
        return False
    if claim_core in evidence_core or evidence_core in claim_core:
        return True
    if len(claim_core) < 4:
        return False
    claim_bigrams = {claim_core[index : index + 2] for index in range(len(claim_core) - 1)}
    evidence_bigrams = {evidence_core[index : index + 2] for index in range(len(evidence_core) - 1)}
    shared = claim_bigrams & evidence_bigrams
    return len(shared) >= 2 and len(shared) / len(claim_bigrams) >= 0.6


def _evidence_modality_is_preserved(claim: str, evidence: str) -> bool:
    """证据中的疑问、条件和不确定性不能在转述时被悄悄抹掉。"""

    if _QUESTION_EVIDENCE_RE.search(evidence) and not _QUESTION_CLAIM_RE.search(claim):
        return False
    for marker in (_HYPOTHETICAL_RE, _UNCERTAINTY_RE):
        if marker.search(evidence) and not marker.search(claim):
            return False
    return True


def _dialogue_evidence_fragments(dialogue_text: str) -> tuple[str, ...]:
    """拆出可直接核对的对话行、正文和引文，不做人物或话题推断。"""

    fragments: list[str] = []
    for raw_line in str(dialogue_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fragments.append(line)
        _prefix, separator, content = line.partition("：")
        if not separator:
            _prefix, separator, content = line.partition(":")
        if separator and content.strip():
            fragments.append(content.strip())
        fragments.extend(
            quoted.strip()
            for quoted in re.findall(r"[“\"]([^”\"]{1,160})[”\"]", line)
            if quoted.strip()
        )
    return tuple(dict.fromkeys(fragments))


def _grounded_context_history_check(
    *,
    reply: str,
    dialogue_text: str,
) -> ReplyCheckResult | None:
    """拦截凭空添加的人物事实，只依据通用人物与状态结构判断风险。"""

    fragments = _dialogue_evidence_fragments(dialogue_text)
    normalized = _normalize_text(reply)
    for pattern in (_PERSON_HISTORY_REFERENCE_RE, _IMPLICIT_GROUP_STATE_RE):
        for match in pattern.finditer(normalized):
            claim = str(match.group("claim") or "").strip()
            if any(
                _evidence_modality_is_preserved(claim, evidence)
                and _direct_evidence_supports_claim(claim, evidence)
                for evidence in fragments
            ):
                continue
            return ReplyCheckResult(
                suitable=False,
                reason="回复添加了可见对话没有直接支持的人物经历、习惯、状态或打算",
                need_replan=True,
                severity="hard",
                failure_code="context_grounding",
            )
    return None


def _persona_contract_rejection(reason: str) -> ReplyCheckResult:
    """人物证据协议不完整时失败关闭，使明确点名场景可安全承接。"""

    return ReplyCheckResult(
        suitable=False,
        reason=reason,
        need_replan=True,
        severity="hard",
        failure_code="persona_grounding",
    )


def _context_contract_rejection(reason: str) -> ReplyCheckResult:
    """对话事实来源协议不完整时失败关闭。"""

    return ReplyCheckResult(
        suitable=False,
        reason=reason,
        need_replan=True,
        severity="hard",
        failure_code="context_grounding",
    )


def _validate_evidence_contract(
    obj: dict[str, Any],
    *,
    scan_key: str,
    claims_key: str,
    reply: str,
    evidence_source: str,
    rejection_factory: Callable[[str], ReplyCheckResult],
    incomplete_reason: str,
    missing_reason: str,
    invalid_reason: str,
    preserve_modality: bool,
    allow_missing_evidence: bool = False,
) -> tuple[ReplyCheckResult | None, bool, int]:
    """统一验证声明协议。

    对话事实始终要求直接证据。人物日常创作开启时，角色声明可以没有证据，
    但仍必须是回复原文，并由语义轴判断是否属于允许的低风险人设发挥。
    """

    if strict_json_bool(obj.get(scan_key)) is not True:
        return rejection_factory(incomplete_reason), False, 0
    claims = obj.get(claims_key)
    if not isinstance(claims, list):
        return rejection_factory(missing_reason), False, 0

    normalized_reply = _normalize_evidence_text(reply)
    normalized_source = _normalize_evidence_text(evidence_source)
    unsupported = False
    claim_count = 0
    for item in claims:
        if not isinstance(item, dict):
            return rejection_factory(invalid_reason), False, claim_count
        claim = str(item.get("claim", "") or "").strip()
        evidence = str(item.get("evidence", "") or "").strip()
        if not claim:
            continue
        claim_count += 1
        normalized_claim = _normalize_evidence_text(claim)
        normalized_evidence = _normalize_evidence_text(evidence)
        claim_is_verbatim = bool(normalized_claim) and normalized_claim in normalized_reply
        directly_supported = claim_is_verbatim and allow_missing_evidence and not evidence
        if not directly_supported:
            directly_supported = (
                claim_is_verbatim
                and bool(normalized_evidence)
                and normalized_evidence in normalized_source
                and _direct_evidence_supports_claim(claim, evidence)
            )
        if preserve_modality and evidence:
            directly_supported = directly_supported and _evidence_modality_is_preserved(
                claim,
                evidence,
            )
        unsupported = unsupported or not directly_supported
    return None, unsupported, claim_count


_CURRENT_MEDIA_MARKER_RE = re.compile(r"\[(图片|表情包|QQ表情)：([^\]\n]{1,400})\]")
_MEDIA_FORM_RE = re.compile(r"(图片|这张图|那张图|表情包|QQ表情|表情|媒体)")
_INTENT_QUESTION_RE = re.compile(r"(啥意思|什么意思|几个意思|什么情况|干嘛|为何|为什么)")
_VISIBLE_SPEECH_PATTERNS = (
    r"写着[“\"]([^”\"\]]{1,80})[”\"]",
    r"文字(?:内容)?(?:是|为)[“\"]([^”\"\]]{1,80})[”\"]",
    r"配文(?:字)?(?:是|为)?[“\"]([^”\"\]]{1,80})[”\"]",
)


def _current_media_only_anchors(current_text: str) -> tuple[str, ...]:
    text = str(current_text or "").strip()
    if not text:
        return ()
    anchors: list[str] = []
    for match in _CURRENT_MEDIA_MARKER_RE.finditer(text):
        label = str(match.group(2) or "").strip()
        label = re.split(r"[；;]", label, maxsplit=1)[0].strip()
        if label:
            anchors.extend(part.strip() for part in re.split(r"[，,、\s]+", label) if part.strip())
        marker = str(match.group(0) or "")
        for pattern in _VISIBLE_SPEECH_PATTERNS:
            speech_match = re.search(pattern, marker)
            if speech_match:
                anchors.append(str(speech_match.group(1) or "").strip())
                break
    if not anchors:
        return ()
    remainder = _CURRENT_MEDIA_MARKER_RE.sub("", text).strip()
    if remainder:
        return ()
    deduped: list[str] = []
    for anchor in anchors:
        if anchor and anchor not in deduped:
            deduped.append(anchor)
    return tuple(deduped)


def _media_meta_reply_check(*, reply: str, current_text: str) -> ReplyCheckResult | None:
    anchors = _current_media_only_anchors(current_text)
    if not anchors:
        return None

    normalized_reply = _normalize_text(reply)
    if not normalized_reply:
        return None

    anchor_in_reply = any(
        anchor and anchor in normalized_reply for anchor in anchors if len(anchor) <= 40
    )
    media_form_mentioned = _MEDIA_FORM_RE.search(normalized_reply) is not None
    asks_about_intent = _INTENT_QUESTION_RE.search(normalized_reply) is not None

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


_EXPLICIT_COMMUNICATION_CONSTRAINT_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?:你|请|先|就|只|千万)?\s*"
    r"(?:别|不要|不用|不许|请勿|务必|必须|只能|只说|别再|不要再)"
)
_NO_QUESTION_CONSTRAINT_RE = re.compile(
    r"(?:别|不要|不用|不必|不许|请勿)(?:再|总是|一直)?"
    r"(?:反问|追问|提问|问(?:我|问题)?|用问题(?:来)?收尾|以问题收尾)"
)
_DURABLE_PERSONA_QUESTION_RE = re.compile(
    rf"(?:你|小青)[^，。！？；;\n]{{0,24}}"
    rf"(?:{_RELATIONSHIP_NOUN}|以前|之前|曾经|小时候|去过|做过|学过|当过|"
    r"认识过|住过|工作过|家住|来自|毕业|学校|大学|城市|老家|哪里人|"
    r"住哪|在哪读|专业|职业|生日|年龄|多大|几岁)"
)
_UNSET_PRECISE_PERSONA_QUERY_RE = re.compile(
    r"(?:你|小青)[^，。！？；;\n]{0,30}"
    r"(?:哪所(?:学校|大学|学院)|哪个城市|哪里人|老家|住哪|具体住址|"
    r"什么专业|哪个专业|专业方向|生日|家庭|父母|家人|对象|伴侣)"
)
_UNSET_PERSONA_POLICY_RE = re.compile(
    r"(?:具体学校|学校、城市|精确学校)[^。；;\n]{0,80}"
    r"(?:没有设定|不主动补|不能添加|不能编造)"
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
_INFERENCE_HEDGE_RE = re.compile(
    r"(?:有?可能|也许|大概|或许|估计|应该|多半|八成|没准儿?|说不定|"
    r"恐怕|看来|看样子|"
    r"吧(?:[。！？!?]|$))"
)
_EVIDENCE_BOUNDARY_RE = re.compile(
    r"(?:不知道|不清楚|不确定|看不出|判断不了|没法判断|不好说|不能确定|"
    r"单凭|光看|只看|没有依据|没依据|不能替|不替|谁知道)"
)
_CURRENT_GROUP_DEIXIS_RE = re.compile(r"(?:群里|这个群|本群|咱们群|你们|各位|大家)")
_GROUP_QUANTIFIER_RE = re.compile(r"(?:都|全都|没几个|几乎没|大多数|多数|不少|很多|一个个)")
_OPEN_GROUP_INVITATION_START_RE = re.compile(r"^(?:大家|各位|你们|有没有人|有人|谁|群里有人)")
_GROUP_INFERENCE_QUESTION_RE = re.compile(r"(?:是不是|为什么|怎么回事|都在|状态|情况|原因)")
_STABLE_PERSONA_REPLY_RE = re.compile(
    r"(?:^|[，。！？；;\n])\s*(?:我|小青)[^，。！？；;\n]{0,18}"
    r"(?:一直|平时|经常|通常|总是|从来|习惯|家住|来自|毕业|学校|大学|"
    r"城市|老家|住址|专业|职业|生日|年龄)"
)


def _is_open_group_invitation(text: str) -> bool:
    return bool(
        _OPEN_GROUP_INVITATION_START_RE.search(text)
        and _GROUP_INFERENCE_QUESTION_RE.search(text) is None
    )


def _requires_configured_profile_boundary(current_text: str, grounding_text: str) -> bool:
    """判断用户是否追问了当前人设明确保留为空的可核验资料。"""

    return bool(
        _UNSET_PRECISE_PERSONA_QUERY_RE.search(_normalize_text(current_text))
        and _UNSET_PERSONA_POLICY_RE.search(str(grounding_text or ""))
    )


def _persona_identity_consistency_check(
    *,
    reply: str,
    grounding_text: str,
) -> ReplyCheckResult | None:
    """拦截日常创作中与稳定性别身份直接冲突的自称或受称。"""

    candidate = _normalize_text(reply)
    grounding = str(grounding_text or "")
    if _FEMALE_PERSONA_RE.search(grounding) and (
        _MALE_SELF_ASSERTION_RE.search(candidate)
        or _MALE_ADDRESS_IN_SELF_STORY_RE.search(candidate)
    ):
        return _persona_contract_rejection("回复中的自称或他人称呼与已设定的女性身份冲突")
    if _MALE_PERSONA_RE.search(grounding) and (
        _FEMALE_SELF_ASSERTION_RE.search(candidate)
        or _FEMALE_ADDRESS_IN_SELF_STORY_RE.search(candidate)
    ):
        return _persona_contract_rejection("回复中的自称或他人称呼与已设定的男性身份冲突")
    return None


def _requires_llm_semantic_check(
    *,
    reply: str,
    current_text: str,
    bot_name: str = "",
    proactive_persona_scan: bool = False,
) -> bool:
    """只把需要语义判断的高风险社交陈述交给远程检查器。

    普通知识问答、即时看法和简短承接由生成模型与确定性门禁负责，避免每条消息
    再串行等待一次远程模型。涉及用户明确表达方式、人物履历、第三方事实或媒体
    交际作用时，仍使用完整语义审查。
    """

    current = _normalize_text(current_text)
    candidate = _normalize_text(reply)
    if _EXPLICIT_COMMUNICATION_CONSTRAINT_RE.search(current):
        return True
    if _DURABLE_PERSONA_QUESTION_RE.search(current):
        return True
    # 主动插话中的第一人称陈述很容易把观点写成现实经历或日程。确定性门禁
    # 负责常见句法，语义检查再区分低风险观点与事实性自述。
    if proactive_persona_scan and "我" in candidate:
        return True
    social_current = current
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
            _STABLE_PERSONA_REPLY_RE,
        )
    ):
        return True
    if _CURRENT_MEDIA_MARKER_RE.search(current) or _CURRENT_MEDIA_MARKER_RE.search(candidate):
        return True
    return False


def _explicit_communication_constraint_check(
    *,
    reply: str,
    current_text: str,
) -> ReplyCheckResult | None:
    """对无需语义推理的明确表达约束做确定性兜底。"""

    current = _normalize_text(current_text)
    candidate = _normalize_text(reply)
    if _NO_QUESTION_CONSTRAINT_RE.search(current) and re.search(r"[?？]", candidate):
        return ReplyCheckResult(
            suitable=False,
            reason="用户明确要求本轮不要追问，但回复仍包含问句",
            need_replan=True,
            severity="hard",
            failure_code="instruction_following",
        )
    return None


def _unsupported_social_speculation_check(
    *,
    reply: str,
    current_text: str,
    bot_name: str,
) -> ReplyCheckResult | None:
    """拦截对第三方或群体状态给出的无证据可能性猜测。"""

    current = _normalize_text(current_text)
    normalized_bot_name = str(bot_name or "").strip()
    if normalized_bot_name:
        current = re.sub(
            rf"^\s*{re.escape(normalized_bot_name)}\s*[，,:：、]?\s*",
            "",
            current,
            count=1,
        )
    candidate = _normalize_text(reply)
    if (
        _CURRENT_GROUP_DEIXIS_RE.search(candidate)
        and _GROUP_QUANTIFIER_RE.search(candidate)
        and _EVIDENCE_BOUNDARY_RE.search(candidate) is None
    ):
        return ReplyCheckResult(
            suitable=False,
            reason="回复对当前群成员的数量、倾向或状态作了可见对话没有支持的概括",
            need_replan=True,
            severity="hard",
            failure_code="context_grounding",
        )
    if (
        _SOCIAL_CONTEXT_QUERY_RE.search(current)
        and not _is_open_group_invitation(current)
        and _INFERENCE_HEDGE_RE.search(candidate)
        and _EVIDENCE_BOUNDARY_RE.search(candidate) is None
    ):
        return ReplyCheckResult(
            suitable=False,
            reason="回复用不确定措辞补写了可见对话没有支持的第三方或群体状态",
            need_replan=True,
            severity="hard",
            failure_code="context_grounding",
        )
    return None


async def _request_checker_completion(
    *,
    checker_secrets: dict[str, Any],
    prompt: str,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
) -> tuple[dict[str, Any], str]:
    """按能力逐级移除可选参数，兼容只实现基础 Chat Completions 的端点。"""

    optional_payloads: tuple[dict[str, Any] | None, ...] = (
        {
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        },
        {"thinking": {"type": "disabled"}},
        None,
    )
    for index, extra_payload in enumerate(optional_payloads):
        try:
            return await chat_completions_raw_with_fallback_paths(
                secrets=checker_secrets,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                top_p=0.8,
                max_tokens=max(256, int(max_tokens)),
                timeout_seconds=timeout_seconds,
                max_retry=max_retry,
                retry_interval_seconds=retry_interval_seconds,
                extra_payload=extra_payload,
            )
        except LLMError as exc:
            is_last = index == len(optional_payloads) - 1
            if getattr(exc, "status", None) != 400 or is_last:
                raise
    raise RuntimeError("reply_checker request fallback exhausted")


def _interpret_checker_response(
    *,
    content: str,
    reply: str,
    current_text: str,
    history_text: str,
    grounding_text: str,
    allow_low_stakes_persona_fiction: bool,
) -> ReplyCheckResult:
    """把远端 JSON 协议转换为本地结论，且在缺字段时保持 fail-safe 语义。"""

    obj = parse_first_json_object(content)
    if not obj:
        return _checker_unavailable("reply_checker invalid response")
    suitable = strict_json_bool(obj.get("suitable"))
    need_replan = strict_json_bool(obj.get("need_replan"))
    if suitable is None or need_replan is None:
        return _checker_unavailable("reply_checker invalid boolean fields")
    reason = str(obj.get("reason", "") or "").strip()
    axis_names = (
        "context_coherent",
        "speaker_correct",
        "instruction_followed",
        "persona_grounded",
        "factually_plausible",
        "non_template",
    )
    axis_values: dict[str, bool] = {}
    for axis_name in axis_names:
        if axis_name not in obj:
            continue
        axis_value = strict_json_bool(obj.get(axis_name))
        if axis_value is None:
            return _checker_unavailable(f"reply_checker invalid {axis_name}")
        axis_values[axis_name] = axis_value
    if (
        _EXPLICIT_COMMUNICATION_CONSTRAINT_RE.search(current_text)
        and "instruction_followed" not in axis_values
    ):
        return ReplyCheckResult(
            suitable=False,
            reason="reply_checker 未判断最新消息中的明确交流约束",
            need_replan=True,
            severity="hard",
            failure_code="instruction_following",
        )

    persona_protocol_error, unsupported_persona_claim, persona_claim_count = (
        _validate_evidence_contract(
            obj,
            scan_key="persona_scan_complete",
            claims_key="persona_claims",
            reply=reply,
            evidence_source=grounding_text,
            rejection_factory=_persona_contract_rejection,
            incomplete_reason="reply_checker 未完成人物陈述扫描",
            missing_reason="reply_checker 缺少人物陈述清单",
            invalid_reason="reply_checker 人物陈述格式无效",
            preserve_modality=True,
            allow_missing_evidence=allow_low_stakes_persona_fiction,
        )
    )
    if persona_protocol_error is not None:
        return persona_protocol_error
    if unsupported_persona_claim:
        axis_values["persona_grounded"] = False
        suitable = False
        need_replan = True
        if not reason:
            reason = "人物陈述没有受控资料中的直接证据"

    context_protocol_error, unsupported_context_claim, context_claim_count = (
        _validate_evidence_contract(
            obj,
            scan_key="context_scan_complete",
            claims_key="context_claims",
            reply=reply,
            evidence_source=f"{history_text}\n{current_text}",
            rejection_factory=_context_contract_rejection,
            incomplete_reason="reply_checker 未完成对话事实扫描",
            missing_reason="reply_checker 缺少对话事实清单",
            invalid_reason="reply_checker 对话事实格式无效",
            preserve_modality=True,
        )
    )
    if context_protocol_error is not None:
        return context_protocol_error
    if unsupported_context_claim:
        axis_values["context_coherent"] = False
        suitable = False
        need_replan = True
        if not reason:
            reason = "对话特定陈述没有当前或历史对话中的直接证据"
    if any(value is False for value in axis_values.values()):
        suitable = False

    severity = str(obj.get("severity", "") or "").strip().lower()
    hard_axes = (
        "context_coherent",
        "speaker_correct",
        "instruction_followed",
        "persona_grounded",
        "factually_plausible",
    )
    if any(axis_values.get(name) is False for name in hard_axes):
        severity = "hard"
        need_replan = True
    elif not suitable and severity not in {"hard", "soft"}:
        # 兼容旧检查器响应：要求重新规划的拒绝视为硬错误，其余视为软风格问题。
        severity = "hard" if need_replan else "soft"
    elif suitable:
        severity = "soft"
    failure_code = ""
    if axis_values.get("persona_grounded") is False and all(
        axis_values.get(name) is not False
        for name in ("speaker_correct", "instruction_followed", "factually_plausible")
    ):
        failure_code = "persona_grounding"
    elif axis_values.get("instruction_followed") is False and all(
        axis_values.get(name) is not False
        for name in (
            "context_coherent",
            "speaker_correct",
            "persona_grounded",
            "factually_plausible",
        )
    ):
        failure_code = "instruction_following"
    elif axis_values.get("context_coherent") is False and all(
        axis_values.get(name) is not False
        for name in (
            "speaker_correct",
            "instruction_followed",
            "persona_grounded",
            "factually_plausible",
        )
    ):
        failure_code = "context_grounding"
    return ReplyCheckResult(
        suitable=suitable,
        reason=reason,
        need_replan=need_replan,
        severity=severity,
        failure_code=failure_code,
        persona_claim_count=persona_claim_count,
        context_claim_count=context_claim_count,
    )


async def _llm_check(
    *,
    secrets: dict[str, Any],
    check_input: _LLMCheckInput,
    request_policy: _LLMRequestPolicy,
) -> ReplyCheckResult:
    if "_ai" in secrets and secrets.get("_ai") is None:
        return ReplyCheckResult(True, "reply_checker AI route unavailable", False, "soft")

    # 历史只保留末尾 800 字符，降低输入 token 数。
    _hist = check_input.history_text.strip()
    if len(_hist) > 800:
        _hist = _hist[-800:]

    _current = str(check_input.current_text or "").strip()
    if len(_current) > 500:
        _current = _current[-500:]
    _current_block = ""
    if _current:
        _current_block = (
            "当前最新用户消息（这是待检查回复要回应的目标；"
            "如果里面有媒体摘要，需要判断它在聊天里的交际作用："
            "可见文字通常是对方借媒体说的话，情绪标签或 QQ 表情名称通常是对方的语气/态度/反应，"
            "图片内容通常是对方抛出的新话题素材）：\n"
            f"{_current}\n\n"
        )

    _policy = ""
    if check_input.policy_text.strip():
        _policy = "策略：" + check_input.policy_text.strip()[:200] + "\n"

    _grounding = str(check_input.grounding_text or "").strip()
    if len(_grounding) > 1600:
        _grounding = _grounding[-1600:]
    _grounding_block = ""
    if _grounding:
        _grounding_block = (
            "用于核对角色稳定身份、经历和偏好的受控资料。只把能够直接支持待检查陈述的内容"
            "视为既定人物事实，不用相近主题或他人的自述替角色补全：\n"
            f"{_grounding}\n\n"
        )

    if check_input.allow_low_stakes_persona_fiction:
        persona_policy = (
            "角色允许为闲聊即兴补充普通、低风险、不可核验且与稳定人设一致的日常小经历。"
            "这类片段可以没有受控资料原文，但不能改变稳定身份，不能添加精确学校、城市、"
            "专业方向、生日、家庭或可识别的持续现实关系等可核验资料。故事可以出现不具名临时配角，"
            "但不能把他们写成持续人物档案；叙事视角和他人对角色的称呼必须与既定性别、年龄、身份一致。"
            "不能编造重大或高风险人生事件、"
            "现实承诺，也不能拿它证明外部事实。把这种合规片段视为 persona_grounded=true，"
            "并在 persona_claims 中保留原文、将 evidence 留空。"
        )
    else:
        persona_policy = (
            "角色人物资料采用闭世界边界：受控资料没有写出的具体往事、现实关系、所在地、"
            "身份资历、长期习惯、现实时间安排、线下义务和现实活动都视为未知。"
        )

    prompt = (
        f"你是聊天回复质量审查器。{check_input.bot_name}是一个拟人聊天角色。{persona_policy}"
        "当下的低风险观点、口味和能力表达可以自然出现，不能仅因资料没写就判错。"
        "无论人物日常创作是否开启，真实用户、群友、第三方、当前媒体和外部世界都严格按证据判断，"
        "人物创作许可不能替他们补事实。"
        "只根据本次输入审查，不调用记忆中的具体案例，也不为某个词、领域或事件设置特殊标准。"
        "使用同一组通用原则评估任何话题；只找有证据的错误，不把自然闲聊润色成标准答案。"
        "只输出一个 JSON object，不要输出推理过程。\n\n"
        f"当前对话目标：{check_input.goal}\n"
        f"{_policy}"
        f"最近的对话记录：\n{_hist}\n\n"
        f"{_current_block}"
        f"{_grounding_block}"
        "注意：如果回复里出现 [表情包：...] 或 [QQ表情：...]，表示最终消息会附带相应媒体，"
        "这些媒体也算回复内容的一部分，需要一起判断是否自然、贴切。\n\n"
        f"待检查的最终回复：\n{check_input.reply}\n\n"
        "按以下顺序审查：\n"
        "1. 上下文与说话人：确认回复接的是正确最新消息，并区分角色、用户、第三方、引用和假设。\n"
        "2. 指令与交流偏好：识别最新消息对本轮表达方式作出的明确约束，判断回复是否遵守；"
        "不要把内容上的反驳误当成可以忽略表达方式，也不要从含混语气臆造约束。\n"
        "3. 人物证据契约：逐句扫描第一人称过去经历、身份、背景、现实关系、长期习惯、"
        "现实时间安排、线下义务、既定处境和具体打算；"
        "省略主语的个人回答也要扫描。人物日常创作中的泛称人物和生活片段也属于 persona_claims，"
        "不是关于当前真实群友的 context_claims。拆开能分别判真的原子陈述，逐条放入 persona_claims。\n"
        "4. 对话事实契约：逐句扫描关于用户、第三方或早先对话的具体言行、经历、关系、"
        "习惯、动机、是否在场、当前状态和打算，"
        "拆成原子陈述后逐条放入 context_claims。\n"
        "5. 每条 claim 必须是回复中的连续原文；既定 persona evidence 必须是受控资料的连续原文，"
        "context evidence 必须是当前或最近对话的连续原文，并能独立支持同一事实。"
        "没有直接证据就把 evidence 留空；如果允许人物日常创作，只有符合上述创作边界的 persona claim"
        " 才能在 evidence 为空时通过。相关主题、常见情况和合理推断都不是证据；"
        "提问、假设、玩笑和转述只证明相应表达出现过，不证明其中事件为真。"
        "不确定措辞只能保留来源里已有的不确定性，不能凭空新建一个可能原因、习惯或群体状态。"
        "证据的来源、肯定或否定、疑问、条件和不确定性都必须在 claim 中保留。"
        "缺少证据既不能支持正面答案，也不能支持负面答案。\n"
        "6. 完整性：扫完全部分句才令两个 scan_complete 为 true，不得漏掉不利于通过的陈述。"
        "不符合人物日常创作边界且缺证据的角色陈述令 persona_grounded=false；"
        "用户或第三方陈述缺证据时令 context_coherent=false。\n"
        "7. 事实与交流作用：核对数字、单位、比较或因果；判断回复是否回应具体内容并增加交流作用，"
        "而不是重复近期相同的结论、追问或表达套路。回复规模应与这一轮需求相称；"
        "没有分析请求时，不要用穷举可能性、连续追问或清单式展开代替自然承接。\n"
        "8. 媒体与场合：按当前媒体摘要理解可见内容和语气；除非正在讨论媒介本身，不要只评论媒体形式。"
        "附带媒体应增加交流作用。公开群聊还要尊重对象和基本安全边界。\n\n"
        "无依据人物或对话事实、忽略最新明确表达约束、上下文错位、说话人混淆、"
        "明显事实或媒体语义错误属于 hard；"
        "只影响措辞、节奏或模板感的问题属于 soft。口语、简短、犹豫、情绪、承认不知道和不完整回答本身不是错误。\n\n"
        "请分别判断 context_coherent、speaker_correct、instruction_followed、persona_grounded、"
        "factually_plausible、non_template。\n"
        '仅输出JSON：{"suitable":true/false,"reason":"...","need_replan":true/false,'
        '"severity":"hard/soft","context_coherent":true/false,'
        '"speaker_correct":true/false,"instruction_followed":true/false,'
        '"persona_grounded":true/false,'
        '"factually_plausible":true/false,"non_template":true/false,'
        '"persona_scan_complete":true,"persona_claims":['
        '{"claim":"待检查回复的连续原文","evidence":"受控资料的连续原文或空字符串"}],'
        '"context_scan_complete":true,"context_claims":['
        '{"claim":"待检查回复的连续原文","evidence":"对话中的连续原文或空字符串"}]}'
    )
    checker_secrets = dict(secrets)
    if checker_secrets.get("_ai") is not None:
        # 审查使用独立的高质量 route，并取消主回复模型的显式固定，避免模型自审。
        checker_secrets["_route"] = "checker"
        checker_secrets["_pinned_model"] = None
    resp, _path = await _request_checker_completion(
        checker_secrets=checker_secrets,
        prompt=prompt,
        max_tokens=request_policy.max_tokens,
        timeout_seconds=request_policy.timeout_seconds,
        max_retry=request_policy.max_retry,
        retry_interval_seconds=request_policy.retry_interval_seconds,
    )
    content = llm_client.extract_response_content(resp)
    return _interpret_checker_response(
        content=content,
        reply=check_input.reply,
        current_text=_current,
        history_text=_hist,
        grounding_text=_grounding,
        allow_low_stakes_persona_fiction=check_input.allow_low_stakes_persona_fiction,
    )


async def check_reply(
    *,
    http_session,
    secrets: dict[str, Any],
    bot_name: str,
    reply: str,
    heuristic_reply: str = "",
    goal: str,
    current_text: str = "",
    policy_text: str = "",
    grounding_text: str = "",
    history: Sequence[StoredMessage],
    chat_history_text: str,
    enable_llm_checker: bool,
    max_repeat_compare: int,
    similarity_threshold: float,
    max_assistant_in_row: int,
    max_tokens: int = 1024,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    llm_checker_mode: str = "always",
    check_omitted_persona_episode: bool = False,
    allow_low_stakes_persona_fiction: bool = False,
) -> ReplyCheckResult:
    heuristic_source = str(heuristic_reply or reply or "").strip()
    h = _heuristic_check(
        reply=heuristic_source,
        history=history,
        bot_name=bot_name,
        max_repeat_compare=max_repeat_compare,
        similarity_threshold=similarity_threshold,
        max_assistant_in_row=max_assistant_in_row,
    )
    if h:
        return h
    media_h = _media_meta_reply_check(reply=heuristic_source, current_text=current_text)
    if media_h:
        return media_h
    persona_consistency_h = _persona_identity_consistency_check(
        reply=heuristic_source,
        grounding_text=grounding_text,
    )
    if persona_consistency_h:
        return persona_consistency_h
    communication_h = _explicit_communication_constraint_check(
        reply=heuristic_source,
        current_text=current_text,
    )
    if communication_h:
        return communication_h
    if _requires_configured_profile_boundary(current_text, grounding_text):
        return ReplyCheckResult(
            suitable=False,
            reason="用户询问了当前人设明确未设定的精确现实资料，必须使用稳定边界回答",
            need_replan=True,
            severity="hard",
            failure_code="persona_grounding",
        )
    if not allow_low_stakes_persona_fiction:
        self_history_h = _grounded_self_history_check(
            reply=heuristic_source,
            grounding_text=grounding_text,
            check_omitted_episode=check_omitted_persona_episode,
        )
        if self_history_h:
            return self_history_h
    context_history_h = _grounded_context_history_check(
        reply=heuristic_source,
        dialogue_text=f"{chat_history_text}\n{current_text}",
    )
    if context_history_h:
        return context_history_h
    social_speculation_h = _unsupported_social_speculation_check(
        reply=heuristic_source,
        current_text=current_text,
        bot_name=bot_name,
    )
    if social_speculation_h:
        return social_speculation_h
    if not enable_llm_checker:
        return ReplyCheckResult(True, "", False, "soft")
    normalized_checker_mode = str(llm_checker_mode or "always").strip().lower()
    if normalized_checker_mode == "risk" and not _requires_llm_semantic_check(
        reply=heuristic_source,
        current_text=current_text,
        bot_name=bot_name,
        proactive_persona_scan=check_omitted_persona_episode,
    ):
        return ReplyCheckResult(True, "", False, "soft")
    try:
        # ``http_session`` 仍是公共插件调用契约的一部分；当前统一 gateway 自行拥有
        # HTTP client 生命周期，不能把调用方 session 混入其 route/fallback 重试链。
        _ = http_session
        return await _llm_check(
            secrets=secrets,
            check_input=_LLMCheckInput(
                bot_name=bot_name,
                reply=reply,
                current_text=current_text,
                goal=goal,
                policy_text=policy_text,
                grounding_text=grounding_text,
                history_text=chat_history_text,
                allow_low_stakes_persona_fiction=allow_low_stakes_persona_fiction,
            ),
            request_policy=_LLMRequestPolicy(
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                max_retry=max_retry,
                retry_interval_seconds=retry_interval_seconds,
            ),
        )
    except Exception as exc:
        _log.warning("reply_checker LLM call failed: %s", type(exc).__name__)
        return _checker_unavailable("reply_checker unavailable")
