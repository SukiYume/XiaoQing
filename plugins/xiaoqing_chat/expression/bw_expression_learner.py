from __future__ import annotations

import difflib
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .bw_expression_store import ExpressionRecord, ExpressionStore
from ..config.config import PersonalityConfig
from ..llm.llm_client import chat_completions_raw_with_fallback_paths
from ..memory.memory import StoredMessage
from ..planning.pfc_utils import get_items_from_json, extract_first_json_list

_LEARN_PROMPT = """你是对话表达方式学习器。你会从对话里抽取“像真人的表达方式/口癖”，并总结成可复用的表达风格。

要求：
- 只学“怎么说”，不要学具体事实信息
- 输出要短，能直接插入到机器人表达里
- 避免脏话、辱骂、人身攻击、极端内容
- situation 用一句话描述“什么时候/什么语境下”
- style 用一句话描述“说话方式/口癖”，不要太长

下面是对话（每行开头是 source_id）：
{dialogue}

请输出 JSON 数组，每个元素包含：
- situation: string
- style: string
- source_id: string

示例：
[
  {{"situation":"对方问你吃什么","style":"我一般就随便吃点，懒得纠结","source_id":"m012"}}
]"""

_SINGLE_CHECK_PROMPT = """你是表达方式审核器。你要判断一条“表达方式”是否适合用于 QQ 群聊机器人说话。

机器人设定：
{persona_text}

待审核：
- situation: {situation}
- style: {style}

要求：
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
    h = hashlib.md5(f"{chat_id}|{situation}|{style}|{time.time()}".encode("utf-8")).hexdigest()
    return h[:12]

def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").strip(), (b or "").strip()).ratio()

def _build_dialogue(messages: Sequence[StoredMessage], *, bot_name: str, max_lines: int = 60) -> str:
    lines: list[str] = []
    for msg in messages[-max_lines:]:
        lid = getattr(msg, "local_id", "") or ""
        sid = lid or f"t{int(msg.ts or 0)}"
        role = "你" if msg.role == "assistant" else "对方"
        name = bot_name if msg.role == "assistant" else (msg.name or "用户")
        text = (msg.content or "").strip()
        if not text:
            continue
        if len(text) > 160:
            text = text[:120].rstrip() + "…"
        lines.append(f"{sid} {role}({name})：{text}")
    return "\n".join(lines).strip()

async def learn_from_messages(
    *,
    http_session,
    secrets: dict[str, Any],
    bot_name: str,
    chat_id: str,
    personality: PersonalityConfig,
    messages: Sequence[StoredMessage],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
) -> list[LearnedExpression]:
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return []

    dialogue = _build_dialogue(messages, bot_name=bot_name)
    if not dialogue:
        return []

    prompt = _LEARN_PROMPT.format(dialogue=dialogue)
    resp, _path = await chat_completions_raw_with_fallback_paths(
        session=http_session,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=min(0.7, float(temperature)),
        top_p=float(top_p),
        max_tokens=min(900, max(500, int(max_tokens))),
        timeout_seconds=float(timeout_seconds),
        max_retry=int(max_retry),
        retry_interval_seconds=float(retry_interval_seconds),
        proxy=proxy,
        endpoint_path=endpoint_path,
    )
    content = (((resp.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
    arr = extract_first_json_list(str(content))
    out: list[LearnedExpression] = []
    for it in arr:
        situation = str(it.get("situation", "") or "").strip()
        style = str(it.get("style", "") or "").strip()
        source_id = str(it.get("source_id", "") or "").strip()
        if not situation or not style:
            continue
        if len(situation) > 80:
            situation = situation[:77].rstrip() + "…"
        if len(style) > 80:
            style = style[:77].rstrip() + "…"
        out.append(LearnedExpression(situation=situation, style=style, source_id=source_id))
    return out[:12]

async def single_expression_check(
    *,
    http_session,
    secrets: dict[str, Any],
    bot_name: str,
    personality: PersonalityConfig,
    situation: str,
    style: str,
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
) -> tuple[bool, bool, str, str, str]:
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return False, False, "", "", ""

    identity = (personality.identity or "").strip()
    persona_text = f"你的名字是{bot_name}，{identity}" if identity else f"你的名字是{bot_name}"
    prompt = _SINGLE_CHECK_PROMPT.format(persona_text=persona_text, situation=situation.strip(), style=style.strip())
    resp, _path = await chat_completions_raw_with_fallback_paths(
        session=http_session,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=min(0.4, float(temperature)),
        top_p=float(top_p),
        max_tokens=min(400, int(max_tokens)),
        timeout_seconds=float(timeout_seconds),
        max_retry=int(max_retry),
        retry_interval_seconds=float(retry_interval_seconds),
        proxy=proxy,
        endpoint_path=endpoint_path,
    )
    content = (((resp.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
    ok, obj = get_items_from_json(
        str(content),
        "checked",
        "rejected",
        "reason",
        "modified_situation",
        "modified_style",
        default_values={"checked": False, "rejected": False, "reason": "", "modified_situation": "", "modified_style": ""},
        allow_array=False,
    )
    if not ok or not isinstance(obj, dict):
        return False, False, "", "", ""
    checked = bool(obj.get("checked", False))
    rejected = bool(obj.get("rejected", False))
    reason = str(obj.get("reason", "") or "").strip()
    ms = str(obj.get("modified_situation", "") or "").strip()
    mt = str(obj.get("modified_style", "") or "").strip()
    return checked, rejected, reason, ms, mt

async def upsert_learned(
    *,
    store: ExpressionStore,
    chat_id: str,
    learned: Sequence[LearnedExpression],
    similarity_threshold: float = 0.72,
    self_reflect: bool,
    http_session,
    secrets: dict[str, Any],
    bot_name: str,
    personality: PersonalityConfig,
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
) -> int:
    items = store.load()
    now = time.time()
    changed = 0

    for it in learned:
        sit = it.situation.strip()
        sty = it.style.strip()
        if not sit or not sty:
            continue
        best: Optional[ExpressionRecord] = None
        best_score = 0.0
        for ex in items:
            if ex.chat_id != chat_id:
                continue
            score = _similarity(sit, ex.situation)
            if score > best_score:
                best = ex
                best_score = score
        if best and best_score >= float(similarity_threshold):
            if sit not in best.content_list:
                best.content_list.append(sit)
            best.count += 1
            best.last_active_time = now
            best.checked = False
            best.rejected = False
            best.modified_by = "ai"
            changed += 1
            continue

        rec = ExpressionRecord(
            expression_id=_mk_id(chat_id, sit, sty),
            chat_id=chat_id,
            situation=sit,
            style=sty,
            content_list=[sit],
            count=1,
            last_active_time=now,
            checked=False,
            rejected=False,
            modified_by="ai",
        )
        items.append(rec)
        changed += 1

    if not changed:
        return 0

    if self_reflect:
        for ex in items[-min(20, len(items)) :]:
            if ex.chat_id != chat_id:
                continue
            if ex.checked or ex.rejected:
                continue
            checked, rejected, _reason, ms, mt = await single_expression_check(
                http_session=http_session,
                secrets=secrets,
                bot_name=bot_name,
                personality=personality,
                situation=ex.situation,
                style=ex.style,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                max_retry=max_retry,
                retry_interval_seconds=retry_interval_seconds,
                proxy=proxy,
                endpoint_path=endpoint_path,
            )
            if rejected:
                ex.checked = False
                ex.rejected = True
                ex.modified_by = "ai"
                continue
            if checked:
                if ms:
                    ex.situation = ms[:80].strip()
                if mt:
                    ex.style = mt[:80].strip()
                ex.checked = True
                ex.rejected = False
                ex.modified_by = "ai"

    items.sort(key=lambda x: (x.chat_id, -x.last_active_time, -x.count))
    items = items[:2000]
    store.save(items)
    return changed
