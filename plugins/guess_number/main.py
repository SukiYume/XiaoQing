"""提供有界输入、会话隔离和动态范围提示的猜数字游戏。"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from core.args import tokenize
from core.plugin_base import Segments, has_control_characters, segments
from core.public_errors import public_error_response

logger = logging.getLogger(__name__)

PLUGIN_NAME = "guess_number"
SESSION_TIMEOUT_SECONDS = 180.0
MAX_COMMAND_CHARS = 64
MAX_SESSION_INPUT_CHARS = 64

DifficultyKey = Literal["easy", "normal", "hard", "hell"]
SessionAction = Literal["status", "restart", "help"]
CommandAction = Literal["start", "status", "restart", "help"]


@dataclass(frozen=True, slots=True)
class Difficulty:
    """一份难度配置，同时作为命令别名和会话状态的唯一真相源。"""

    key: DifficultyKey
    label: str
    minimum: int
    maximum: int
    max_attempts: int
    aliases: frozenset[str]


EASY = Difficulty("easy", "简单 ⭐", 1, 50, 10, frozenset({"easy", "e", "简单"}))
NORMAL = Difficulty("normal", "普通 ⭐⭐", 1, 100, 7, frozenset({"normal", "n", "普通"}))
HARD = Difficulty("hard", "困难 ⭐⭐⭐", 1, 200, 8, frozenset({"hard", "h", "困难"}))
HELL = Difficulty(
    "hell",
    "地狱 ⭐⭐⭐⭐⭐",
    1,
    1000,
    10,
    frozenset({"hell", "nightmare", "地狱"}),
)
DIFFICULTIES = (EASY, NORMAL, HARD, HELL)
_DIFFICULTY_BY_ALIAS = {
    alias: difficulty for difficulty in DIFFICULTIES for alias in difficulty.aliases
}

_HELP_ALIASES = frozenset({"help", "帮助", "?"})
_STATUS_ALIASES = frozenset({"status", "状态", "info", "信息"})
_RESTART_ALIASES = frozenset({"restart", "重新开始", "重开"})
_SESSION_STATUS_ALIASES = _STATUS_ALIASES | {"?"}
_COMMAND_TRIGGERS = frozenset({"猜数字", "guess", "猜"})
_COMMAND_ACTION_BY_ALIAS: dict[str, SessionAction] = {
    **dict.fromkeys(_HELP_ALIASES, "help"),
    **dict.fromkeys(_STATUS_ALIASES, "status"),
    **dict.fromkeys(_RESTART_ALIASES, "restart"),
}
_GUESS_PATTERN = re.compile(r"[0-9]{1,4}\Z")

HELP_TEXT = """🎮 猜数字游戏

开始游戏
/猜数字  普通难度（1-100，7 次）
/猜数字 简单/easy  1-50，10 次
/猜数字 困难/hard  1-200，8 次
/猜数字 地狱/hell  1-1000，10 次

管理游戏
/猜数字 status  查看当前状态
/猜数字 restart  结束本插件旧游戏并按普通难度重开
/猜数字 help  显示帮助

游戏开始后直接发送数字；发送「退出」或「取消」可放弃。会话 3 分钟无操作自动结束。
"""

OTHER_SESSION_TEXT = "⚠️ 当前已有其他插件会话；请先发送「退出」结束该会话，再开始猜数字。"
INVALID_STATE_TEXT = "⚠️ 猜数字会话状态无效，已安全结束；请重新开始游戏。"


class _GameSession(Protocol):
    plugin_name: str

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...


class _GuessNumberContext(Protocol):
    async def create_session(
        self,
        initial_data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _GameSession: ...

    async def get_session(self) -> _GameSession | None: ...

    async def end_session(self) -> bool: ...


class GuessNumberCommandError(ValueError):
    """表示可直接反馈给用户的命令格式错误。"""


class InvalidGameState(ValueError):
    """表示当前插件会话不满足游戏状态不变量。"""


@dataclass(frozen=True, slots=True)
class CommandRequest:
    action: CommandAction
    difficulty: Difficulty = NORMAL


@dataclass(frozen=True, slots=True)
class GameState:
    target: int
    minimum: int
    maximum: int
    attempts: int
    max_attempts: int
    history: tuple[int, ...]
    difficulty: Difficulty

    @property
    def remaining(self) -> int:
        return self.max_attempts - self.attempts

    @property
    def hint(self) -> str:
        return f"{self.minimum}-{self.maximum}"


def _parse_request(args: object) -> CommandRequest:
    """完整消费单参数命令，拒绝静默忽略和未知难度回退。"""

    if type(args) is not str:
        raise TypeError("guess_number arguments must be a string")
    if len(args) > MAX_COMMAND_CHARS:
        raise GuessNumberCommandError(f"命令参数不能超过 {MAX_COMMAND_CHARS} 个字符")
    if has_control_characters(args, include_c1=True):
        raise GuessNumberCommandError("命令参数不能包含控制字符")
    try:
        tokens = tokenize(args)
    except ValueError as exc:
        raise GuessNumberCommandError("命令中的引号没有闭合") from exc
    if not tokens:
        return CommandRequest("start")
    if len(tokens) != 1:
        raise GuessNumberCommandError("用法：/猜数字 [简单|普通|困难|地狱|status|restart|help]")

    token = tokens[0].casefold()
    action = _COMMAND_ACTION_BY_ALIAS.get(token)
    if action is not None:
        return CommandRequest(action)
    difficulty = _DIFFICULTY_BY_ALIAS.get(token)
    if difficulty is None:
        raise GuessNumberCommandError("未知难度；请使用简单、普通、困难或地狱")
    return CommandRequest("start", difficulty)


def _owns_session(session: _GameSession) -> bool:
    return type(session.plugin_name) is str and session.plugin_name == PLUGIN_NAME


def _read_state_int(session: _GameSession, key: str) -> int:
    value = session.get(key)
    if type(value) is not int:
        raise InvalidGameState(f"{key} must be an integer")
    return value


def _load_game_state(session: _GameSession) -> GameState:
    """从会话恢复并交叉验证状态，不信任冗余或任意对象值。"""

    raw_difficulty = session.get("difficulty", NORMAL.key)
    if type(raw_difficulty) is not str:
        raise InvalidGameState("difficulty must be a string")
    difficulty = _DIFFICULTY_BY_ALIAS.get(raw_difficulty.casefold())
    if difficulty is None:
        raise InvalidGameState("difficulty is unknown")

    target = _read_state_int(session, "target")
    minimum = _read_state_int(session, "min")
    maximum = _read_state_int(session, "max")
    attempts = _read_state_int(session, "attempts")
    max_attempts = _read_state_int(session, "max_attempts")
    raw_history = session.get("history")
    if (
        type(raw_history) is not list
        or len(raw_history) > difficulty.max_attempts
        or any(type(item) is not int for item in raw_history)
    ):
        raise InvalidGameState("history must be an integer list")
    history = tuple(raw_history)

    if max_attempts != difficulty.max_attempts or not 0 <= attempts < max_attempts:
        raise InvalidGameState("attempt counters are inconsistent")
    if len(history) != attempts or any(
        not difficulty.minimum <= guess <= difficulty.maximum for guess in history
    ):
        raise InvalidGameState("history is inconsistent")
    if not difficulty.minimum <= minimum <= target <= maximum <= difficulty.maximum:
        raise InvalidGameState("range is inconsistent")
    if target in history or any(minimum <= guess <= maximum for guess in history):
        raise InvalidGameState("active history contradicts the current range")
    return GameState(
        target=target,
        minimum=minimum,
        maximum=maximum,
        attempts=attempts,
        max_attempts=max_attempts,
        history=history,
        difficulty=difficulty,
    )


def _format_history(history: tuple[int, ...]) -> str:
    return " → ".join(str(guess) for guess in history) if history else "（尚未开始）"


def _format_status(state: GameState) -> str:
    return (
        "📊 游戏状态\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"难度: {state.difficulty.label}\n"
        f"当前范围: {state.hint}\n"
        f"剩余次数: {state.remaining}/{state.max_attempts}\n"
        f"已猜次数: {state.attempts}\n"
        f"猜测历史: {_format_history(state.history)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "继续直接发送数字猜测"
    )


async def _start_game(
    difficulty: Difficulty,
    context: _GuessNumberContext,
    *,
    restart: bool = False,
) -> Segments:
    """检测到现有会话时只替换本插件游戏，并创建最小规范状态。"""

    existing = await context.get_session()
    if existing is not None:
        if not _owns_session(existing):
            return segments(OTHER_SESSION_TEXT)
        if restart:
            await context.end_session()
        else:
            try:
                state = _load_game_state(existing)
            except InvalidGameState:
                await context.end_session()
                logger.warning("Discarded invalid guess_number session before starting a game")
            else:
                return segments(
                    "🎮 你已经有一个进行中的游戏！\n"
                    f"当前范围: {state.hint}\n"
                    f"剩余次数: {state.remaining}\n\n"
                    "直接发送数字继续猜测\n"
                    "发送「退出」/「取消」放弃游戏\n"
                    "发送 /猜数字 restart 重新开始"
                )

    target = secrets.randbelow(difficulty.maximum - difficulty.minimum + 1) + difficulty.minimum
    await context.create_session(
        initial_data={
            "target": target,
            "min": difficulty.minimum,
            "max": difficulty.maximum,
            "attempts": 0,
            "max_attempts": difficulty.max_attempts,
            "history": [],
            "difficulty": difficulty.key,
        },
        timeout=SESSION_TIMEOUT_SECONDS,
    )
    logger.info(
        "Game started: difficulty=%s, range=%d-%d, max_attempts=%d",
        difficulty.key,
        difficulty.minimum,
        difficulty.maximum,
        difficulty.max_attempts,
    )
    return segments(
        "🎮 猜数字游戏开始！\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"难度: {difficulty.label}\n"
        f"范围: {difficulty.minimum} 到 {difficulty.maximum}\n"
        f"机会: {difficulty.max_attempts} 次\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 请直接发送一个数字开始猜测\n"
        "💡 输入「退出」/「取消」可以放弃游戏"
    )


async def _handle_status(context: _GuessNumberContext) -> Segments:
    """读取本插件状态；发现损坏状态时安全结束对应会话。"""

    session = await context.get_session()
    if session is None:
        return segments("📊 当前没有进行中的游戏\n发送 /猜数字 开始新游戏")
    if not _owns_session(session):
        return segments(OTHER_SESSION_TEXT)
    try:
        state = _load_game_state(session)
    except InvalidGameState:
        await context.end_session()
        logger.warning("Discarded invalid guess_number session during status query")
        return segments(INVALID_STATE_TEXT)
    return segments(_format_status(state))


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: _GuessNumberContext,
) -> Segments:
    """处理开始、状态、重开和帮助命令；普通猜测由会话入口处理。"""

    del command, event
    try:
        request = _parse_request(args)
        if request.action == "help":
            return segments(HELP_TEXT)
        if request.action == "status":
            return await _handle_status(context)
        return await _start_game(
            request.difficulty,
            context,
            restart=request.action == "restart",
        )
    except GuessNumberCommandError as exc:
        return segments(str(exc))
    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="guess_number.handle")


def _parse_session_input(
    text: object,
) -> int | SessionAction | None:
    """把会话文本归一为控制动作或有界 ASCII 猜测。

    活跃会话会先于普通命令路由收到消息，因此这里还要识别完整的
    ``/猜数字 status``、``/猜数字 restart`` 和 ``/猜数字 help``。
    """

    if type(text) is not str:
        raise TypeError("guess_number session input must be a string")
    if len(text) > MAX_SESSION_INPUT_CHARS or has_control_characters(text, include_c1=True):
        return None
    normalized = text.strip().casefold()
    if normalized in _SESSION_STATUS_ALIASES:
        return "status"
    if normalized in _RESTART_ALIASES:
        return "restart"
    if normalized in _HELP_ALIASES:
        return "help"

    parts = normalized.split()
    if len(parts) == 2 and parts[0].removeprefix("/") in _COMMAND_TRIGGERS:
        return _COMMAND_ACTION_BY_ALIAS.get(parts[1])
    if _GUESS_PATTERN.fullmatch(normalized) is None:
        return None
    return int(normalized)


async def handle_session(
    text: str,
    event: dict[str, Any],
    context: _GuessNumberContext,
    session: _GameSession,
) -> Segments:
    """在框架的单会话事务内处理一次状态查询或数字猜测。"""

    del event
    if not _owns_session(session):
        raise ValueError("guess_number received a foreign session")
    try:
        state = _load_game_state(session)
    except InvalidGameState:
        await context.end_session()
        logger.warning("Discarded invalid guess_number session during continuation")
        return segments(INVALID_STATE_TEXT)

    parsed = _parse_session_input(text)
    if parsed == "status":
        return segments(_format_status(state))
    if parsed == "restart":
        return await _start_game(NORMAL, context, restart=True)
    if parsed == "help":
        return segments(HELP_TEXT)
    if parsed is None:
        return segments(
            f"❓ 请输入一个 ASCII 数字（{state.minimum}-{state.maximum}）\n"
            "💡 输入「退出」/「取消」可以放弃游戏"
        )
    guess = parsed
    if not state.minimum <= guess <= state.maximum:
        return segments(f"⚠️ 请输入 {state.minimum} 到 {state.maximum} 之间的数字！")

    attempts = state.attempts + 1
    remaining = state.max_attempts - attempts
    history = (*state.history, guess)
    if guess == state.target:
        await context.end_session()
        logger.info("Game won: difficulty=%s, attempts=%d", state.difficulty.key, attempts)
        return segments(
            "🎉 恭喜你猜对了！\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"答案是: {state.target}\n"
            f"尝试次数: {attempts} 次\n"
            f"猜测历史: {_format_history(history)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{_get_rating(attempts, state.max_attempts)}"
        )
    if remaining == 0:
        await context.end_session()
        logger.info("Game lost: difficulty=%s", state.difficulty.key)
        return segments(
            "😢 游戏结束，次数用尽！\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"正确答案是: {state.target}\n"
            f"你的猜测: {_format_history(history)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "再接再厉！发送 /猜数字 开始新游戏"
        )

    session.set("attempts", attempts)
    session.set("history", list(history))
    if guess < state.target:
        minimum = guess + 1
        maximum = state.maximum
        hint = "📈 太小了！"
        session.set("min", minimum)
    else:
        minimum = state.minimum
        maximum = guess - 1
        hint = "📉 太大了！"
        session.set("max", maximum)
    return segments(f"{hint}\n剩余次数: {remaining}\n当前范围: {minimum}-{maximum}")


def _get_rating(attempts: int, max_attempts: int) -> str:
    """按已用机会比例给出稳定评价。"""

    if (
        type(attempts) is not int
        or type(max_attempts) is not int
        or not 1 <= attempts <= max_attempts
    ):
        raise ValueError("rating attempt counters are invalid")
    if attempts == 1:
        return "🏆 难以置信！一发入魂！"
    ratio = attempts / max_attempts
    if ratio <= 0.3:
        return "⭐⭐⭐⭐⭐ 太厉害了！"
    if ratio <= 0.5:
        return "⭐⭐⭐⭐ 表现优秀！"
    if ratio <= 0.7:
        return "⭐⭐⭐ 不错哦~"
    if ratio <= 0.9:
        return "⭐⭐ 还可以更好！"
    return "⭐ 险胜！"
