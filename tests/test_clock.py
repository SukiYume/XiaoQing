"""
Tests for core/clock.py - Clock and random interfaces for testability
"""

import sys
import time
from typing import Sequence
from typing import TypeVar
from unittest.mock import MagicMock, Mock

import pytest

from core.clock import (
    IClock,
    IRandom,
    SystemClock,
    SystemRandom,
)

T = TypeVar("T")

# ============================================================
# IClock Protocol Tests
# ============================================================

@pytest.mark.unit
def test_iclock_protocol_has_now_method():
    """Test IClock protocol requires now() method"""
    class MockClock:
        def now(self):
            return 1234567890.0

    clock = MockClock()
    assert hasattr(clock, "now")
    assert callable(clock.now)
    assert isinstance(clock.now(), float)


@pytest.mark.unit
def test_iclock_protocol_with_mock():
    """Test IClock protocol with mock"""
    mock_clock = MagicMock()
    mock_clock.now.return_value = 123.456

    assert hasattr(mock_clock, "now")
    assert callable(mock_clock.now)
    assert mock_clock.now() == 123.456


# ============================================================
# IRandom Protocol Tests
# ============================================================

@pytest.mark.unit
def test_irandom_protocol_has_required_methods():
    """Test IRandom protocol requires random() and choice() methods"""
    class MockRandom:
        def random(self):
            return 0.5

        def choice(self, seq: Sequence[T]) -> T:
            return seq[0] if seq else None

    random_obj = MockRandom()
    assert hasattr(random_obj, "random")
    assert callable(random_obj.random)
    assert hasattr(random_obj, "choice")
    assert callable(random_obj.choice)
    assert isinstance(random_obj.random(), float)
    assert random_obj.choice([1, 2, 3]) == 1


@pytest.mark.unit
def test_irandom_protocol_with_mock():
    """Test IRandom protocol with mock"""
    mock_random = MagicMock()
    mock_random.random.return_value = 0.75
    mock_random.choice.return_value = "selected"

    assert mock_random.random() == 0.75
    assert mock_random.choice(["a", "b", "c"]) == "selected"


# ============================================================
# SystemClock Tests
# ============================================================

@pytest.mark.unit
def test_system_clock_now():
    """Test SystemClock.now returns current time"""
    clock = SystemClock()
    now = clock.now()

    assert isinstance(now, float)
    assert now > 0


@pytest.mark.unit
def test_system_clock_now_increases():
    """Test SystemClock.now increases over time"""
    clock = SystemClock()
    now1 = clock.now()
    time.sleep(0.01)  # Small delay
    now2 = clock.now()

    assert now2 > now1


@pytest.mark.unit
def test_system_clock_matches_time_time():
    """Test SystemClock.now matches time.time()"""
    clock = SystemClock()
    clock_now = clock.now()
    time_now = time.time()

    # Should be very close (within 1 second)
    assert abs(clock_now - time_now) < 1.0


@pytest.mark.unit
def test_system_clock_has_now_method():
    """Test SystemClock has now() method (duck typing)"""
    clock = SystemClock()
    assert hasattr(clock, "now")
    assert callable(clock.now)


# ============================================================
# SystemRandom Tests
# ============================================================

@pytest.mark.unit
def test_system_random_random():
    """Test SystemRandom.random returns valid range"""
    random_obj = SystemRandom()
    value = random_obj.random()

    assert isinstance(value, float)
    assert 0.0 <= value < 1.0


@pytest.mark.unit
def test_system_random_random_distribution():
    """Test SystemRandom.random produces varied values"""
    random_obj = SystemRandom()
    values = [random_obj.random() for _ in range(100)]

    # Should have variety
    assert len(set(values)) > 90  # Most should be unique
    assert all(0.0 <= v < 1.0 for v in values)


@pytest.mark.unit
def test_system_random_choice():
    """Test SystemRandom.choice returns element from sequence"""
    random_obj = SystemRandom()
    seq = [1, 2, 3, 4, 5]

    result = random_obj.choice(seq)
    assert result in seq


@pytest.mark.unit
def test_system_random_choice_single_element():
    """Test SystemRandom.choice with single element"""
    random_obj = SystemRandom()
    seq = ["only"]

    result = random_obj.choice(seq)
    assert result == "only"


@pytest.mark.unit
def test_system_random_choice_string():
    """Test SystemRandom.choice with string"""
    random_obj = SystemRandom()
    result = random_obj.choice("hello")

    assert result in "hello"


@pytest.mark.unit
def test_system_random_choice_empty_sequence():
    """Test SystemRandom.choice raises on empty sequence"""
    random_obj = SystemRandom()

    with pytest.raises(IndexError):
        random_obj.choice([])


@pytest.mark.unit
def test_system_random_has_required_methods():
    """Test SystemRandom has required methods (duck typing)"""
    random_obj = SystemRandom()
    assert hasattr(random_obj, "random")
    assert callable(random_obj.random)
    assert hasattr(random_obj, "choice")
    assert callable(random_obj.choice)


# ============================================================
# Protocol Compatibility Tests
# ============================================================

@pytest.mark.unit
def test_custom_clock_implementation():
    """Test custom clock implementation matches protocol"""
    class CustomClock:
        def __init__(self, fixed_time):
            self._fixed_time = fixed_time

        def now(self):
            return self._fixed_time

    clock = CustomClock(1234567890.0)
    # Duck typing - just verify it has the method and works
    assert hasattr(clock, "now")
    assert callable(clock.now)
    assert clock.now() == 1234567890.0


@pytest.mark.unit
def test_custom_random_implementation():
    """Test custom random implementation matches protocol"""
    class DeterministicRandom:
        def __init__(self, sequence):
            self._sequence = sequence
            self._index = 0

        def random(self):
            value = self._sequence[self._index % len(self._sequence)]
            self._index += 1
            return value

        def choice(self, seq):
            if not seq:
                return None
            return seq[self._index % len(seq)]

    random_obj = DeterministicRandom([0.1, 0.5, 0.9])
    # Duck typing - verify it has the methods
    assert hasattr(random_obj, "random")
    assert hasattr(random_obj, "choice")
    assert random_obj.random() == 0.1
    assert random_obj.random() == 0.5


# ============================================================
# Mock Clock for Testing Tests
# ============================================================

@pytest.mark.unit
def test_mock_clock_for_testing():
    """Test using mock clock for deterministic tests"""
    class MockClock:
        def __init__(self):
            self._current_time = 1000.0

        def set_time(self, t):
            self._current_time = t

        def now(self):
            return self._current_time

    clock = MockClock()
    assert clock.now() == 1000.0

    clock.set_time(2000.0)
    assert clock.now() == 2000.0


@pytest.mark.unit
def test_mock_random_for_testing():
    """Test using mock random for deterministic tests"""
    class MockRandom:
        def __init__(self, return_values=None):
            self._values = return_values or [0.5]
            self._index = 0

        def random(self):
            value = self._values[self._index % len(self._values)]
            self._index += 1
            return value

        def choice(self, seq):
            if not seq:
                return None
            return seq[0]  # Always return first

    random_obj = MockRandom([0.1, 0.2, 0.3])
    assert random_obj.random() == 0.1
    assert random_obj.random() == 0.2
    assert random_obj.choice(["a", "b", "c"]) == "a"


# ============================================================
# Integration Tests
# ============================================================

@pytest.mark.unit
def test_clock_and_random_together():
    """Test using both clock and random interfaces together"""
    class MockSystem:
        def __init__(self):
            self.clock = SystemClock()
            self.random = SystemRandom()

        def get_timestamp_and_random(self):
            return {
                "timestamp": self.clock.now(),
                "random_value": self.random.random(),
            }

    system = MockSystem()
    data = system.get_timestamp_and_random()

    assert "timestamp" in data
    assert "random_value" in data
    assert isinstance(data["timestamp"], float)
    assert isinstance(data["random_value"], float)
    assert 0.0 <= data["random_value"] < 1.0
