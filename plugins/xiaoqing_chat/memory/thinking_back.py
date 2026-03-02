from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

@dataclass(frozen=True)
class ThinkingBackRecord:
    ts: float
    question: str
    answer: str

def _path(data_dir: Path, chat_id: str) -> Path:
    return data_dir / "thinking_back" / f"{chat_id}.jsonl"

def _load_recent(path: Path, *, max_lines: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[-max_lines:]
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out

def get_cached_answer(
    *,
    data_dir: Path,
    chat_id: str,
    question: str,
    window_seconds: float,
    max_scan_lines: int = 400,
) -> str:
    q = (question or "").strip()
    if not q:
        return ""
    path = _path(data_dir, chat_id)
    now = time.time()
    for obj in reversed(_load_recent(path, max_lines=max_scan_lines)):
        ts_val = obj.get("ts", 0.0)
        try:
            ts = float(ts_val)
        except Exception:
            ts = 0.0
        if window_seconds > 0 and ts and now - ts > window_seconds:
            continue
        if str(obj.get("question", "")).strip() != q:
            continue
        ans = str(obj.get("answer", "")).strip()
        if ans:
            return ans
    return ""

def append_record(
    *,
    data_dir: Path,
    chat_id: str,
    question: str,
    answer: str,
    max_entries: int = 200,
    max_bytes: int = 2_000_000,
) -> None:
    q = (question or "").strip()
    a = (answer or "").strip()
    if not q or not a:
        return
    path = _path(data_dir, chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "question": q, "answer": a}
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return

    try:
        if path.stat().st_size <= max_bytes:
            return
    except OSError:
        return

    items = _load_recent(path, max_lines=max(1, int(max_entries)) if max_entries > 0 else 200)
    try:
        with path.open("w", encoding="utf-8") as f:
            for obj in items[-max_entries:]:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError:
        return
