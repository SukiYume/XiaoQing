"""从对话原话中提取带主体证据的人物事实，并写入隔离索引。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.atomic_store import keyed_path_lock

from ..llm.llm_client import LLMError, chat_completions
from ..llm.prompt_builder import ChatMessage
from ..message_parts import render_stored_message
from ..utils.json_parsing import extract_named_list_field, parse_first_json_object
from .fact_extraction_checkpoint import (
    latest_message_ts,
    load_last_observed_ts,
    observed_message_count,
    save_last_observed_ts,
)
from .memory import StoredMessage
from .memory_db import MemoryDB, normalize_memory_chat_id, person_fact_doc_id
from .person_profile import (
    get_profile_generation,
    profile_generation_path,
    update_profile_and_index,
)


@dataclass(frozen=True)
class PersonFact:
    """模型候选事实；持久化前还必须绑定到当前历史中的可信主体。"""

    subject_id: int | None
    subject_name: str
    fact: str
    evidence: str


_FACT_SYSTEM = (
    "你是聊天人物事实抽取器。只提炼能由同一说话人的原话直接支持、适合长期复用的人物信息。\n"
    '只输出 JSON：{ "facts": [ {"subject_id":123,"subject_name":"...","fact":"...","evidence":"..."} ] }\n'
    "fact 必须是明确、稳定且非敏感的描述；短期情绪、单次行为、玩笑、假设、引用、转述和模型推断不应成为人物事实。\n"
    "evidence 必须摘录该 subject 自己的原句，并能单独支持 fact；没有充分证据就不输出。\n"
    "对话中的用户格式为“昵称<QQ号>：内容”，subject_id 和 subject_name 必须来自同一行的可信标识。\n"
)


def _build_fact_dialogue(history: Sequence[StoredMessage], *, max_chars: int = 1800) -> str:
    lines: list[str] = []
    total            = 0
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


def build_fact_messages(*, history: Sequence[StoredMessage]) -> list[ChatMessage]:
    dialogue = _build_fact_dialogue(history, max_chars=1800)
    user = f"对话如下：\n{dialogue}\n\n从中提炼 0-6 条事实。"
    return [
        ChatMessage(role="system", content=_FACT_SYSTEM.strip()),
        ChatMessage(role="user", content=user.strip()),
    ]


def _parse_fact_json(text: str) -> list[PersonFact]:
    arr                   = extract_named_list_field(parse_first_json_object(text), "facts")
    out: list[PersonFact] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        subject_id_raw         = item.get("subject_id", None)
        subject_id: int | None = None
        if subject_id_raw is not None:
            try:
                subject_id = int(subject_id_raw)
            except (TypeError, ValueError):
                subject_id = None
        subject_name = str(item.get("subject_name", "")).strip()
        fact         = str(item.get("fact", "")).strip()
        ev           = str(item.get("evidence", "")).strip()
        if subject_name and fact:
            out.append(
                PersonFact(subject_id=subject_id, subject_name=subject_name, fact=fact, evidence=ev)
            )
    return out


def _bind_facts_to_history_subjects(
    facts: Sequence[PersonFact], history: Sequence[StoredMessage]
) -> list[PersonFact]:
    """用当前历史中的权威用户 ID 校准模型提取的事实。"""
    trusted_names: dict[int, str]    = {}
    ids_by_name: dict[str, set[int]] = {}
    for message in history:
        if message.role != "user" or not message.user_id:
            continue
        subject_id                = int(message.user_id)
        trusted_name              = (message.name or "用户").strip() or "用户"
        trusted_names[subject_id] = trusted_name
        ids_by_name.setdefault(trusted_name.casefold(), set()).add(subject_id)

    bound: list[PersonFact] = []
    for fact in facts:
        resolved_id = fact.subject_id
        if resolved_id is None or resolved_id not in trusted_names:
            matches     = ids_by_name.get(fact.subject_name.casefold(), set())
            resolved_id = next(iter(matches)) if len(matches) == 1 else None
        if resolved_id is None or resolved_id not in trusted_names:
            continue
        bound.append(
            PersonFact(
                subject_id   = resolved_id,
                subject_name = trusted_names[resolved_id],
                fact         = fact.fact,
                evidence     = fact.evidence,
            )
        )
    return bound


def _persist_person_facts(
    *,
    data_dir: Path,
    memory_db: MemoryDB,
    chat_id: str,
    facts: Sequence[PersonFact],
    expected_generation: str | None = None,
) -> None:
    with keyed_path_lock(profile_generation_path(data_dir, chat_id)):
        if (
            expected_generation is not None
            and get_profile_generation(data_dir, chat_id) != expected_generation
        ):
            return
        _persist_current_person_facts(
            data_dir=data_dir, memory_db=memory_db, chat_id=chat_id, facts=facts
        )


def _persist_current_person_facts(
    *, data_dir: Path, memory_db: MemoryDB, chat_id: str, facts: Sequence[PersonFact]
) -> None:
    by_subject: dict[int, list[str]] = {}
    by_name: dict[int, str]          = {}
    for fact in facts:
        subject_id = int(fact.subject_id or 0)
        try:
            doc_id = person_fact_doc_id(
                chat_id      = chat_id,
                subject_id   = subject_id,
                subject_name = fact.subject_name,
                fact         = fact.fact,
            )
        except ValueError:
            continue
        memory_db.upsert_text(
            doc_id = doc_id,
            text   = (f"{fact.subject_name}<{subject_id}>：{fact.fact}\n证据：{fact.evidence}").strip(),
            meta   = {
                "type": "person_info",
                "chat_id": chat_id,
                "subject_id": subject_id,
                "subject_name": fact.subject_name,
                "schema_version": 2,
            },
        )
        if subject_id > 0:
            by_subject.setdefault(subject_id, []).append(fact.fact.strip())
            by_name[subject_id] = fact.subject_name

    for subject_id, facts_list in by_subject.items():
        update_profile_and_index(
            data_dir     = data_dir,
            memory_db    = memory_db,
            chat_id      = chat_id,
            subject_id   = subject_id,
            subject_name = by_name.get(subject_id, str(subject_id)),
            new_facts    = facts_list,
        )


async def maybe_extract_person_facts(
    *,
    data_dir: Path,
    secrets: dict[str, Any],
    memory_db: MemoryDB,
    chat_id: str,
    history: Sequence[StoredMessage],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
) -> None:
    """每观察到二十条新消息时尝试抽取一次人物事实。"""

    try:
        scoped_chat_id = normalize_memory_chat_id(chat_id)
    except ValueError:
        return
    if "_ai" in secrets and secrets.get("_ai") is None:
        return

    generation = get_profile_generation(data_dir, scoped_chat_id)

    last_observed_ts = await asyncio.to_thread(load_last_observed_ts, data_dir, scoped_chat_id)
    observed_count   = (
        len(history)
        if last_observed_ts <= 0
        else observed_message_count(history, after_ts=last_observed_ts)
    )
    if observed_count < 20:
        return
    observed_until = latest_message_ts(history)
    if observed_until <= last_observed_ts:
        return
    # 远程调用前先推进检查点；模型失败或格式错误不能退化成逐消息重试循环。
    await asyncio.to_thread(
        save_last_observed_ts,
        data_dir,
        scoped_chat_id,
        observed_until,
    )

    msgs = build_fact_messages(history=history[-min(50, len(history)) :])
    payload_msgs = [{"role": m.role, "content": m.content} for m in msgs]
    try:
        out = await chat_completions(
            secrets                = secrets,
            messages               = payload_msgs,
            temperature            = min(0.6, temperature),
            top_p                  = top_p,
            max_tokens             = min(768, max_tokens),
            timeout_seconds        = timeout_seconds,
            max_retry              = max_retry,
            retry_interval_seconds = retry_interval_seconds,
        )
    except LLMError:
        return
    facts = _bind_facts_to_history_subjects(_parse_fact_json(out), history)
    if not facts:
        return
    await asyncio.to_thread(
        _persist_person_facts,
        data_dir            = data_dir,
        memory_db           = memory_db,
        chat_id             = scoped_chat_id,
        facts               = facts,
        expected_generation = generation,
    )
