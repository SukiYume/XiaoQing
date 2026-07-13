import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from core.plugin_base import atomic_write_text

from .constants import DATETIME_FORMAT

logger = logging.getLogger(__name__)

class PaperStorage:
    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.notes_file = data_dir / "paper_notes.json"
        self.writing_file = data_dir / "writing_ideas.json"
        self.topics_file = data_dir / "research_topics.json"
        self.deadlines_file = data_dir / "deadlines.json"
        lock_key = str(data_dir.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(lock_key, threading.RLock())

    def _load_json(self, path: Path) -> dict[str, Any]:
        """Load JSON file with error handling."""
        with self._lock:
            if not path.exists():
                return {}
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.error("Paper storage load failed: error_type=JSONDecodeError")
                return {}
            except OSError as exc:
                logger.error(
                    "Paper storage load failed: error_type=%s",
                    type(exc).__name__,
                )
                return {}
            except Exception as exc:
                logger.error(
                    "Paper storage load failed: error_type=%s",
                    type(exc).__name__,
                )
                return {}

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
                "user": user_id
            }
            data[paper_id].append(note)
            return self._save_json(self.notes_file, data)

    def get_paper_notes(self, paper_id: str, user_id: int) -> list[dict[str, Any]]:
        data = self._load_json(self.notes_file)
        notes = data.get(paper_id, [])
        return [note for note in notes if int(note.get("user", -1)) == int(user_id)]

    def delete_paper_note(self, paper_id: str, index: int, user_id: int) -> bool:
        with self._lock:
            data = self._load_json(self.notes_file)
            if paper_id not in data:
                return False

            notes = data[paper_id]
            target_indices = [
                i for i, note in enumerate(notes)
                if int(note.get("user", -1)) == int(user_id)
            ]
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
                "user": user_id
            }
            data[section].append(idea)
            return self._save_json(self.writing_file, data)

    def get_writing_ideas(self, section: str, user_id: int) -> list[dict[str, Any]]:
        data = self._load_json(self.writing_file)
        return [
            idea for idea in data.get(section, [])
            if int(idea.get("user", -1)) == int(user_id)
        ]

    def list_writing_sections(self, user_id: int) -> list[str]:
        data = self._load_json(self.writing_file)
        return [
            section for section, ideas in data.items()
            if any(int(idea.get("user", -1)) == int(user_id) for idea in ideas)
        ]

    def delete_writing_idea(self, section: str, index: int, user_id: int) -> bool:
        with self._lock:
            data = self._load_json(self.writing_file)
            if section not in data:
                return False

            ideas = data[section]
            owned_indices = [
                position for position, idea in enumerate(ideas)
                if int(idea.get("user", -1)) == int(user_id)
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
                and int(entry.get("user", -1)) == int(user_id)
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
            if isinstance(entry, dict) and int(entry.get("user", -1)) == int(user_id)
        ]

    def remove_topic(self, keyword: str, user_id: int) -> bool:
        with self._lock:
            data = self._load_json(self.topics_file)
            if "keywords" not in data:
                return False

            keyword = keyword.strip().lower()
            target = next(
                (
                    entry for entry in data["keywords"]
                    if isinstance(entry, dict)
                    and entry.get("value") == keyword
                    and int(entry.get("user", -1)) == int(user_id)
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
                entry for entry in data.get("keywords", [])
                if not isinstance(entry, dict) or int(entry.get("user", -1)) != int(user_id)
            ]
            return self._save_json(self.topics_file, data)

    def add_reference(self, user_id: int, bibcode: str, bibtex: str) -> bool:
        path = self.data_dir / f"references_{int(user_id)}.bib"
        with self._lock:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            if re.search(r"@\w+\{" + re.escape(bibcode) + r",", existing):
                return False
            atomic_write_text(path, existing + "\n" + bibtex + "\n")
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
                "created": datetime.now().strftime(DATETIME_FORMAT)
            }
            data["deadlines"].append(deadline)
            return self._save_json(self.deadlines_file, data)

    def get_deadlines(self, user_id: int) -> list[dict[str, Any]]:
        data = self._load_json(self.deadlines_file)
        deadlines = data.get("deadlines", [])
        deadlines = [
            deadline for deadline in deadlines
            if int(deadline.get("user", -1)) == int(user_id)
        ]
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
                if int(deadline.get("user", -1)) == int(user_id)
            ]
            ordered.sort(key=lambda item: item[1].get("date", ""))
            if index < 0 or index >= len(ordered):
                return False

            target_index, _ = ordered[index]
            deadlines.pop(target_index)
            return self._save_json(self.deadlines_file, data)
