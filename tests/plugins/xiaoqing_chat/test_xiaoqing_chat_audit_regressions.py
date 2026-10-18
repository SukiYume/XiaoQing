"""Regression coverage for privacy, reset, delivery and dialogue correctness."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import plugins.xiaoqing_chat.commands as commands_module
from core.delivery import SENSITIVE_ACTION_KEY
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.handler_context import HandlerContext
from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from plugins.xiaoqing_chat.message_parts import build_text_message_parts
from plugins.xiaoqing_chat.reply_draft import RejectedCandidate
from plugins.xiaoqing_chat.reply_fallbacks import finish_rejected_candidate
from plugins.xiaoqing_chat.runtime_state import ChatRuntime, ChatRuntimeState
from plugins.xiaoqing_chat.smalltalk_models import GeneratedSmalltalkTurn, PreparedSmalltalkTurn
from plugins.xiaoqing_chat.turn_delivery import finalize_smalltalk_turn
from plugins.xiaoqing_chat.turn_generation import generate_smalltalk_turn
from tests.helpers.settings_snapshot import with_settings_reader
from tests.helpers.xiaoqing_chat_test_support import reply_plan


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pemdo", "等等，我好像没跟上你们说的是哪件事"),
        ("你好", "等等，我好像没跟上你们说的是哪件事"),
        ("1+1等于几", "这个我真拿不准，不瞎说了"),
    ],
)
def test_context_fallback_preserves_neutral_intent(text, expected):
    plan = reply_plan(
        forced             = True,
        text               = text,
        request_id         = "p",
        history            = [],
        runtime            = None,
        context            = None,
        bot_name           = "小青",
        effective_identity = "",
    )
    rejected = RejectedCandidate(
        text   = "unsupported draft",
        result = ReplyCheckResult(False, "unsupported context", True, "hard", "context_grounding"),
    )
    assert finish_rejected_candidate(plan, rejected).text == expected


def test_context_fallback_preserves_true_statement():
    plan = reply_plan(
        forced             = True,
        text               = "我觉得地球绕太阳转",
        request_id         = "p",
        history            = [],
        runtime            = None,
        context            = None,
        bot_name           = "小青",
        effective_identity = "",
    )
    rejected = RejectedCandidate(
        text   = "unsupported draft",
        result = ReplyCheckResult(False, "unsupported context", True, "hard", "context_grounding"),
    )
    assert finish_rejected_candidate(plan, rejected).text == "等等，我好像没跟上你们说的是哪件事"


def test_invalid_hot_reload_retains_cached_valid_runtime(tmp_path):
    from plugins.xiaoqing_chat import chat_runtime

    plugin_dir = tmp_path / "plugin"
    config_dir = plugin_dir / "config"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "xiaoqing_config.json"
    config_file.write_text(json.dumps({"reply_probability_base": 0.55}), encoding="utf-8")
    context = with_settings_reader(
        SimpleNamespace(
            plugin_dir = plugin_dir,
            data_dir   = tmp_path / "data",
            config     = {},
            secrets    = {},
            logger     = MagicMock(),
        )
    )
    state = ChatRuntimeState()
    with patch.object(chat_runtime, "_state", return_value=state):
        valid = chat_runtime.load_runtime(context)
        config_file.write_text(json.dumps({"reply_probability_base": 2}), encoding="utf-8")
        # Force a new observed version even on filesystems with coarse timestamps.
        state._runtime_mtime[str(plugin_dir)] = -2
        assert chat_runtime.load_runtime(context) is valid
        config_file.write_text(json.dumps({"reply_probability_base": 0.7}), encoding="utf-8")
        state._runtime_mtime[str(plugin_dir)] = -3
        assert chat_runtime.load_runtime(context).cfg.reply_probability_base == 0.7
        assert state.get_runtime(str(plugin_dir)).cfg.reply_probability_base == 0.7


def make_turn(tmp_path, *, forced=True):
    runtime = ChatRuntime(XiaoQingChatConfig(), [])
    state   = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.pfc_state_store.get_async = AsyncMock(return_value=None)
    context = SimpleNamespace(data_dir=tmp_path, logger=MagicMock())
    hctx     = HandlerContext("g1", runtime, state, {}, tmp_path, "小青", context)
    prepared = PreparedSmalltalkTurn(
        text              = "hello",
        mentioned         = forced,
        is_private        = False,
        forced            = forced,
        force_reason      = "command",
        brain_chat_active = False,
        mood_text         = "",
    )
    return hctx, prepared


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "exception"])
async def test_speculative_memory_is_joined_after_parent_failure(tmp_path, failure):
    hctx, prepared = make_turn(tmp_path, forced=False)
    started     = asyncio.Event()
    child_tasks = []
    generated   = GeneratedSmalltalkTurn()

    async def memory(**kwargs):
        child_tasks.append(asyncio.current_task())
        started.set()
        await asyncio.Event().wait()

    async def generation(**kwargs):
        await started.wait()
        if failure == "exception":
            raise RuntimeError("synthetic provider failure")
        await asyncio.Event().wait()

    lock    = asyncio.Lock()
    patches = patch.multiple(
        "plugins.xiaoqing_chat.turn_generation",
        GeneratedSmalltalkTurn=lambda: generated,
        ensure_user_message_recorded=AsyncMock(return_value="m1"),
        chat_lock                       = lambda _: lock,
        _generate_reply_result          = generation,
        retrieve_memory_context         = memory,
        run_pfc_once                    = generation,
        normalize_generated_reply_state = Mock(),
        schedule_pfc_state_flush        = Mock(),
        log_step                        = Mock(),
    )
    patches.start()
    task = asyncio.create_task(
        generate_smalltalk_turn(prepared, {"group_id": 1, "user_id": 2}, hctx.context, hctx)
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError, match="synthetic provider failure"):
                await task
        await asyncio.sleep(0)
        assert len(child_tasks) == 1
        assert child_tasks[0].done()
    finally:
        task.cancel()
        for child in child_tasks:
            child.cancel()
        await asyncio.gather(task, *child_tasks, return_exceptions=True)
        patches.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["new_message", "reset_during_typing", "reset_before_ack"])
async def test_new_message_or_reset_prevents_old_reply_commit(tmp_path, change):
    from core.delivery import DeliverySegments
    from plugins.xiaoqing_chat.memory.person_profile import clear_profiles_and_memory

    hctx, prepared = make_turn(tmp_path, forced=False)
    latest    = {"id": "m1"}
    parts     = build_text_message_parts("old reply")
    generated = GeneratedSmalltalkTurn(
        local_id="m1", reply="old reply", reply_parts=parts, session_generation=""
    )
    output = SimpleNamespace(outbound_batches=[[{"type": "text", "data": {"text": "old reply"}}]])
    lock = asyncio.Lock()

    async def newer_message():
        await asyncio.sleep(0.01)
        if change == "new_message":
            latest["id"] = "m2"
        elif change == "reset_during_typing":
            clear_profiles_and_memory(tmp_path, "g1", None)

    update = asyncio.create_task(newer_message())
    record_reply = AsyncMock(return_value=[])
    with (
        patch("plugins.xiaoqing_chat.turn_delivery._compute_typing_delay", return_value=0.05),
        patch.multiple(
            "plugins.xiaoqing_chat.turn_delivery",
            chat_lock                 = lambda _: lock,
            most_recent_user_local_id = lambda _: latest["id"],
            _build_reply_payload=Mock(return_value=output),
            sync_message_parts_to_registry = lambda state, values, **kwargs: values,
            _record_bot_reply              = record_reply,
            media_action_detail=Mock(return_value={}),
            schedule_pfc_state_flush      = Mock(),
            schedule_action_history_flush = Mock(),
            spawn_bg_task                 = Mock(),
            display_reply_text            = lambda _: "old reply",
            log_step                      = Mock(),
        ),
    ):
        result = await finalize_smalltalk_turn(
            prepared,
            generated,
            {"group_id": 1, "user_id": 2},
            hctx.context,
            hctx,
            started_at=0,
        )
    await update
    if change == "reset_before_ack":
        assert isinstance(result, DeliverySegments)
        clear_profiles_and_memory(tmp_path, "g1", None)
        await result.delivery_receipt.record(True)
    else:
        assert result == []
    record_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_sensitive_login_code_is_excluded_from_assistant_context(tmp_path):
    from plugins.xiaoqing_chat import handlers
    from plugins.xiaoqing_chat.llm.prompt_builder import build_dialogue_prompt
    from plugins.xiaoqing_chat.store_binding import bind_all_stores

    state = ChatRuntimeState()
    bind_all_stores(state, tmp_path)
    runtime = ChatRuntime(XiaoQingChatConfig(), [])
    context = SimpleNamespace(logger=MagicMock())
    code   = "SYNTHETIC-LOGIN-CODE-ONLY"
    action = {
        SENSITIVE_ACTION_KEY: True,
        "action": "send_private_msg",
        "params": {"user_id": 456, "message": [{"type": "text", "data": {"text": code}}]},
    }
    with (
        patch.object(handlers, "load_runtime", return_value=runtime),
        patch.object(handlers, "bound_state", return_value=state),
        patch.object(handlers, "chat_lock", return_value=asyncio.Lock()),
        patch.object(handlers, "allocate_local_id", return_value="m1"),
        patch.object(handlers, "configured_bot_name", return_value="小青"),
        patch.object(handlers, "schedule_memory_persist"),
        patch.object(handlers, "schedule_action_history_flush"),
        patch.object(handlers, "log_step"),
    ):
        await handlers.observe_outgoing_action(action, context, source_plugin="pendo")
    history = state.memory_store.get("u456")
    assert history == []
    assert code not in build_dialogue_prompt(history, bot_name="小青")


@pytest.mark.asyncio
async def test_summary_started_before_reset_preserves_cleared_memory(tmp_path):
    from plugins.xiaoqing_chat.llm import summarizer
    from plugins.xiaoqing_chat.memory.topic_summary_cache import load_topic_summary_entries
    from plugins.xiaoqing_chat.store_binding import bind_all_stores

    state = ChatRuntimeState()
    bind_all_stores(state, tmp_path)
    state.memory_store.append("g1", role="user", name="User", user_id=1, content="old private fact")
    history = state.memory_store.get("g1")
    started, release = asyncio.Event(), asyncio.Event()

    async def model(**kwargs):
        started.set()
        await release.wait()
        return json.dumps(
            {"topic": "old topic", "summary": "old private fact", "keywords": [], "key_points": []}
        )

    with patch.object(summarizer, "chat_completions", side_effect=model):
        task = asyncio.create_task(
            summarizer.maybe_update_topic_summary(
                data_dir                = tmp_path,
                memory_db               = state.memory_db,
                secrets                 = {},
                bot_name                = "小青",
                chat_id                 = "g1",
                history                 = history,
                min_messages_per_update = 1,
                max_cache_topics        = 10,
                llm                     = LLMCallConfig(
                    temperature            = 0,
                    top_p                  = 1,
                    max_tokens             = 128,
                    timeout_seconds        = 1,
                    max_retry              = 0,
                    retry_interval_seconds = 0,
                ),
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            await commands_module.reset_chat_session(state, "g1", tmp_path)
            assert state.memory_store.get("g1") == []
            assert load_topic_summary_entries(tmp_path, "g1") == []
            release.set()
            await task
            assert load_topic_summary_entries(tmp_path, "g1") == []
            assert state.memory_db.query("old private fact", chat_id="g1", min_score=-1) == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_reply_processing_preserves_four_requested_sentences():
    from plugins.xiaoqing_chat.llm.postprocess import process_llm_response

    output = process_llm_response(
        "第一句。第二句。第三句。第四句。",
        XiaoQingChatConfig().postprocess,
        bot_name="小青",
    )
    assert output == ["第一句。", "第二句。", "第三句。\n第四句。"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_action", ["fetch_knowledge", "rethink_goal"])
async def test_second_planner_block_is_respected(tmp_path, first_action):
    from plugins.xiaoqing_chat.planning import pfc_engine
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.store_binding import bind_all_stores

    state = ChatRuntimeState()
    bind_all_stores(state, tmp_path)
    generate = AsyncMock(return_value="unexpected reply")
    with (
        patch.object(
            pfc_engine,
            "plan_next_action",
            side_effect=[
                PFCPlan(first_action, "need more context"),
                PFCPlan("block_and_ignore", "current message must be ignored"),
            ],
        ),
        patch.object(pfc_engine, "build_memory_block", new=AsyncMock(return_value="")),
        patch.object(pfc_engine, "analyze_goals", new=AsyncMock(return_value=[])),
        patch.object(pfc_engine, "log_step"),
    ):
        result = await pfc_engine.run_pfc_once(
            context=SimpleNamespace(data_dir=tmp_path),
            runtime_cfg     = XiaoQingChatConfig(),
            secrets         = {},
            bot_name        = "小青",
            is_private      = False,
            chat_id         = "g1",
            current_text    = "stop",
            memory_store    = state.memory_store,
            action_history  = state.action_history,
            memory_db       = state.memory_db,
            pfc_state_store = state.pfc_state_store,
            generate_reply  = generate,
        )
    generate.assert_not_awaited()
    assert result.reply == ""
    assert result.action == "wait"
    assert result.reason.startswith("current_message_blocked:")


def test_real_experiment_context_loads_runtime(tmp_path):
    from plugins.xiaoqing_chat.chat_runtime import load_runtime
    from plugins.xiaoqing_chat.experiments.anthropomorphic_group import make_experiment_context

    context = make_experiment_context(
        session    = None,
        config     = {},
        secrets    = {},
        data_dir   = tmp_path,
        user_id    = 1,
        group_id   = 2,
        request_id = "audit-experiment",
    )
    assert load_runtime(context).cfg.enable_smalltalk
    assert context.get_settings_snapshot().secrets == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("store_kind", ["expression", "jargon"])
@pytest.mark.parametrize("restore_path", ["old_snapshot", "backup_recovery"])
async def test_reset_learning_records_stay_cleared(tmp_path, store_kind, restore_path):
    from plugins.xiaoqing_chat.expression.bw_expression_store import ExpressionRecord
    from plugins.xiaoqing_chat.expression.bw_jargon_store import JargonRecord
    from plugins.xiaoqing_chat.store_binding import bind_all_stores

    state = ChatRuntimeState()
    bind_all_stores(state, tmp_path)
    if store_kind == "expression":
        store = state.bw_expr_store
        seed  = [ExpressionRecord("old", "g1", "private situation", "private style")]
    else:
        store = state.bw_jargon_store
        seed = [JargonRecord("private jargon", scope_chat_id="g1")]
    store.save(seed)
    old_snapshot = store.load()
    await commands_module.reset_chat_session(state, "g1", tmp_path)
    assert not store.load()

    if restore_path == "old_snapshot":
        store.save(old_snapshot)
    else:
        path   = store._path()
        backup = path.with_name(path.name + ".bak")
        assert "private" not in backup.read_text(encoding="utf-8")
        path.write_text("{broken", encoding="utf-8")
    assert not store.load()


@pytest.mark.asyncio
async def test_action_history_bounds_memory_and_disk(tmp_path):
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore, ActionRecord

    store = ActionHistoryStore()
    store.bind(tmp_path)
    for index in range(500):
        store.append("g1", ActionRecord(index, "m1", "wait", str(index), {}, False))
    store.flush("g1")
    assert len(json.loads((tmp_path / "action_history/g1.json").read_text(encoding="utf-8"))) == 200
    assert len(await store.get_recent_async("g1", max_items=1000)) == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["I like coffee", "a quote absent from every message"])
async def test_unverified_person_fact_is_rejected(tmp_path, evidence):
    from plugins.xiaoqing_chat.memory import knowledge_extract
    from plugins.xiaoqing_chat.memory.person_profile import load_profile
    from plugins.xiaoqing_chat.store_binding import bind_all_stores

    state = ChatRuntimeState()
    bind_all_stores(state, tmp_path)
    for index in range(20):
        state.memory_store.append(
            "g1",
            role    = "user",
            name    = "Alice" if index % 2 == 0 else "Bob",
            user_id = 1 if index % 2 == 0 else 2,
            content = "I like tea" if index % 2 == 0 else "I like coffee",
        )
    history  = state.memory_store.get("g1")
    response = json.dumps(
        {
            "facts": [
                {
                    "subject_id": 1,
                    "subject_name": "Alice",
                    "fact": "likes coffee",
                    "evidence": evidence,
                }
            ]
        }
    )
    with patch.object(knowledge_extract, "chat_completions", new=AsyncMock(return_value=response)):
        await knowledge_extract.maybe_extract_person_facts(
            data_dir  = tmp_path,
            secrets   = {},
            memory_db = state.memory_db,
            chat_id   = "g1",
            history   = history,
            llm       = LLMCallConfig(
                temperature            = 0,
                top_p                  = 1,
                max_tokens             = 128,
                timeout_seconds        = 1,
                max_retry              = 0,
                retry_interval_seconds = 0,
            ),
        )
    assert load_profile(tmp_path, chat_id="g1", subject_id=1) is None
    hits = state.memory_db.query(
        "likes coffee", chat_id="g1", type_filter="person_info", min_score=-1
    )
    assert hits == []


def test_reply_processing_preserves_code_fence_and_last_statement():
    from plugins.xiaoqing_chat.llm.postprocess import process_llm_response

    raw = '```python\nprint("one!")\nprint("two!")\nprint("three!")\nprint("four!")\n```'
    output = "\n".join(process_llm_response(raw, XiaoQingChatConfig().postprocess, bot_name="小青"))
    assert output == raw


def test_reply_processing_preserves_json_string_spacing_and_length():
    from plugins.xiaoqing_chat.llm.postprocess import process_llm_response

    payload = {"content": "A  B, " * 100, "sentences": "第一句。第二句。第三句。第四句。"}
    raw = json.dumps(payload, ensure_ascii=False)
    output = process_llm_response(raw, XiaoQingChatConfig().postprocess, bot_name="小青")
    assert output == [raw]
    assert json.loads(output[0]) == payload


@pytest.mark.asyncio
async def test_sensitive_credential_delivery_skips_core_observers_and_wire_metadata():
    from core.app_delivery import AppDeliveryMixin
    from core.delivery import strip_receipt
    from plugins.pendo.handlers.web import WebHandler

    context = SimpleNamespace(send_action=AsyncMock(return_value=True))
    assert await WebHandler._send_private_text(context, "456", "SYNTHETIC-CODE") is True
    action = context.send_action.await_args.args[0]
    assert action[SENSITIVE_ACTION_KEY] is True
    app = SimpleNamespace(plugin_manager=Mock())
    await AppDeliveryMixin._notify_outgoing_action_observers(app, action)
    app.plugin_manager.resolve_service.assert_not_called()
    wire_action = strip_receipt(action)
    assert SENSITIVE_ACTION_KEY not in wire_action
    assert wire_action["params"]["message"][0]["data"]["text"] == "SYNTHETIC-CODE"


@pytest.mark.asyncio
async def test_experiment_send_action_commits_delivery_receipt(tmp_path):
    from core.delivery import DeliveryReceipt, attach_receipt
    from plugins.xiaoqing_chat.experiments.anthropomorphic_group import make_experiment_context

    context = make_experiment_context(
        session    = None,
        config     = {},
        secrets    = {},
        data_dir   = tmp_path,
        user_id    = 1,
        group_id   = 2,
        request_id = "receipt",
    )
    commit, rollback, unknown = AsyncMock(), AsyncMock(), AsyncMock()
    receipt = DeliveryReceipt(expected_actions=1, commit=commit, rollback=rollback, unknown=unknown)
    action = {"action": "send_group_msg", "params": {"group_id": 2, "message": []}}
    assert await context.send_action(attach_receipt(action, receipt)) is True
    commit.assert_awaited_once()
    rollback.assert_not_awaited()
    assert len(context.delivered_actions) == 1
