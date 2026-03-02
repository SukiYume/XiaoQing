"""Clock and random interfaces for testability."""

from __future__ import annotations

import random
import time
from typing import Protocol, Sequence, TypeVar


T = TypeVar("T")


class IClock(Protocol):
    def now(self) -> float:
        ...


class IRandom(Protocol):
    def random(self) -> float:
        ...

    def choice(self, seq: Sequence[T]) -> T:
        ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class SystemRandom:
    def random(self) -> float:
        return random.random()

    def choice(self, seq: Sequence[T]) -> T:
        return random.choice(seq)
