"""guess_number 插件的命令、状态不变量和会话所有权回归测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from core.context import PluginContext
from core.session import Session, SessionManager
from plugins.guess_number import main as guess_number


class MockSession:
    def __init__(
        self,
        data: dict[str, Any] | None = None,
        *,
        plugin_name: str = guess_number.PLUGIN_NAME,
    ) -> None:
        self.plugin_name = plugin_name
        self.data        = dict(data or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class MockContext:
    def __init__(self, session: MockSession | None = None) -> None:
        self._session                                           = session
        self.created: list[tuple[dict[str, Any], float | None]] = []
        self.end_calls                                          = 0
        self.request_id                                         = "guess-number-test"
        self.secrets: dict[str, Any]                            = {"plugins": {"guess_number": {}}}

    async def create_session(
        self,
        initial_data: dict[str, Any] | None = None,
        timeout: float | None               = None,
    ) -> MockSession:
        data = dict(initial_data or {})
        self.created.append((data, timeout))
        self._session = MockSession(data)
        return self._session

    async def get_session(self) -> MockSession | None:
        return self._session

    async def end_session(self) -> bool:
        self.end_calls += 1
        existed       = self._session is not None
        self._session = None
        return existed


@pytest.fixture
def context() -> MockContext:
    return MockContext()


@pytest.fixture
def event() -> dict[str, Any]:
    return {"user_id": 12345, "message_type": "private"}


def _game_data(
    *,
    target: int               = 50,
    minimum: int              = 1,
    maximum: int              = 100,
    attempts: int             = 0,
    history: list[int] | None = None,
    difficulty: str           = "normal",
    max_attempts: int         = 7,
) -> dict[str, Any]:
    return {
        "target": target,
        "min": minimum,
        "max": maximum,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "history": list(history or []),
        "difficulty": difficulty,
    }


def _text(response: guess_number.Segments) -> str:
    assert len(response) == 1
    return str(response[0]["data"]["text"])


def _real_context(tmp_path: Path, manager: SessionManager) -> PluginContext:
    return PluginContext(
        config              = {},
        secrets             = {},
        plugin_name         = guess_number.PLUGIN_NAME,
        plugin_dir          = tmp_path,
        data_dir            = tmp_path / "data",
        http_session        = None,
        send_action         = AsyncMock(),
        reload_config       = lambda: None,
        reload_plugins      = lambda: None,
        get_command_catalog = tuple,
        list_plugins        = list,
        session_manager     = manager,
        current_user_id     = 123,
        current_group_id    = 456,
        request_id          = "guess-number-integration",
    )


@pytest.mark.parametrize(
    ("args", "action", "difficulty"),
    [
        ("", "start", "normal"),
        ("easy", "start", "easy"),
        ("E", "start", "easy"),
        ("简单", "start", "easy"),
        ("normal", "start", "normal"),
        ("普通", "start", "normal"),
        ("hard", "start", "hard"),
        ("h", "start", "hard"),
        ("困难", "start", "hard"),
        ("hell", "start", "hell"),
        ("nightmare", "start", "hell"),
        ("地狱", "start", "hell"),
        ("help", "help", "normal"),
        ("帮助", "help", "normal"),
        ("?", "help", "normal"),
        ("status", "status", "normal"),
        ("信息", "status", "normal"),
        ("restart", "restart", "normal"),
        ("重开", "restart", "normal"),
    ],
)
def test_parse_request_aliases(args: str, action: str, difficulty: str) -> None:
    request = guess_number._parse_request(args)
    assert request.action == action
    assert request.difficulty.key == difficulty


@pytest.mark.parametrize(
    "args",
    ["unknown", "easy extra", '"unfinished', "easy\n", "x" * 65],
)
def test_parse_request_rejects_incomplete_or_ambiguous_input(args: str) -> None:
    with pytest.raises(guess_number.GuessNumberCommandError):
        guess_number._parse_request(args)


def test_parse_request_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        guess_number._parse_request(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "difficulty", "maximum", "attempts"),
    [
        ("", "normal", 100, 7),
        ("easy", "easy", 50, 10),
        ("hard", "hard", 200, 8),
        ("hell", "hell", 1000, 10),
    ],
)
async def test_handle_starts_each_difficulty_with_minimal_state(
    context: MockContext,
    event: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    args: str,
    difficulty: str,
    maximum: int,
    attempts: int,
) -> None:
    monkeypatch.setattr(guess_number.secrets, "randbelow", lambda size: min(41, size - 1))

    response = await guess_number.handle("猜数字", args, event, context)

    assert "游戏开始" in _text(response)
    assert context._session is not None
    assert context._session.get("difficulty") == difficulty
    assert context._session.get("max") == maximum
    assert context._session.get("max_attempts") == attempts
    assert context._session.get("target") == 42
    assert "remaining" not in context._session.data
    assert "hint" not in context._session.data
    assert context.created[0][1] == guess_number.SESSION_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_help_and_invalid_command_do_not_create_session(
    context: MockContext,
    event: dict[str, Any],
) -> None:
    assert "猜数字游戏" in _text(await guess_number.handle("猜数字", "help", event, context))
    assert "未知难度" in _text(await guess_number.handle("猜数字", "impossible", event, context))
    assert context.created == []


@pytest.mark.asyncio
async def test_existing_own_game_is_reported_without_replacement(
    event: dict[str, Any],
) -> None:
    session = MockSession(_game_data(target=42))
    context = MockContext(session)

    response = await guess_number.handle("猜数字", "", event, context)

    assert "进行中的游戏" in _text(response)
    assert context._session is session
    assert context.created == []
    assert context.end_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["", "restart", "status"])
async def test_foreign_session_is_never_deleted_or_replaced(
    event: dict[str, Any],
    args: str,
) -> None:
    session = MockSession({"token": "keep"}, plugin_name="other_plugin")
    context = MockContext(session)

    response = await guess_number.handle("猜数字", args, event, context)

    assert "其他插件会话" in _text(response)
    assert context._session is session
    assert context.created == []
    assert context.end_calls == 0


@pytest.mark.asyncio
async def test_restart_replaces_only_own_game(
    event: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = MockContext(MockSession(_game_data(target=42)))
    monkeypatch.setattr(guess_number.secrets, "randbelow", lambda _size: 10)

    response = await guess_number.handle("猜数字", "restart", event, context)

    assert "游戏开始" in _text(response)
    assert context.end_calls == 1
    assert context.created[0][0]["target"] == 11


@pytest.mark.asyncio
async def test_start_discards_invalid_own_state_before_recreating(
    event: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = MockContext(MockSession(_game_data(target=True)))
    monkeypatch.setattr(guess_number.secrets, "randbelow", lambda _size: 0)

    response = await guess_number.handle("猜数字", "", event, context)

    assert "游戏开始" in _text(response)
    assert context.end_calls == 1
    assert context._session is not None
    assert context._session.get("target") == 1


@pytest.mark.asyncio
async def test_status_handles_none_valid_and_invalid_sessions(
    event: dict[str, Any],
) -> None:
    context = MockContext()
    assert "当前没有" in _text(await guess_number.handle("猜数字", "status", event, context))

    context._session = MockSession(
        _game_data(
            target   = 50,
            minimum  = 26,
            maximum  = 74,
            attempts = 2,
            history  = [25, 75],
        )
    )
    rendered = _text(await guess_number.handle("猜数字", "status", event, context))
    assert "26-74" in rendered
    assert "5/7" in rendered
    assert "25 → 75" in rendered

    context._session = MockSession(_game_data(max_attempts=8))
    assert "状态无效" in _text(await guess_number.handle("猜数字", "status", event, context))
    assert context._session is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("42", 42),
        (" 0042 ", 42),
        ("status", "status"),
        ("状态", "status"),
        ("?", "status"),
        ("/猜数字 status", "status"),
        ("/guess 信息", "status"),
        ("/猜 restart", "restart"),
        ("/猜数字 help", "help"),
        ("/猜数字 restart extra", None),
        ("+42", None),
        ("-1", None),
        ("1_0", None),
        ("٤٢", None),
        ("42\n", None),
        ("9" * 65, None),
    ],
)
def test_parse_session_input_is_bounded_ascii(text: str, expected: object) -> None:
    assert guess_number._parse_session_input(text) == expected


def test_parse_session_input_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        guess_number._parse_session_input(42)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["abc", "+42", "٤٢", "42\n", "9" * 65])
async def test_invalid_guess_does_not_consume_attempt(
    event: dict[str, Any],
    text: str,
) -> None:
    session = MockSession(_game_data())
    context = MockContext(session)

    response = await guess_number.handle_session(text, event, context, session)

    assert "ASCII 数字" in _text(response)
    assert session.get("attempts") == 0
    assert session.get("history") == []


@pytest.mark.asyncio
async def test_out_of_dynamic_range_does_not_consume_attempt(event: dict[str, Any]) -> None:
    session = MockSession(
        _game_data(target=50, minimum=26, maximum=74, attempts=2, history=[25, 75])
    )
    context = MockContext(session)

    response = await guess_number.handle_session("75", event, context, session)

    assert "26 到 74" in _text(response)
    assert session.get("attempts") == 2
    assert session.get("history") == [25, 75]


@pytest.mark.asyncio
async def test_low_and_high_guesses_update_only_canonical_state(
    event: dict[str, Any],
) -> None:
    session = MockSession(_game_data())
    context = MockContext(session)

    low = _text(await guess_number.handle_session("25", event, context, session))
    assert "太小" in low and "26-100" in low
    assert session.data == _game_data(
        target   = 50,
        minimum  = 26,
        maximum  = 100,
        attempts = 1,
        history  = [25],
    )

    high = _text(await guess_number.handle_session("75", event, context, session))
    assert "太大" in high and "26-74" in high
    assert session.data == _game_data(
        target   = 50,
        minimum  = 26,
        maximum  = 74,
        attempts = 2,
        history  = [25, 75],
    )


@pytest.mark.asyncio
async def test_session_status_uses_same_formatter_without_mutation(
    event: dict[str, Any],
) -> None:
    session = MockSession(
        _game_data(target=50, minimum=26, maximum=74, attempts=2, history=[25, 75])
    )
    context = MockContext(session)
    before  = dict(session.data)

    response = await guess_number.handle_session("info", event, context, session)

    assert "26-74" in _text(response)
    assert "25 → 75" in _text(response)
    assert session.data == before


@pytest.mark.asyncio
async def test_prefixed_session_controls_are_not_swallowed(
    event: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = MockSession(
        _game_data(target=50, minimum=26, maximum=74, attempts=2, history=[25, 75])
    )
    context = MockContext(session)
    before  = dict(session.data)

    status = await guess_number.handle_session("/猜数字 status", event, context, session)
    assert "26-74" in _text(status)
    assert session.data == before

    help_response = await guess_number.handle_session("/猜数字 help", event, context, session)
    assert "猜数字游戏" in _text(help_response)
    assert session.data == before

    monkeypatch.setattr(guess_number.secrets, "randbelow", lambda _size: 10)
    restarted = await guess_number.handle_session("/猜数字 restart", event, context, session)
    assert "游戏开始" in _text(restarted)
    assert context.end_calls == 1
    assert context.created[-1][0]["target"] == 11


@pytest.mark.asyncio
async def test_prefixed_session_control_with_extra_arg_is_rejected_without_mutation(
    event: dict[str, Any],
) -> None:
    session = MockSession(_game_data())
    context = MockContext(session)
    before  = dict(session.data)

    response = await guess_number.handle_session("/猜数字 restart extra", event, context, session)

    assert "ASCII 数字" in _text(response)
    assert session.data == before
    assert context.end_calls == 0


@pytest.mark.asyncio
async def test_correct_guess_ends_game_and_rates_result(event: dict[str, Any]) -> None:
    session = MockSession(_game_data(target=42))
    context = MockContext(session)

    response = await guess_number.handle_session("42", event, context, session)

    rendered = _text(response)
    assert "猜对" in rendered
    assert "一发入魂" in rendered
    assert "42" in rendered
    assert context._session is None
    assert context.end_calls == 1


@pytest.mark.asyncio
async def test_last_wrong_guess_ends_game_without_persisting_dead_state(
    event: dict[str, Any],
) -> None:
    session = MockSession(
        _game_data(
            target   = 50,
            minimum  = 7,
            maximum  = 100,
            attempts = 6,
            history  = [1, 2, 3, 4, 5, 6],
        )
    )
    context = MockContext(session)

    response = await guess_number.handle_session("7", event, context, session)

    assert "次数用尽" in _text(response)
    assert "正确答案是: 50" in _text(response)
    assert context._session is None
    assert session.get("attempts") == 6


@pytest.mark.asyncio
async def test_invalid_active_state_is_ended_safely(event: dict[str, Any]) -> None:
    session = MockSession(_game_data(target=50, attempts=1, history=[50]))
    context = MockContext(session)

    response = await guess_number.handle_session("40", event, context, session)

    assert "状态无效" in _text(response)
    assert context._session is None
    assert context.end_calls == 1


@pytest.mark.asyncio
async def test_foreign_session_cannot_enter_continuation(event: dict[str, Any]) -> None:
    session = MockSession(_game_data(), plugin_name="other_plugin")
    context = MockContext(session)

    with pytest.raises(ValueError, match="foreign"):
        await guess_number.handle_session("50", event, context, session)

    assert context._session is session
    assert context.end_calls == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"target": True},
        {"difficulty": object()},
        {"difficulty": "unknown"},
        {"max_attempts": 8},
        {"history": [1] * 11},
        {"attempts": 7, "history": [1, 2, 3, 4, 5, 6, 7], "min": 8},
        {"history": (1,)},
        {"attempts": 1, "history": [1001]},
        {"attempts": 1, "history": [50]},
        {"attempts": 1, "history": [25]},
        {"min": 60},
    ],
)
def test_load_game_state_rejects_inconsistent_values(changes: dict[str, Any]) -> None:
    data = _game_data()
    data.update(changes)
    with pytest.raises(guess_number.InvalidGameState):
        guess_number._load_game_state(MockSession(data))


def test_load_game_state_accepts_legacy_alias_and_ignores_old_derived_fields() -> None:
    data = _game_data(
        target       = 25,
        maximum      = 50,
        difficulty   = "easy",
        max_attempts = 10,
    )
    data.update({"remaining": 10, "hint": "1-50"})
    state = guess_number._load_game_state(MockSession(data))
    assert state.difficulty is guess_number.EASY
    assert state.remaining == 10
    assert state.hint == "1-50"


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        (1, "一发入魂"),
        (2, "太厉害了"),
        (3, "表现优秀"),
        (4, "不错"),
        (6, "还可以更好"),
        (7, "险胜"),
    ],
)
def test_rating_branches(attempts: int, expected: str) -> None:
    assert expected in guess_number._get_rating(attempts, 7)


@pytest.mark.parametrize(("attempts", "maximum"), [(0, 7), (8, 7), (True, 7), (1, 0)])
def test_rating_rejects_invalid_counters(attempts: object, maximum: object) -> None:
    rating: Any = guess_number._get_rating
    with pytest.raises(ValueError):
        rating(attempts, maximum)


@pytest.mark.asyncio
async def test_handle_redacts_unexpected_start_failure(
    context: MockContext,
    event: dict[str, Any],
) -> None:
    with patch.object(
        context,
        "get_session",
        new=AsyncMock(side_effect=RuntimeError("internal secret")),
    ):
        response = await guess_number.handle("猜数字", "", event, context)
    assert "XQ-PLUGIN-UNEXPECTED" in _text(response)
    assert "internal secret" not in _text(response)


@pytest.mark.asyncio
async def test_real_session_transaction_persists_range_and_reentrant_win(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    context = _real_context(tmp_path, manager)
    monkeypatch.setattr(guess_number.secrets, "randbelow", lambda _size: 49)
    await guess_number.handle("猜数字", "", {}, context)

    async def guess_low(session: Session) -> guess_number.Segments:
        return await guess_number.handle_session("25", {}, context, session)

    await manager.update(123, 456, guess_low)
    persisted = await manager.peek(123, 456)
    assert persisted is not None
    assert persisted.get("attempts") == 1
    assert persisted.get("min") == 26
    assert "remaining" not in persisted.data and "hint" not in persisted.data

    async def guess_correct(session: Session) -> guess_number.Segments:
        return await guess_number.handle_session("50", {}, context, session)

    result = await manager.update(123, 456, guess_correct)
    assert result is not None and "猜对" in _text(result)
    assert await manager.peek(123, 456) is None


@pytest.mark.asyncio
async def test_real_foreign_session_survives_restart_command(tmp_path: Path) -> None:
    manager = SessionManager()
    context = _real_context(tmp_path, manager)
    other   = await manager.create(123, 456, "other_plugin", {"keep": True})

    response = await guess_number.handle("猜数字", "restart", {}, context)

    assert "其他插件会话" in _text(response)
    current = await manager.peek(123, 456)
    assert current is not None
    assert current.plugin_name == "other_plugin"
    assert current.session_id == other.session_id
    assert current.get("keep") is True
