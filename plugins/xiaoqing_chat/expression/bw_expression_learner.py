"""从真实用户消息中抽取、审核并持久化可复用表达方式。

学习输入排除机器人消息，模型返回的 source_id 必须能对应本轮真实输入，防止无来源
表达混入存储。相似情境合并时 style 才是可展示示例；自审只处理本轮实际改变的记录，
最后按会话独立裁剪容量，不能因一个群达到上限而删除其他群的数据。
"""

from __future__ import annotations

import difflib
import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..config.config import PersonalityConfig
from ..llm.gateway import chat_completions_raw_with_fallback_paths, extract_response_content
from ..llm.llm_config import LLMCallConfig
from ..memory.memory import StoredMessage
from ..message_parts import render_stored_message
from ..persona import compose_persona_identity
from ..planning.pfc_utils import get_items_from_json
from ..utils.json_parsing import parse_first_json_array, strict_json_bool
from ..utils.text import clip_text
from .bw_expression_store import ExpressionRecord, ExpressionStore

_LEARN_PROMPT = """你是对话表达方式学习器。你会从真实用户发言中抽取可迁移的表达特征。

要求：
- 只从对方/群友发言学习，不学习机器人自己之前说过的话
- 只学“怎么说”：语气、节奏、句式和省略方式；不要保存原话中的事实、人物、物品、经历、偏好或观点
- style 应描述可复用的表达方式，而不是一条可直接冒充个人经历的台词
- 无法脱离原始内容安全复用时就跳过，不为了凑数量输出
- 输出要短，并保留实际来源 source_id
- 避免脏话、辱骂、人身攻击、极端内容
- situation 用一句话描述“什么时候/什么语境下”
- style 用一句话描述表达形式，不要太长

下面是对话（每行开头是 source_id）：
{dialogue}

请输出 JSON 数组，每个元素包含：
- situation: string
- style: string
- source_id: string

只输出以下结构的 JSON 数组：
[{{"situation":"...","style":"...","source_id":"输入中真实存在的 source_id"}}]"""

_SINGLE_CHECK_PROMPT = """你是表达方式审核器。你要判断一条“表达方式”是否适合用于 QQ 群聊机器人说话。

机器人设定：
{persona_text}

待审核：
- situation: {situation}
- style: {style}

要求：
- 只审核表达形式是否自然、可迁移并符合人设，不因为某个具体话题本身熟悉就放行
- style 不得携带未经人设或记忆支持的第一人称经历、稳定偏好或现实关系
- style 如果只是复述原消息内容而没有可复用的表达特征，应拒绝
- 如果合适：checked=true, rejected=false
- 如果不合适且无法修正：checked=false, rejected=true
- 如果可以修正：checked=true, rejected=false，并给出 modified_situation / modified_style

请严格输出 JSON：
{{
  "checked": true,
  "rejected": false,
  "reason": "",
  "modified_situation": "",
  "modified_style": ""
}}"""


@dataclass(frozen=True)
class LearnedExpression:
    situation: str
    style: str
    source_id: str


def _mk_id(chat_id: str, situation: str, style: str) -> str:
    h = hashlib.md5(f"{chat_id}|{situation}|{style}|{time.time()}".encode()).hexdigest()
    return h[:12]


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").strip(), (b or "").strip()).ratio()


def _build_dialogue(
    messages: Sequence[StoredMessage], *, max_lines: int = 60
) -> tuple[str, frozenset[str]]:
    lines: list[str]     = []
    source_ids: set[str] = set()
    for msg in messages[-max_lines:]:
        if msg.role == "assistant":
            continue
        lid  = msg.local_id
        sid  = lid or f"t{int(msg.ts or 0)}"
        role = "对方"
        name = msg.name or "用户"
        text = render_stored_message(msg)
        if not text:
            continue
        if len(text) > 160:
            text = text[:120].rstrip() + "…"
        lines.append(f"{sid} {role}({name})：{text}")
        source_ids.add(sid)
    return "\n".join(lines).strip(), frozenset(source_ids)


async def learn_from_messages(
    *,
    secrets: dict[str, Any],
    messages: Sequence[StoredMessage],
    llm: LLMCallConfig,
) -> list[LearnedExpression]:
    if "_ai" in secrets and secrets.get("_ai") is None:
        return []

    dialogue, source_ids = _build_dialogue(messages)
    if not dialogue:
        return []

    prompt = _LEARN_PROMPT.format(dialogue=dialogue)
    resp, _ = await chat_completions_raw_with_fallback_paths(
        secrets  = secrets,
        messages = [{"role": "user", "content": prompt}],
        **llm.bounded(max_temperature=0.7, max_tokens=900, min_tokens=500).as_kwargs(),
    )
    content                      = extract_response_content(resp)
    arr                          = parse_first_json_array(str(content))
    out: list[LearnedExpression] = []
    for it in arr:
        situation = str(it.get("situation", "") or "").strip()
        style     = str(it.get("style", "") or "").strip()
        source_id = str(it.get("source_id", "") or "").strip()
        if not situation or not style or source_id not in source_ids:
            continue
        out.append(
            LearnedExpression(
                situation=clip_text(situation, 80), style=clip_text(style, 80), source_id=source_id
            )
        )
    return out[:12]


async def single_expression_check(
    *,
    secrets: dict[str, Any],
    bot_name: str,
    personality: PersonalityConfig,
    situation: str,
    style: str,
    llm: LLMCallConfig,
) -> tuple[bool, bool, str, str, str]:
    if "_ai" in secrets and secrets.get("_ai") is None:
        return False, False, "", "", ""

    persona_text = compose_persona_identity(personality.identity, bot_name)
    prompt       = _SINGLE_CHECK_PROMPT.format(
        persona_text=persona_text, situation=situation.strip(), style=style.strip()
    )
    resp, _ = await chat_completions_raw_with_fallback_paths(
        secrets  = secrets,
        messages = [{"role": "user", "content": prompt}],
        **llm.bounded(max_temperature=0.4, max_tokens=400).as_kwargs(),
    )
    content = extract_response_content(resp)
    ok, obj = get_items_from_json(
        str(content),
        "checked",
        "rejected",
        "reason",
        optional_items = ("modified_situation", "modified_style"),
        default_values = {
            "checked": False,
            "rejected": False,
            "reason": "",
            "modified_situation": "",
            "modified_style": "",
        },
        allow_array=False,
    )
    if not ok or not isinstance(obj, dict):
        return False, False, "", "", ""
    checked  = strict_json_bool(obj.get("checked"))
    rejected = strict_json_bool(obj.get("rejected"))
    if checked is None or rejected is None:
        return False, False, "", "", ""
    reason = str(obj.get("reason", "") or "").strip()
    ms     = str(obj.get("modified_situation", "") or "").strip()
    mt     = str(obj.get("modified_style", "") or "").strip()
    return checked, rejected, reason, ms, mt


def _closest_expression(
    items: Sequence[ExpressionRecord], chat_id: str, situation: str, threshold: float
) -> tuple[ExpressionRecord | None, bool]:
    """返回本群最相近且达到阈值的有效表达；命中已否决的表达时第二项为 True。"""
    best: ExpressionRecord | None = None
    best_score                    = 0.0
    for ex in items:
        if ex.chat_id != chat_id:
            continue
        score = _similarity(situation, ex.situation)
        if ex.rejected:
            if score >= threshold:
                return None, True
            continue
        if score > best_score:
            best, best_score = ex, score
    return (best if best_score >= threshold else None), False


def _merge_learned(
    items: list[ExpressionRecord],
    learned: Sequence[LearnedExpression],
    *,
    chat_id: str,
    threshold: float,
    now: float,
) -> tuple[int, dict[str, ExpressionRecord]]:
    """把新学到的表达并入相近记录或新建记录；被否决过的相近情境不再学习。"""
    changed                              = 0
    touched: dict[str, ExpressionRecord] = {}
    for it in learned:
        sit, sty = it.situation.strip(), it.style.strip()
        if not sit or not sty:
            continue
        best, rejected_match = _closest_expression(items, chat_id, sit, threshold)
        if rejected_match:
            continue
        if best is None:
            best = ExpressionRecord(
                expression_id = _mk_id(chat_id, sit, sty),
                chat_id       = chat_id,
                situation     = sit,
                style         = sty,
                content_list  = [],
                count         = 0,
            )
            items.append(best)
        if sty not in best.content_list:
            best.content_list.append(sty)
        best.count += 1
        best.last_active_time = now
        best.checked          = False
        best.rejected         = False
        best.modified_by      = "ai"
        changed += 1
        touched[best.expression_id] = best
    return changed, touched


async def _self_review(
    records: Sequence[ExpressionRecord],
    *,
    secrets: dict[str, Any],
    bot_name: str,
    personality: PersonalityConfig,
    llm: LLMCallConfig,
) -> None:
    for ex in records:
        if ex.checked or ex.rejected:
            continue
        checked, rejected, _reason, ms, mt = await single_expression_check(
            secrets     = secrets,
            bot_name    = bot_name,
            personality = personality,
            situation   = ex.situation,
            style       = ex.style,
            llm         = llm,
        )
        if rejected:
            ex.checked, ex.rejected, ex.modified_by = False, True, "ai"
        elif checked:
            if ms:
                ex.situation = ms[:80].strip()
            if mt:
                ex.style = mt[:80].strip()
            ex.checked, ex.rejected, ex.modified_by = True, False, "ai"


async def upsert_learned(
    *,
    store: ExpressionStore,
    chat_id: str,
    learned: Sequence[LearnedExpression],
    similarity_threshold: float = 0.72,
    max_store: int              = 2000,
    self_reflect: bool,
    secrets: dict[str, Any],
    bot_name: str,
    personality: PersonalityConfig,
    llm: LLMCallConfig,
    expected_generation: str | None = None,
) -> int:
    generation = store.generation(chat_id) if expected_generation is None else expected_generation
    items      = store.load()
    changed, touched = _merge_learned(
        items, learned, chat_id=chat_id, threshold=float(similarity_threshold), now=time.time()
    )
    if not changed:
        return 0
    if self_reflect:
        # 同一记录本轮可能被多个候选命中，按 expression_id 去重后至多审核一次。
        await _self_review(
            list(touched.values())[-20:],
            secrets     = secrets,
            bot_name    = bot_name,
            personality = personality,
            llm         = llm,
        )

    scoped = [item for item in items if item.chat_id == chat_id]
    other  = [item for item in items if item.chat_id != chat_id]
    scoped.sort(key=lambda item: (item.last_active_time, item.count), reverse=True)
    limit = max(0, int(max_store))
    items = other + (scoped[:limit] if limit else [])
    with store.generation_guard(chat_id, generation) as current:
        if not current:
            return 0
        store.save(items)
    return changed
