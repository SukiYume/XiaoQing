from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

class PlanReplyLogger:
    def __init__(self) -> None:
        self._data_dir: Optional[Path] = None

    def bind(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def _trim_if_needed(self, path: Path, *, max_bytes: int = 2_000_000, keep_lines: int = 1500) -> None:
        try:
            if path.stat().st_size <= max_bytes:
                return
        except OSError:
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return
        if keep_lines > 0 and len(lines) > keep_lines:
            lines = lines[-keep_lines:]
        try:
            path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except OSError:
            return

    def log(self, *, chat_id: str, payload: dict[str, Any]) -> None:
        if not self._data_dir:
            return
        path = self._data_dir / "plan_reply_log" / f"{chat_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.time(), **payload}
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            return
        self._trim_if_needed(path)
