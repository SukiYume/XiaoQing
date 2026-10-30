"""命令验收契约与当前源码命令目录一致：新增插件不能让完整验收在执行前中止。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import run_command_matrix as matrix

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_interrupted_scenario_runs_remaining_cleanup(monkeypatch, error) -> None:
    steps = tuple(
        matrix.ScenarioStep(
            name, "demo.test", name, (), (), (), (), cleanup=name.startswith("cleanup")
        )
        for name in ("create", "interrupt", "ordinary", "cleanup1", "cleanup2")
    )
    scenario = matrix.BusinessScenario(
        "probe",
        "demo",
        "probe",
        "safe",
        "local",
        "private",
        "admin",
        False,
        (),
        ("demo.test",),
        steps,
    )
    calls   = []
    failure = error("interrupted")

    class Client:
        def send(self, message, **kwargs):
            calls.append(message)
            if message == "interrupt":
                raise failure
            if message == "cleanup1":
                raise RuntimeError("cleanup failed")
            return matrix.EventResponse(200, {}, 0)

    monkeypatch.setattr(
        matrix,
        "_scenario_step_result",
        lambda *args, **kwargs: {"execution_status": "passed_business_semantics"},
    )
    with pytest.raises(error) as caught:
        matrix.execute_business_scenarios(
            Client(),
            [scenario],
            risks          = frozenset({"safe"}),
            dependencies   = frozenset({"local"}),
            plugins        = frozenset(),
            code_prefixes  = (),
            plan_only      = False,
            delay_ms       = 0,
            redactions     = (),
            fixture_values = {},
            run_id         = "probe",
        )
    assert caught.value is failure
    assert calls == ["create", "interrupt", "cleanup1", "cleanup2"]


def test_policy_and_scenario_contracts_cover_the_source_catalog() -> None:
    records = matrix.load_source_catalog()
    policy  = matrix.load_policy(ROOT / "tests" / "command_matrix_policy.json")
    matrix.validate_policy_against_catalog(policy, records)

    contract = matrix.load_scenario_contract(ROOT / "tests" / "command_scenario_contracts.json")
    matrix.build_business_scenarios(
        contract, records, expected_plugins=frozenset(policy["plugins"])
    )


@pytest.mark.parametrize("name", sorted(matrix.SCENARIO_BUILTIN_VALUES))
def test_fixture_cannot_override_runtime_identity(tmp_path, name):
    import json

    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps({name: "replacement"}), encoding="utf-8")
    with pytest.raises(matrix.MatrixError):
        matrix.load_scenario_fixtures(path)
