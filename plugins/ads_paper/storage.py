"""按用户隔离的论文笔记、主题、截稿日期与 BibTeX 原子存储。"""

import hashlib
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
from weakref import WeakValueDictionary

from core.plugin_base import atomic_write_bytes, atomic_write_text

from .bibtex import BibTeXParseError, citation_entries
from .constants import DATETIME_FORMAT

logger = logging.getLogger(__name__)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _owned_by(record: Any, user_id: int) -> bool:
    """兼容旧数字字符串，但拒绝布尔值和畸形记录。"""

    if not isinstance(record, dict) or type(user_id) is not int or user_id <= 0:
        return False
    owner = record.get("user")
    if isinstance(owner, bool):
        return False
    try:
        return int(owner) == user_id
    except (TypeError, ValueError):
        return False


class PaperStorageCorruptionError(RuntimeError):
    """The original document is preserved and must be explicitly repaired."""


class PaperStorage:
    _locks_guard: ClassVar[Any] = threading.Lock()
    _locks: ClassVar[WeakValueDictionary[str, Any]] = WeakValueDictionary()

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.notes_file = data_dir / "paper_notes.json"
        self.writing_file = data_dir / "writing_ideas.json"
        self.topics_file = data_dir / "research_topics.json"
        self.deadlines_file = data_dir / "deadlines.json"
        lock_key = str(data_dir.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(lock_key, threading.RLock())

    def _validate_document(self, path: Path, value: dict[str, Any]) -> None:
        """验证后续读写依赖的嵌套容器，避免覆盖结构已损坏的原文件。"""

        if path in {self.notes_file, self.writing_file}:
            collections = value.values()
        elif path == self.topics_file:
            collections = (value.get("keywords", []),)
        elif path == self.deadlines_file:
            collections = (value.get("deadlines", []),)
        else:
            return
        for records in collections:
            if not isinstance(records, list) or any(
                not isinstance(record, dict) for record in records
            ):
                raise ValueError("paper storage contains an invalid record collection")

    def _load_json(self, path: Path) -> dict[str, Any]:
        """Load an object document or fail closed after making a quarantine copy."""
        with self._lock:
            if not path.exists():
                return {}
            try:
                payload = path.read_bytes()
                value = json.loads(
                    payload.decode("utf-8"),
                    parse_constant=_reject_json_constant,
                )
                if not isinstance(value, dict):
                    raise ValueError("paper storage root must be an object")
                self._validate_document(path, value)
                return value
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                digest = hashlib.sha256(payload).hexdigest()[:12]
                quarantine = path.with_name(f"{path.name}.corrupt-{digest}")
                try:
                    if not quarantine.exists():
                        atomic_write_bytes(quarantine, payload)
                except OSError as quarantine_exc:
                    logger.error(
                        "Paper storage quarantine failed: error_type=%s",
                        type(quarantine_exc).__name__,
                    )
                logger.error(
                    "Paper storage is corrupt and read-only until repaired: error_type=%s digest=%s",
                    type(exc).__name__,
                    digest,
                )
                raise PaperStorageCorruptionError(
                    "paper storage is corrupt; original preserved for explicit recovery"
                ) from exc
            except OSError as exc:
                logger.error(
                    "Paper storage load failed: error_type=%s",
                    type(exc).__name__,
                )
                raise

    def _save_json(self, path: Path, data: dict[str, Any]) -> bool:
        """Save data to JSON file with error handling.

        Returns:
            True if successful, False otherwise
        """
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
                return True
            except Exception as exc:
                logger.error(
                    "Paper storage save failed: error_type=%s",
                    type(exc).__name__,
                )
                return False

    def add_paper_note(self, paper_id: str, content: str, user_id: int) -> bool:
        with self._lock:
            data = self._load_json(self.notes_file)
            if paper_id not in data:
                data[paper_id] = []

            note = {
                "content": content.strip(),
                "time": datetime.now().strftime(DATETIME_FORMAT),
                "user": user_id,
            }
            data[paper_id].append(note)
            return self._save_json(self.notes_file, data)

    def get_paper_notes(self, paper_id: str, user_id: int) -> list[dict[str, Any]]:
        data = self._load_json(self.notes_file)
        notes = data.get(paper_id, [])
        return [note for note in notes if _owned_by(note, user_id)]

    def delete_paper_note(self, paper_id: str, index: int, user_id: int) -> bool:
        with self._lock:
            data = self._load_json(self.notes_file)
            if paper_id not in data:
                return False

            notes = data[paper_id]
            target_indices = [i for i, note in enumerate(notes) if _owned_by(note, user_id)]
            if index < 0 or index >= len(target_indices):
                return False

            notes.pop(target_indices[index])
            if not notes:
                del data[paper_id]

            return self._save_json(self.notes_file, data)

    def add_writing_idea(self, section: str, content: str, user_id: int) -> bool:
        with self._lock:
            data = self._load_json(self.writing_file)
            if section not in data:
                data[section] = []

            idea = {
                "content": content.strip(),
                "time": datetime.now().strftime(DATETIME_FORMAT),
                "user": user_id,
            }
            data[section].append(idea)
            return self._save_json(self.writing_file, data)

    def get_writing_ideas(self, section: str, user_id: int) -> list[dict[str, Any]]:
        data = self._load_json(self.writing_file)
        return [idea for idea in data.get(section, []) if _owned_by(idea, user_id)]

    def list_writing_sections(self, user_id: int) -> list[str]:
        data = self._load_json(self.writing_file)
        return [
            section
            for section, ideas in data.items()
            if any(_owned_by(idea, user_id) for idea in ideas)
        ]

    def delete_writing_idea(self, section: str, index: int, user_id: int) -> bool:
        with self._lock:
            data = self._load_json(self.writing_file)
            if section not in data:
                return False

            ideas = data[section]
            owned_indices = [
                position for position, idea in enumerate(ideas) if _owned_by(idea, user_id)
            ]
            if index < 0 or index >= len(owned_indices):
                return False

            ideas.pop(owned_indices[index])
            if not ideas:
                del data[section]

            return self._save_json(self.writing_file, data)

    def add_topic(self, keyword: str, user_id: int) -> bool:
        with self._lock:
            data = self._load_json(self.topics_file)
            if "keywords" not in data:
                data["keywords"] = []

            keyword = keyword.strip().lower()
            entries = data["keywords"]
            if not any(
                isinstance(entry, dict)
                and entry.get("value") == keyword
                and _owned_by(entry, user_id)
                for entry in entries
            ):
                entries.append({"value": keyword, "user": user_id})
                return self._save_json(self.topics_file, data)
            return False

    def get_topics(self, user_id: int) -> list[str]:
        data = self._load_json(self.topics_file)
        return [
            str(entry.get("value"))
            for entry in data.get("keywords", [])
            if _owned_by(entry, user_id)
        ]

    def remove_topic(self, keyword: str, user_id: int) -> bool:
        with self._lock:
            data = self._load_json(self.topics_file)
            if "keywords" not in data:
                return False

            keyword = keyword.strip().lower()
            target = next(
                (
                    entry
                    for entry in data["keywords"]
                    if isinstance(entry, dict)
                    and entry.get("value") == keyword
                    and _owned_by(entry, user_id)
                ),
                None,
            )
            if target is not None:
                data["keywords"].remove(target)
                return self._save_json(self.topics_file, data)
            return False

    def clear_topics(self, user_id: int) -> bool:
        """Clear all research topics."""
        with self._lock:
            data = self._load_json(self.topics_file)
            data["keywords"] = [
                entry for entry in data.get("keywords", []) if not _owned_by(entry, user_id)
            ]
            return self._save_json(self.topics_file, data)

    def add_reference(self, user_id: int, bibcode: str, bibtex: str) -> bool:
        path = self.data_dir / f"references_{int(user_id)}.bib"
        with self._lock:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            try:
                existing_entries = citation_entries(existing)
                new_entries = citation_entries(bibtex)
            except BibTeXParseError:
                logger.error("BibTeX storage parse failed; existing file was not modified")
                raise
            existing_keys = {entry.citation_key for entry in existing_entries}
            new_keys = {entry.citation_key for entry in new_entries}
            if not new_entries or str(bibcode).strip() not in new_keys:
                raise BibTeXParseError("ADS BibTeX export does not contain the requested bibcode")
            if existing_keys.intersection(new_keys):
                return False
            separator = "\n\n" if existing.strip() else ""
            atomic_write_text(path, existing.rstrip() + separator + bibtex.strip() + "\n")
            return True

    def get_references(self, user_id: int) -> str:
        path = self.data_dir / f"references_{int(user_id)}.bib"
        with self._lock:
            return path.read_text(encoding="utf-8") if path.exists() else ""

    def add_deadline(self, name: str, date: str, user_id: int) -> bool:
        with self._lock:
            data = self._load_json(self.deadlines_file)
            if "deadlines" not in data:
                data["deadlines"] = []

            deadline = {
                "name": name.strip(),
                "date": date.strip(),
                "user": user_id,
                "created": datetime.now().strftime(DATETIME_FORMAT),
            }
            data["deadlines"].append(deadline)
            return self._save_json(self.deadlines_file, data)

    def get_deadlines(self, user_id: int) -> list[dict[str, Any]]:
        data = self._load_json(self.deadlines_file)
        deadlines = data.get("deadlines", [])
        deadlines = [deadline for deadline in deadlines if _owned_by(deadline, user_id)]
        return sorted(deadlines, key=lambda x: x.get("date", ""))

    def delete_deadline(self, index: int, user_id: int) -> bool:
        with self._lock:
            data = self._load_json(self.deadlines_file)
            if "deadlines" not in data:
                return False

            deadlines = data["deadlines"]
            ordered = [
                (i, deadline)
                for i, deadline in enumerate(deadlines)
                if _owned_by(deadline, user_id)
            ]
            ordered.sort(key=lambda item: item[1].get("date", ""))
            if index < 0 or index >= len(ordered):
                return False

            target_index, _ = ordered[index]
            deadlines.pop(target_index)
            return self._save_json(self.deadlines_file, data)
