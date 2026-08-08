"""随机选择插件的解析、抽样和消息契约测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugins.choice import main as choice

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def context() -> SimpleNamespace:
    return SimpleNamespace(
        logger=MagicMock(),
        request_id="test-choice",
        secrets={},
    )


class TestChoicePackageContract:
    def test_entrypoints_and_constants(self) -> None:
        assert callable(choice.handle)
        assert choice.MIN_OPTIONS == 2
        assert choice.MAX_OPTIONS == 50
        assert choice.MAX_CHOICES == 10
        assert "随机选择" in choice.HELP_TEXT
        assert "**" not in choice.HELP_TEXT

    def test_manifest_matches_runtime_entrypoints(self) -> None:
        manifest = json.loads(
            (ROOT / "plugins" / "choice" / "plugin.json").read_text(encoding="utf-8")
        )
        assert manifest["name"] == "choice"
        assert manifest["entry"] == "main.py"
        assert manifest["commands"][0]["triggers"] == ["choice", "决定", "选择", "抽奖"]
        assert manifest["schedule"] == []


class TestParseChoiceArgs:
    def test_basic_arguments(self) -> None:
        assert choice.parse_choice_args("问题 A B C") == (
            "问题",
            ["A", "B", "C"],
            1,
            False,
        )

    def test_quoted_text_and_both_flags(self) -> None:
        assert choice.parse_choice_args('"吃什么好" "ice cream" pizza -n 2 --unique') == (
            "吃什么好",
            ["ice cream", "pizza"],
            2,
            True,
        )

    def test_flags_may_appear_before_positional_text(self) -> None:
        assert choice.parse_choice_args("-u -n 2 问题 A B C") == (
            "问题",
            ["A", "B", "C"],
            2,
            True,
        )

    @pytest.mark.parametrize("args", ["", "   ", "help", "帮助"])
    def test_empty_or_help_arguments_return_empty_request(self, args: str) -> None:
        assert choice.parse_choice_args(args) == (None, [], 1, False)

    @pytest.mark.parametrize("args", ["-u", "-n 2", "--", "-u -n 2"])
    def test_flags_without_question_are_rejected(self, args: str) -> None:
        with pytest.raises(choice.ChoiceArgumentError, match="问题"):
            choice.parse_choice_args(args)

    @pytest.mark.parametrize("alias", ["help", "HELP", "帮助"])
    def test_help_with_extra_text_is_not_an_exact_help_request(self, alias: str) -> None:
        assert choice.parse_choice_args(f"{alias} 其余内容") == (
            alias,
            ["其余内容"],
            1,
            False,
        )

    def test_double_dash_treats_following_flags_as_text(self) -> None:
        assert choice.parse_choice_args("符号 -- -n -u") == (
            "符号",
            ["-n", "-u"],
            1,
            False,
        )

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (None, "必须是文本"),
            ("x" * 4_097, "参数过长"),
            ('问题 "A B', "引号"),
            ("问题 A B -n", "必须提供"),
            ("问题 A B -n two", "ASCII"),
            ("问题 A B -n １２", "ASCII"),
            ("问题 A B -n -1", "ASCII"),
            ("问题 A B -n 2 -n 3", "只能指定一次"),
            ("问题 A B --other", "不支持"),
        ],
    )
    def test_invalid_arguments_fail_with_stable_messages(
        self,
        args: object,
        message: str,
    ) -> None:
        with pytest.raises(choice.ChoiceArgumentError, match=message):
            choice.parse_choice_args(args)

    def test_question_must_be_bounded_and_printable(self) -> None:
        with pytest.raises(choice.ChoiceArgumentError, match="问题必须"):
            choice.parse_choice_args(f"{'问' * 101} A B")
        with pytest.raises(choice.ChoiceArgumentError, match="问题必须"):
            choice.parse_choice_args('"问\n题" A B')

    def test_question_without_options_is_left_for_sampling_validation(self) -> None:
        assert choice.parse_choice_args("问题") == ("问题", [], 1, False)


class TestMakeChoice:
    def test_non_unique_mode_preserves_duplicate_weight_and_requested_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rng = MagicMock()
        rng.choices.return_value = ["A", "A", "B", "A", "B"]
        monkeypatch.setattr(choice, "_RNG", rng)

        result = choice.make_choice(["A", "A", "B"], 5, unique=False)

        assert result == ["A", "A", "B", "A", "B"]
        rng.choices.assert_called_once_with(["A", "A", "B"], k=5)
        rng.sample.assert_not_called()

    def test_unique_mode_deduplicates_by_text_before_sampling(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rng = MagicMock()
        rng.sample.return_value = ["A", "C"]
        monkeypatch.setattr(choice, "_RNG", rng)

        result = choice.make_choice(["A", "A", "B", "C", "B"], 2, unique=True)

        assert result == ["A", "C"]
        rng.sample.assert_called_once_with(["A", "B", "C"], k=2)
        rng.choices.assert_not_called()

    def test_unique_count_uses_distinct_option_count(self) -> None:
        with pytest.raises(choice.ChoiceArgumentError, match="不同选项数量"):
            choice.make_choice(["A", "A", "B"], 3, unique=True)

    @pytest.mark.parametrize(
        ("options", "count", "unique", "message"),
        [
            (("A", "B"), 1, False, "必须是列表"),
            (["A"], 1, False, "至少需要"),
            ([str(index) for index in range(51)], 1, False, "选项过多"),
            (["", "B"], 1, False, "每个选项"),
            (["A\nB", "C"], 1, False, "每个选项"),
            (["A" * 201, "B"], 1, False, "每个选项"),
            (["A", "B"], 0, False, "1–10"),
            (["A", "B"], 11, False, "1–10"),
            (["A", "B"], True, False, "1–10"),
            (["A", "B"], 1, 1, "布尔值"),
        ],
    )
    def test_invalid_sampling_inputs_are_rejected(
        self,
        options: object,
        count: object,
        unique: object,
        message: str,
    ) -> None:
        with pytest.raises(choice.ChoiceArgumentError, match=message):
            choice.make_choice(options, count, unique)  # type: ignore[arg-type]


class TestFormatChoiceResult:
    def test_single_result_is_compact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rng = MagicMock()
        rng.choice.return_value = "🎲"
        monkeypatch.setattr(choice, "_RNG", rng)

        assert choice.format_choice_result("午饭", ["火锅"], 3) == "🎲 午饭：火锅"

    def test_multiple_results_include_order_and_statistics(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rng = MagicMock()
        rng.choice.return_value = "🎯"
        monkeypatch.setattr(choice, "_RNG", rng)

        result = choice.format_choice_result("午饭", ["火锅", "日料"], 3)

        assert result == "🎯 午饭：\n  1. 火锅\n  2. 日料\n\n已从 3 个选项中选择 2 个"


class TestChoiceCommand:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", ["", "help", "帮助"])
    async def test_help_is_local(self, args: str, context: SimpleNamespace) -> None:
        result = await choice.handle("choice", args, {}, context)
        assert "随机选择助手" in str(result)
        context.logger.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_choice_uses_original_weighted_pool(
        self,
        context: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rng = MagicMock()
        rng.choices.return_value = ["火锅"]
        rng.choice.return_value = "🎲"
        monkeypatch.setattr(choice, "_RNG", rng)

        result = await choice.handle("choice", "午饭 火锅 火锅 日料", {}, context)

        assert "🎲 午饭：火锅" in str(result)
        rng.choices.assert_called_once_with(["火锅", "火锅", "日料"], k=1)
        logged = "\n".join(str(call) for call in context.logger.mock_calls)
        assert "午饭" not in logged
        assert "火锅" not in logged

    @pytest.mark.asyncio
    async def test_non_unique_overdraw_keeps_requested_count(
        self,
        context: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rng = MagicMock()
        rng.choices.return_value = ["A", "B", "A", "A", "B"]
        rng.choice.return_value = "🎯"
        monkeypatch.setattr(choice, "_RNG", rng)

        result = await choice.handle("choice", "抽取 A B -n 5", {}, context)

        assert "已从 2 个选项中选择 5 个" in str(result)

    @pytest.mark.asyncio
    async def test_unique_mode_reports_distinct_pool_size(
        self,
        context: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rng = MagicMock()
        rng.sample.return_value = ["A", "B"]
        rng.choice.return_value = "✨"
        monkeypatch.setattr(choice, "_RNG", rng)

        result = await choice.handle("choice", "抽取 A A B C -n 2 -u", {}, context)

        assert "已从 3 个选项中选择 2 个" in str(result)
        rng.sample.assert_called_once_with(["A", "B", "C"], k=2)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("args", "message"),
        [
            ("-u", "问题"),
            ("问题 A", "至少需要"),
            ("问题 A A -n 2 -u", "不同选项数量"),
            ("问题 A B -n nope", "ASCII"),
            ("问题 A B --bad", "不支持"),
        ],
    )
    async def test_user_input_errors_are_stable(
        self,
        args: str,
        message: str,
        context: SimpleNamespace,
    ) -> None:
        result = await choice.handle("choice", args, {}, context)
        assert message in str(result)
        context.logger.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_unexpected_random_failure_uses_public_error(
        self,
        context: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rng = MagicMock()
        rng.choices.side_effect = RuntimeError("internal detail")
        monkeypatch.setattr(choice, "_RNG", rng)

        result = await choice.handle("choice", "问题 A B", {}, context)

        assert "XQ-PLUGIN-UNEXPECTED" in str(result)
        assert "internal detail" not in str(result)
