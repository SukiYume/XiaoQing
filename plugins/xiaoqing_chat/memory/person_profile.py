from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

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

def load_profile(data_dir: Path, *, chat_id: str, subject_id: int) -> Optional[PersonProfile]:
    path = _profile_path(data_dir, chat_id, subject_id)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("subject_name", "")).strip()
        updated_at = float(raw.get("updated_at", 0.0) or 0.0)
        facts = raw.get("facts", [])
        if not isinstance(facts, list):
            facts = []
        fact_list = [str(x).strip() for x in facts if isinstance(x, str) and str(x).strip()]
        return PersonProfile(chat_id=chat_id, subject_id=subject_id, subject_name=name or str(subject_id), facts=fact_list, updated_at=updated_at)
    except Exception:
        return None

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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

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
        chat_id=chat_id, subject_id=subject_id, subject_name=subject_name or str(subject_id), facts=[], updated_at=0.0
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
        doc_id=f"profile:{chat_id}:{subject_id}",
        text=profile_text,
        meta={"type": "person_profile", "chat_id": chat_id, "subject_id": subject_id, "subject_name": existing.subject_name},
    )

def build_profile_block(memory_db: MemoryDB, *, chat_id: str, subject_id: Optional[int]) -> str:
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
