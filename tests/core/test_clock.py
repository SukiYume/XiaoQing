# 验证时钟和随机源替身为测试提供确定性结果。
"""
Tests for core/clock.py - Clock and random interfaces for testability
"""

import time

import pytest

from core.clock import (
    SystemClock,
    SystemRandom,
    now_in_configured_timezone,
)


def test_now_in_configured_timezone_uses_settings_snapshot() -> None:
    class Context:
        def get_settings_snapshot(self):
            return type("Settings", (), {"config": {"timezone": "UTC"}})()

    value = now_in_configured_timezone(Context())
    assert value.tzinfo is not None
    assert value.utcoffset().total_seconds() == 0


@pytest.mark.unit
def test_system_clock_now():
    """Test SystemClock.now returns current time"""
    clock = SystemClock()
    now   = clock.now()

    assert isinstance(now, float)
    assert now > 0


@pytest.mark.unit
def test_system_clock_matches_time_time():
    """Test SystemClock.now matches time.time()"""
    clock     = SystemClock()
    clock_now = clock.now()
    time_now  = time.time()

    # Should be very close (within 1 second)
    assert abs(clock_now - time_now) < 1.0


@pytest.mark.unit
def test_system_random_random():
    """Test SystemRandom.random returns valid range"""
    random_obj = SystemRandom()
    value      = random_obj.random()

    assert isinstance(value, float)
    assert 0.0 <= value < 1.0


@pytest.mark.unit
def test_system_random_choice():
    """Test SystemRandom.choice returns element from sequence"""
    random_obj = SystemRandom()
    seq        = [1, 2, 3, 4, 5]

    result = random_obj.choice(seq)
    assert result in seq


@pytest.mark.unit
def test_system_random_choice_single_element():
    """Test SystemRandom.choice with single element"""
    random_obj = SystemRandom()
    seq        = ["only"]

    result = random_obj.choice(seq)
    assert result == "only"


@pytest.mark.unit
def test_system_random_choice_string():
    """Test SystemRandom.choice with string"""
    random_obj = SystemRandom()
    result     = random_obj.choice("hello")

    assert result in "hello"


@pytest.mark.unit
def test_system_random_choice_empty_sequence():
    """Test SystemRandom.choice raises on empty sequence"""
    random_obj = SystemRandom()

    with pytest.raises(IndexError):
        random_obj.choice([])
