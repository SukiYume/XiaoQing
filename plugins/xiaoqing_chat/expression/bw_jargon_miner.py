from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Sequence

from .bw_jargon_store import JargonRecord, JargonStore
from ..llm.llm_client import chat_completions_raw_with_fallback_paths
from ..memory.memory import StoredMessage

_RE_JSON_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
_RE_ARRAY = re.compile(r"\[[\s\S]*\]")
_logger = logging.getLogger("plugin.xiaoqing_chat")

_EXTRACT_PROMPT = """你是黑话/缩写挖掘器。你会从对话里抽取可能的黑话、缩写、简称、专有词。

要求：
- 只抽取词条本身，不要抽取整句
- 如果只是普通常见词，不算黑话
- term 用原文；最多输出 10 条

对话如下：
{dialogue}

请输出 JSON 数组，每个元素包含：
- term: string
- is_jargon: boolean
- meaning: string (如果你能确定，给一个简短解释；否则留空)

示例：
[
  {{"term":"PFC","is_jargon":true,"meaning":"前额叶皮层架构的简称"}}
]"""

_INFER_PROMPT = """你是黑话/缩写解释器。你会根据上下文给出一个尽量简短准确的解释。

词条：{term}
上下文片段：
{contexts}

请严格输出 JSON：
{{
  "meaning": "",
  "is_global": false
}}"""

def _extract_json_array(text: str) -> list[dict[str, Any]]:
    s = (text or "").strip()
    blocks = _RE_JSON_BLOCK.findall(s)
    if blocks:
        s = blocks[0].strip()
    m = _RE_ARRAY.search(s)
    if m:
        s = m.group(0)
    try:
        arr = json.loads(s)
    except Exception:
        return []
    if not isinstance(arr, list):
        return []
    return [x for x in arr if isinstance(x, dict)]

def _render_dialogue(messages: Sequence[StoredMessage], *, max_lines: int = 30) -> str:
    lines: list[str] = []
    for msg in messages[-max_lines:]:
        t = (msg.content or "").strip()
        if not t:
            continue
        if len(t) > 200:
            t = t[:160].rstrip() + "…"
        name = msg.name or ("小青" if msg.role == "assistant" else "用户")
        role = "小青" if msg.role == "assistant" else "对方"
        lines.append(f"{role}({name})：{t}")
    return "\n".join(lines).strip()

def _bump_chat_count(chat_counts: list[list[Any]], chat_id: str) -> list[list[Any]]:
    out: list[list[Any]] = []
    found = False
    for item in chat_counts:
        if not isinstance(item, list) or len(item) < 2:
            continue
        cid = str(item[0])
        cnt = int(item[1] or 0)
        if cid == chat_id:
            out.append([cid, cnt + 1])
            found = True
        else:
            out.append([cid, cnt])
    if not found:
        out.append([chat_id, 1])
    out.sort(key=lambda x: (-int(x[1] or 0), str(x[0])))
    return out[:30]

async def mine_jargon(
    *,
    http_session,
    secrets: dict[str, Any],
    store: JargonStore,
    chat_id: str,
    messages: Sequence[StoredMessage],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
    infer_threshold: int = 3,
) -> int:
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return 0

    dialogue = _render_dialogue(messages)
    if not dialogue:
        return 0

    t0 = time.monotonic()
    try:
        _logger.info("xiaoqing_chat step=%s", json.dumps({"step": "jargon.extract.start", "chat_id": chat_id, "model": model}, ensure_ascii=False))
    except Exception:
        pass
    prompt = _EXTRACT_PROMPT.format(dialogue=dialogue)
    resp, _path = await chat_completions_raw_with_fallback_paths(
        session=http_session,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=min(0.4, float(temperature)),
        top_p=float(top_p),
        max_tokens=min(700, max(400, int(max_tokens))),
        timeout_seconds=float(timeout_seconds),
        max_retry=int(max_retry),
        retry_interval_seconds=float(retry_interval_seconds),
        proxy=proxy,
        endpoint_path=endpoint_path,
    )
    content = (((resp.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
    arr = _extract_json_array(str(content))
    try:
        _logger.info(
            "xiaoqing_chat step=%s",
            json.dumps({"step": "jargon.extract.done", "chat_id": chat_id, "candidates": len(arr), "elapsed_s": round(time.monotonic() - t0, 3)}, ensure_ascii=False),
        )
    except Exception:
        pass

    db = store.load()
    changed = 0
    now = time.time()
    context_snip = dialogue.splitlines()[-6:]
    context_text = "\n".join(context_snip).strip()

    for it in arr[:12]:
        term = str(it.get("term", "") or "").strip()
        if not term or len(term) > 32:
            continue
        is_jargon = bool(it.get("is_jargon", True))
        meaning = str(it.get("meaning", "") or "").strip()
        if not is_jargon:
            continue

        rec = db.get(term)
        if not rec:
            rec = JargonRecord(content=term, count=0, updated_at=now)
            db[term] = rec
        rec.count = int(rec.count or 0) + 1
        rec.updated_at = now
        rec.is_jargon = True
        rec.chat_id_counts = _bump_chat_count(rec.chat_id_counts if isinstance(rec.chat_id_counts, list) else [], chat_id)
        if context_text and context_text not in rec.raw_content:
            rec.raw_content.append(context_text)
            rec.raw_content = rec.raw_content[-20:]
        if meaning and not rec.meaning:
            rec.meaning = meaning[:120].strip()
            rec.is_complete = True
        changed += 1

    to_infer: list[tuple[str, JargonRecord]] = []
    for term, rec in db.items():
        if rec.is_complete:
            continue
        if rec.count < int(infer_threshold):
            continue
        if rec.last_inference_count >= rec.count:
            continue
        to_infer.append((term, rec))

    for term, rec in to_infer[:6]:
        it0 = time.monotonic()
        contexts = "\n---\n".join(rec.raw_content[-6:]).strip() or "（无）"
        ip = _INFER_PROMPT.format(term=term, contexts=contexts)
        r2, _p2 = await chat_completions_raw_with_fallback_paths(
            session=http_session,
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=[{"role": "user", "content": ip}],
            temperature=min(0.2, float(temperature)),
            top_p=float(top_p),
            max_tokens=min(300, int(max_tokens)),
            timeout_seconds=float(timeout_seconds),
            max_retry=int(max_retry),
            retry_interval_seconds=float(retry_interval_seconds),
            proxy=proxy,
            endpoint_path=endpoint_path,
        )
        c2 = (((r2.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
        obj = {}
        s2 = (str(c2) or "").strip()
        blocks = _RE_JSON_BLOCK.findall(s2)
        if blocks:
            s2 = blocks[0].strip()
        m2 = re.search(r"\{[\s\S]*\}", s2)
        if m2:
            s2 = m2.group(0)
        try:
            obj = json.loads(s2)
        except Exception:
            obj = {}
        meaning = str(obj.get("meaning", "") or "").strip()
        try:
            _logger.info(
                "xiaoqing_chat step=%s",
                json.dumps({"step": "jargon.infer.done", "chat_id": chat_id, "term": term, "elapsed_s": round(time.monotonic() - it0, 3)}, ensure_ascii=False),
            )
        except Exception:
            pass
        is_global = bool(obj.get("is_global", False))
        if meaning:
            rec.meaning = meaning[:200].strip()
            rec.is_complete = True
        rec.is_global = bool(is_global)
        rec.last_inference_count = rec.count
        rec.updated_at = time.time()
        changed += 1

    store.save(list(db.values()))
    return changed
