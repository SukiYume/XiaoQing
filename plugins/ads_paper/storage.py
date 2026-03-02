import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .constants import DATETIME_FORMAT

logger = logging.getLogger(__name__)

class PaperStorage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.notes_file = data_dir / "paper_notes.json"
        self.writing_file = data_dir / "writing_ideas.json"
        self.topics_file = data_dir / "research_topics.json"
        self.deadlines_file = data_dir / "deadlines.json"
        self._lock = threading.Lock()

    def _load_json(self, path: Path) -> dict[str, Any]:
        """Load JSON file with error handling."""
        with self._lock:
            if not path.exists():
                return {}
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                logger.error("Failed to parse JSON from %s: %s", path, e)
                return {}
            except IOError as e:
                logger.error("Failed to read file %s: %s", path, e)
                return {}
            except Exception as e:
                logger.exception("Unexpected error loading %s: %s", path, e)
                return {}

    def _save_json(self, path: Path, data: dict[str, Any]) -> bool:
        """Save data to JSON file with error handling.

        Returns:
            True if successful, False otherwise
        """
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                return True
            except IOError as e:
                logger.error("Failed to write file %s: %s", path, e)
                return False
            except Exception as e:
                logger.exception("Unexpected error saving %s: %s", path, e)
                return False

    def add_paper_note(self, paper_id: str, content: str, user_id: int) -> bool:
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

    def get_paper_notes(self, paper_id: str) -> list[dict[str, Any]]:
        data = self._load_json(self.notes_file)
        return data.get(paper_id, [])

    def delete_paper_note(self, paper_id: str, index: int) -> bool:
        data = self._load_json(self.notes_file)
        if paper_id not in data:
            return False

        notes = data[paper_id]
        if index < 0 or index >= len(notes):
            return False

        notes.pop(index)
        if not notes:
            del data[paper_id]

        return self._save_json(self.notes_file, data)

    def add_writing_idea(self, section: str, content: str, user_id: int) -> bool:
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

    def get_writing_ideas(self, section: str) -> list[dict[str, Any]]:
        data = self._load_json(self.writing_file)
        return data.get(section, [])

    def list_writing_sections(self) -> list[str]:
        data = self._load_json(self.writing_file)
        return list(data.keys())

    def delete_writing_idea(self, section: str, index: int) -> bool:
        data = self._load_json(self.writing_file)
        if section not in data:
            return False

        ideas = data[section]
        if index < 0 or index >= len(ideas):
            return False

        ideas.pop(index)
        if not ideas:
            del data[section]

        return self._save_json(self.writing_file, data)

    def add_topic(self, keyword: str) -> bool:
        data = self._load_json(self.topics_file)
        if "keywords" not in data:
            data["keywords"] = []

        keyword = keyword.strip().lower()
        if keyword not in data["keywords"]:
            data["keywords"].append(keyword)
            return self._save_json(self.topics_file, data)
        return False

    def get_topics(self) -> list[str]:
        data = self._load_json(self.topics_file)
        return data.get("keywords", [])

    def remove_topic(self, keyword: str) -> bool:
        data = self._load_json(self.topics_file)
        if "keywords" not in data:
            return False

        keyword = keyword.strip().lower()
        if keyword in data["keywords"]:
            data["keywords"].remove(keyword)
            return self._save_json(self.topics_file, data)
        return False

    def clear_topics(self) -> bool:
        """Clear all research topics."""
        data = {"keywords": []}
        return self._save_json(self.topics_file, data)

    def add_deadline(self, name: str, date: str, user_id: int) -> bool:
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

    def get_deadlines(self) -> list[dict[str, Any]]:
        data = self._load_json(self.deadlines_file)
        deadlines = data.get("deadlines", [])
        return sorted(deadlines, key=lambda x: x.get("date", ""))

    def delete_deadline(self, index: int) -> bool:
        data = self._load_json(self.deadlines_file)
        if "deadlines" not in data:
            return False

        deadlines = data["deadlines"]
        if index < 0 or index >= len(deadlines):
            return False

        deadlines.pop(index)
        return self._save_json(self.deadlines_file, data)
