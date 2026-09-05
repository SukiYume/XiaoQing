"""按会话和主体保存人物资料，并同步长期记忆索引。"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from core.atomic_store import atomic_write_text, keyed_path_lock
from core.plugin_base import load_json, write_json

from ..store_base import coerce_finite_float
from .memory_db import MemoryDB


@dataclass
class PersonProfile:
    chat_id: str
    subject_id: int
    subject_name: str
    facts: list[str]
    updated_at: float


def _profile_path(data_dir: Path, chat_id: str, subject_id: int) -> Path:
    safe_chat_id = (chat_id or "").strip() or "default"
    return data_dir / "person_profiles" / safe_chat_id / f"{subject_id}.json"


def profile_generation_path(data_dir: Path, chat_id: str) -> Path:
    """代际标记与人物档案共用会话路径锁，阻止重置前提取结果重新落盘。"""
    root   = (data_dir / "person_profiles").resolve()
    target = (_profile_path(data_dir, chat_id, 0).parent / ".generation").resolve()
    if not target.is_relative_to(root):
        raise ValueError("invalid profile chat_id")
    return target


def get_profile_generation(data_dir: Path, chat_id: str) -> str:
    try:
        return profile_generation_path(data_dir, chat_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def clear_profiles_and_memory(data_dir: Path, chat_id: str, memory_db: MemoryDB | None) -> None:
    """重置档案、备份与向量记录，并使所有旧提取任务失效。"""
    generation_path = profile_generation_path(data_dir, chat_id)
    with keyed_path_lock(generation_path):
        atomic_write_text(generation_path, str(time.time_ns()))
        for path in generation_path.parent.iterdir():
            if path.name.endswith((".json", ".json.bak")) and path.is_file():
                path.unlink()
        delete_chat = getattr(memory_db, "delete_chat", None)
        if callable(delete_chat) and delete_chat(chat_id):
            save = getattr(memory_db, "save", None)
            if callable(save):
                save()


def load_profile(data_dir: Path, *, chat_id: str, subject_id: int) -> PersonProfile | None:
    path = _profile_path(data_dir, chat_id, subject_id)
    if not path.exists():
        return None
    try:
        raw = load_json(path, default=None)
    except OSError:
        return None
    if not isinstance(raw, dict):
        return None

    name      = str(raw.get("subject_name", "")).strip()
    facts     = raw.get("facts", [])
    fact_list = (
        [value.strip() for value in facts if isinstance(value, str) and value.strip()]
        if isinstance(facts, list)
        else []
    )
    return PersonProfile(
        chat_id      = chat_id,
        subject_id   = subject_id,
        subject_name = name or str(subject_id),
        facts        = fact_list,
        updated_at=coerce_finite_float(raw.get("updated_at"), default=0.0, minimum=0.0),
    )


def save_profile(data_dir: Path, profile: PersonProfile) -> None:
    path = _profile_path(data_dir, str(profile.chat_id), profile.subject_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "chat_id": profile.chat_id,
        "subject_id": profile.subject_id,
        "subject_name": profile.subject_name,
        "facts": profile.facts,
        "updated_at": profile.updated_at,
    }
    write_json(path, payload)


def update_profile_and_index(
    *,
    data_dir: Path,
    memory_db: MemoryDB,
    chat_id: str,
    subject_id: int,
    subject_name: str,
    new_facts: Sequence[str],
    max_facts: int = 120,
) -> None:
    if subject_id <= 0:
        return
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return
    now = time.time()
    existing = load_profile(data_dir, chat_id=chat_id, subject_id=subject_id) or PersonProfile(
        chat_id      = chat_id,
        subject_id   = subject_id,
        subject_name = subject_name or str(subject_id),
        facts        = [],
        updated_at   = 0.0,
    )
    if subject_name and (not existing.subject_name or existing.subject_name == str(subject_id)):
        existing.subject_name = subject_name

    seen = set(existing.facts)
    for f in new_facts:
        f = (f or "").strip()
        if not f or f in seen:
            continue
        existing.facts.append(f)
        seen.add(f)
    if max_facts > 0 and len(existing.facts) > max_facts:
        existing.facts = existing.facts[-max_facts:]
    existing.updated_at = now

    save_profile(data_dir, existing)

    profile_text = (
        f"{existing.subject_name}<{existing.subject_id}> 的已知信息：\n- "
        + "\n- ".join(existing.facts[-20:])
    ).strip()
    memory_db.bind(data_dir)
    memory_db.upsert_text(
        doc_id = f"profile:{chat_id}:{subject_id}",
        text   = profile_text,
        meta   = {
            "type": "person_profile",
            "chat_id": chat_id,
            "subject_id": subject_id,
            "subject_name": existing.subject_name,
        },
    )


def build_profile_block(memory_db: MemoryDB, *, chat_id: str, subject_id: int | None) -> str:
    if not subject_id:
        return ""
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return ""
    item = memory_db.get(f"profile:{chat_id}:{int(subject_id)}")
    if not item:
        return ""
    text = (item.text or "").strip()
    if not text:
        return ""
    return "关于当前说话人你记得：\n" + text + "\n"
