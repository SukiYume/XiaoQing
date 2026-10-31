"""证据契约：审查模型列出的陈述必须能在对话或资料原文中找到依据，并保持原有的主体与语气。"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..utils.json_parsing import strict_json_bool
from .arithmetic import measured_values_are_supported
from .check_result import ReplyCheckResult
from .communication_constraints import (
    extract_local_term_definitions as _explicit_term_definitions,
)


def normalize_text(s: str) -> str:
    t = (s or "").strip()
    # 中文省略号也承担分句作用。先归一化，避免人物事实藏在“……”后绕过
    # 只从句首扫描的确定性门禁。
    t = re.sub(r"…+", "。", t)
    return re.sub(r"\s+", " ", t)


_EVIDENCE_NEGATION_RE = re.compile(r"(?:不|没|无|未|非|否)")


_QUESTION_EVIDENCE_RE = re.compile(r"[?？]|(?:吗|是否|有没有|是不是|能否|会不会|该不该)")


_QUESTION_CLAIM_RE = re.compile(
    r"[?？]|(?:问|询问|想知道|想确认|确认一下|吗|是否|有没有|是不是|能否|会不会|该不该)"
)


_HYPOTHETICAL_RE = re.compile(r"(?:如果|假设|假如|要是|倘若|若是)")


_UNCERTAINTY_RE = re.compile(r"(?:可能|也许|大概|或许|似乎|好像|不一定|未必)")


# 她承认自己不知道、记不清、没看到：这是在说缺口，不是对别人的事实陈述。
_ADMITS_GAP_RE = re.compile(
    r"(?:我|咱)[^，,。！!？?]{0,3}?(?:不知道|不清楚|不确定|记不清|不记得|没看到|没注意|没看清)"
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
        previous   = normalized
        normalized = _CLAIM_SCAFFOLD_RE.sub("", normalized)
    return normalized


def direct_evidence_supports_claim(claim: str, evidence: str) -> bool:
    """核对两段原文是否至少表达同一个明示事实。

    这不做领域语义推断：先要求 evidence 是受控资料原文，再核对极性和文字
    覆盖。宽松的双字组覆盖只用于容纳人称、系词和轻微语序差异，不允许用一段
    相关但不同义的身份资料支持新经历。
    """

    claim_core    = _claim_evidence_core(claim)
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
    claim_bigrams    = {claim_core[index : index + 2] for index in range(len(claim_core) - 1)}
    evidence_bigrams = {evidence_core[index : index + 2] for index in range(len(evidence_core) - 1)}
    shared           = claim_bigrams & evidence_bigrams
    return len(shared) >= 2 and len(shared) / len(claim_bigrams) >= 0.6


def evidence_modality_is_preserved(claim: str, evidence: str) -> bool:
    """证据中的疑问、条件和不确定性不能在转述时被悄悄抹掉。"""

    if _QUESTION_EVIDENCE_RE.search(evidence) and not _QUESTION_CLAIM_RE.search(claim):
        return False
    for marker in (_HYPOTHETICAL_RE, _UNCERTAINTY_RE):
        if marker.search(evidence) and not marker.search(claim):
            return False
    return True


def dialogue_evidence_fragments(dialogue_text: str) -> tuple[str, ...]:
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


_PERSON_REFERENCE_RE = re.compile(r"你|您|他|她|大家|有人|群友|群里|我们|咱")


_DIALOGUE_SPEAKER_RE = re.compile(
    r"\s*(?:\d{1,2}:\d{2},\s*)?([^:：,，\n(（]{1,24})(?:[(（][^)）\n]*[)）])?[:：]"
)


def _claim_concerns_people(claim: str, source_records: dict[str, str], *, bot_name: str) -> bool:
    """陈述是否涉及群里的人：带人称，或提到对话里出现过的说话人。"""
    if _PERSON_REFERENCE_RE.search(claim):
        return True
    # 对话行形如“17:37, 阿白: 你好呀”；时间前缀不能当成人名。
    speakers = {
        match.group(1).strip()
        for line in source_records.values()
        if (match := _DIALOGUE_SPEAKER_RE.match(line))
    }
    return any(name and name != bot_name and name in claim for name in speakers)


def validate_evidence_contract(
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
    allow_missing_evidence: bool          = False,
    source_records: dict[str, str] | None = None,
    bot_name: str                         = "",
    current_sender: str                   = "",
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

    unsupported = False
    claim_count = 0
    for item in claims:
        if not isinstance(item, dict):
            return rejection_factory(invalid_reason), False, claim_count
        claim, evidence = item.get("claim"), item.get("evidence", "")
        if source_records is not None and "source_ids" in item:
            identifiers = item["source_ids"]
            if not isinstance(identifiers, list) or any(
                not isinstance(identifier, str) or identifier not in source_records
                for identifier in identifiers
            ):
                return rejection_factory(invalid_reason), False, claim_count
            evidence = "\n".join(source_records[identifier] for identifier in identifiers)
            if isinstance(claim, str) and not definition_author_claim_supported(
                claim, identifiers, source_records, bot_name=bot_name, current_sender=current_sender
            ):
                return (
                    rejection_factory("词义定义作者的个人状态缺少独立发言依据，应按实际自述者归属"),
                    False,
                    claim_count,
                )
        if not isinstance(claim, str) or not isinstance(evidence, str):
            return rejection_factory(invalid_reason), False, claim_count
        claim, evidence = claim.strip(), evidence.strip()
        if not claim:
            continue
        claim_is_verbatim = evidence_is_visible(claim, reply)
        # 审查模型误把资料原文列为候选陈述时，该条目不属于候选，不能据此拒绝候选。
        if not claim_is_verbatim and evidence_is_visible(claim, evidence_source):
            continue
        # 对话条目中的调侃、提议和假设不断言事实；作品设定、公共知识和梗的含义由语义轴判断。
        # 未标注时按事实处理。
        if (
            claims_key == "context_claims"
            and claim_is_verbatim
            and (
                str(item.get("kind") or "").strip().lower() in {"creative", "knowledge"}
                or _ADMITS_GAP_RE.search(claim)
            )
        ):
            continue
        if (
            claims_key == "context_claims"
            and source_records is not None
            and item.get("source_ids") == []
            and not _claim_concerns_people(claim, source_records, bot_name=bot_name)
        ):
            # 审查模型自己也找不到出处、又不涉及任何人的条目（作品常识、当天日期）不是群聊
            # 事实；它的真假由 factually_plausible 等语义轴判断，本地不能按“编造对话”拒绝。
            continue
        claim_count += 1
        # 本地只核验引用来源；自然改写、否定和不确定性的支持关系由模型语义轴判断。
        # 允许日常创作时证据本可留空，引用格式不规范不应比留空更严。
        directly_supported = claim_is_verbatim and allow_missing_evidence
        if not directly_supported:
            directly_supported = (
                claim_is_verbatim
                and bool(evidence)
                and evidence_is_visible(evidence, evidence_source)
            )
        if directly_supported and claims_key == "context_claims":
            # 计算结果的底数可能来自引用之外的消息（例如图上的单价），再对照全部对话核一次。
            directly_supported = measured_values_are_supported(
                claim, evidence
            ) or measured_values_are_supported(claim, evidence_source)
        unsupported = unsupported or not directly_supported
    return None, unsupported, claim_count


def dialogue_source_records(history_text: str, current_text: str) -> dict[str, str]:
    records = {
        f"h{index}": re.sub(r"^\[[^\]\n]{1,96}\]\s*", "", line)
        for index, line in enumerate(history_text.splitlines(), 1)
        if line.strip()
    }
    records["current"] = current_text
    return records


def definition_source_ids(records: dict[str, str]) -> list[str]:
    """定义消息保持独立来源用途，包含被后续更正的旧定义。"""
    identifiers = {
        identifier
        for definition in _explicit_term_definitions(records, latest_only=False)
        for identifier in definition["source_ids"]
    }
    return [key for key in records if key == "jargon" or key in identifiers]


def definition_source_instruction(records: dict[str, str]) -> str:
    identifiers = definition_source_ids(records)
    if not identifiers:
        return ""
    return (
        "已识别的词义定义来源编号为 " + ", ".join(identifiers) + "。"
        "这些来源用于还原群内用语。定义作者的个人经历和当前状态需要另有本人陈述支持。"
    )


def _dialogue_source_author(value: str) -> str:
    value = re.sub(r"^\d{1,2}:\d{2},\s*", "", value.strip())
    match = re.match(r"([^:：\n]{1,80})[:：]", value)
    return match[1].strip() if match else ""


def definition_author_claim_supported(
    claim: str,
    identifiers: list[str],
    records: dict[str, str],
    *,
    bot_name: str,
    current_sender: str,
) -> bool:
    """定义作者与经历当事人分别需要来源，机器人旧回复保持引用链用途。"""
    definitions = set(definition_source_ids(records))
    if not definitions:
        return True
    if re.search(r"(?:叫|称作|称为|词义|定义|这个词|这种说法)", claim):
        return True
    for identifier in definitions:
        author = (
            current_sender
            if identifier == "current"
            else _dialogue_source_author(records[identifier])
        )
        if not author or author not in claim:
            continue
        supported = False
        for source_id in identifiers:
            if source_id in definitions:
                continue
            source        = records[source_id]
            source_author = (
                current_sender if source_id == "current" else _dialogue_source_author(source)
            )
            if source_author == f"{bot_name}(你)" or "[插件通知:" in source_author:
                continue
            if source_author == author or author in source:
                supported = True
                break
        if not supported:
            return False
    return True


def _canonical_evidence(value: str) -> str:
    punctuation = str.maketrans("：，；！？“”‘’", ":,;!?\"\"''")
    return "".join(value.translate(punctuation).split())


def evidence_is_visible(evidence: str, source: str) -> bool:
    """引用允许全半角标点和排版空白变化，事实文字保持一致。"""
    if not evidence.strip():
        return False
    normalized_source = _canonical_evidence(source)
    if _canonical_evidence(evidence) in normalized_source:
        return True
    # 多条来源可以分别引用。引用中的人名前缀与原始消息作者保持一致，正文可取连续片段。
    speaker_line = re.compile(r"^(?:\[[^\]\n]{1,96}\])?(?:\d{1,2}:\d{2},)?([^:\n]{1,80}):(.*)$")
    source_lines = [_canonical_evidence(line) for line in source.splitlines() if line.strip()]
    for quoted in (_canonical_evidence(line) for line in evidence.splitlines() if line.strip()):
        if quoted in normalized_source:
            continue
        match = speaker_line.match(quoted)
        if match is None or not match[2]:
            return False
        if not any(
            (original := speaker_line.match(line))
            and original[1] == match[1]
            and match[2] in original[2]
            for line in source_lines
        ):
            return False
    return True


# 审查模型摘引时常用省略号、斜杠或引号把几处原文拼在一起，也会调换前后顺序。
_EVIDENCE_SPLICE_RE = re.compile(r"…+|\.{3,}|[/／|｜“”\"「」『』]")


def spliced_evidence_is_visible(evidence: str, source: str) -> bool:
    """逐段核对拼接引用：拆开后每一段都须是这条来源里的原文，不要求原顺序。"""
    fragments         = [_canonical_evidence(part) for part in _EVIDENCE_SPLICE_RE.split(evidence)]
    fragments         = [fragment for fragment in fragments if fragment]
    normalized_source = _canonical_evidence(source)
    return bool(fragments) and all(fragment in normalized_source for fragment in fragments)
