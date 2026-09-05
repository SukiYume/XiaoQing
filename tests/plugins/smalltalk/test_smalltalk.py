"""Smalltalk 的路由、持久化、provider 与异常边界测试。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.smalltalk import main as smalltalk
from tests.helpers.assertions import text_segments_text
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import settings_snapshot

ROOT = REPOSITORY_ROOT


class _Context:
    def __init__(
        self,
        data_dir: Path,
        *,
        user_id: object  = 123,
        group_id: object = None,
    ) -> None:
        self.data_dir         = data_dir
        self.current_user_id  = user_id
        self.current_group_id = group_id
        self.request_id       = "smalltalk-test-request"
        self.logger           = logging.getLogger("test.smalltalk")
        self.config: object   = {"plugins": {"smalltalk": {"voice_probability": 0.0}}}
        self.chat_reply       = SimpleNamespace(
            reply=AsyncMock(return_value=smalltalk.segments("provider response"))
        )
        self.voice_synthesis = SimpleNamespace(
            synthesize_text=AsyncMock(
                return_value=[{"type": "record", "data": {"file": "voice.wav"}}]
            )
        )
        self.capabilities = SimpleNamespace(
            chat_reply      = self.chat_reply,
            voice_synthesis = None,
        )

    def get_settings_snapshot(self):
        config = self.config if isinstance(self.config, dict) else {}
        return settings_snapshot(config=config)


@pytest.fixture
def context(tmp_path: Path) -> _Context:
    return _Context(tmp_path)


@pytest.fixture
def event() -> dict[str, object]:
    return {"user_id": 123, "message_type": "private", "message": "test"}


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _set_voice_probability(context: _Context, value: object) -> None:
    context.config = {"plugins": {"smalltalk": {"voice_probability": value}}}


def test_manifest_contract() -> None:
    plugin_dir = ROOT / "plugins" / "smalltalk"
    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    commands = {command["name"]: command for command in manifest["commands"]}

    assert manifest["entry"] == "main.py"
    assert (plugin_dir / manifest["entry"]).is_file()
    assert manifest["concurrency"] == "parallel"
    assert set(commands) == {"qa", "qa_list", "qa_remove"}
    assert all(command["admin_only"] is True for command in commands.values())
    assert set(commands["qa"]["triggers"]) == {"记忆", "记住", "学习"}
    assert commands["qa_list"]["triggers"] == ["对话"]
    assert commands["qa_remove"]["triggers"] == ["删除对话"]


def test_readme_and_global_docs_match_runtime_contract() -> None:
    readme = (ROOT / "plugins" / "smalltalk" / "README.md").read_text(encoding="utf-8")
    global_docs = (ROOT / "docs" / "09-plugins.md").read_text(encoding="utf-8")

    for marker in (
        "/记忆",
        "/记住",
        "/学习",
        "/对话",
        "/删除对话",
        "chat.reply",
        "voice.synthesize_text",
        "精确匹配",
    ):
        assert marker in readme
    assert "笑话" not in readme
    smalltalk_section = global_docs.split("### `smalltalk`：基础闲聊与分域问答", 1)[1].split(
        "### `chat`：Coze 单轮对话", 1
    )[0]
    for marker in ("/记忆", "/记住", "/学习", "/对话", "/删除对话"):
        assert marker in smalltalk_section
    assert "精确 QA 命中" in smalltalk_section


def test_default_responses_are_returned_as_a_copy(context: _Context) -> None:
    responses = smalltalk._load_responses(context)

    assert responses == list(smalltalk.DEFAULT_RESPONSES)
    responses.append("local mutation")
    assert "local mutation" not in smalltalk.DEFAULT_RESPONSES


def test_custom_responses_use_priority_cleaning_and_deduplication(
    context: _Context,
) -> None:
    _write_json(
        context.data_dir / "小青.json",
        {
            "小青": [
                None,
                " ",
                " 优先回复 ",
                "优先回复",
                "x" * (smalltalk.MAX_RANDOM_RESPONSE_LENGTH + 1),
            ]
        },
    )
    _write_json(context.data_dir / "responses.json", {"responses": ["低优先级回复"]})

    assert smalltalk._load_responses(context) == ["优先回复"]


@pytest.mark.parametrize(
    "primary",
    [[], {}, {"小青": []}, {"小青": "invalid"}, {"小青": [None, " "]}],
)
def test_invalid_primary_response_file_falls_through_to_compat_file(
    primary: object,
    context: _Context,
) -> None:
    _write_json(context.data_dir / "小青.json", primary)
    _write_json(context.data_dir / "responses.json", {"responses": ["兼容回复"]})

    assert smalltalk._load_responses(context) == ["兼容回复"]


def test_malformed_response_json_falls_through(context: _Context) -> None:
    (context.data_dir / "小青.json").write_text("{broken", encoding="utf-8")
    _write_json(context.data_dir / "responses.json", {"responses": ["后备回复"]})

    assert smalltalk._load_responses(context) == ["后备回复"]


def test_custom_response_count_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    context: _Context,
) -> None:
    monkeypatch.setattr(smalltalk, "MAX_CUSTOM_RESPONSES", 2)
    _write_json(context.data_dir / "responses.json", {"responses": ["一", "二", "三"]})

    assert smalltalk._load_responses(context) == ["一", "二"]


def test_bot_name_only_uses_clean_response(
    monkeypatch: pytest.MonkeyPatch,
    context: _Context,
) -> None:
    _write_json(context.data_dir / "responses.json", {"responses": ["第一", "第二"]})
    monkeypatch.setattr(smalltalk.random, "choice", lambda values: values[-1])

    assert text_segments_text(smalltalk.call_bot_name_only(context)) == "第二"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, 1),
        ("42", 42),
        (0, None),
        (-1, None),
        (True, None),
        ("-1", None),
        ("1.5", None),
        ("9" * 21, None),
        (None, None),
    ],
)
def test_positive_id_validation(value: object, expected: int | None) -> None:
    assert smalltalk._positive_id(value) == expected


def test_qa_scope_prefers_group_then_private_and_rejects_missing_actor(tmp_path: Path) -> None:
    assert smalltalk._qa_scope(_Context(tmp_path, user_id="12", group_id="34")) == "group_34"
    assert smalltalk._qa_scope(_Context(tmp_path, user_id="12")) == "private_12"
    with pytest.raises(ValueError, match="positive user or group ID"):
        smalltalk._qa_scope(_Context(tmp_path, user_id=True, group_id="bad"))


def test_qa_normalization_removes_invalid_duplicates_and_oversized_values() -> None:
    raw = {
        1: ["invalid key"],
        " ": ["invalid question"],
        "问题": [None, " ", " 回答一 ", "回答一", "回答二"],
        " 问题 ": ["回答一", "回答三"],
        "超" * (smalltalk.MAX_QUESTION_LENGTH + 1): ["invalid"],
        "坏回答": ["x" * (smalltalk.MAX_ANSWER_LENGTH + 1)],
        "坏容器": "answer",
    }

    assert smalltalk._normalize_qa(raw) == {"问题": ["回答一", "回答二", "回答三"]}
    assert smalltalk._normalize_qa([]) == {}


def test_qa_normalization_enforces_question_and_answer_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smalltalk, "MAX_QUESTIONS", 2)
    monkeypatch.setattr(smalltalk, "MAX_ANSWERS_PER_QUESTION", 2)

    assert smalltalk._normalize_qa({"一": ["a", "b", "c"], "二": ["d"], "三": ["e"]}) == {
        "一": ["a", "b"],
        "二": ["d"],
    }


@pytest.mark.asyncio
async def test_corrupted_qa_file_is_safely_normalized(context: _Context) -> None:
    _write_json(
        smalltalk._qa_file(context),
        {"有效": [None, "回答", "回答"], "空": [], "坏": "not-a-list"},
    )

    assert await smalltalk._load_qa(context) == {"有效": ["回答"]}
    assert await smalltalk.get_qa_answer(context, "有效") == "回答"
    assert await smalltalk.get_qa_answer(context, "空") is None


@pytest.mark.asyncio
async def test_add_qa_persists_scope_and_audit(
    context: _Context,
    event: dict[str, object],
) -> None:
    result = await smalltalk.handle("qa", "你好 你好呀", event, context)

    assert text_segments_text(result) == "对话添加成功了！"
    assert await smalltalk.get_qa_answer(context, "你好") == "你好呀"
    assert (context.data_dir / "QA_private_123.json").is_file()
    audit = json.loads((context.data_dir / "QA_audit.json").read_text(encoding="utf-8"))
    assert audit["entries"][-1]["operation"] == "add"
    assert audit["entries"][-1]["scope"] == "private_123"
    assert audit["entries"][-1]["owner"] == 123


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param("", "格式", id="empty"),
        pytest.param("只有问题", "格式", id="missing-answer"),
        pytest.param(f"{'问' * 129} 回答", "问题不能为空", id="question-too-long"),
        pytest.param(f"问题 {'答' * 1001}", "回答不能为空", id="answer-too-long"),
    ],
)
async def test_add_qa_validates_shape_and_lengths(
    args: str,
    expected: str,
    context: _Context,
    event: dict[str, object],
) -> None:
    result = await smalltalk.handle("qa", args, event, context)

    assert expected in text_segments_text(result)
    assert await smalltalk._load_qa(context) == {}


@pytest.mark.asyncio
async def test_add_qa_rejects_duplicates_and_caps(
    monkeypatch: pytest.MonkeyPatch,
    context: _Context,
    event: dict[str, object],
) -> None:
    monkeypatch.setattr(smalltalk, "MAX_ANSWERS_PER_QUESTION", 2)
    assert "成功" in text_segments_text(await smalltalk.handle("qa", "问题 回答一", event, context))
    assert "已经" in text_segments_text(await smalltalk.handle("qa", "问题 回答一", event, context))
    assert "成功" in text_segments_text(await smalltalk.handle("qa", "问题 回答二", event, context))
    assert "上限" in text_segments_text(await smalltalk.handle("qa", "问题 回答三", event, context))

    monkeypatch.setattr(smalltalk, "MAX_QUESTIONS", 1)
    assert "上限" in text_segments_text(await smalltalk.handle("qa", "另一个 回答", event, context))


@pytest.mark.asyncio
async def test_qa_is_isolated_between_groups_and_private_users(tmp_path: Path) -> None:
    group_one = _Context(tmp_path, user_id=1, group_id=10)
    group_two = _Context(tmp_path, user_id=1, group_id=20)
    private = _Context(tmp_path, user_id=1)

    await smalltalk._add_qa(group_one, "问题 群一")
    await smalltalk._add_qa(group_two, "问题 群二")
    await smalltalk._add_qa(private, "问题 私聊")

    assert await smalltalk.get_qa_answer(group_one, "问题") == "群一"
    assert await smalltalk.get_qa_answer(group_two, "问题") == "群二"
    assert await smalltalk.get_qa_answer(private, "问题") == "私聊"


@pytest.mark.asyncio
async def test_list_qa_exact_query_and_empty_states(
    context: _Context,
    event: dict[str, object],
) -> None:
    assert "还没有" in text_segments_text(await smalltalk.handle("qa_list", "", event, context))
    await smalltalk._add_qa(context, "天气 晴")
    await smalltalk._add_qa(context, "天气 雨")
    await smalltalk._add_qa(context, "天气预报 多云")

    index   = text_segments_text(await smalltalk.handle("qa_list", "", event, context))
    exact   = text_segments_text(await smalltalk.handle("qa_list", "天气", event, context))
    missing = text_segments_text(await smalltalk.handle("qa_list", "天气预", event, context))

    assert "天气" in index and "天气预报" in index
    assert "晴" in exact and "雨" in exact and "多云" not in exact
    assert missing == "没有这个问题的回答"


def test_qa_list_output_reserves_space_for_omission_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smalltalk, "MAX_QA_REPLY_LENGTH", 30)

    result = smalltalk._bounded_lines("列表：", ["a" * 10, "b" * 10, "c" * 10])

    assert len(result) <= 30
    assert "未显示" in result


@pytest.mark.asyncio
async def test_remove_specific_last_and_whole_question(
    context: _Context,
    event: dict[str, object],
) -> None:
    await smalltalk._add_qa(context, "问题 带 空格 的回答")
    await smalltalk._add_qa(context, "问题 回答二")

    removed = await smalltalk.handle("qa_remove", "问题 带 空格 的回答", event, context)
    assert "指定回答已删除" in text_segments_text(removed)
    assert await smalltalk._load_qa(context) == {"问题": ["回答二"]}

    removed_last = await smalltalk.handle("qa_remove", "问题 回答二", event, context)
    assert "指定回答已删除" in text_segments_text(removed_last)
    assert await smalltalk._load_qa(context) == {}

    await smalltalk._add_qa(context, "问题 回答一")
    await smalltalk._add_qa(context, "问题 回答二")
    removed_whole = await smalltalk.handle("qa_remove", "问题", event, context)
    assert "2 个回答已删除" in text_segments_text(removed_whole)


@pytest.mark.asyncio
async def test_remove_qa_reports_missing_inputs(
    context: _Context,
    event: dict[str, object],
) -> None:
    assert "格式" in text_segments_text(await smalltalk.handle("qa_remove", "", event, context))
    assert "没有这个对话" in text_segments_text(
        await smalltalk.handle("qa_remove", "不存在", event, context)
    )
    await smalltalk._add_qa(context, "问题 回答")
    assert "没有这个回答" in text_segments_text(
        await smalltalk.handle("qa_remove", "问题 另一个", event, context)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alias",
    ["help", "HELP", "帮助", "?"],
    ids=["lower-help", "upper-help", "chinese-help", "question-mark"],
)
@pytest.mark.parametrize(
    ("command", "marker"),
    [("qa", "问答添加"), ("qa_list", "问答查询"), ("qa_remove", "问答删除")],
)
async def test_help_aliases(
    alias: str,
    command: str,
    marker: str,
    context: _Context,
    event: dict[str, object],
) -> None:
    result = await smalltalk.handle(command, alias, event, context)

    assert marker in text_segments_text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["help", "HELP", "帮助", "?"])
async def test_help_with_extra_text_runs_the_domain_command(
    alias: str,
    context: _Context,
    event: dict[str, object],
) -> None:
    result = await smalltalk.handle("qa", f"{alias} extra", event, context)

    assert text_segments_text(result) == "对话添加成功了！"
    assert await smalltalk._load_qa(context) == {alias: ["extra"]}


@pytest.mark.asyncio
async def test_unknown_command(context: _Context, event: dict[str, object]) -> None:
    assert (
        text_segments_text(await smalltalk.handle("unknown", "args", event, context)) == "未知命令"
    )


@pytest.mark.asyncio
async def test_corrupted_audit_entries_are_replaced_with_valid_records(
    context: _Context,
) -> None:
    _write_json(context.data_dir / "QA_audit.json", {"entries": "invalid"})

    await smalltalk._add_qa(context, "问题 回答")

    audit = json.loads((context.data_dir / "QA_audit.json").read_text(encoding="utf-8"))
    assert len(audit["entries"]) == 1
    assert audit["entries"][0]["question"] == "问题"


@pytest.mark.asyncio
async def test_audit_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    context: _Context,
) -> None:
    monkeypatch.setattr(smalltalk, "MAX_AUDIT_ENTRIES", 2)
    _write_json(
        context.data_dir / "QA_audit.json",
        {"entries": [{"old": 1}, "invalid", {"old": 2}]},
    )

    await smalltalk._add_qa(context, "问题 回答")

    audit = json.loads((context.data_dir / "QA_audit.json").read_text(encoding="utf-8"))
    assert audit["entries"] == [
        {"old": 2},
        {
            "at": audit["entries"][1]["at"],
            "operation": "add",
            "scope": "private_123",
            "owner": 123,
            "question": "问题",
        },
    ]


@pytest.mark.asyncio
async def test_audit_failure_does_not_turn_committed_qa_into_false_failure(
    monkeypatch: pytest.MonkeyPatch,
    context: _Context,
) -> None:
    original_write = smalltalk._write_json

    def fail_audit(path: Path, value: object) -> None:
        if path.name == "QA_audit.json":
            raise OSError("private audit path")
        original_write(path, value)

    monkeypatch.setattr(smalltalk, "_write_json", fail_audit)

    assert text_segments_text(await smalltalk._add_qa(context, "问题 回答")) == "对话添加成功了！"
    assert await smalltalk.get_qa_answer(context, "问题") == "回答"


@pytest.mark.asyncio
async def test_main_qa_write_failure_uses_public_error_boundary(
    monkeypatch: pytest.MonkeyPatch,
    context: _Context,
    event: dict[str, object],
) -> None:
    monkeypatch.setattr(
        smalltalk,
        "_write_json",
        lambda *_args: (_ for _ in ()).throw(OSError("private data path")),
    )

    result = await smalltalk.handle("qa", "问题 回答", event, context)

    text = text_segments_text(result)
    assert "XQ-PLUGIN-UNEXPECTED" in text
    assert "private data path" not in text


@pytest.mark.asyncio
async def test_concurrent_qa_additions_are_serialized(context: _Context) -> None:
    await asyncio.gather(
        *(
            smalltalk._add_qa(context, f"并发 回答{index}")
            for index in range(smalltalk.MAX_ANSWERS_PER_QUESTION)
        )
    )

    assert len((await smalltalk._load_qa(context))["并发"]) == smalltalk.MAX_ANSWERS_PER_QUESTION
    audit = json.loads((context.data_dir / "QA_audit.json").read_text(encoding="utf-8"))
    assert len(audit["entries"]) == smalltalk.MAX_ANSWERS_PER_QUESTION


@pytest.mark.asyncio
async def test_handle_exception_uses_public_error_boundary(
    monkeypatch: pytest.MonkeyPatch,
    context: _Context,
    event: dict[str, object],
) -> None:
    monkeypatch.setattr(
        smalltalk,
        "parse",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("private parser detail")),
    )

    result = await smalltalk.handle("qa", "问题 回答", event, context)

    text = text_segments_text(result)
    assert "XQ-PLUGIN-UNEXPECTED" in text
    assert "private parser detail" not in text


@pytest.mark.asyncio
async def test_blank_smalltalk_does_not_call_provider(
    context: _Context,
    event: dict[str, object],
) -> None:
    assert await smalltalk.handle_smalltalk(" \n ", event, context) == []
    context.chat_reply.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_qa_match_bypasses_chat_provider(
    context: _Context,
    event: dict[str, object],
) -> None:
    await smalltalk._add_qa(context, "你好 你好呀")

    result = await smalltalk.handle_smalltalk("你好", event, context)

    assert text_segments_text(result) == "你好呀"
    context.chat_reply.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_qa_hot_path_caches_snapshot_and_offloads_json_io(
    context: _Context,
    event: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_json(smalltalk._qa_file(context), {"缓存问题": ["缓存回答"]})
    event_loop_thread        = threading.get_ident()
    load_threads: list[int]  = []
    write_threads: list[int] = []
    original_load            = smalltalk._load_json
    original_write           = smalltalk._write_json

    def tracked_load(path: Path, default: object) -> object:
        load_threads.append(threading.get_ident())
        return original_load(path, default)

    def tracked_write(path: Path, value: object) -> None:
        write_threads.append(threading.get_ident())
        original_write(path, value)

    monkeypatch.setattr(smalltalk, "_load_json", tracked_load)
    monkeypatch.setattr(smalltalk, "_write_json", tracked_write)

    first  = await smalltalk.handle_smalltalk("缓存问题", event, context)
    second = await smalltalk.handle_smalltalk("缓存问题", event, context)
    added  = await smalltalk.handle("qa", "新问题 新回答", event, context)

    assert text_segments_text(first) == text_segments_text(second) == "缓存回答"
    assert "成功" in text_segments_text(added)
    assert len(load_threads) == 2  # QA 快照与 audit 快照各只加载一次。
    assert all(thread_id != event_loop_thread for thread_id in load_threads)
    assert write_threads
    assert all(thread_id != event_loop_thread for thread_id in write_threads)


@pytest.mark.asyncio
async def test_chat_provider_receives_only_normalized_actor(
    context: _Context,
) -> None:
    context.current_user_id  = "42"
    context.current_group_id = "84"

    result = await smalltalk._call_chat_api("private prompt", context)

    assert text_segments_text(result) == "provider response"
    context.chat_reply.reply.assert_awaited_once_with(
        "private prompt",
        {"user_id": 42, "group_id": 84},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_result", [None, "invalid", [1], ["text"]])
async def test_invalid_chat_provider_result_uses_fallback(
    provider_result: object,
    context: _Context,
) -> None:
    context.chat_reply.reply.return_value = provider_result

    result = await smalltalk._call_chat_api("test", context)

    assert text_segments_text(result) == "暂时无法回复，请稍后再试~"


@pytest.mark.asyncio
async def test_missing_or_failed_chat_provider_uses_fallback(context: _Context) -> None:
    context.capabilities.chat_reply = None
    missing                         = await smalltalk._call_chat_api("test", context)
    assert text_segments_text(missing) == "暂时无法回复，请稍后再试~"

    context.capabilities.chat_reply      = context.chat_reply
    context.chat_reply.reply.side_effect = RuntimeError("private provider detail")
    failed                               = await smalltalk._call_chat_api("test", context)
    assert text_segments_text(failed) == "暂时无法回复，请稍后再试~"
    assert "private provider detail" not in text_segments_text(failed)


@pytest.mark.asyncio
async def test_unmatched_smalltalk_uses_chat_then_voice_boundary(
    context: _Context,
    event: dict[str, object],
) -> None:
    result = await smalltalk.handle_smalltalk("随便聊聊", event, context)

    assert text_segments_text(result) == "provider response"
    context.chat_reply.reply.assert_awaited_once()


@pytest.mark.parametrize(
    "value",
    [True, False, "0.5", None, float("nan"), float("inf"), -0.1, 1.1],
)
def test_invalid_explicit_voice_probability_disables_voice(
    value: object,
    context: _Context,
) -> None:
    _set_voice_probability(context, value)
    assert smalltalk._voice_probability(context) == 0.0


def test_voice_probability_defaults_and_accepts_endpoints(context: _Context) -> None:
    context.config = {}
    assert smalltalk._voice_probability(context) == smalltalk.DEFAULT_VOICE_PROBABILITY
    context.config = {"plugins": {}}
    assert smalltalk._voice_probability(context) == smalltalk.DEFAULT_VOICE_PROBABILITY
    context.config = {"plugins": {"smalltalk": {}}}
    assert smalltalk._voice_probability(context) == smalltalk.DEFAULT_VOICE_PROBABILITY

    _set_voice_probability(context, 0)
    assert smalltalk._voice_probability(context) == 0.0
    _set_voice_probability(context, 1)
    assert smalltalk._voice_probability(context) == 1.0


@pytest.mark.parametrize("config", [None, [], {"plugins": []}, {"plugins": {"smalltalk": []}}])
def test_snapshot_normalizes_malformed_voice_namespace_to_absent(
    config: object,
    context: _Context,
) -> None:
    context.config = config
    assert smalltalk._voice_probability(context) == smalltalk.DEFAULT_VOICE_PROBABILITY


@pytest.mark.asyncio
async def test_zero_voice_probability_never_samples_or_calls_provider(
    monkeypatch: pytest.MonkeyPatch,
    context: _Context,
) -> None:
    context.capabilities.voice_synthesis = context.voice_synthesis
    monkeypatch.setattr(
        smalltalk.random,
        "random",
        lambda: (_ for _ in ()).throw(AssertionError("must not sample")),
    )
    reply = smalltalk.segments("文字")

    assert await smalltalk._maybe_convert_to_voice(reply, context) == reply
    context.voice_synthesis.synthesize_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_probability_threshold_is_half_open(
    monkeypatch: pytest.MonkeyPatch,
    context: _Context,
) -> None:
    _set_voice_probability(context, 0.5)
    context.capabilities.voice_synthesis = context.voice_synthesis
    monkeypatch.setattr(smalltalk.random, "random", lambda: 0.5)
    reply = smalltalk.segments("文字")

    assert await smalltalk._maybe_convert_to_voice(reply, context) == reply
    context.voice_synthesis.synthesize_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_success_concatenates_bounded_text(
    context: _Context,
) -> None:
    _set_voice_probability(context, 1.0)
    context.capabilities.voice_synthesis = context.voice_synthesis
    reply                                = [
        {"type": "text", "data": {"text": "第一"}},
        {"type": "text", "data": {"text": "第二"}},
    ]

    result = await smalltalk._maybe_convert_to_voice(reply, context)

    assert result == [{"type": "record", "data": {"file": "voice.wav"}}]
    context.voice_synthesis.synthesize_text.assert_awaited_once_with("第一第二")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        [],
        [{"type": "image", "data": {"file": "image.jpg"}}],
        [
            {"type": "text", "data": {"text": "文字"}},
            {"type": "image", "data": {"file": "image.jpg"}},
        ],
        [{"type": "text", "data": []}],
        [{"type": "text", "data": {"text": " "}}],
    ],
)
async def test_non_pure_text_reply_is_not_converted(
    reply: list[dict[str, object]],
    context: _Context,
) -> None:
    _set_voice_probability(context, 1.0)
    context.capabilities.voice_synthesis = context.voice_synthesis

    assert await smalltalk._maybe_convert_to_voice(reply, context) == reply
    context.voice_synthesis.synthesize_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_voice_text_is_not_partially_submitted(context: _Context) -> None:
    _set_voice_probability(context, 1.0)
    context.capabilities.voice_synthesis = context.voice_synthesis
    reply = smalltalk.segments("x" * (smalltalk.MAX_VOICE_TEXT_LENGTH + 1))

    assert await smalltalk._maybe_convert_to_voice(reply, context) == reply
    context.voice_synthesis.synthesize_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_voice_provider_keeps_text(context: _Context) -> None:
    _set_voice_probability(context, 1.0)
    reply = smalltalk.segments("文字")

    assert await smalltalk._maybe_convert_to_voice(reply, context) == reply


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_result", [None, [], "invalid", [1]])
async def test_invalid_voice_provider_result_keeps_text(
    provider_result: object,
    context: _Context,
) -> None:
    _set_voice_probability(context, 1.0)
    context.capabilities.voice_synthesis                 = context.voice_synthesis
    context.voice_synthesis.synthesize_text.return_value = provider_result
    reply                                                = smalltalk.segments("文字")

    assert await smalltalk._maybe_convert_to_voice(reply, context) == reply


@pytest.mark.asyncio
async def test_voice_provider_exception_keeps_text(context: _Context) -> None:
    _set_voice_probability(context, 1.0)
    context.capabilities.voice_synthesis                = context.voice_synthesis
    context.voice_synthesis.synthesize_text.side_effect = RuntimeError("private voice detail")
    reply                                               = smalltalk.segments("文字")

    result = await smalltalk._maybe_convert_to_voice(reply, context)

    assert result == reply
    assert "private voice detail" not in text_segments_text(result)
