"""把一个会话的旧数据写成统一记忆的导入操作。只读旧文件，不改不删。

- 向量库里的人物事实和人物资料目录里的事实 → ``person.fact``（可信度记为旧数据，首次并页面时去重）；
- 向量库里的话题摘要 → ``legacy.summary``（只在明确回指以前的事时取用，到期删除）；
- ``life/self_timeline.json`` 里这个会话产生的自我陈述 → ``self.*``（共用的记进 ``global``）；
- ``life/open_loops/<会话>.json`` → ``person.loop``。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.plugin_base import load_json

_SELF_KINDS    = {"state", "plan", "promise", "story"}
_FACT_RE       = re.compile(r"^[^：:\n]*[：:]\s*(.+?)(?:\n证据[：:].*)?$", re.DOTALL)
_KEEP_CLOSED_S = 7 * 86400.0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _read(path: Path) -> Any:
    raw: Any = load_json(path, default=None)
    return raw


def _docs(data_dir: Path) -> list[dict[str, Any]]:
    raw = _read(data_dir / "vdb" / "memory.docs.json")
    if isinstance(raw, dict):
        inner = raw.get("docs", raw)
        raw   = list(inner.values()) if isinstance(inner, dict) else []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _fact_text(text: str) -> str:
    match = _FACT_RE.match(str(text or "").strip())
    return (match.group(1) if match else str(text or "")).strip().splitlines()[0][:80]


def _person_ops(data_dir: Path, chat_id: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ops : list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()

    def add(key: str, subject_id: int, name: str, text: str, at: float) -> None:
        clean = " ".join(text.split())
        if subject_id <= 0 or not clean or (subject_id, clean) in seen:
            return
        seen.add((subject_id, clean))
        ops.append(
            {
                "op"         : "import",
                "kind"       : "person.fact",
                "key"        : key,
                "subject"    : f"person:{subject_id}",
                "text"       : clean,
                "data"       : {"name": name[:32]} if name else {},
                "observed_at": at,
            }
        )

    for doc in docs:
        meta = _mapping(doc.get("meta"))
        if meta.get("type") != "person_info" or meta.get("chat_id") != chat_id:
            continue
        add(
            str(doc.get("doc_id") or ""),
            _int(meta.get("subject_id")),
            str(meta.get("subject_name") or ""),
            _fact_text(str(doc.get("text") or "")),
            _float(meta.get("updated_at")),
        )
    folder = data_dir / "person_profiles" / chat_id
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            raw = _read(path)
            if not isinstance(raw, dict):
                continue
            subject_id = _int(raw.get("subject_id")) or _int(path.stem)
            name       = str(raw.get("subject_name") or "")
            at         = _float(raw.get("updated_at"))
            for index, fact in enumerate(raw.get("facts") or ()):
                if isinstance(fact, str):
                    add(f"profile:{chat_id}:{subject_id}:{index}", subject_id, name, fact, at)
    return ops


def _summary_ops(chat_id: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    subject = f"group:{chat_id[1:]}" if chat_id.startswith("g") else f"person:{chat_id[1:]}"
    ops     = []
    for doc in docs:
        meta = _mapping(doc.get("meta"))
        if meta.get("type") != "topic_summary" or meta.get("chat_id") != chat_id:
            continue
        keywords = [str(word)[:12] for word in meta.get("keywords") or () if str(word).strip()][:12]
        ops.append(
            {
                "op"         : "import",
                "kind"       : "legacy.summary",
                "key"        : str(doc.get("doc_id") or ""),
                "subject"    : subject,
                "text"       : str(doc.get("text") or "")[:400],
                "data"       : {"keywords": keywords},
                "observed_at": _float(meta.get("updated_at")),
            }
        )
    return ops


def _self_ops(data_dir: Path, chat_id: str) -> list[dict[str, Any]]:
    raw = _read(data_dir / "life" / "self_timeline.json")
    ops = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict) or item.get("chat_id") != chat_id:
            continue
        kind = str(item.get("kind") or "")
        if kind not in _SELF_KINDS or not str(item.get("text") or "").strip():
            continue
        if item.get("source") == "agent":
            # 旧版本从办事回复里也抽了这些（“在帮他记账”“到点提醒你”）：说的是办事进度，
            # 提醒和后台任务由工具兑现，不是她自己的生活和欠着的事。
            continue
        shared = item.get("shared") is not False and kind != "promise"
        status = str(item.get("status") or "active")
        ops.append(
            {
                "op"   : "import",
                "kind" : f"self.{kind}",
                "key"  : f"self:{item.get('entry_id')}",
                "scope": "global" if shared else chat_id,
                "text" : str(item.get("text")),
                "data" : {
                    "counterpart": str(item.get("counterpart") or "")[:20],
                    "source"     : str(item.get("source") or "chat")[:16],
                },
                "observed_at": _float(item.get("ts")),
                "expires_at" : _float(item.get("expires_at")),
                "status"     : status if status in ("active", "done", "dropped") else "active",
            }
        )
    return ops


def _loop_ops(data_dir: Path, chat_id: str) -> list[dict[str, Any]]:
    raw = _read(data_dir / "life" / "open_loops" / f"{chat_id}.json")
    ops = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        user_id = _int(item.get("user_id"))
        title   = str(item.get("title") or "").strip()
        due_at  = _float(item.get("due_at"))
        if user_id <= 0 or not title or not due_at:
            continue
        status = str(item.get("status") or "open")
        ops.append(
            {
                "op"     : "import",
                "kind"   : "person.loop",
                "key"    : f"loop:{item.get('loop_id')}",
                "subject": f"person:{user_id}",
                "text"   : title,
                "data"   : {
                    "user_id"  : user_id,
                    "user_name": str(item.get("user_name") or "")[:32],
                    "due_at"   : due_at,
                    "loop"     : "asked" if status == "asked" else "open",
                    "asked_at" : _float(item.get("asked_at")),
                    "surfaced" : _int(item.get("surfaced")),
                },
                "observed_at": _float(item.get("created_at")),
                "expires_at" : due_at + _KEEP_CLOSED_S,
                "status"     : "done" if status == "closed" else "active",
            }
        )
    return ops


def legacy_ops(data_dir: Path, chat_id: str) -> list[dict[str, Any]]:
    """这个会话要导入的全部旧数据；任一部分读不出来时跳过那部分。"""

    data_dir = Path(data_dir)
    docs     = _docs(data_dir)
    ops      = [
        *_person_ops(data_dir, chat_id, docs),
        *_summary_ops(chat_id, docs),
        *_self_ops(data_dir, chat_id),
        *_loop_ops(data_dir, chat_id),
    ]
    return [op for op in ops if op.get("key")]
