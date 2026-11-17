"""xiaoqing_chat 测试共享 fixture、导入和私有 helper。"""

import inspect
import itertools
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from core.ai import AICompletionResult, AIModelInfo
from core.interfaces import PluginCapabilities, PluginPrincipal
from plugins.xiaoqing_chat import main as xiaoqing_chat
from plugins.xiaoqing_chat.chat_runtime import load_runtime
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.handler_context import handle_errors
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.message_parts import message_parts_to_legacy
from plugins.xiaoqing_chat.planning.planned_action import PlannedAction
from plugins.xiaoqing_chat.reply_draft import ReplyDraft
from plugins.xiaoqing_chat.reply_generator import generate_reply_draft
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState, reset_global_state
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.assertions import logged_events
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import with_settings_reader
from tests.helpers.xiaoqing_chat_memory_test_support import MemoryHarness, put_history

_MISSING_TEST_CONFIG = object()


PROJECT_ROOT = REPOSITORY_ROOT


def _explicit_test_attribute(target, name: str):
    values = vars(target)
    if name in values:
        return values[name]
    child = values.get("_mock_children", {}).get(name)
    return child if child is not None else _MISSING_TEST_CONFIG


def _fill_test_config_defaults(target, defaults) -> None:
    """让轻量测试替身遵守完整的生产配置契约。"""

    if type(target) is type(defaults):
        return
    for name in type(defaults).model_fields:
        default_value = getattr(defaults, name)
        current       = _explicit_test_attribute(target, name)
        if current is _MISSING_TEST_CONFIG:
            setattr(target, name, deepcopy(default_value))
            continue
        if hasattr(type(default_value), "model_fields") and not hasattr(
            type(current), "model_fields"
        ):
            _fill_test_config_defaults(current, default_value)


def _complete_test_runtime_config(runtime):
    cfg = runtime.cfg
    if not isinstance(cfg, XiaoQingChatConfig):
        defaults = XiaoQingChatConfig()
        _fill_test_config_defaults(cfg, defaults)
        cfg.llm_config = defaults.llm_config
    return runtime


class _EmptyMemory:
    """在场、但什么都没记的统一记忆：读取都是空的，清空会得到确认。"""

    async def record(self, _payload: dict[str, Any]) -> None:
        return None

    async def recall(self, _payload: dict[str, Any]) -> None:
        return None

    async def search(self, _payload: dict[str, Any]) -> None:
        return None

    async def forget(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"version": 1, "scope": payload.get("scope"), "generation": 1}


def set_context_principal(
    context,
    event: dict[str, Any],
    *,
    group_role: str    = "member",
    is_bot_admin: bool = False,
) -> None:
    group_id          = event.get("group_id")
    context.principal = PluginPrincipal(
        kind         = "user",
        user_id      = event.get("user_id"),
        group_id     = group_id,
        is_bot_admin = is_bot_admin,
        is_private   = group_id in (None, ""),
        group_role   = group_role if group_id not in (None, "") else "unknown",
    )
    context.capabilities = PluginCapabilities(
        is_bot_admin = is_bot_admin,
        ai           = provider_test_ai(),
        memory       = _EmptyMemory(),  # type: ignore[arg-type]
    )
    context.config = {
        **dict(getattr(context, "config", {}) or {}),
        "plugins": {
            "xiaoqing_chat": {
                "ai": {
                    "model_aliases": {
                        "deepseek": "deepseek-flash",
                        "glm"     : "glm-5.2",
                    },
                }
            }
        },
    }


def provider_test_ai() -> SimpleNamespace:
    models = (
        AIModelInfo("deepseek-flash", "deepseek", "deepseek-chat", ("text",)),
        AIModelInfo("glm-5.2", "zhipu", "glm-5.2", ("text",)),
    )
    return SimpleNamespace(
        list_models = lambda route, **kwargs: models,
        complete    = AsyncMock(),
    )


async def ack_reply_delivery(result, *, delivered: bool = True):
    receipt = getattr(result, "delivery_receipt", None)
    assert receipt is not None
    await receipt.record(delivered)
    return receipt


def build_xiaoqing_catalog():
    """从生产 manifest 构造测试使用的同一份 Core 目录快照。"""

    from core.models import PluginManifest
    from core.router import build_command_catalog_node

    manifest_path = PROJECT_ROOT / "plugins" / "xiaoqing_chat" / "plugin.json"
    manifest = PluginManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    return build_command_catalog_node(
        manifest.name,
        manifest.commands[0].model_dump(),
        root=True,
    )


@pytest.fixture
def mock_context(tmp_path: Path):
    """Create a mock plugin context for xiaoqing_chat"""
    context         = MagicMock()
    context.config  = {"bot_name": "小青"}
    context.secrets = {
        "openai_api_key": "test_key",
        "plugins"       : {
            "xiaoqing_chat": {"api_key": "test", "api_base": "http://test", "model": "test-model"}
        },
    }
    context.plugin_name    = "xiaoqing_chat"
    context.plugin_dir     = tmp_path / "plugins" / "xiaoqing_chat"
    context.data_dir       = tmp_path / "data" / "xiaoqing_chat"
    context.http_session   = AsyncMock()
    context.send_action    = AsyncMock()
    context.reload_config  = Mock()
    context.reload_plugins = Mock()
    context.get_command_catalog = Mock(return_value=(build_xiaoqing_catalog(),))
    context.list_plugins = Mock(return_value=["xiaoqing_chat"])
    context.current_user_id  = 12345
    context.current_group_id = 67890
    context.request_id       = "test-request-123"
    context.state            = {}
    context.session_manager  = None
    # 统一记忆在场并确认清空：重置的回复才是“已重置”。
    context.capabilities.memory = _EmptyMemory()
    return with_settings_reader(context)


@pytest.fixture
def sample_group_event():
    """Create a sample group message event"""
    return {
        "post_type"   : "message",
        "message_type": "group",
        "time"        : 1234567890,
        "self_id"     : 11111,
        "user_id"     : 12345,
        "group_id"    : 67890,
        "message"     : [{"type": "text", "data": {"text": "/xc 你好"}}],
        "raw_message" : "/xc 你好",
        "font"        : 0,
        "sender"      : {
            "user_id" : 12345,
            "nickname": "TestUser",
            "card"    : "",
            "sex"     : "unknown",
            "age"     : 0,
            "area"    : "",
            "level"   : "",
            "role"    : "member",
            "title"   : "",
        },
        "message_id" : 1,
        "message_seq": 1,
    }


__all__ = (
    "PluginCapabilities",
    "PluginPrincipal",
    "ack_reply_delivery",
    "build_xiaoqing_catalog",
    "provider_test_ai",
    "set_context_principal",
    "handle_errors",
    "message_parts_to_legacy",
    "mock_context",
    "sample_group_event",
    "xiaoqing_chat",
)


def reply_plan(**overrides):
    """构造完整的回复生成计划；测试只需写出与用例相关的字段。"""

    from plugins.xiaoqing_chat.planning.planned_action import PlannedAction
    from plugins.xiaoqing_chat.reply_plan import ReplyGenerationPlan
    from plugins.xiaoqing_chat.runtime_state import ChatRuntime

    overrides              = {key: value for key, value in overrides.items() if value is not None}
    runtime                = overrides.pop("runtime", None) or ChatRuntime(XiaoQingChatConfig(), [])
    values: dict[str, Any] = {
        "text"              : "",
        "event"             : {},
        "context"           : MagicMock(),
        "runtime"           : _complete_test_runtime_config(runtime),
        "state"             : MagicMock(),
        "forced"            : False,
        "action"            : PlannedAction(think_level=0, reasoning=""),
        "bot_name"          : "小青",
        "chat_id"           : "g1",
        "is_private"        : False,
        "history"           : [],
        "earlier_history"   : (),
        "max_context_size"  : 10,
        "secrets"           : {},
        "model"             : "",
        "foreground"        : None,
        "request_id"        : "",
        "profile_block"     : "",
        "policy_block"      : "",
        "knowledge_block"   : "",
        "expression_block"  : "",
        "jargon_explanation": "",
        "keyword_rules"     : (),
        "regex_rules"       : (),
        "merged_reasoning"  : "",
        "effective_goal"    : "",
        "tool_info_block"   : "",
        "effective_identity": "",
        "effective_style"   : "",
        "state_text"        : "",
        "started_at"        : 0.0,
    }
    unknown = set(overrides) - set(values) - {"media_candidates"}
    if unknown:
        raise TypeError(f"unknown reply plan fields: {sorted(unknown)}")
    values.update(overrides)
    return ReplyGenerationPlan(**values)


def active_review_sessions(store) -> list[Any]:
    """读取复盘存储里仍在等待的会话，按创建时间倒序；仅供测试检查落盘状态。"""
    from plugins.xiaoqing_chat.memory.review_sessions import _decode_session

    active = store._load_sessions_state().get("active", {})
    if not isinstance(active, dict):
        return []
    sessions = [_decode_session(str(sid), obj) for sid, obj in active.items()]
    return sorted(sessions, key=lambda item: item.created_at, reverse=True)


# ── 从公开入口跑一整轮对话：剧本模型与上下文 ──────────────────────────────
#
# 下面这组辅助让测试直接调用 ``handle_smalltalk`` / ``handle_internal``，走真实的
# 门控、规划、生成、检查与投递代码，只在模型（``context.capabilities.ai``）这条边界上
# 放一个按调用种类回答的替身。这一组是新增的，不改上面任何已有的东西。


@dataclass(frozen=True)
class ModelCall:
    """剧本模型收到的一次调用；``kind`` 是它被识别成的调用种类。"""

    kind    : str
    route   : str
    messages: list[dict[str, Any]]
    kwargs  : dict[str, Any]  # 路由之外随请求发出的其余参数（pinned_model、extra_payload 等）

    @property
    def text(self) -> str:
        return "\n".join(str(message.get("content", "")) for message in self.messages)


_ScriptAnswer = str | Sequence[str] | Callable[[ModelCall], "str | Awaitable[str]"]


def plan_answer(action: str, reason: str = "planner", thinking: str = "") -> str:
    """规划器的一次回答：JSON，``action`` 是 PFC 的行动名。"""

    return json.dumps(
        {"action": action, "reason": reason, "thinking": thinking}, ensure_ascii=False
    )


def goals_answer(*goals: str) -> str:
    """目标分析的一次回答：JSON 数组；不给目标就是 ``[]``（所有目标都已失效）。"""

    return json.dumps(
        [{"goal": goal, "reasoning": f"聊天里正在说{goal}"} for goal in goals], ensure_ascii=False
    )


def say_bye_answer(say_bye: bool, reason: str = "收尾") -> str:
    """结束对话前“要不要再说一句告别”的回答。"""

    return json.dumps({"say_bye": "yes" if say_bye else "no", "reason": reason}, ensure_ascii=False)


class ScriptedChatModel:
    """按调用种类回答的剧本模型，经 ``context.capabilities.ai`` 注入。

    一轮对话里模型会被叫好几次，所以不按先后顺序、而按调用种类给剧本：

    - ``reply``     聊天路由（route ``chat``）的回复生成；
    - ``plan``      规划器选行动（route ``reasoning``，要求输出 ``"action"``）；
    - ``goals``     重新分析会话目标（要求输出 ``"goal"`` 数组）；
    - ``say_bye``   结束对话前决定要不要告别（要求输出 ``"say_bye"``）；
    - ``reasoning`` 其余走 ``reasoning`` 路由的调用（生活记录抽取等后台任务）。

    种类靠“路由 + 提示词要求模型输出的 JSON 键”识别，这些键是输出契约，不随措辞变化；
    别的路由（如 ``checker``、识图）直接按路由名当种类，剧本里写那个路由名即可。

    每个种类的剧本可以是一个字符串（每次都这样回答）、一个列表（依次回答，用完后重复
    最后一个），或一个收到 :class:`ModelCall` 的函数（可以是协程，用来在模型这条边界上
    设置屏障）。没写剧本的种类回答空串，并记在 :attr:`unscripted` 里，
    ``settle_chat_background`` 会据此报错，免得测试悄悄走上没想到的路。
    """

    def __init__(self, **script: _ScriptAnswer) -> None:
        self._script = dict(script)
        self._served: dict[str, int] = {}
        self.calls = list[ModelCall]()
        self.unscripted = list[ModelCall]()
        self.models = (AIModelInfo("scripted-chat", "scripted", "scripted-model", ("text",)),)

    @staticmethod
    def classify(route: str, messages: list[dict[str, Any]]) -> str:
        if route == "chat":
            return "reply"
        if route != "reasoning":
            return route
        text = "\n".join(str(message.get("content", "")) for message in messages)
        if '"say_bye"' in text:
            return "say_bye"
        if '"action"' in text:
            return "plan"
        if '"goal"' in text:
            return "goals"
        return "reasoning"

    def calls_of(self, kind: str) -> list[ModelCall]:
        return [call for call in self.calls if call.kind == kind]

    def list_models(self, route: str, **_kwargs: Any) -> tuple[AIModelInfo, ...]:
        return self.models

    async def complete(
        self, route: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> AICompletionResult:
        call = ModelCall(self.classify(route, messages), route, list(messages), dict(kwargs))
        self.calls.append(call)
        text = await self._answer(call)
        return AICompletionResult(
            response      = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]},
            profile       = "scripted-chat",
            provider      = "scripted",
            model         = "scripted-model",
            finish_reason = "stop",
            attempts      = 1,
        )

    async def _answer(self, call: ModelCall) -> str:
        script = self._script.get(call.kind)
        if script is None:
            self.unscripted.append(call)
            return ""
        if callable(script):
            answer = script(call)
            return str(await answer if inspect.isawaitable(answer) else answer)
        if isinstance(script, str):
            return script
        index                   = self._served.get(call.kind, 0)
        self._served[call.kind] = index + 1
        return script[min(index, len(script) - 1)]


# 一轮对话里除被测行为之外都固定下来的部署配置：不受当前时刻影响（作息、发送延迟）、
# 不额外起后台模型任务（生活记录、表达学习）。想测的开关由各测试再叠加覆盖。
QUIET_CHAT_CONFIG: dict[str, Any] = {
    "io_persist_debounce_seconds": 0.0,
    "heartflow"                  : {"enable_heartflow": False},
    "humanize"                   : {"enable_typing_delay": False},
    "expression"                 : {"enable_expression_learning": False},
    "life"                       : {
        "enable_schedule_state"       : False,
        "enable_daily_life"           : False,
        "enable_self_timeline"        : False,
        "enable_open_loops"           : False,
        "enable_private_closing_guard": False,
    },
}
# 群里没人叫她时，是否接话只看掷骰子：概率 1 必接，概率 0 必不接。
GROUP_GATE_OPEN  : dict[str, Any] = {"reply_probability_base": 1.0}
GROUP_GATE_CLOSED: dict[str, Any] = {"reply_probability_base": 0.0}


def _merge_config(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def scripted_chat_context(context, model: ScriptedChatModel, *config: dict[str, Any]):
    """把 ``mock_context`` 配成能跑完整一轮对话：模型换成剧本模型，部署配置用
    :data:`QUIET_CHAT_CONFIG` 再依次叠加 ``config``。

    能力对象用真实的 ``PluginCapabilities``，只有 ai 和（空的）统一记忆；agent、生活等能力
    都不在场，这一轮走聊天流程。插件目录是不存在的临时目录，行为配置取代码里的默认值，
    不随仓库里的配置文件变化。
    """

    context.capabilities = PluginCapabilities(ai=model, memory=_EmptyMemory())  # type: ignore[arg-type]
    merged = QUIET_CHAT_CONFIG
    for extra in config:
        merged = _merge_config(merged, extra)
    context.config = {**dict(context.config), "plugins": {"xiaoqing_chat": merged}}
    return context


def chat_message_event(event: dict[str, Any], text: str, **overrides: Any) -> dict[str, Any]:
    """基于样例事件造一条 ``text`` 消息：事件时间取现在（它会成为聊天记录里的发言时间）。"""

    return {
        **event,
        "time"       : int(time.time()),
        "message"    : [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        **overrides,
    }


def seed_chat_history(state, chat_id: str, count: int, *, user_id: int = 99) -> None:
    """往聊天记录里放 ``count`` 条刚发生的、两人你来我往的旧消息（旧到新）。"""

    start = time.time() - count
    for index in range(count):
        mine = index % 2 == 1
        state.memory_store.append(
            chat_id,
            role    = "assistant" if mine else "user",
            name    = "小青" if mine else "群友",
            user_id = None if mine else user_id,
            content = f"第{index}句",
            ts      = start + index,
        )


def user_message(
    text: str, ts: float, *, user_id: int = 7, name: str = "阿梨", message_id: int | None = None
) -> StoredMessage:
    """聊天记录里的一条用户发言。"""

    return StoredMessage(
        role="user", name=name, ts=ts, user_id=user_id, content=text, message_id=message_id
    )


def bot_message(text: str, ts: float, *, source: str = "xiaoqing_chat") -> StoredMessage:
    """聊天记录里她自己（由 ``source`` 这个插件）说的一句。"""

    return StoredMessage(role="assistant", name="小青", ts=ts, content=text, source_plugin=source)


async def settle_chat_background(context, model: ScriptedChatModel | None = None) -> None:
    """用插件自己的关闭流程收尾：后台落盘和任务都结束；再检查剧本模型有没有收到没写剧本的调用。"""

    await xiaoqing_chat.shutdown(context)
    if model is not None:
        assert not model.unscripted, f"unscripted model calls: {[c.kind for c in model.unscripted]}"


async def draft_reply(
    context,
    event: dict,
    *,
    text: str,
    forced: bool        = False,
    reasoning: str      = "继续回复",
    plan_reasoning: str = "",
) -> ReplyDraft | None:
    """用真实的运行时配置和状态直接起草一条回复（模型调用由 ``context`` 里的剧本模型回答）。"""

    return await generate_reply_draft(
        text    = text,
        event   = event,
        context = context,
        runtime = load_runtime(context),
        state   = bound_state(context),
        forced  = forced,
        action=PlannedAction(think_level=1, reasoning=reasoning),
        plan_reasoning = plan_reasoning,
        bot_name       = "小青",
    )


# ── 主动开口：定时检查的现场 ───────────────────────────────────────────────
#
# 主动开口从定时任务的处理函数 ``scheduled_proactive`` 进入，走真实的运行时状态、统一记忆、
# 聊天记录和投递回执；边界上只有剧本模型、网上生活能力和假时钟。

# 2027-01-15 周五 16:00（上海）：在活跃时段内，默认人设卡里这会儿是自由时间。
NOW  = 1_800_000_000.0
HOUR = 3600.0
# 每个现场用不同的数据目录编号，同一个测试里连续起多个现场互不串。
SCENE_IDS = itertools.count()


class Clock:
    """假时钟：``time.time()`` 返回 ``now``，测试用 ``advance`` 推进。"""

    def __init__(self) -> None:
        self.now = NOW

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(time, "time", lambda: fake.now)
    return fake


@pytest.fixture(autouse=True)
def no_swallowed_errors(caplog):
    """定时任务的处理函数会吞掉异常只记日志；没有预期中的失败时，不能让异常悄悄把测试变成“没开口”。"""

    yield
    swallowed = [str(event) for event in logged_events(caplog, "public_error")]
    assert not swallowed, swallowed


@dataclass
class Run:
    """一次定时检查的结果和现场。"""

    deliveries: list[Any]
    model     : ScriptedChatModel
    state     : ChatRuntimeState
    life      : Any
    context   : Any

    @property
    def delivery(self) -> Any:
        return self.deliveries[0] if self.deliveries else None

    def draft_material(self) -> str:
        """起草时她看到的材料（用户消息）。"""

        return str(self.model.calls_of("reply")[0].messages[1]["content"])


def scene(
    context,
    tmp_path,
    model: ScriptedChatModel,
    *,
    life: Any                               = None,
    memory: Any                             = None,
    groups: tuple[int, ...]                 = (),
    config: dict[str, Any] | None           = None,
    proactive_config: dict[str, Any] | None = None,
) -> ChatRuntimeState:
    """一个全新的现场：运行时状态和数据目录都是新的，模型是剧本模型，主动开口打开。"""

    reset_global_state()
    context.data_dir = tmp_path / f"scene{next(SCENE_IDS)}"
    scripted_chat_context(
        context,
        model,
        {"proactive": {"enabled": True, **(proactive_config or {})}},
        config or {},
    )
    capabilities = replace(context.capabilities, life_context=life)
    if memory is not None:
        capabilities = replace(capabilities, memory=memory)
    context.capabilities   = capabilities
    context.default_groups = lambda: list(groups)
    return bound_state(context)


def proactive_model(
    reply: Any     = None,
    checker: Any   = None,
    reasoning: Any = None,
    vision: Any    = None,
) -> ScriptedChatModel:
    """主动开口一轮用的剧本模型：只给写了剧本的种类。"""

    script = {"reply": reply, "checker": checker, "reasoning": reasoning, "vision": vision}
    return ScriptedChatModel(
        **{kind: answer for kind, answer in script.items() if answer is not None}
    )


async def group_run(
    context,
    tmp_path,
    history: list[StoredMessage],
    *,
    reply: Any                    = None,
    checker: Any                  = None,
    reasoning: Any                = None,
    vision: Any                   = None,
    life: Any                     = None,
    harness: MemoryHarness | None = None,
    seed: Any                     = None,
    group: int                    = 42,
    config: dict[str, Any] | None = None,
    **proactive_config: Any,
) -> Run:
    """``group`` 在默认群里时跑一次定时检查：聊天记录 ``history`` 放在这个群里。"""

    model   = proactive_model(reply, checker, reasoning, vision)
    harness = harness or MemoryHarness(tmp_path / f"memory{next(SCENE_IDS)}")
    state   = scene(
        context,
        tmp_path,
        model,
        life             = life,
        memory           = harness.service(""),
        groups           = (group,),
        config           = config,
        proactive_config = proactive_config,
    )
    if history:
        put_history(state, f"g{group}", history)
    if seed is not None:
        seed(state)
    deliveries = await xiaoqing_chat.scheduled_proactive(context)
    return Run(deliveries, model, state, life, context)


async def commit(run: Run) -> None:
    """投递确认：真实的回执提交，之后等后台落盘和抽取收尾。"""

    await run.delivery.receipt.record(True)
    await settle_chat_background(run.context, run.model)
