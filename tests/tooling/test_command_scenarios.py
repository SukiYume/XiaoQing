"""统一命令 matrix 的动态业务场景、覆盖审计与清理回归。"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from scripts.run_command_matrix import (
    BusinessScenario,
    EventResponse,
    MatrixCase,
    MatrixError,
    ScenarioStep,
    build_business_scenarios,
    execute_business_scenarios,
    execute_matrix,
    load_policy,
    load_scenario_contract,
    load_source_catalog,
)
from tests.helpers.paths import REPOSITORY_ROOT

ROOT          = REPOSITORY_ROOT
POLICY_PATH   = ROOT / "tests" / "command_matrix_policy.json"
SCENARIO_PATH = ROOT / "tests" / "command_scenario_contracts.json"


def _reply(text: str, *, duration_ms: float = 1.0) -> EventResponse:
    return EventResponse(
        200,
        {"actions": [{"params": {"message": [{"type": "text", "data": {"text": text}}]}}]},
        duration_ms,
    )


def test_scenario_contract_audits_every_plugin_and_closes_dynamic_coverage() -> None:
    """新增插件、失效命令码或没有回归/场景证据的动态命令必须失败关闭。"""

    policy    = load_policy(POLICY_PATH)
    records   = load_source_catalog()
    contract  = load_scenario_contract(SCENARIO_PATH)
    scenarios = build_business_scenarios(
        contract,
        records,
        expected_plugins=frozenset(policy["plugins"]),
    )

    assert set(contract["plugins"]) == set(policy["plugins"])
    assert len(contract["plugins"]) == 30
    assert len(scenarios) >= 17
    assert sum(len(scenario.steps) for scenario in scenarios) >= 150
    assert len({code for scenario in scenarios for code in scenario.covers}) >= 80

    scenario_codes: dict[str, set[str]] = {}
    for scenario in scenarios:
        scenario_codes.setdefault(scenario.plugin, set()).update(scenario.covers)
        if scenario.risk != "read_only":
            assert any(step.cleanup for step in scenario.steps), scenario.scenario_id

    for plugin, audit in contract["plugins"].items():
        dynamic    = set(audit["dynamic_codes"])
        regression = set(audit["regression_codes"])
        assert dynamic == regression | (scenario_codes.get(plugin, set()) & dynamic)
        for relative in audit["test_files"]:
            test_path = ROOT / relative
            text = test_path.read_text(encoding="utf-8")
            assert "def test_" in text, f"{plugin} 的测试证据没有测试函数: {relative}"


def test_pendo_scenarios_require_same_id_and_all_three_event_shapes() -> None:
    """Pendo 不能退化回相互独立的静态样例或只测一种 Event。"""

    policy    = load_policy(POLICY_PATH)
    scenarios = build_business_scenarios(
        load_scenario_contract(SCENARIO_PATH),
        load_source_catalog(),
        expected_plugins=frozenset(policy["plugins"]),
    )
    pendo = {scenario.scenario_id: scenario for scenario in scenarios if scenario.plugin == "pendo"}

    assert {
        "pendo-todo-same-id",
        "pendo-note-same-id",
        "pendo-diary-same-id",
        "pendo-ledger-same-id",
        "pendo-event-single",
        "pendo-event-recurring",
        "pendo-event-multi-node",
    } <= set(pendo)

    expected_capture_names = {
        "pendo-todo-same-id": {"todo_id"},
        "pendo-note-same-id": {"target_id", "note_id"},
        "pendo-diary-same-id": {"diary_id"},
        "pendo-ledger-same-id": {"ledger_id", "quick_ledger_id"},
        "pendo-event-single": {"single_event_id"},
        "pendo-event-recurring": {"recurring_collection_id", "recurring_child_id"},
        "pendo-event-multi-node": {
            "multi_collection_id",
            "multi_first_id",
            "multi_middle_id",
            "multi_last_id",
        },
    }
    for scenario_id, expected in expected_capture_names.items():
        captured = {name for step in pendo[scenario_id].steps for name, _pattern in step.captures}
        assert captured == expected
        templates = "\n".join(step.message for step in pendo[scenario_id].steps)
        for name in expected:
            assert f"{{{{{name}}}}}" in templates

    recurring_templates = "\n".join(step.message for step in pendo["pendo-event-recurring"].steps)
    multi_templates     = "\n".join(step.message for step in pendo["pendo-event-multi-node"].steps)
    assert "{{recurring_collection_id}}" in recurring_templates
    assert "{{recurring_child_id}}" in recurring_templates
    assert all(
        f"{{{{{name}}}}}" in multi_templates
        for name in ("multi_collection_id", "multi_first_id", "multi_middle_id", "multi_last_id")
    )


def test_qingpet_scenario_reaches_every_strict_argument_rejection_and_cleans_up() -> None:
    """QingPet 错误样例必须在已启用且已有宠物的群中命中真实处理器。"""

    policy    = load_policy(POLICY_PATH)
    scenarios = build_business_scenarios(
        load_scenario_contract(SCENARIO_PATH),
        load_source_catalog(),
        expected_plugins=frozenset(policy["plugins"]),
    )
    scenario = next(item for item in scenarios if item.scenario_id == "qingpet-basic-lifecycle")
    invalid_steps = [step for step in scenario.steps if step.step_id.startswith("invalid_")]

    assert len(invalid_steps) == 23
    assert all(step.expect_all or step.expect_any for step in invalid_steps)
    assert scenario.step_delay_ms >= 3_100
    assert scenario.steps[0].step_id == "enable"
    assert scenario.steps[1].step_id == "adopt"
    assert {step.step_id for step in scenario.steps if step.cleanup} == {
        "disable_trade",
        "delete",
        "disable",
    }


def test_mutating_adnmb_and_color_commands_have_cleanup_scenarios() -> None:
    """外部订阅和自定义颜色不能再被当作无状态只读样例直接执行。"""

    policy    = load_policy(POLICY_PATH)
    scenarios = build_business_scenarios(
        load_scenario_contract(SCENARIO_PATH),
        load_source_catalog(),
        expected_plugins=frozenset(policy["plugins"]),
    )
    by_id = {scenario.scenario_id: scenario for scenario in scenarios}

    adnmb = by_id["adnmb-subscription-lifecycle"]
    assert {"adnmb.adnmb.-a", "adnmb.adnmb.-e"} <= set(adnmb.covers)
    assert any(step.captures for step in adnmb.steps)
    assert any(step.cleanup for step in adnmb.steps)

    color = by_id["color-custom-lifecycle"]
    assert color.actor == "bot_admin"
    assert color.scope == "group"
    assert {"color.color.add", "color.color.delete"} <= set(color.covers)
    assert any(step.cleanup for step in color.steps)


def test_scenario_contract_rejects_uncovered_dynamic_command() -> None:
    contract = deepcopy(load_scenario_contract(SCENARIO_PATH))
    contract["plugins"]["smalltalk"]["regression_codes"] = []
    contract["scenarios"] = [
        item for item in contract["scenarios"] if item["id"] != "smalltalk-qa-lifecycle"
    ]

    with pytest.raises(MatrixError, match="动态覆盖不闭合"):
        build_business_scenarios(
            contract,
            load_source_catalog(),
            expected_plugins=frozenset(load_policy(POLICY_PATH)["plugins"]),
        )


def test_scenario_cover_must_have_a_real_event_step() -> None:
    """不能只把命令码写进 covers 就冒充已经设计了运行态步骤。"""

    contract = deepcopy(load_scenario_contract(SCENARIO_PATH))
    scenario = next(
        item for item in contract["scenarios"] if item["id"] == "smalltalk-qa-lifecycle"
    )
    scenario["steps"] = [
        step for step in scenario["steps"] if step["code"] != "smalltalk.qa_remove"
    ]

    with pytest.raises(MatrixError, match="没有实际 /event 步骤"):
        build_business_scenarios(
            contract,
            load_source_catalog(),
            expected_plugins=frozenset(load_policy(POLICY_PATH)["plugins"]),
        )


def test_scenario_contract_rejects_stale_code_and_missing_plugin_audit() -> None:
    records  = load_source_catalog()
    expected = frozenset(load_policy(POLICY_PATH)["plugins"])

    stale = deepcopy(load_scenario_contract(SCENARIO_PATH))
    stale["plugins"]["smalltalk"]["dynamic_codes"].append("smalltalk.removed")
    with pytest.raises(MatrixError, match="动态命令码已失效"):
        build_business_scenarios(stale, records, expected_plugins=expected)

    missing = deepcopy(load_scenario_contract(SCENARIO_PATH))
    del missing["plugins"]["echo"]
    with pytest.raises(MatrixError, match="插件审计不完整"):
        build_business_scenarios(missing, records, expected_plugins=expected)


class _SequenceSender:
    def __init__(self, replies: list[EventResponse]) -> None:
        self.replies                          = list(replies)
        self.sent: list[tuple[str, str, str]] = []

    def send(self, message: str, *, scope: str, actor: str) -> EventResponse:
        self.sent.append((message, scope, actor))
        if not self.replies:
            raise AssertionError(f"unexpected event: {message}")
        return self.replies.pop(0)


def test_dynamic_scenario_captures_id_fails_semantics_and_still_cleans_up() -> None:
    """捕获值必须进入后续 /event；普通步骤失败后，清理仍要使用该真实 ID。"""

    scenario = BusinessScenario(
        "demo-lifecycle",
        "demo",
        "capture and cleanup",
        "isolated_state",
        "local",
        "private",
        "user",
        False,
        (),
        ("demo.create", "demo.view", "demo.edit", "demo.delete"),
        (
            ScenarioStep(
                "create",
                "demo.create",
                "/demo create marker-{{run_id}}",
                ("created",),
                (),
                ("XQ-PLUGIN-UNEXPECTED",),
                (("item_id", r"ID `([0-9a-f]{8})`"),),
            ),
            ScenarioStep(
                "view",
                "demo.view",
                "/demo view {{item_id}}",
                ("marker-{{run_id}}", "{{item_id}}"),
                (),
                ("not found", "XQ-PLUGIN-UNEXPECTED"),
                (),
            ),
            ScenarioStep(
                "bad-edit",
                "demo.edit",
                "/demo edit {{item_id}} invalid",
                ("must reject",),
                (),
                ("XQ-PLUGIN-UNEXPECTED",),
                (),
            ),
            ScenarioStep(
                "would-be-skipped",
                "demo.view",
                "/demo view {{item_id}}",
                ("never",),
                (),
                ("XQ-PLUGIN-UNEXPECTED",),
                (),
            ),
            ScenarioStep(
                "cleanup",
                "demo.delete",
                "/demo delete {{item_id}}",
                ("deleted",),
                (),
                ("XQ-PLUGIN-UNEXPECTED",),
                (),
                True,
            ),
        ),
    )
    sender = _SequenceSender(
        [
            _reply("created ID `abc12345`"),
            _reply("marker-run42 abc12345"),
            _reply("silently accepted"),
            _reply("deleted abc12345"),
        ]
    )

    results = execute_business_scenarios(
        sender,
        [scenario],
        risks          = frozenset({"isolated_state"}),
        dependencies   = frozenset({"local"}),
        plugins        = frozenset(),
        code_prefixes  = (),
        plan_only      = False,
        delay_ms       = 0,
        redactions     = (),
        fixture_values = {},
        run_id         = "run42",
    )

    assert [row["execution_status"] for row in results] == [
        "passed_business_semantics",
        "passed_business_semantics",
        "failed",
        "not_executed",
        "passed_cleanup_contract",
    ]
    assert results[2]["failure"] == "business_semantic_assertion_failed"
    assert results[3]["skip_reason"] == "prior_scenario_step_failed"
    assert [message for message, _scope, _actor in sender.sent] == [
        "/demo create marker-run42",
        "/demo view abc12345",
        "/demo edit abc12345 invalid",
        "/demo delete abc12345",
    ]


def test_dynamic_scenario_missing_capture_never_sends_unresolved_cleanup() -> None:
    scenario = BusinessScenario(
        "missing-id",
        "demo",
        "missing capture",
        "isolated_state",
        "local",
        "private",
        "user",
        False,
        (),
        ("demo.create", "demo.delete"),
        (
            ScenarioStep(
                "create",
                "demo.create",
                "/demo create",
                ("created",),
                (),
                ("XQ-PLUGIN-UNEXPECTED",),
                (("item_id", r"ID `([0-9a-f]{8})`"),),
            ),
            ScenarioStep(
                "cleanup",
                "demo.delete",
                "/demo delete {{item_id}}",
                ("deleted",),
                (),
                ("XQ-PLUGIN-UNEXPECTED",),
                (),
                True,
            ),
        ),
    )
    sender = _SequenceSender([_reply("created without an id")])

    results = execute_business_scenarios(
        sender,
        [scenario],
        risks          = frozenset({"isolated_state"}),
        dependencies   = frozenset({"local"}),
        plugins        = frozenset(),
        code_prefixes  = (),
        plan_only      = False,
        delay_ms       = 0,
        redactions     = (),
        fixture_values = {},
        run_id         = "run42",
    )

    assert results[0]["failure"] == "business_semantic_assertion_failed"
    assert results[1]["failure"] == "unresolved_scenario_variable"
    assert sender.sent == [("/demo create", "private", "user")]


def test_weak_stateful_catalog_case_is_not_executed_without_business_fixture() -> None:
    case = MatrixCase(
        case_id              = "stateful-static",
        plugin               = "demo",
        code                 = "demo.create",
        kind                 = "normal",
        example_index        = 1,
        message              = "/demo create",
        scope                = "private",
        actor                = "user",
        permission           = "public",
        risk                 = "isolated_state",
        dependency           = "local",
        sensitive            = False,
        policy_reason        = "writes state",
        semantic_expectation = "observable_business_reply",
    )
    sender = _SequenceSender([])

    results = execute_matrix(
        sender,  # type: ignore[arg-type]
        [case],
        risks         = frozenset({"isolated_state"}),
        dependencies  = frozenset({"local"}),
        plugins       = frozenset(),
        code_prefixes = (),
        plan_only     = False,
        delay_ms      = 0,
        redactions    = (),
    )

    assert results[0]["execution_status"] == "not_executed"
    assert results[0]["skip_reason"] == "business_scenario_required"
    assert sender.sent == []


def test_scenarios_only_skips_direct_matrix_without_sending() -> None:
    case = MatrixCase(
        case_id              = "read-only-static",
        plugin               = "demo",
        code                 = "demo.status",
        kind                 = "normal",
        example_index        = 1,
        message              = "/demo status",
        scope                = "private",
        actor                = "user",
        permission           = "public",
        risk                 = "read_only",
        dependency           = "local",
        sensitive            = False,
        policy_reason        = "read only",
        semantic_expectation = "observable_business_reply",
    )
    sender = _SequenceSender([])

    results = execute_matrix(
        sender,  # type: ignore[arg-type]
        [case],
        risks          = frozenset({"read_only"}),
        dependencies   = frozenset({"local"}),
        plugins        = frozenset(),
        code_prefixes  = (),
        plan_only      = False,
        scenarios_only = True,
        delay_ms       = 0,
        redactions     = (),
    )

    assert results[0]["execution_status"] == "not_executed"
    assert results[0]["skip_reason"] == "scenarios_only"
    assert sender.sent == []


def test_scenario_runtime_values_render_synthetic_identity() -> None:
    scenario = BusinessScenario(
        "runtime-values",
        "demo",
        "render runtime values",
        "isolated_state",
        "local",
        "group",
        "group_owner",
        False,
        (),
        ("demo.delete",),
        (
            ScenarioStep(
                "delete",
                "demo.delete",
                "/demo delete @{{test_user_id}} in {{test_group_id}}",
                ("deleted",),
                (),
                ("XQ-PLUGIN-UNEXPECTED",),
                (),
                True,
            ),
        ),
    )
    sender = _SequenceSender([_reply("deleted")])

    results = execute_business_scenarios(
        sender,
        [scenario],
        risks          = frozenset({"isolated_state"}),
        dependencies   = frozenset({"local"}),
        plugins        = frozenset(),
        code_prefixes  = (),
        plan_only      = False,
        delay_ms       = 0,
        redactions     = (),
        fixture_values = {},
        run_id         = "run42",
        runtime_values = {"test_user_id": "991001", "test_group_id": "992002"},
    )

    assert results[0]["execution_status"] == "passed_cleanup_contract"
    assert sender.sent == [("/demo delete @991001 in 992002", "group", "group_owner")]


def test_scenario_fixture_file_must_not_be_committed() -> None:
    """契约只声明 fixture 名；真实主机、路径和凭据不能进入受跟踪 JSON。"""

    contract_text = SCENARIO_PATH.read_text(encoding="utf-8")
    payload: dict[str, Any] = json.loads(contract_text)
    fixture_names           = {
        name for scenario in payload["scenarios"] for name in scenario["required_fixtures"]
    }
    assert fixture_names == {
        "codex_test_cwd",
        "minecraft_server_name",
        "ssh_test_host",
    }
    assert "scenario_fixtures.json" not in contract_text
    assert "/tests/command_scenario_fixtures.local.json" in (ROOT / ".gitignore").read_text(
        encoding="utf-8"
    )


def test_scenario_fixture_values_are_redacted_from_command_and_response() -> None:
    scenario = BusinessScenario(
        "fixture-redaction",
        "demo",
        "redact fixture",
        "privileged",
        "external",
        "private",
        "bot_admin",
        False,
        ("host",),
        ("demo.connect",),
        (
            ScenarioStep(
                "connect",
                "demo.connect",
                "/demo connect {{host}}",
                ("connected",),
                (),
                ("XQ-PLUGIN-UNEXPECTED",),
                (),
                True,
            ),
        ),
    )
    sender = _SequenceSender([_reply("connected secret.example")])

    results = execute_business_scenarios(
        sender,
        [scenario],
        risks          = frozenset({"privileged"}),
        dependencies   = frozenset({"external"}),
        plugins        = frozenset(),
        code_prefixes  = (),
        plan_only      = False,
        delay_ms       = 0,
        redactions     = ("secret.example",),
        fixture_values = {"host": "secret.example"},
        run_id         = "run42",
    )

    assert results[0]["execution_status"] == "passed_cleanup_contract"
    assert "secret.example" not in results[0]["message"]
    assert "secret.example" not in results[0]["response_text"]
