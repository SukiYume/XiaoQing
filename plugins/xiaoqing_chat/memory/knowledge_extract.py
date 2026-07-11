from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from ..llm.llm_client import LLMError, chat_completions
from ..utils.json_parsing import extract_named_list_field, parse_first_json_object
from .memory import StoredMessage
from .memory_db import MemoryDB
from .person_profile import update_profile_and_index
from ..llm.prompt_builder import ChatMessage
from ..message_parts import render_stored_message


@dataclass(frozen=True)
class WordDef:
    word: str
    definition: str


@dataclass(frozen=True)
class PersonFact:
    subject_id: Optional[int]
    subject_name: str
    fact: str
    evidence: str


_WORD_SYSTEM = (
    "你是词语解释助手。你会解释群聊里的黑话/缩写/不明词。\n"
    '输出 JSON：{ "items": [ {"word":"...","definition":"..."} ] }\n'
    "definition 要简短（30-80 字），尽量用口语说明。\n"
)


def build_word_messages(*, words: Sequence[str]) -> list[ChatMessage]:
    user = ("需要解释的词：\n{words}\n\n输出 JSON。").format(
        words="\n".join(f"- {w}" for w in words if w)
    )
    return [
        ChatMessage(role="system", content=_WORD_SYSTEM.strip()),
        ChatMessage(role="user", content=user.strip()),
    ]


def _parse_word_json(text: str) -> list[WordDef]:
    arr = extract_named_list_field(parse_first_json_object(text), "items")
    out: list[WordDef] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        w = str(item.get("word", "")).strip()
        d = str(item.get("definition", "")).strip()
        if w and d:
            out.append(WordDef(word=w, definition=d))
    return out


async def upsert_word_defs(
    *,
    http_session,
    secrets: dict[str, Any],
    memory_db: MemoryDB,
    words: Sequence[str],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    uniq = []
    seen = set()
    for w in words:
        w = (w or "").strip()
        if not w or w in seen:
            continue
        seen.add(w)
        uniq.append(w)
    if not uniq:
        return
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return
    msgs = build_word_messages(words=uniq[:8])
    payload_msgs = [{"role": m.role, "content": m.content} for m in msgs]
    out = await chat_completions(
        session=http_session,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=payload_msgs,
        temperature=min(0.6, temperature),
        top_p=top_p,
        max_tokens=min(512, max_tokens),
        timeout_seconds=timeout_seconds,
        max_retry=max_retry,
        retry_interval_seconds=retry_interval_seconds,
        proxy=proxy,
        endpoint_path=endpoint_path,
        extra_payload=extra_payload,
    )
    items = _parse_word_json(out)
    if not items:
        return
    for it in items:
        memory_db.upsert_text(
            doc_id=f"word:{it.word}",
            text=f"{it.word}：{it.definition}",
            meta={"type": "word_def", "word": it.word, "global_approved": True},
        )


_FACT_SYSTEM = (
    "你是聊天事实抽取助手。你会从对话里提炼可复用的“人物事实/偏好/约定”。\n"
    '只输出 JSON：{ "facts": [ {"subject_id":123,"subject_name":"...","fact":"...","evidence":"..."} ] }\n'
    "要求：fact 必须是可长期记忆的客观描述；不要输出隐私敏感信息；evidence 是对话中的原句摘录。\n"
    "注意：对话中用户格式为“昵称<QQ号>：内容”，请优先正确填写 subject_id。\n"
)


def _build_fact_dialogue(history: Sequence[StoredMessage], *, max_chars: int = 1800) -> str:
    lines: list[str] = []
    total = 0
    for msg in history:
        if msg.role != "user":
            continue
        if not msg.user_id:
            continue
        name = (msg.name or "用户").strip() or "用户"
        text = render_stored_message(msg).strip()
        if not text:
            continue
        if len(text) > 220:
            text = text[:217].rstrip() + "..."
        line = f"{name}<{int(msg.user_id)}>：{text}"
        if total + len(line) > max_chars and lines:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines).strip()


def build_fact_messages(*, bot_name: str, history: Sequence[StoredMessage]) -> list[ChatMessage]:
    dialogue = _build_fact_dialogue(history, max_chars=1800)
    user = ("对话如下：\n{dialogue}\n\n从中提炼 0-6 条事实。").format(dialogue=dialogue)
    return [
        ChatMessage(role="system", content=_FACT_SYSTEM.strip()),
        ChatMessage(role="user", content=user.strip()),
    ]


def _parse_fact_json(text: str) -> list[PersonFact]:
    arr = extract_named_list_field(parse_first_json_object(text), "facts")
    out: list[PersonFact] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        subject_id_raw = item.get("subject_id", None)
        subject_id: Optional[int] = None
        if subject_id_raw is not None:
            try:
                subject_id = int(subject_id_raw)
            except (TypeError, ValueError):
                subject_id = None
        subject_name = str(item.get("subject_name", "")).strip()
        fact = str(item.get("fact", "")).strip()
        ev = str(item.get("evidence", "")).strip()
        if subject_name and fact:
            out.append(
                PersonFact(subject_id=subject_id, subject_name=subject_name, fact=fact, evidence=ev)
            )
    return out


def _bind_facts_to_history_subjects(
    facts: Sequence[PersonFact], history: Sequence[StoredMessage]
) -> list[PersonFact]:
    """Resolve model facts against authoritative user IDs from this history."""
    trusted_names: dict[int, str] = {}
    ids_by_name: dict[str, set[int]] = {}
    for message in history:
        if message.role != "user" or not message.user_id:
            continue
        subject_id = int(message.user_id)
        trusted_name = (message.name or "用户").strip() or "用户"
        trusted_names[subject_id] = trusted_name
        ids_by_name.setdefault(trusted_name.casefold(), set()).add(subject_id)

    bound: list[PersonFact] = []
    for fact in facts:
        subject_id = fact.subject_id
        if subject_id not in trusted_names:
            matches = ids_by_name.get(fact.subject_name.casefold(), set())
            subject_id = next(iter(matches)) if len(matches) == 1 else None
        if subject_id is None or subject_id not in trusted_names:
            continue
        bound.append(
            PersonFact(
                subject_id=subject_id,
                subject_name=trusted_names[subject_id],
                fact=fact.fact,
                evidence=fact.evidence,
            )
        )
    return bound


async def maybe_extract_person_facts(
    *,
    data_dir: Path,
    http_session,
    secrets: dict[str, Any],
    memory_db: MemoryDB,
    bot_name: str,
    chat_id: str,
    history: Sequence[StoredMessage],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    if len(history) < 20:
        return
    if len(history) % 20 != 0:
        return
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return

    msgs = build_fact_messages(bot_name=bot_name, history=history[-min(50, len(history)) :])
    payload_msgs = [{"role": m.role, "content": m.content} for m in msgs]
    try:
        out = await chat_completions(
            session=http_session,
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=payload_msgs,
            temperature=min(0.6, temperature),
            top_p=top_p,
            max_tokens=min(768, max_tokens),
            timeout_seconds=timeout_seconds,
            max_retry=max_retry,
            retry_interval_seconds=retry_interval_seconds,
            proxy=proxy,
            endpoint_path=endpoint_path,
            extra_payload=extra_payload,
        )
    except LLMError:
        return
    facts = _bind_facts_to_history_subjects(_parse_fact_json(out), history)
    if not facts:
        return
    by_subject: dict[int, list[str]] = {}
    by_name: dict[int, str] = {}
    for f in facts:
        subject_id = int(f.subject_id or 0)
        doc_key = f"{subject_id}:{f.subject_name}:{f.fact}".strip()
        # Use stable hash (hashlib) instead of built-in hash() which varies
        # across Python processes due to hash randomization.
        import hashlib

        stable_hash = int(hashlib.sha256(doc_key.encode("utf-8")).hexdigest()[:14], 16) % 10_000_000
        doc_id = f"person:{subject_id}:{stable_hash}"
        memory_db.upsert_text(
            doc_id=doc_id,
            text=f"{f.subject_name}<{subject_id}>：{f.fact}\n证据：{f.evidence}".strip(),
            meta={
                "type": "person_info",
                "chat_id": chat_id,
                "subject_id": subject_id,
                "subject_name": f.subject_name,
            },
        )
        if subject_id > 0:
            by_subject.setdefault(subject_id, []).append(f.fact.strip())
            by_name[subject_id] = f.subject_name

    for sid, facts_list in by_subject.items():
        update_profile_and_index(
            data_dir=data_dir,
            memory_db=memory_db,
            chat_id=chat_id,
            subject_id=sid,
            subject_name=by_name.get(sid, str(sid)),
            new_facts=facts_list,
        )
