"""按会话和主体保存人物资料，并同步长期记忆索引。"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.atomic_store import atomic_write_text, keyed_path_lock
from core.plugin_base import load_json, write_json

from ..store_base import coerce_finite_float
from .memory_db import MemoryDB

# 同一时间只有一个值的属性：改口时新值替换旧值，旧值留作“以前说过”。
SINGLE_VALUED_ATTRIBUTES = frozenset(
    {"居住地", "家乡", "学校", "专业", "年级", "工作", "公司", "年龄", "生日", "感情状态", "称呼"}
)
_MAX_SUPERSEDED = 20


@dataclass
class PersonProfile:
    chat_id     : str
    subject_id  : int
    subject_name: str
    facts       : list[str]
    updated_at  : float
    # 单值属性 → 当前生效的那条事实。
    attributes: dict[str, str] = field(default_factory=dict)
    # 被改口替换的旧事实：{"attribute", "fact", "replaced_by", "replaced_at"}。
    superseded: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class SupersededFact:
    attribute: str
    old_fact : str
    new_fact : str


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


def write_if_profile_generation(
    data_dir: Path,
    chat_id: str,
    expected_generation: str | None,
    write: Callable[[], None],
) -> None:
    """在画像代际锁内执行写入；提取开始后画像被重置（代际变化）时放弃，旧结果不会重新落盘。"""

    with keyed_path_lock(profile_generation_path(data_dir, chat_id)):
        if (
            expected_generation is not None
            and get_profile_generation(data_dir, chat_id) != expected_generation
        ):
            return
        write()


def clear_profiles_and_memory(data_dir: Path, chat_id: str, memory_db: MemoryDB | None) -> None:
    """重置档案、备份与向量记录，并使所有旧提取任务失效。"""
    generation_path = profile_generation_path(data_dir, chat_id)
    with keyed_path_lock(generation_path):
        atomic_write_text(generation_path, str(time.time_ns()))
        for path in generation_path.parent.iterdir():
            if path.name.endswith((".json", ".json.bak")) and path.is_file():
                path.unlink()
        if memory_db is not None and memory_db.delete_chat(chat_id):
            memory_db.save()


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
    raw_attributes = raw.get("attributes")
    attributes     = {
        str(key): str(value)
        for key, value in (raw_attributes.items() if isinstance(raw_attributes, dict) else ())
        if str(key) in SINGLE_VALUED_ATTRIBUTES and str(value) in fact_list
    }
    raw_superseded = raw.get("superseded")
    superseded     = [
        item
        for item in (raw_superseded if isinstance(raw_superseded, list) else ())
        if isinstance(item, dict) and str(item.get("fact") or "").strip()
    ][-_MAX_SUPERSEDED:]
    return PersonProfile(
        chat_id      = chat_id,
        subject_id   = subject_id,
        subject_name = name or str(subject_id),
        facts        = fact_list,
        updated_at=coerce_finite_float(raw.get("updated_at"), default=0.0, minimum=0.0),
        attributes = attributes,
        superseded = superseded,
    )


def save_profile(data_dir: Path, profile: PersonProfile) -> None:
    path = _profile_path(data_dir, str(profile.chat_id), profile.subject_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "chat_id"     : profile.chat_id,
        "subject_id"  : profile.subject_id,
        "subject_name": profile.subject_name,
        "facts"       : profile.facts,
        "updated_at"  : profile.updated_at,
        "attributes"  : profile.attributes,
        "superseded"  : profile.superseded,
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
    max_facts: int                       = 120,
    attributes: Mapping[str, str] | None = None,
) -> list[SupersededFact]:
    """追加新事实并更新画像索引。

    ``attributes`` 把新事实映射到单值属性；同一属性已有不同的事实时，旧事实移出当前
    资料、记进 ``superseded``，返回这些被替换的旧事实，调用方据此标记旧的检索记录。
    """

    replaced: list[SupersededFact] = []
    if subject_id <= 0:
        return replaced
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return replaced
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
        if not f:
            continue
        attribute = str((attributes or {}).get(f) or "")
        if attribute in SINGLE_VALUED_ATTRIBUTES:
            old = existing.attributes.get(attribute)
            if old and old != f:
                existing.facts = [fact for fact in existing.facts if fact != old]
                seen.discard(old)
                existing.superseded.append(
                    {"attribute": attribute, "fact": old, "replaced_by": f, "replaced_at": now}
                )
                replaced.append(SupersededFact(attribute, old, f))
            existing.attributes[attribute] = f
        if f in seen:
            continue
        existing.facts.append(f)
        seen.add(f)
    if max_facts > 0 and len(existing.facts) > max_facts:
        existing.facts = existing.facts[-max_facts:]
    live                = set(existing.facts)
    existing.attributes = {
        key: value for key, value in existing.attributes.items() if value in live
    }
    existing.superseded = existing.superseded[-_MAX_SUPERSEDED:]
    existing.updated_at = now

    save_profile(data_dir, existing)

    profile_text = _profile_text(existing)
    memory_db.bind(data_dir)
    memory_db.upsert_text(
        doc_id = f"profile:{chat_id}:{subject_id}",
        text   = profile_text,
        meta   = {
            "type"        : "person_profile",
            "chat_id"     : chat_id,
            "subject_id"  : subject_id,
            "subject_name": existing.subject_name,
        },
    )
    return replaced


def _profile_text(profile: PersonProfile) -> str:
    lines = [f"{profile.subject_name}<{profile.subject_id}> 的已知信息："]
    lines += [f"- {fact}" for fact in profile.facts[-20:]]
    earlier = profile.superseded[-3:]
    if earlier:
        lines.append("以前说过、后来改了（按现在的说法为准）：")
        lines += [f"- {item.get('attribute')}：原来是“{item.get('fact')}”" for item in earlier]
    return "\n".join(lines).strip()


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
