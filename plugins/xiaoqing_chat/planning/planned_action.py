from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlannedAction:
    action: str
    think_level: int
    reasoning: str
    question: str
    unknown_words: list[str]
    params: dict[str, object] = field(default_factory=dict)
