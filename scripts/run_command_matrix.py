"""从运行中 Core 的统一目录生成并执行可审计的 HTTP/WS 命令矩阵。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any, Protocol

import aiohttp
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 允许从仓库根目录直接执行 ``python scripts/run_command_matrix.py``；
# 此时 Python 默认只把 scripts/ 放入 sys.path，无法导入同级 core/plugins。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_POLICY_PATH   = PROJECT_ROOT / "tests" / "command_matrix_policy.json"
DEFAULT_SCENARIO_PATH = PROJECT_ROOT / "tests" / "command_scenario_contracts.json"
DEFAULT_ENDPOINT      = "http://127.0.0.1:12000/event"
DEFAULT_WS_ENDPOINT   = "http://127.0.0.1:12000/ws"
DEFAULT_SECRETS_PATH  = PROJECT_ROOT / "config" / "secrets.json"

RISKS = frozenset({"read_only", "isolated_state", "privileged"})
DEPENDENCIES = frozenset({"local", "external"})
SCOPES = ("private", "group")
ACTORS = frozenset({"user", "group_owner", "bot_admin"})
CASE_KINDS = frozenset({"normal", "invalid", "alias", "permission_denied", "context_denied"})
SCENARIO_BUILTIN_VALUES = frozenset({"run_id", "test_user_id", "test_group_id"})
SCENARIO_ID_OFFSET = 100_000
STATE_MODELS = frozenset(
    {
        "stateless",
        "external_read_only",
        "reversible_config",
        "dynamic_resource",
        "session",
        "async_job",
        "external_session",
    }
)
PUBLIC_ERROR_CODE            = "XQ-PLUGIN-UNEXPECTED"
MAX_PERSISTED_RESPONSE_CHARS = 2_000
WS_ACTION_SETTLE_SECONDS     = 0.15

_AUTHORIZATION     = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_JWT               = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|password|passwd|secret)\s*[:=]\s*[^\s,;]+"
)
_MESSAGE_SEEDS = count(time.time_ns(), 1_000_000)


class MatrixError(RuntimeError):
    """命令矩阵无法安全生成或执行。"""


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """一个命令节点的自动执行边界。"""

    risk: str
    dependency: str
    sensitive: bool
    reason: str
    invalid_expect_any: tuple[str, ...]
    invalid_reject_any: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatrixCase:
    """一条稳定、可序列化的运行态测试用例。"""

    case_id: str
    plugin: str
    code: str
    kind: str
    example_index: int
    message: str
    scope: str
    actor: str
    permission: str
    risk: str
    dependency: str
    sensitive: bool
    policy_reason: str
    semantic_expectation: str
    invalid_expect_any: tuple[str, ...] = ()
    invalid_reject_any: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        """敏感命令只保留稳定标识，不写入报告。"""

        payload = asdict(self)
        if self.sensitive:
            payload["message"] = "<REDACTED_SENSITIVE_COMMAND>"
        return payload


@dataclass(frozen=True, slots=True)
class EventResponse:
    """一次 HTTP 或 WebSocket 入站请求的最小观测面。"""

    status: int | None
    payload: Any
    duration_ms: float
    transport_error: str | None = None


@dataclass(frozen=True, slots=True)
class ScenarioStep:
    """动态业务场景中的一次真实 ``/event`` 请求。"""

    step_id: str
    code: str
    message: str
    expect_all: tuple[str, ...]
    expect_any: tuple[str, ...]
    reject_any: tuple[str, ...]
    captures: tuple[tuple[str, str], ...]
    cleanup: bool = False

    def public_dict(self, *, sensitive: bool) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "code": self.code,
            "message": "<REDACTED_SENSITIVE_COMMAND>" if sensitive else self.message,
            "expect_all": list(self.expect_all),
            "expect_any": list(self.expect_any),
            "reject_any": list(self.reject_any),
            "capture_names": [name for name, _pattern in self.captures],
            "cleanup": self.cleanup,
        }


@dataclass(frozen=True, slots=True)
class BusinessScenario:
    """带动态变量、严格回复断言和清理步骤的插件业务闭环。"""

    scenario_id: str
    plugin: str
    description: str
    risk: str
    dependency: str
    scope: str
    actor: str
    sensitive: bool
    required_fixtures: tuple[str, ...]
    covers: tuple[str, ...]
    steps: tuple[ScenarioStep, ...]
    step_delay_ms: int = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "plugin": self.plugin,
            "description": self.description,
            "risk": self.risk,
            "dependency": self.dependency,
            "scope": self.scope,
            "actor": self.actor,
            "sensitive": self.sensitive,
            "required_fixtures": list(self.required_fixtures),
            "covers": list(self.covers),
            "step_delay_ms": self.step_delay_ms,
            "steps": [step.public_dict(sensitive=self.sensitive) for step in self.steps],
        }


class EventSender(Protocol):
    """目录、通用矩阵和业务场景共用的最小发送接口。"""

    def send(self, message: str, *, scope: str, actor: str) -> EventResponse: ...


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MatrixError(f"{label}含重复 JSON 键: {key}: {path}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise MatrixError(f"无法读取{label}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MatrixError(f"{label}必须是 JSON 对象: {path}")
    return payload


def load_runtime_auth(path: Path) -> tuple[str, int, tuple[int, ...]]:
    """从本地密钥文件读取入站 token；密钥永不进入输出。"""

    payload = _read_json_object(path, label="运行密钥")
    token  = payload.get("inbound_token")
    admins = payload.get("admin_user_ids")
    if not isinstance(token, str) or not token:
        raise MatrixError("config/secrets.json 缺少 inbound_token")
    if not isinstance(admins, list):
        raise MatrixError("config/secrets.json 缺少 admin_user_ids")
    valid_admins = tuple(value for value in admins if type(value) is int and value > 0)
    if not valid_admins:
        raise MatrixError("至少需要一个正整数管理员 ID")
    return token, valid_admins[0], valid_admins


def validate_test_id_isolation(
    *,
    user_id: int,
    group_id: int,
    scenario_user_id: int,
    scenario_group_id: int,
    admin_ids: tuple[int, ...],
) -> None:
    """确保普通主体不会继承管理员权限，业务场景也不会复用矩阵会话。"""

    if user_id in admin_ids or scenario_user_id in admin_ids:
        raise MatrixError("普通矩阵用户和业务场景用户不能使用 Bot 管理员 ID")
    if user_id == scenario_user_id or group_id == scenario_group_id:
        raise MatrixError("动态业务场景必须使用独立的用户 ID 和群 ID")


def load_policy(path: Path) -> dict[str, Any]:
    """读取并校验人工维护的风险/依赖分类，未知项一律失败关闭。"""

    payload = _read_json_object(path, label="命令矩阵策略")
    if payload.get("schema_version") != 1:
        raise MatrixError("命令矩阵策略 schema_version 必须为 1")
    default  = payload.get("default")
    plugins  = payload.get("plugins")
    commands = payload.get("commands")
    if (
        not isinstance(default, dict)
        or not isinstance(plugins, dict)
        or not isinstance(commands, dict)
    ):
        raise MatrixError("命令矩阵策略缺少 default/plugins/commands 对象")
    _validate_policy_record(default, label="default")
    for plugin, record in plugins.items():
        if not isinstance(plugin, str) or not plugin or not isinstance(record, dict):
            raise MatrixError("plugins 策略必须是非空插件名到对象的映射")
        _validate_policy_record(record, label=f"plugin {plugin}", partial=True)
    for code, record in commands.items():
        if not isinstance(code, str) or not code or not isinstance(record, dict):
            raise MatrixError("commands 策略必须是非空命令码前缀到对象的映射")
        _validate_policy_record(record, label=f"command {code}", partial=True)
    return payload


def _validate_policy_record(
    record: dict[str, Any],
    *,
    label: str,
    partial: bool = False,
) -> None:
    allowed = {
        "risk",
        "dependency",
        "sensitive",
        "reason",
        "invalid_expect_any",
        "invalid_reject_any",
    }
    unknown = set(record) - allowed
    if unknown:
        raise MatrixError(f"{label} 含未知策略字段: {sorted(unknown)}")
    if (not partial or "risk" in record) and record.get("risk") not in RISKS:
        raise MatrixError(f"{label}.risk 必须是 {sorted(RISKS)} 之一")
    if (not partial or "dependency" in record) and record.get("dependency") not in DEPENDENCIES:
        raise MatrixError(f"{label}.dependency 必须是 {sorted(DEPENDENCIES)} 之一")
    if (not partial or "sensitive" in record) and type(record.get("sensitive")) is not bool:
        raise MatrixError(f"{label}.sensitive 必须是布尔值")
    if "reason" in record and (
        not isinstance(record["reason"], str) or not record["reason"].strip()
    ):
        raise MatrixError(f"{label}.reason 必须是非空字符串")
    for field in ("invalid_expect_any", "invalid_reject_any"):
        if field in record:
            _string_tuple(record[field], label=f"{label}.{field}", allow_empty=False)


def policy_for(code: str, plugin: str, policy: dict[str, Any]) -> PolicyDecision:
    """先应用插件默认值，再按最长稳定命令码前缀覆盖。"""

    merged        = dict(policy["default"])
    plugin_record = policy["plugins"].get(plugin)
    if not isinstance(plugin_record, dict):
        raise MatrixError(f"插件 {plugin} 没有显式测试策略")
    merged.update(plugin_record)
    if "reason" not in plugin_record:
        merged["reason"] = f"{plugin} 插件默认分类"

    matching = [
        (prefix, record)
        for prefix, record in policy["commands"].items()
        if code == prefix or code.startswith(f"{prefix}.")
    ]
    for _prefix, record in sorted(matching, key=lambda item: item[0].count(".")):
        merged.update(record)
    _validate_policy_record(merged, label=f"resolved {code}")
    return PolicyDecision(
        risk               = str(merged["risk"]),
        dependency         = str(merged["dependency"]),
        sensitive          = bool(merged["sensitive"]),
        reason             = str(merged.get("reason") or f"{plugin} 插件默认分类"),
        invalid_expect_any = tuple(merged.get("invalid_expect_any", ())),
        invalid_reject_any = tuple(merged.get("invalid_reject_any", ())),
    )


def validate_policy_against_catalog(
    policy: dict[str, Any],
    records: list[dict[str, Any]],
) -> None:
    """阻止新增插件或删除命令后继续沿用陈旧安全分类。"""

    codes           = {str(record.get("code", "")) for record in records}
    plugins         = {str(record.get("plugin", "")) for record in records}
    missing_plugins = sorted(plugins - set(policy["plugins"]))
    if missing_plugins:
        raise MatrixError(f"以下插件缺少测试策略: {missing_plugins}")
    stale_prefixes = sorted(
        prefix
        for prefix in policy["commands"]
        if not any(code == prefix or code.startswith(f"{prefix}.") for code in codes)
    )
    if stale_prefixes:
        raise MatrixError(f"以下命令策略已失效: {stale_prefixes}")
    for record in records:
        policy_for(str(record["code"]), str(record["plugin"]), policy)


_SCENARIO_TEMPLATE_VALUE = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}")


def _string_tuple(value: Any, *, label: str, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise MatrixError(f"{label} 必须是非空字符串数组")
    result = tuple(item.strip() for item in value)
    if not allow_empty and not result:
        raise MatrixError(f"{label} 不能为空")
    if len(result) != len(set(result)):
        raise MatrixError(f"{label} 含重复项")
    return result


def load_scenario_contract(path: Path) -> dict[str, Any]:
    """读取全插件状态模型审计与动态业务场景。"""

    payload = _read_json_object(path, label="动态业务场景契约")
    if payload.get("schema_version") != 1:
        raise MatrixError("动态业务场景契约 schema_version 必须为 1")
    plugins   = payload.get("plugins")
    scenarios = payload.get("scenarios")
    if not isinstance(plugins, dict) or not isinstance(scenarios, list):
        raise MatrixError("动态业务场景契约缺少 plugins 对象或 scenarios 数组")
    return payload


def _validate_scenario_audit(
    payload: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    expected_plugins: frozenset[str] | None,
) -> dict[str, set[str]]:
    """校验每个插件的状态模型、回归证据和动态命令清单。"""

    plugins = payload["plugins"]
    if expected_plugins is not None and set(plugins) != set(expected_plugins):
        missing = sorted(set(expected_plugins) - set(plugins))
        stale   = sorted(set(plugins) - set(expected_plugins))
        raise MatrixError(f"动态场景插件审计不完整: missing={missing}, stale={stale}")

    catalog_by_plugin: dict[str, set[str]] = {}
    for record in records:
        catalog_by_plugin.setdefault(str(record["plugin"]), set()).add(str(record["code"]))

    dynamic_by_plugin: dict[str, set[str]] = {}
    for plugin, raw in plugins.items():
        if not isinstance(plugin, str) or not plugin or not isinstance(raw, dict):
            raise MatrixError("plugins 审计必须是非空插件名到对象的映射")
        allowed = {"state_model", "reason", "test_files", "dynamic_codes", "regression_codes"}
        unknown = set(raw) - allowed
        if unknown:
            raise MatrixError(f"插件 {plugin} 含未知动态审计字段: {sorted(unknown)}")
        state_model = raw.get("state_model")
        if state_model not in STATE_MODELS:
            raise MatrixError(f"插件 {plugin}.state_model 必须是 {sorted(STATE_MODELS)} 之一")
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise MatrixError(f"插件 {plugin}.reason 必须是非空字符串")
        test_files = _string_tuple(
            raw.get("test_files"), label=f"插件 {plugin}.test_files", allow_empty=False
        )
        for relative in test_files:
            path = (PROJECT_ROOT / relative).resolve()
            try:
                path.relative_to(PROJECT_ROOT)
            except ValueError as exc:
                raise MatrixError(f"插件 {plugin} 的测试路径越界: {relative}") from exc
            if not path.is_file() or path.suffix != ".py":
                raise MatrixError(f"插件 {plugin} 的测试证据不存在: {relative}")

        dynamic = set(_string_tuple(raw.get("dynamic_codes"), label=f"插件 {plugin}.dynamic_codes"))
        regression = set(
            _string_tuple(raw.get("regression_codes"), label=f"插件 {plugin}.regression_codes")
        )
        if not regression <= dynamic:
            raise MatrixError(
                f"插件 {plugin} 的 regression_codes 不属于 dynamic_codes: "
                f"{sorted(regression - dynamic)}"
            )
        catalog_codes = catalog_by_plugin.get(plugin, set())
        stale_codes   = sorted(dynamic - catalog_codes)
        if stale_codes:
            raise MatrixError(f"插件 {plugin} 的动态命令码已失效: {stale_codes}")
        if state_model in {"stateless", "external_read_only"} and dynamic:
            raise MatrixError(f"插件 {plugin} 被标为 {state_model}，却声明了动态命令")
        if state_model not in {"stateless", "external_read_only"} and not dynamic:
            raise MatrixError(f"插件 {plugin} 是 {state_model}，但 dynamic_codes 为空")
        dynamic_by_plugin[plugin] = dynamic
    return dynamic_by_plugin


def _scenario_placeholders(value: str) -> set[str]:
    return set(_SCENARIO_TEMPLATE_VALUE.findall(value))


def _validate_scenario_step_coverage(
    scenario_id: str,
    covers: tuple[str, ...],
    steps: list[ScenarioStep],
) -> None:
    """拒绝只有覆盖标签、没有真实事件步骤的命令码。"""

    uncovered_by_steps = set(covers) - {step.code for step in steps}
    if uncovered_by_steps:
        raise MatrixError(
            f"动态场景 {scenario_id}.covers 含没有实际 /event 步骤的命令码: "
            f"{sorted(uncovered_by_steps)}"
        )


def _validate_scenario_cleanup(
    scenario_id: str,
    risk: str,
    steps: list[ScenarioStep],
) -> None:
    """所有写状态场景都必须声明至少一个尽力清理步骤。"""

    if risk != "read_only" and not any(step.cleanup for step in steps):
        raise MatrixError(f"动态场景 {scenario_id} 会写状态但没有 cleanup 步骤")


def _build_scenario_step(
    raw_step: Any,
    *,
    scenario_id: str,
    covers: tuple[str, ...],
    available_values: set[str],
    step_ids: set[str],
) -> ScenarioStep:
    """校验并构造单个动态步骤，同时登记后续步骤可用的捕获变量。"""

    if not isinstance(raw_step, dict):
        raise MatrixError(f"动态场景 {scenario_id} 的步骤必须是对象")
    step_allowed = {
        "id",
        "code",
        "message",
        "expect_all",
        "expect_any",
        "reject_any",
        "captures",
        "cleanup",
    }
    step_unknown = set(raw_step) - step_allowed
    if step_unknown:
        raise MatrixError(f"动态场景 {scenario_id} 的步骤含未知字段: {sorted(step_unknown)}")

    step_id = raw_step.get("id")
    code    = raw_step.get("code")
    message = raw_step.get("message")
    if not isinstance(step_id, str) or not step_id.strip() or step_id in step_ids:
        raise MatrixError(f"动态场景 {scenario_id} 含空或重复 step id: {step_id}")
    step_ids.add(step_id)
    if code not in covers:
        raise MatrixError(f"动态场景 {scenario_id}.{step_id} 的 code 未列入 covers")
    if not isinstance(message, str) or not message.strip():
        raise MatrixError(f"动态场景 {scenario_id}.{step_id}.message 必须是非空字符串")

    expect_all = _string_tuple(
        raw_step.get("expect_all", []),
        label=f"动态场景 {scenario_id}.{step_id}.expect_all",
    )
    expect_any = _string_tuple(
        raw_step.get("expect_any", []),
        label=f"动态场景 {scenario_id}.{step_id}.expect_any",
    )
    reject_any = _string_tuple(
        raw_step.get("reject_any", [PUBLIC_ERROR_CODE]),
        label=f"动态场景 {scenario_id}.{step_id}.reject_any",
    )
    if not expect_all and not expect_any:
        raise MatrixError(f"动态场景 {scenario_id}.{step_id} 至少需要 expect_all 或 expect_any")

    raw_captures = raw_step.get("captures", {})
    if not isinstance(raw_captures, dict):
        raise MatrixError(f"动态场景 {scenario_id}.{step_id}.captures 必须是对象")
    captures: list[tuple[str, str]] = []
    template_values                 = [message, *expect_all, *expect_any, *reject_any]
    for name, pattern in raw_captures.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name)
            or name in available_values
            or not isinstance(pattern, str)
            or not pattern
        ):
            raise MatrixError(f"动态场景 {scenario_id}.{step_id} 含非法或重复 capture: {name}")
        compile_probe = _SCENARIO_TEMPLATE_VALUE.sub("fixture", pattern)
        try:
            compiled = re.compile(compile_probe)
        except re.error as exc:
            raise MatrixError(
                f"动态场景 {scenario_id}.{step_id} capture 正则非法: {name}: {exc}"
            ) from exc
        if compiled.groups != 1:
            raise MatrixError(f"动态场景 {scenario_id}.{step_id} capture {name} 必须恰有一个捕获组")
        captures.append((name, pattern))
        template_values.append(pattern)

    missing_values = set().union(*(_scenario_placeholders(v) for v in template_values))
    missing_values -= available_values
    if missing_values:
        raise MatrixError(
            f"动态场景 {scenario_id}.{step_id} 使用尚未定义的变量: {sorted(missing_values)}"
        )
    available_values.update(name for name, _pattern in captures)

    cleanup = raw_step.get("cleanup", False)
    if type(cleanup) is not bool:
        raise MatrixError(f"动态场景 {scenario_id}.{step_id}.cleanup 必须是布尔值")
    return ScenarioStep(
        step_id,
        str(code),
        message,
        expect_all,
        expect_any,
        reject_any,
        tuple(captures),
        cleanup,
    )


def build_business_scenarios(
    payload: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    expected_plugins: frozenset[str] | None = None,
) -> list[BusinessScenario]:
    """构造并失败关闭地校验动态场景及其命令覆盖。"""

    dynamic_by_plugin = _validate_scenario_audit(
        payload, records, expected_plugins=expected_plugins
    )
    code_to_plugin = {str(record["code"]): str(record["plugin"]) for record in records}
    scenarios: list[BusinessScenario] = []
    identifiers: set[str] = set()
    scenario_codes: dict[str, set[str]] = {plugin: set() for plugin in payload["plugins"]}

    for raw in payload["scenarios"]:
        if not isinstance(raw, dict):
            raise MatrixError("scenarios 每一项都必须是对象")
        allowed = {
            "id",
            "plugin",
            "description",
            "risk",
            "dependency",
            "scope",
            "actor",
            "sensitive",
            "required_fixtures",
            "covers",
            "step_delay_ms",
            "steps",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise MatrixError(f"动态场景含未知字段: {sorted(unknown)}")
        scenario_id = raw.get("id")
        plugin      = raw.get("plugin")
        description = raw.get("description")
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            raise MatrixError("动态场景 id 必须是非空字符串")
        if scenario_id in identifiers:
            raise MatrixError(f"动态场景 id 重复: {scenario_id}")
        identifiers.add(scenario_id)
        if plugin not in payload["plugins"]:
            raise MatrixError(f"动态场景 {scenario_id} 使用未知插件: {plugin}")
        if not isinstance(description, str) or not description.strip():
            raise MatrixError(f"动态场景 {scenario_id}.description 必须是非空字符串")
        if raw.get("risk") not in RISKS or raw.get("dependency") not in DEPENDENCIES:
            raise MatrixError(f"动态场景 {scenario_id} 的 risk/dependency 非法")
        if raw.get("scope") not in SCOPES or raw.get("actor") not in ACTORS:
            raise MatrixError(f"动态场景 {scenario_id} 的 scope/actor 非法")
        if type(raw.get("sensitive")) is not bool:
            raise MatrixError(f"动态场景 {scenario_id}.sensitive 必须是布尔值")
        step_delay_ms = raw.get("step_delay_ms", 0)
        if type(step_delay_ms) is not int or not 0 <= step_delay_ms <= 60_000:
            raise MatrixError(f"动态场景 {scenario_id}.step_delay_ms 必须是 0-60000 的整数")

        fixtures = _string_tuple(
            raw.get("required_fixtures", []),
            label=f"动态场景 {scenario_id}.required_fixtures",
        )
        covers = _string_tuple(
            raw.get("covers"), label=f"动态场景 {scenario_id}.covers", allow_empty=False
        )
        for code in covers:
            if code_to_plugin.get(code) != plugin:
                raise MatrixError(f"动态场景 {scenario_id} 覆盖未知或跨插件命令码: {code}")
        scenario_codes[str(plugin)].update(covers)

        raw_steps = raw.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise MatrixError(f"动态场景 {scenario_id}.steps 不能为空")
        reserved_fixtures = set(fixtures) & SCENARIO_BUILTIN_VALUES
        if reserved_fixtures:
            raise MatrixError(
                f"动态场景 {scenario_id}.required_fixtures 使用保留变量: "
                f"{sorted(reserved_fixtures)}"
            )
        available_values   = {*SCENARIO_BUILTIN_VALUES, *fixtures}
        step_ids: set[str] = set()
        steps              = [
            _build_scenario_step(
                raw_step,
                scenario_id      = scenario_id,
                covers           = covers,
                available_values = available_values,
                step_ids         = step_ids,
            )
            for raw_step in raw_steps
        ]

        _validate_scenario_step_coverage(scenario_id, covers, steps)
        _validate_scenario_cleanup(scenario_id, str(raw["risk"]), steps)
        scenarios.append(
            BusinessScenario(
                scenario_id       = scenario_id,
                plugin            = str(plugin),
                description       = description.strip(),
                risk              = str(raw["risk"]),
                dependency        = str(raw["dependency"]),
                scope             = str(raw["scope"]),
                actor             = str(raw["actor"]),
                sensitive         = bool(raw["sensitive"]),
                required_fixtures = fixtures,
                covers            = covers,
                steps             = tuple(steps),
                step_delay_ms     = step_delay_ms,
            )
        )

    for plugin, raw in payload["plugins"].items():
        dynamic    = dynamic_by_plugin[plugin]
        regression = set(raw["regression_codes"])
        # 场景可以携带只读的前置查询（例如先从时间线捕获真实 ID），但闭合审计
        # 只统计该插件声明为动态状态的命令码，避免把合法 setup 步骤误报成 extra。
        covered = (scenario_codes.get(plugin, set()) & dynamic) | regression
        if covered != dynamic:
            raise MatrixError(
                f"插件 {plugin} 的动态覆盖不闭合: "
                f"missing={sorted(dynamic - covered)}, extra={sorted(covered - dynamic)}"
            )
    return scenarios


def response_text(payload: Any) -> str:
    """提取 OneBot action 中的文本；媒体只记录类型，不落本地内容。"""

    if not isinstance(payload, dict):
        return ""
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return ""
    chunks: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        params = action.get("params")
        if not isinstance(params, dict):
            continue
        message = params.get("message")
        if isinstance(message, str):
            chunks.append(message)
            continue
        if not isinstance(message, list):
            continue
        for segment in message:
            if not isinstance(segment, dict):
                continue
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            text = data.get("text")
            if isinstance(text, str):
                chunks.append(text)
            elif segment.get("type") in {"file", "image", "record", "video"}:
                chunks.append(f"<{segment.get('type')}>")
    return " ".join(chunks)


def _sanitize_text(value: str, redactions: tuple[str, ...]) -> str:
    for secret in sorted((item for item in redactions if item), key=len, reverse=True):
        value = value.replace(secret, "<REDACTED>")
    value = _AUTHORIZATION.sub(r"\1<REDACTED>", value)
    value = _JWT.sub("<REDACTED_JWT>", value)
    value = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<REDACTED>", value)
    if len(value) > MAX_PERSISTED_RESPONSE_CHARS:
        return f"{value[:MAX_PERSISTED_RESPONSE_CHARS]}<truncated>"
    return value


class EventClient:
    """串行发送带唯一消息 ID 的真实 OneBot 事件。"""

    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        admin_id: int,
        user_id: int,
        group_id: int,
        timeout: float,
    ) -> None:
        self.endpoint  = endpoint
        self.token     = token
        self.admin_id  = admin_id
        self.user_id   = user_id
        self.group_id  = group_id
        self.timeout   = timeout
        self._sequence = 0
        # 多个 client 会分别抓目录和跑动态场景；毫秒时间种子会让它们从同一个
        # message_id 起步，触发 Core 去重并表现为无回复。每个实例预留独立号段。
        self._message_seed = next(_MESSAGE_SEEDS)

    def _build_event(self, message: str, *, scope: str, actor: str) -> dict[str, Any]:
        """为 HTTP/WS 传输构造完全相同的 OneBot 事件。"""

        self._sequence += 1
        if scope not in SCOPES:
            raise MatrixError(f"未知消息场景: {scope}")
        user_id               = self.admin_id if actor == "bot_admin" else self.user_id
        role                  = "owner" if actor in {"bot_admin", "group_owner"} else "member"
        event: dict[str, Any] = {
            "time": int(time.time()),
            "post_type": "message",
            "message_type": scope,
            "sub_type": "normal" if scope == "group" else "friend",
            "message_id": self._message_seed + self._sequence,
            "user_id": user_id,
            "message": message,
            "raw_message": message,
            "font": 0,
            "sender": {
                "user_id": user_id,
                "nickname": "XiaoQingMatrix",
                "card": "XiaoQingMatrix",
                "role": role,
            },
        }
        if scope == "group":
            event["group_id"] = self.group_id
        return event

    def send(self, message: str, *, scope: str, actor: str) -> EventResponse:
        event = self._build_event(message, scope=scope, actor=actor)
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
                "X-XiaoQing-Response-Mode": "actions",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                raw    = response.read()
            try:
                payload: Any = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {"raw": raw.decode("utf-8", errors="replace")}
            return EventResponse(status, payload, (time.perf_counter() - started) * 1_000)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {"raw": raw.decode("utf-8", errors="replace")}
            return EventResponse(exc.code, payload, (time.perf_counter() - started) * 1_000)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return EventResponse(
                None,
                None,
                (time.perf_counter() - started) * 1_000,
                f"{type(exc).__name__}: {exc}",
            )


class WebSocketEventClient(EventClient):
    """通过真实入站 WebSocket 串行发送事件，并收集该连接上的回复 action。"""

    async def _send_event(self, event: dict[str, Any]) -> EventResponse:
        started = time.perf_counter()
        timeout = aiohttp.ClientTimeout(
            total        = None,
            sock_connect = self.timeout,
            sock_read    = self.timeout,
        )
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.ws_connect(
                    self.endpoint,
                    headers = {"Authorization": f"Bearer {self.token}"},
                    timeout = self.timeout,
                ) as websocket,
            ):
                await websocket.send_json(event)
                actions: list[dict[str, Any]] = []
                while True:
                    wait_seconds = self.timeout if not actions else WS_ACTION_SETTLE_SECONDS
                    try:
                        message = await websocket.receive(timeout=wait_seconds)
                    except TimeoutError:
                        if actions:
                            break
                        raise
                    if message.type is aiohttp.WSMsgType.TEXT:
                        try:
                            payload = json.loads(message.data)
                        except json.JSONDecodeError:
                            return EventResponse(
                                200,
                                {"actions": actions, "ws_error": "invalid JSON response"},
                                (time.perf_counter() - started) * 1_000,
                            )
                        if isinstance(payload, dict) and "error" in payload:
                            return EventResponse(
                                200,
                                {"actions": actions, "ws_error": payload["error"]},
                                (time.perf_counter() - started) * 1_000,
                            )
                        if isinstance(payload, dict):
                            actions.append(payload)
                        continue
                    if message.type in {
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    }:
                        break
                return EventResponse(
                    200,
                    {"actions": actions},
                    (time.perf_counter() - started) * 1_000,
                )
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
            return EventResponse(
                None,
                None,
                (time.perf_counter() - started) * 1_000,
                f"{type(exc).__name__}: {exc}",
            )

    def send(self, message: str, *, scope: str, actor: str) -> EventResponse:
        event = self._build_event(message, scope=scope, actor=actor)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._send_event(event))
        raise MatrixError("WebSocket matrix 的同步入口不能在正在运行的 asyncio loop 内调用")


def _parse_catalog_page(response: EventResponse) -> dict[str, Any]:
    if response.transport_error or response.status != 200:
        raise MatrixError(
            f"读取运行态命令目录失败: status={response.status}, error={response.transport_error}"
        )
    text = response_text(response.payload).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MatrixError("/help json 未返回可解析的目录 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("commands"), list):
        raise MatrixError("/help json 返回结构缺少 commands 数组")
    return payload


def fetch_runtime_catalog(client: EventSender) -> list[dict[str, Any]]:
    """通过运行中 Bot 自己的 ``/help json`` 分页读取目录，避免读到未加载源码。"""

    first = _parse_catalog_page(
        client.send("/help json page 1", scope="private", actor="bot_admin")
    )
    total_pages = first.get("total_pages")
    if type(total_pages) is not int or total_pages < 1:
        raise MatrixError("运行态目录 total_pages 非法")
    records = list(first["commands"])
    for page in range(2, total_pages + 1):
        payload = _parse_catalog_page(
            client.send(f"/help json page {page}", scope="private", actor="bot_admin")
        )
        if payload.get("page") != page or payload.get("total_pages") != total_pages:
            raise MatrixError(f"运行态目录第 {page} 页的分页元数据不一致")
        records.extend(payload["commands"])

    codes = [record.get("code") for record in records if isinstance(record, dict)]
    if len(codes) != len(records) or any(not isinstance(code, str) or not code for code in codes):
        raise MatrixError("运行态目录含无效命令记录")
    if len(codes) != len(set(codes)):
        raise MatrixError("运行态目录含重复稳定命令码")
    return records


def load_source_catalog() -> list[dict[str, Any]]:
    """从当前工作树构建同格式快照，用于发现进程未重启或插件漏载。"""

    from core.models import PluginManifest
    from core.router import build_command_catalog_node

    records: list[dict[str, Any]] = []
    for path in sorted((PROJECT_ROOT / "plugins").glob("*/plugin.json")):
        payload = _read_json_object(path, label="插件 manifest")
        try:
            manifest = PluginManifest.model_validate(payload)
        except ValidationError as exc:
            raise MatrixError(f"插件 manifest 校验失败: {path}: {exc}") from exc
        for command in manifest.commands:
            root = build_command_catalog_node(
                manifest.name,
                command.model_dump(),
                root=True,
            )
            for node in root.walk():
                record                = node.to_dict()
                record["subcommands"] = [child.code for child in node.children]
                records.append(record)
    return records


def catalog_hash(records: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        sorted(records, key=lambda record: str(record.get("code", ""))),
        ensure_ascii = False,
        sort_keys    = True,
        separators   = (",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _actor_for(permission: str, scope: str) -> str:
    if permission == "bot_admin":
        return "bot_admin"
    if permission == "group_admin":
        return "group_owner" if scope == "group" else "bot_admin"
    return "user"


def _case_id(code: str, kind: str, scope: str, index: int, message: str) -> str:
    raw = f"{code}\0{kind}\0{scope}\0{index}\0{message}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _alias_message(record: dict[str, Any], alias: str) -> str:
    path     = record.get("path")
    examples = record.get("examples")
    if not isinstance(path, list) or not path or not all(isinstance(item, str) for item in path):
        raise MatrixError(f"{record.get('code')} 缺少合法 path")
    arguments: list[str] = []
    if isinstance(examples, list) and examples and isinstance(examples[0], str):
        tokens = examples[0].strip().lstrip("/").split()
        if len(tokens) >= len(path):
            arguments = tokens[len(path) :]
    return "/" + " ".join([*path[:-1], alias, *arguments])


def build_matrix(
    records: list[dict[str, Any]],
    policy: dict[str, Any],
) -> list[MatrixCase]:
    """生成样例、别名、权限拒绝和场景拒绝四类互补用例。"""

    validate_policy_against_catalog(policy, records)
    cases: list[MatrixCase] = []
    for record in records:
        code       = str(record["code"])
        plugin     = str(record["plugin"])
        permission = str(record.get("permission") or "public")
        contexts   = record.get("contexts")
        if (
            not isinstance(contexts, list)
            or not contexts
            or any(scope not in SCOPES for scope in contexts)
        ):
            raise MatrixError(f"{code} 的 contexts 非法")
        decision = policy_for(code, plugin, policy)

        for kind, field, expectation in (
            ("normal", "examples", "observable_business_reply"),
            ("invalid", "invalid_examples", "rejected_without_unhandled_exception"),
        ):
            examples = record.get(field)
            if not isinstance(examples, list) or not examples:
                raise MatrixError(f"{code} 缺少 {field}")
            for index, example in enumerate(examples, 1):
                if not isinstance(example, str) or not example.strip():
                    raise MatrixError(f"{code} 含空 {field}")
                cases.extend(
                    MatrixCase(
                        case_id              = _case_id(code, kind, scope, index, example),
                        plugin               = plugin,
                        code                 = code,
                        kind                 = kind,
                        example_index        = index,
                        message              = example,
                        scope                = scope,
                        actor                = _actor_for(permission, scope),
                        permission           = permission,
                        risk                 = decision.risk,
                        dependency           = decision.dependency,
                        sensitive            = decision.sensitive,
                        policy_reason        = decision.reason,
                        semantic_expectation = expectation,
                        invalid_expect_any   = decision.invalid_expect_any,
                        invalid_reject_any   = decision.invalid_reject_any,
                    )
                    for scope in contexts
                )

        aliases = record.get("aliases")
        if not isinstance(aliases, list):
            raise MatrixError(f"{code} 的 aliases 非法")
        for index, alias in enumerate(aliases, 1):
            if not isinstance(alias, str) or not alias:
                raise MatrixError(f"{code} 含空别名")
            message = _alias_message(record, alias)
            cases.extend(
                MatrixCase(
                    case_id              = _case_id(code, "alias", scope, index, message),
                    plugin               = plugin,
                    code                 = code,
                    kind                 = "alias",
                    example_index        = index,
                    message              = message,
                    scope                = scope,
                    actor                = _actor_for(permission, scope),
                    permission           = permission,
                    risk                 = decision.risk,
                    dependency           = decision.dependency,
                    sensitive            = decision.sensitive,
                    policy_reason        = decision.reason,
                    semantic_expectation = "alias_reaches_same_command_path",
                    invalid_expect_any   = decision.invalid_expect_any,
                    invalid_reject_any   = decision.invalid_reject_any,
                )
                for scope in contexts
            )

        first_example = str(record["examples"][0])
        if permission != "public":
            for index, scope in enumerate(contexts, 1):
                cases.append(
                    MatrixCase(
                        case_id=_case_id(
                            code,
                            "permission_denied",
                            scope,
                            index,
                            first_example,
                        ),
                        plugin               = plugin,
                        code                 = code,
                        kind                 = "permission_denied",
                        example_index        = index,
                        message              = first_example,
                        scope                = scope,
                        actor                = "user",
                        permission           = permission,
                        risk                 = "read_only",
                        dependency           = "local",
                        sensitive            = False,
                        policy_reason        = "Core 必须在插件处理器之前拒绝未授权主体",
                        semantic_expectation = "permission_denied_by_core",
                    )
                )
        denied_contexts = [scope for scope in SCOPES if scope not in contexts]
        for index, scope in enumerate(denied_contexts, 1):
            cases.append(
                MatrixCase(
                    case_id=_case_id(code, "context_denied", scope, index, first_example),
                    plugin=plugin,
                    code=code,
                    kind="context_denied",
                    example_index=index,
                    message=first_example,
                    scope=scope,
                    actor="bot_admin",
                    permission=permission,
                    risk="read_only",
                    dependency="local",
                    sensitive=False,
                    policy_reason="Core 必须在插件处理器之前拒绝错误会话场景",
                    semantic_expectation="context_denied_by_core",
                )
            )

    identifiers = [case.case_id for case in cases]
    if len(identifiers) != len(set(identifiers)):
        raise MatrixError("生成的矩阵含重复 case_id")
    return cases


def evaluate_response(
    case: MatrixCase,
    response: EventResponse,
    *,
    redactions: tuple[str, ...],
) -> dict[str, Any]:
    text = response_text(response.payload)
    actions = response.payload.get("actions") if isinstance(response.payload, dict) else None
    failure: str | None = None
    semantic_strength = "observable_reply"
    if response.transport_error:
        failure = "transport_error"
    elif response.status != 200:
        failure = "unexpected_http_status"
    elif isinstance(response.payload, dict) and response.payload.get("ws_error"):
        failure = "websocket_error"
    elif not isinstance(actions, list):
        failure = "invalid_onebot_response"
    elif not actions:
        failure = "missing_reply_action"
    elif PUBLIC_ERROR_CODE in text:
        failure = "unhandled_plugin_exception"
    elif case.kind == "invalid" and (case.invalid_expect_any or case.invalid_reject_any):
        semantic_strength = "strict_invalid_assertion"
        if case.invalid_expect_any and not any(
            marker in text for marker in case.invalid_expect_any
        ):
            failure = "invalid_rejection_not_observed"
        elif any(marker in text for marker in case.invalid_reject_any):
            failure = "invalid_forbidden_reply_observed"
    elif case.kind == "permission_denied":
        semantic_strength = "strict_core_assertion"
        expected          = "管理员" if case.permission == "group_admin" else "权限不足"
        if expected not in text:
            failure = "permission_denial_not_observed"
    elif case.kind == "context_denied":
        semantic_strength = "strict_core_assertion"
        if "当前会话类型不支持此命令" not in text:
            failure = "context_denial_not_observed"

    persisted_text = (
        "<REDACTED_SENSITIVE_RESPONSE>" if case.sensitive else _sanitize_text(text, redactions)
    )
    return {
        **case.public_dict(),
        "execution_status": "failed" if failure else "passed_runtime_contract",
        "failure": failure,
        "http_status": response.status,
        "duration_ms": round(response.duration_ms, 2),
        "action_count": len(actions) if isinstance(actions, list) else 0,
        "semantic_strength": semantic_strength,
        "response_text": persisted_text,
        "transport_error": _sanitize_text(response.transport_error or "", redactions) or None,
    }


def execute_matrix(
    client: EventSender,
    cases: list[MatrixCase],
    *,
    risks: frozenset[str],
    dependencies: frozenset[str],
    plugins: frozenset[str],
    code_prefixes: tuple[str, ...],
    kinds: frozenset[str] = CASE_KINDS,
    plan_only: bool,
    scenarios_only: bool = False,
    delay_ms: int,
    redactions: tuple[str, ...],
) -> list[dict[str, Any]]:
    """串行执行选中用例；未执行项也写入结果，避免把跳过误算成通过。"""

    results: list[dict[str, Any]] = []
    for position, case in enumerate(cases, 1):
        skip_reason: str | None = None
        if plan_only:
            skip_reason = "plan_only"
        elif scenarios_only:
            skip_reason = "scenarios_only"
        elif case.kind not in kinds:
            skip_reason = "kind_filter"
        elif plugins and case.plugin not in plugins:
            skip_reason = "plugin_filter"
        elif code_prefixes and not any(
            case.code == prefix or case.code.startswith(f"{prefix}.") for prefix in code_prefixes
        ):
            skip_reason = "code_filter"
        elif case.risk not in risks:
            skip_reason = "risk_policy"
        elif case.dependency not in dependencies:
            skip_reason = "dependency_policy"
        elif case.risk != "read_only" and case.kind in {"normal", "invalid", "alias"}:
            # 单条样例没有前置 fixture、动态 ID 串联和清理能力。允许它直接写状态
            # 会制造残留，也会把“对象不存在”误计成通过，因此统一交给业务场景层。
            skip_reason = "business_scenario_required"

        if skip_reason:
            results.append(
                {
                    **case.public_dict(),
                    "execution_status": "not_executed",
                    "skip_reason": skip_reason,
                    "semantic_strength": "none",
                }
            )
            continue

        response = client.send(case.message, scope=case.scope, actor=case.actor)
        result = evaluate_response(case, response, redactions=redactions)
        results.append(result)
        print(
            f"[{position:04d}/{len(cases):04d}] {case.code} {case.kind}/{case.scope} "
            f"{result['execution_status']} {result.get('duration_ms', 0):.2f}ms",
            flush=True,
        )
        if delay_ms:
            time.sleep(delay_ms / 1_000)
    return results


def _render_scenario_template(template: str, values: dict[str, str]) -> str:
    """只替换显式 ``{{name}}``，缺失变量立即失败。"""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise MatrixError(f"动态场景变量尚未捕获: {name}")
        return values[name]

    return _SCENARIO_TEMPLATE_VALUE.sub(replace, template)


def _scenario_step_result(
    scenario: BusinessScenario,
    step: ScenarioStep,
    response: EventResponse,
    *,
    rendered_message: str,
    values: dict[str, str],
    redactions: tuple[str, ...],
) -> dict[str, Any]:
    """执行 include/exclude/capture 严格断言，并只在全部通过后发布变量。"""

    text = response_text(response.payload)
    actions = response.payload.get("actions") if isinstance(response.payload, dict) else None
    failure: str | None = None
    details: list[str] = []
    captured: dict[str, str] = {}
    if response.transport_error:
        failure = "transport_error"
    elif response.status != 200:
        failure = "unexpected_http_status"
    elif not isinstance(actions, list):
        failure = "invalid_onebot_response"
    elif not actions:
        failure = "missing_reply_action"
    else:
        for expected in step.expect_all:
            rendered = _render_scenario_template(expected, values)
            if rendered not in text:
                details.append(f"missing:{expected}")
        if step.expect_any and not any(
            _render_scenario_template(expected, values) in text for expected in step.expect_any
        ):
            details.append(f"missing_any:{list(step.expect_any)}")
        for forbidden in step.reject_any:
            rendered = _render_scenario_template(forbidden, values)
            if rendered and rendered in text:
                details.append(f"forbidden:{forbidden}")
        for name, raw_pattern in step.captures:
            pattern = _render_scenario_template(raw_pattern, values)
            match   = re.search(pattern, text)
            if match is None:
                details.append(f"capture_missing:{name}")
                continue
            value = match.group(1).strip()
            if not value:
                details.append(f"capture_empty:{name}")
            else:
                captured[name] = value
        if details:
            failure = "business_semantic_assertion_failed"

    if failure is None:
        values.update(captured)
    persisted_text = (
        "<REDACTED_SENSITIVE_RESPONSE>" if scenario.sensitive else _sanitize_text(text, redactions)
    )
    persisted_message = (
        "<REDACTED_SENSITIVE_COMMAND>"
        if scenario.sensitive
        else _sanitize_text(rendered_message, redactions)
    )
    return {
        "scenario_id": scenario.scenario_id,
        "plugin": scenario.plugin,
        "description": scenario.description,
        "step_id": step.step_id,
        "code": step.code,
        "kind": "business_scenario",
        "scope": scenario.scope,
        "actor": scenario.actor,
        "risk": scenario.risk,
        "dependency": scenario.dependency,
        "sensitive": scenario.sensitive,
        "cleanup": step.cleanup,
        "message": persisted_message,
        "execution_status": (
            "failed"
            if failure
            else "passed_cleanup_contract"
            if step.cleanup
            else "passed_business_semantics"
        ),
        "failure": failure,
        "failure_details": details,
        "http_status": response.status,
        "duration_ms": round(response.duration_ms, 2),
        "action_count": len(actions) if isinstance(actions, list) else 0,
        "semantic_strength": "strict_business_reply",
        "captured_names": sorted(captured),
        "response_text": persisted_text,
        "transport_error": _sanitize_text(response.transport_error or "", redactions) or None,
    }


def execute_business_scenarios(
    client: EventSender,
    scenarios: list[BusinessScenario],
    *,
    risks: frozenset[str],
    dependencies: frozenset[str],
    plugins: frozenset[str],
    code_prefixes: tuple[str, ...],
    plan_only: bool,
    delay_ms: int,
    redactions: tuple[str, ...],
    fixture_values: dict[str, str],
    run_id: str,
    runtime_values: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """串行执行动态场景；业务失败后跳过普通步骤，但仍尽力执行清理。"""

    results: list[dict[str, Any]] = []
    total_steps                   = sum(len(scenario.steps) for scenario in scenarios)
    position                      = 0
    for scenario in scenarios:
        skip_reason: str | None = None
        missing_fixtures        = sorted(set(scenario.required_fixtures) - set(fixture_values))
        if plan_only:
            skip_reason = "plan_only"
        elif plugins and scenario.plugin not in plugins:
            skip_reason = "plugin_filter"
        elif code_prefixes and not any(
            code == prefix or code.startswith(f"{prefix}.")
            for code in scenario.covers
            for prefix in code_prefixes
        ):
            skip_reason = "code_filter"
        elif scenario.risk not in risks:
            skip_reason = "risk_policy"
        elif scenario.dependency not in dependencies:
            skip_reason = "dependency_policy"
        elif missing_fixtures:
            skip_reason = "missing_fixture:" + ",".join(missing_fixtures)

        values          = {"run_id": run_id, **(runtime_values or {}), **fixture_values}
        scenario_failed = False
        for step in scenario.steps:
            position += 1
            base = {
                "scenario_id": scenario.scenario_id,
                "plugin": scenario.plugin,
                "description": scenario.description,
                "step_id": step.step_id,
                "code": step.code,
                "kind": "business_scenario",
                "scope": scenario.scope,
                "actor": scenario.actor,
                "risk": scenario.risk,
                "dependency": scenario.dependency,
                "sensitive": scenario.sensitive,
                "cleanup": step.cleanup,
                "semantic_strength": "none",
            }
            effective_skip = skip_reason
            if effective_skip is None and scenario_failed and not step.cleanup:
                effective_skip = "prior_scenario_step_failed"
            if effective_skip is not None:
                results.append(
                    {
                        **base,
                        "message": (
                            "<REDACTED_SENSITIVE_COMMAND>" if scenario.sensitive else step.message
                        ),
                        "execution_status": "not_executed",
                        "skip_reason": effective_skip,
                    }
                )
                continue

            try:
                rendered_message = _render_scenario_template(step.message, values)
            except MatrixError as exc:
                row = {
                    **base,
                    "message": (
                        "<REDACTED_SENSITIVE_COMMAND>" if scenario.sensitive else step.message
                    ),
                    "execution_status": "failed",
                    "failure": "unresolved_scenario_variable",
                    "failure_details": [str(exc)],
                }
                results.append(row)
                if not step.cleanup:
                    scenario_failed = True
                continue

            response = client.send(
                rendered_message,
                scope = scenario.scope,
                actor = scenario.actor,
            )
            row = _scenario_step_result(
                scenario,
                step,
                response,
                rendered_message = rendered_message,
                values           = values,
                redactions       = redactions,
            )
            results.append(row)
            if row["execution_status"] == "failed" and not step.cleanup:
                scenario_failed = True
            print(
                f"[scenario {position:04d}/{total_steps:04d}] "
                f"{scenario.scenario_id}.{step.step_id} {row['execution_status']} "
                f"{row.get('duration_ms', 0):.2f}ms",
                flush=True,
            )
            effective_delay_ms = max(delay_ms, scenario.step_delay_ms)
            if effective_delay_ms:
                time.sleep(effective_delay_ms / 1_000)
    return results


def load_scenario_fixtures(path: Path | None) -> dict[str, str]:
    """读取非敏感场景参数；值必须是有界字符串且不会写入计划定义。"""

    if path is None:
        return {}
    payload = _read_json_object(path, label="动态场景 fixture")
    fixtures: dict[str, str] = {}
    for key, value in payload.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key)
            or not isinstance(value, str)
            or not value.strip()
            or len(value) > 500
        ):
            raise MatrixError(f"动态场景 fixture 非法: {key}")
        fixtures[key] = value.strip()
    return fixtures


def _probe_health(endpoint: str, token: str, *, timeout: float) -> dict[str, Any]:
    base = endpoint.rsplit("/event", 1)[0] if endpoint.endswith("/event") else endpoint.rstrip("/")
    url = f"{base}/health"
    started = time.perf_counter()
    request = urllib.request.Request(
        url,
        headers = {"Authorization": f"Bearer {token}"},
        method  = "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        try:
            payload: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"raw": raw.decode("utf-8", errors="replace")}
        return {
            "status": response.status,
            "duration_ms": round((time.perf_counter() - started) * 1_000, 2),
            "payload": payload,
        }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "status": None,
            "duration_ms": round((time.perf_counter() - started) * 1_000, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }


def build_summary(
    records: list[dict[str, Any]],
    cases: list[MatrixCase],
    results: list[dict[str, Any]],
    scenarios: list[BusinessScenario],
    scenario_results: list[dict[str, Any]],
    *,
    started_at: str,
    finished_at: str,
    health_before: dict[str, Any],
    health_after: dict[str, Any],
    source_catalog_sha256: str,
    transport: str,
) -> dict[str, Any]:
    node_codes = {str(record["code"]) for record in records}
    normal_nodes = {case.code for case in cases if case.kind == "normal"}
    invalid_nodes = {case.code for case in cases if case.kind == "invalid"}
    executed = [row for row in results if row["execution_status"] != "not_executed"]
    passed = [row for row in executed if row["execution_status"] == "passed_runtime_contract"]
    failed = [row for row in executed if row["execution_status"] == "failed"]
    strict = [row for row in passed if row.get("semantic_strength") == "strict_core_assertion"]
    strict_invalid = [
        row for row in passed if row.get("semantic_strength") == "strict_invalid_assertion"
    ]
    observable = [row for row in passed if row.get("semantic_strength") == "observable_reply"]
    scenario_executed = [
        row for row in scenario_results if row["execution_status"] != "not_executed"
    ]
    scenario_passed = [
        row
        for row in scenario_executed
        if row["execution_status"] in {"passed_business_semantics", "passed_cleanup_contract"}
    ]
    scenario_failed = [row for row in scenario_executed if row["execution_status"] == "failed"]
    cleanup_steps   = [row for row in scenario_executed if row.get("cleanup")]
    durations       = sorted(
        float(row["duration_ms"])
        for row in executed
        if isinstance(row.get("duration_ms"), (int, float))
    )

    def percentile(fraction: float) -> float | None:
        if not durations:
            return None
        index = min(len(durations) - 1, max(0, round((len(durations) - 1) * fraction)))
        return round(durations[index], 2)

    cleanup_required = any(
        row["execution_status"] != "not_executed" and row["risk"] == "isolated_state"
        for row in [*results, *scenario_results]
    )
    health_gate_passed = all(
        item.get("status") == 200
        and isinstance(item.get("payload"), dict)
        and item["payload"].get("status") == "ok"
        for item in (health_before, health_after)
    )
    runtime_catalog_sha256 = catalog_hash(records)
    return {
        "schema_version": 1,
        "transport": transport,
        "started_at": started_at,
        "finished_at": finished_at,
        "catalog": {
            "sha256": runtime_catalog_sha256,
            "source_sha256": source_catalog_sha256,
            "matches_source": runtime_catalog_sha256 == source_catalog_sha256,
            "plugins_with_commands": len({str(record["plugin"]) for record in records}),
            "nodes": len(records),
            "normal_examples": sum(len(record.get("examples", [])) for record in records),
            "invalid_examples": sum(len(record.get("invalid_examples", [])) for record in records),
        },
        "planned_coverage": {
            "cases": len(cases),
            "nodes_with_normal_cases": len(normal_nodes),
            "nodes_with_invalid_cases": len(invalid_nodes),
            "all_nodes_have_normal_and_invalid": normal_nodes == invalid_nodes == node_codes,
            "by_kind": dict(Counter(case.kind for case in cases)),
            "by_risk": dict(Counter(case.risk for case in cases)),
            "by_dependency": dict(Counter(case.dependency for case in cases)),
        },
        "business_scenarios": {
            "planned_scenarios": len(scenarios),
            "planned_steps": sum(len(scenario.steps) for scenario in scenarios),
            "plugins": len({scenario.plugin for scenario in scenarios}),
            "covered_codes": len({code for scenario in scenarios for code in scenario.covers}),
            "executed_steps": len(scenario_executed),
            "passed_business_or_cleanup": len(scenario_passed),
            "failed_steps": len(scenario_failed),
            "not_executed_steps": len(scenario_results) - len(scenario_executed),
            "skip_reasons": dict(
                Counter(
                    str(row.get("skip_reason"))
                    for row in scenario_results
                    if row["execution_status"] == "not_executed"
                )
            ),
            "failed_cases": [
                {
                    "scenario_id": row["scenario_id"],
                    "step_id": row["step_id"],
                    "plugin": row["plugin"],
                    "code": row["code"],
                    "failure": row.get("failure"),
                }
                for row in scenario_failed
            ],
        },
        "execution": {
            "executed": len(executed),
            "passed_runtime_contract": len(passed),
            "failed_runtime_contract": len(failed),
            "strict_core_assertions_passed": len(strict),
            "strict_invalid_assertions_passed": len(strict_invalid),
            "observable_reply_only": len(observable),
            "not_executed": len(results) - len(executed),
            "skip_reasons": dict(
                Counter(
                    str(row.get("skip_reason"))
                    for row in results
                    if row["execution_status"] == "not_executed"
                )
            ),
            "latency_ms": {
                "p50": percentile(0.50),
                "p95": percentile(0.95),
                "p99": percentile(0.99),
            },
            "executed_nodes_by_kind": {
                kind: len({str(row["code"]) for row in executed if row.get("kind") == kind})
                for kind in sorted({str(row.get("kind")) for row in executed})
            },
            "executed_plugins": len({str(row["plugin"]) for row in executed}),
            "failed_cases": [
                {
                    "case_id": row["case_id"],
                    "plugin": row["plugin"],
                    "code": row["code"],
                    "kind": row["kind"],
                    "scope": row["scope"],
                    "failure": row.get("failure"),
                }
                for row in failed
            ],
        },
        "cleanup": {
            "required": cleanup_required,
            "command_steps": len(cleanup_steps),
            "command_steps_passed": sum(
                row["execution_status"] == "passed_cleanup_contract" for row in cleanup_steps
            ),
            "status": (
                "cleanup_commands_passed_residue_not_audited"
                if cleanup_required
                and cleanup_steps
                and all(
                    row["execution_status"] == "passed_cleanup_contract" for row in cleanup_steps
                )
                else "not_verified"
                if cleanup_required
                else "not_required"
            ),
        },
        "health": {
            "gate_passed": health_gate_passed,
            "before": health_before,
            "after": health_after,
        },
    }


def render_report(summary: dict[str, Any]) -> str:
    catalog   = summary["catalog"]
    planned   = summary["planned_coverage"]
    business  = summary["business_scenarios"]
    execution = summary["execution"]
    cleanup   = summary["cleanup"]
    health    = summary["health"]
    transport = summary.get("transport", "http")
    failures  = execution["failed_runtime_contract"] + business["failed_steps"]
    if failures:
        verdict = f"发现 {failures} 条运行态契约失败"
    elif not health["gate_passed"]:
        verdict = "命令用例无失败，但压前/压后健康门禁未通过"
    else:
        verdict = "运行态契约与健康门禁无失败"
    failure_lines = tuple(
        f"- `{row['case_id']}` {row['code']} {row['kind']}/{row['scope']}: {row['failure']}"
        for row in execution["failed_cases"][:20]
    )
    return "\n".join(
        (
            "# 统一命令目录运行态 Matrix 报告",
            "",
            f"结论：{verdict}。这不是对未执行项或未配置业务断言项的‘全部通过’声明。",
            f"入站传输：`{transport}`。",
            "",
            "## 目录与计划覆盖",
            "",
            f"- 运行态目录哈希：`{catalog['sha256']}`",
            f"- 与当前源码目录一致：{catalog['matches_source']}",
            f"- 命令插件/节点：{catalog['plugins_with_commands']}/{catalog['nodes']}",
            f"- 正常/错误样例：{catalog['normal_examples']}/{catalog['invalid_examples']}",
            (
                f"- 自动生成用例：{planned['cases']}；"
                f"所有节点均有正反例：{planned['all_nodes_have_normal_and_invalid']}"
            ),
            f"- 用例类型：`{json.dumps(planned['by_kind'], ensure_ascii=False, sort_keys=True)}`",
            (
                f"- 动态业务场景/步骤：{business['planned_scenarios']}/"
                f"{business['planned_steps']}；覆盖命令码：{business['covered_codes']}"
            ),
            "",
            "## 实际执行",
            "",
            f"- 执行/未执行：{execution['executed']}/{execution['not_executed']}",
            (
                f"- 运行态契约通过/失败：{execution['passed_runtime_contract']}/"
                f"{execution['failed_runtime_contract']}"
            ),
            f"- Core 严格语义断言：{execution['strict_core_assertions_passed']}",
            f"- 错误命令严格拒绝断言：{execution['strict_invalid_assertions_passed']}",
            f"- 仅证明有可观测业务返回：{execution['observable_reply_only']}",
            (
                "- 严格业务场景步骤通过/失败/未执行："
                f"{business['passed_business_or_cleanup']}/{business['failed_steps']}/"
                f"{business['not_executed_steps']}"
            ),
            (
                f"- 延迟 p50/p95/p99：{execution['latency_ms']['p50']}/"
                f"{execution['latency_ms']['p95']}/{execution['latency_ms']['p99']} ms"
            ),
            (
                "- 跳过原因：`"
                f"{json.dumps(execution['skip_reasons'], ensure_ascii=False, sort_keys=True)}`"
            ),
            f"- 压前/压后健康门禁：{health['gate_passed']}",
            "",
            "## 失败明细",
            "",
            *(failure_lines or ("- 无运行态契约失败。",)),
            *(
                tuple(
                    f"- `{row['scenario_id']}.{row['step_id']}` {row['code']}: {row['failure']}"
                    for row in business["failed_cases"][:20]
                )
                or ("- 无动态业务场景失败。",)
            ),
            "",
            "## 结果口径",
            "",
            (
                "- 正常、错误和别名用例的通用 Runner 只断言所选入站传输/OneBot "
                "契约、非空回复及无未捕获插件异常；它不凭传输成功猜测业务语义。"
            ),
            "- 权限拒绝和场景拒绝由 Core 统一返回固定语义，因此可以自动作严格断言。",
            (
                "- 写状态、外部依赖和高权限用例未被选择时明确记为未执行；"
                "外部服务不可用不得算插件业务通过。"
            ),
            (
                "- 动态业务场景使用真实 HTTP 或 WebSocket 入站、捕获并复用运行时 ID、"
                "逐步 include/exclude 断言，并在业务失败后继续尽力清理。"
            ),
            (
                "- 仅列入插件回归证据、但没有可安全运行 fixture 的外部/高权限命令，"
                "不会被动态场景冒充为运行态业务通过。"
            ),
            "",
            "## 清理",
            "",
            f"- 是否需要清理：{cleanup['required']}；清理状态：`{cleanup['status']}`。",
            (
                f"- 清理命令通过：{cleanup['command_steps_passed']}/"
                f"{cleanup['command_steps']}。命令回执仍不等于数据库/文件残留为零。"
            ),
            (
                "- 只要执行过 isolated_state 用例，项目总报告仍必须附上按合成用户/群号"
                "核对为零的清理产物，不能仅写‘清理命令已成功’。"
            ),
            "",
        )
    )


def write_artifacts(
    output_dir: Path,
    records: list[dict[str, Any]],
    cases: list[MatrixCase],
    results: list[dict[str, Any]],
    scenarios: list[BusinessScenario],
    scenario_results: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    if output_dir.exists():
        raise MatrixError(f"输出目录已经存在，拒绝覆盖: {output_dir}")
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
        (output_dir / "runtime-command-catalog.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (output_dir / "command-matrix.json").write_text(
            json.dumps([case.public_dict() for case in cases], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with (output_dir / "command-matrix-results.jsonl").open("w", encoding="utf-8") as handle:
            for row in results:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        (output_dir / "business-scenarios.json").write_text(
            json.dumps(
                [scenario.public_dict() for scenario in scenarios],
                ensure_ascii = False,
                indent       = 2,
            )
            + "\n",
            encoding="utf-8",
        )
        with (output_dir / "business-scenario-results.jsonl").open("w", encoding="utf-8") as handle:
            for row in scenario_results:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        (output_dir / "command-matrix-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (output_dir / "COMMAND_MATRIX_REPORT.md").write_text(
            render_report(summary),
            encoding="utf-8",
        )
    except OSError as exc:
        raise MatrixError(f"写入报告失败，目录可能含不完整产物: {output_dir}: {exc}") from exc


def _parse_csv_set(raw: str, allowed: frozenset[str], *, label: str) -> frozenset[str]:
    values  = frozenset(item.strip() for item in raw.split(",") if item.strip())
    unknown = values - allowed
    if not values or unknown:
        raise MatrixError(f"{label} 必须从 {sorted(allowed)} 中选择，未知值: {sorted(unknown)}")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument(
        "--transport",
        choices = ("http", "websocket"),
        default = "http",
        help    = "命令事件的真实入站传输；健康检查始终使用 --endpoint 对应的 HTTP /health",
    )
    parser.add_argument(
        "--ws-endpoint",
        default = DEFAULT_WS_ENDPOINT,
        help    = "--transport websocket 时使用的入站 WebSocket 地址",
    )
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS_PATH)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIO_PATH)
    parser.add_argument(
        "--scenario-fixtures",
        type = Path,
        help = "可选的本地 JSON fixture；用于外部服务器名等不应写入仓库的参数",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan-only", action="store_true", help="只抓取目录并生成矩阵")
    parser.add_argument(
        "--scenarios-only",
        action = "store_true",
        help   = "只执行动态业务场景；用于修正 fixture 后快速定点复测",
    )
    parser.add_argument(
        "--risks",
        default = "read_only",
        help    = "逗号分隔：read_only,isolated_state,privileged；默认仅 read_only",
    )
    parser.add_argument(
        "--dependencies",
        default = "local",
        help    = "逗号分隔：local,external；默认仅 local",
    )
    parser.add_argument("--allow-stateful", action="store_true")
    parser.add_argument("--allow-privileged", action="store_true")
    parser.add_argument("--plugins", default="", help="只执行逗号分隔的插件；计划仍保留全量")
    parser.add_argument("--codes", default="", help="只执行逗号分隔的稳定命令码前缀")
    parser.add_argument(
        "--kinds",
        default = ",".join(sorted(CASE_KINDS)),
        help    = "只执行逗号分隔的用例类型；例如 invalid",
    )
    parser.add_argument("--user-id", type=int, default=990_721_001)
    parser.add_argument("--group-id", type=int, default=990_721_002)
    parser.add_argument("--scenario-user-id", type=int)
    parser.add_argument("--scenario-group-id", type=int)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--delay-ms", type=int, default=25)
    return parser


def run(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    risks = _parse_csv_set(args.risks, RISKS, label="risks")
    dependencies = _parse_csv_set(args.dependencies, DEPENDENCIES, label="dependencies")
    kinds = _parse_csv_set(args.kinds, CASE_KINDS, label="kinds")
    if "isolated_state" in risks and not args.allow_stateful:
        raise MatrixError("执行 isolated_state 前必须显式提供 --allow-stateful")
    if "privileged" in risks and not args.allow_privileged:
        raise MatrixError("执行 privileged 前必须显式提供 --allow-privileged")
    if args.plan_only and args.scenarios_only:
        raise MatrixError("--plan-only 与 --scenarios-only 不能同时使用")
    scenario_user_id  = args.scenario_user_id or args.user_id + SCENARIO_ID_OFFSET
    scenario_group_id = args.scenario_group_id or args.group_id + SCENARIO_ID_OFFSET
    if (
        args.user_id <= 0
        or args.group_id <= 0
        or scenario_user_id <= 0
        or scenario_group_id <= 0
        or args.delay_ms < 0
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
    ):
        raise MatrixError("测试 ID、超时和 delay-ms 参数非法")

    token, admin_id, all_admins = load_runtime_auth(args.secrets.resolve())
    validate_test_id_isolation(
        user_id           = args.user_id,
        group_id          = args.group_id,
        scenario_user_id  = scenario_user_id,
        scenario_group_id = scenario_group_id,
        admin_ids         = all_admins,
    )
    policy            = load_policy(args.policy.resolve())
    scenario_contract = load_scenario_contract(args.scenarios.resolve())
    fixture_values    = load_scenario_fixtures(
        args.scenario_fixtures.resolve() if args.scenario_fixtures else None
    )
    client_type         = WebSocketEventClient if args.transport == "websocket" else EventClient
    transport_endpoint  = args.ws_endpoint if args.transport == "websocket" else args.endpoint
    client: EventSender = client_type(
        endpoint = transport_endpoint,
        token    = token,
        admin_id = admin_id,
        user_id  = args.user_id,
        group_id = args.group_id,
        timeout  = args.timeout,
    )
    # 动态场景使用独立合成账号和群，避免前面的海量只读/拒绝用例触发插件限流，
    # 也避免残留会话或群状态污染后续 CRUD 闭环。
    scenario_client: EventSender = client_type(
        endpoint = transport_endpoint,
        token    = token,
        admin_id = admin_id,
        user_id  = scenario_user_id,
        group_id = scenario_group_id,
        timeout  = args.timeout,
    )
    started_at = datetime.now().astimezone().isoformat()
    health_before = _probe_health(args.endpoint, token, timeout=args.timeout)
    records        = fetch_runtime_catalog(client)
    source_records = load_source_catalog()
    runtime_sha256 = catalog_hash(records)
    source_sha256  = catalog_hash(source_records)
    if runtime_sha256 != source_sha256:
        raise MatrixError(
            "运行进程目录与当前源码不一致，拒绝执行；请重启 Bot 后重试: "
            f"runtime={runtime_sha256}, source={source_sha256}"
        )
    cases     = build_matrix(records, policy)
    scenarios = build_business_scenarios(
        scenario_contract,
        records,
        expected_plugins=frozenset(policy["plugins"]),
    )
    plugins       = frozenset(item.strip() for item in args.plugins.split(",") if item.strip())
    code_prefixes = tuple(item.strip() for item in args.codes.split(",") if item.strip())
    redactions    = (
        token,
        str(admin_id),
        *(str(value) for value in all_admins),
        *fixture_values.values(),
    )
    results = execute_matrix(
        client,
        cases,
        risks          = risks,
        dependencies   = dependencies,
        plugins        = plugins,
        code_prefixes  = code_prefixes,
        kinds          = kinds,
        plan_only      = args.plan_only,
        scenarios_only = args.scenarios_only,
        delay_ms       = args.delay_ms,
        redactions     = redactions,
    )
    scenario_results = execute_business_scenarios(
        scenario_client,
        scenarios,
        risks          = risks,
        dependencies   = dependencies,
        plugins        = plugins,
        code_prefixes  = code_prefixes,
        plan_only      = args.plan_only,
        delay_ms       = args.delay_ms,
        redactions     = redactions,
        fixture_values = fixture_values,
        run_id         = hashlib.sha256(started_at.encode("utf-8")).hexdigest()[:10],
        runtime_values = {
            "test_user_id": str(scenario_user_id),
            "test_group_id": str(scenario_group_id),
        },
    )
    health_after = _probe_health(args.endpoint, token, timeout=args.timeout)
    finished_at = datetime.now().astimezone().isoformat()
    summary     = build_summary(
        records,
        cases,
        results,
        scenarios,
        scenario_results,
        started_at            = started_at,
        finished_at           = finished_at,
        health_before         = health_before,
        health_after          = health_after,
        source_catalog_sha256 = source_sha256,
        transport             = args.transport,
    )
    output_dir = args.output
    if output_dir is None:
        stamp      = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        output_dir = PROJECT_ROOT / "test_reports" / "runs" / "project" / f"command-matrix-{stamp}"
    output_dir = output_dir.resolve()
    write_artifacts(
        output_dir,
        records,
        cases,
        results,
        scenarios,
        scenario_results,
        summary,
    )
    return output_dir, summary


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args   = parser.parse_args(argv)
    try:
        output_dir, summary = run(args)
    except MatrixError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"报告已写入: {output_dir}")
    failed = (
        summary["execution"]["failed_runtime_contract"]
        + summary["business_scenarios"]["failed_steps"]
    )
    return 1 if failed or not summary["health"]["gate_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
