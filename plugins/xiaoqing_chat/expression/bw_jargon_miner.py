from __future__ import annotations

import json
import logging
import time
from typing import Any, Sequence

from ..llm.llm_client import LLMError, chat_completions_raw_with_fallback_paths
from ..logging_utils import _redacted_value, sanitize_log_fields
from ..memory.memory import StoredMessage
from .bw_jargon_store import JargonRecord, JargonStore
from .expr_utils import extract_json_array, extract_json_obj, render_dialogue

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


def _log_jargon_step(
    step: str,
    *,
    chat_id: str,
    fields: dict[str, Any] | None = None,
    warning: bool = False,
) -> None:
    payload = {"step": step, "chat_id": _redacted_value(chat_id)}
    if fields:
        payload.update(sanitize_log_fields(fields))
    try:
        message = "xiaoqing_chat step=%s"
        encoded = json.dumps(payload, ensure_ascii=False)
        if warning:
            _logger.warning(message, encoded)
        else:
            _logger.info(message, encoded)
    except Exception:
        pass


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
    extra_payload: dict[str, Any] | None = None,
) -> int:
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return 0

    dialogue = render_dialogue(messages)
    if not dialogue:
        return 0

    t0 = time.monotonic()
    _log_jargon_step("jargon.extract.start", chat_id=chat_id, fields={"model": model})
    prompt = _EXTRACT_PROMPT.format(dialogue=dialogue)
    try:
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
            extra_payload=extra_payload,
        )
    except LLMError as exc:
        _log_jargon_step(
            "jargon.extract.fail",
            chat_id=chat_id,
            fields={
                "error_type": type(exc).__name__,
                "elapsed_s": round(time.monotonic() - t0, 3),
            },
            warning=True,
        )
        return 0
    content = (((resp.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
    arr = extract_json_array(str(content))
    _log_jargon_step(
        "jargon.extract.done",
        chat_id=chat_id,
        fields={"candidates": len(arr), "elapsed_s": round(time.monotonic() - t0, 3)},
    )

    db = store.load()
    changed = 0
    now = time.time()
    context_snip = dialogue.splitlines()[-6:]
    context_text = "\n".join(context_snip).strip()

    for it in arr[:12]:
        term = str(it.get("term", "") or "").strip()
        if not term or len(term) > 32:
            continue
        is_jargon = it.get("is_jargon") is True
        meaning = str(it.get("meaning", "") or "").strip()
        if not is_jargon:
            continue

        record_key = store.key_for(term, chat_id)
        rec = db.get(record_key)
        if not rec:
            rec = JargonRecord(content=term, scope_chat_id=chat_id, count=0, updated_at=now)
            db[record_key] = rec
        rec.count = int(rec.count or 0) + 1
        rec.updated_at = now
        rec.is_jargon = True
        chat_id_counts = rec.chat_id_counts if isinstance(rec.chat_id_counts, list) else []
        rec.chat_id_counts = _bump_chat_count(chat_id_counts, chat_id)
        if context_text and context_text not in rec.raw_content:
            rec.raw_content.append(context_text)
            rec.raw_content = rec.raw_content[-20:]
        if meaning and not rec.meaning:
            rec.meaning = meaning[:120].strip()
            rec.is_complete = True
        changed += 1

    to_infer: list[tuple[str, JargonRecord]] = []
    for _record_key, rec in db.items():
        if rec.is_global or rec.scope_chat_id != chat_id:
            continue
        if rec.is_complete:
            continue
        if rec.count < int(infer_threshold):
            continue
        if rec.last_inference_count >= rec.count:
            continue
        to_infer.append((rec.content, rec))

    for term, rec in to_infer[:6]:
        it0 = time.monotonic()
        contexts = "\n---\n".join(rec.raw_content[-6:]).strip() or "（无）"
        ip = _INFER_PROMPT.format(term=term, contexts=contexts)
        try:
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
                extra_payload=extra_payload,
            )
        except LLMError as exc:
            _log_jargon_step(
                "jargon.infer.fail",
                chat_id=chat_id,
                fields={
                    "term": term,
                    "error_type": type(exc).__name__,
                    "elapsed_s": round(time.monotonic() - it0, 3),
                },
                warning=True,
            )
            continue
        c2 = (((r2.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
        obj = extract_json_obj(str(c2))
        meaning = str(obj.get("meaning", "") or "").strip()
        _log_jargon_step(
            "jargon.infer.done",
            chat_id=chat_id,
            fields={"term": term, "elapsed_s": round(time.monotonic() - it0, 3)},
        )
        if meaning:
            rec.meaning = meaning[:200].strip()
            rec.is_complete = True
        # Model output may explain a term but may never widen its visibility.
        rec.is_global = False
        rec.last_inference_count = rec.count
        rec.updated_at = time.time()
        changed += 1

    store.save(list(db.values()))
    return changed
