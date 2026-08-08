"""Static quality gates for executable tests."""

from __future__ import annotations

import ast
import re
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_ROOT.parent
REMOVED_RUNTIME_PATHS = {
    "plugins/adnmb/user.py",
    "plugins/signin/sony.py",
    "tests/run_all_tests.py",
}
REMOVED_QINGPET_APIS = {
    "apply_economy_multiplier",
    "check_and_consume_minigame_cooldown",
    "create_trade_listing",
    "deactivate_listing",
    "grant_daily_reward",
    "vote_pet_show",
}
# 这些名字曾经只被测试调用。测试若需要观察内部状态，应把探针留在
# ``tests/helpers``，不能再次扩大生产模块的维护契约。
REMOVED_DEAD_OR_TEST_ONLY_RUNTIME_APIS = {
    "core/bounded_http.py": {"decoded_bytes"},
    "core/atomic_store.py": {"active_keyed_lock_count"},
    "core/config.py": {"last_notified_revision", "save_secrets"},
    "core/plugin_runtime.py": {"is_quarantined"},
    "core/scheduler.py": {"clear_prefix"},
    "core/server.py": {"inflight_count", "lane_count", "pending_for_key"},
    "core/session.py": {"active_key_lock_count", "list_user_sessions"},
    "plugins/codex/manager.py": {"reset_manager_for_tests", "wait_idle"},
    "plugins/pendo/config.py": {"reset_runtime_config"},
    "plugins/pendo/utils/db_ops.py": {"cleanup_db_singleton"},
}
CRITICAL_COORDINATORS = {
    ("plugins/xiaoqing_chat/reply_generator.py", "_generate_reply_draft"): 140,
    ("plugins/codex/runner.py", "run"): 120,
    ("plugins/xiaoqing_chat/planning/pfc_engine.py", "run_pfc_once"): 140,
}
REQUIRED_COORDINATOR_PHASES = {
    "plugins/xiaoqing_chat/reply_generator.py": {
        "_prepare_reply_generation",
        "_build_attempt_messages",
        "_request_reply_candidate",
        "_resolve_candidate_draft",
        "_check_candidate_draft",
    },
    "plugins/codex/runner.py": {
        "_spawn_process",
        "_execute_process",
        "_capture_result",
        "_cleanup_run_resources",
    },
    "plugins/xiaoqing_chat/planning/pfc_engine.py": {
        "_prepare_pfc_session",
        "_plan_pfc_action",
        "_fetch_pfc_knowledge",
        "_finish_pfc_action",
    },
}
# 这些测试验证的是无法仅靠公开行为稳定覆盖的安全或发布契约，因此允许读取生产源码。
# 清单精确到测试函数；新增读取点与已经失效的旧条目都会触发门禁。
ALLOWED_PROJECT_SOURCE_READERS = {
    # 日志脱敏与敏感信息审计。
    (
        "plugins/test_cross_plugin_regressions.py",
        "test_sensitive_log_regressions_are_absent",
    ),
    (
        "plugins/test_internal_log_redaction.py",
        "test_targeted_exception_log_calls_never_receive_raw_exception_values",
    ),
    (
        "plugins/test_public_error_redaction.py",
        "test_public_plugin_runtime_never_uses_unredacted_traceback_logging",
    ),
    (
        "plugins/test_sensitive_logging_audit.py",
        "test_qingssh_and_minecraft_logger_calls_reject_sensitive_ast_arguments",
    ),
    (
        "plugins/test_shell_jupyter_log_privacy.py",
        "test_shell_and_jupyter_ordinary_logger_ast_never_receives_raw_payloads",
    ),
    (
        "plugins/test_shell_jupyter_log_privacy.py",
        "test_shell_and_qingssh_broad_exception_handlers_never_format_exception_text",
    ),
    (
        "plugins/test_xiaoqing_log_privacy.py",
        "test_identifier_log_paths_have_no_unredacted_direct_logger_arguments",
    ),
    (
        "plugins/test_xiaoqing_log_privacy.py",
        "test_reply_prompt_debug_never_passes_prompt_content_to_ordinary_logger",
    ),
    # 受限 HTTP 传输不得退回直接、无界的响应读取。
    (
        "plugins/test_arxiv_bounded_http.py",
        "test_sync_arxiv_paths_forbid_unbounded_requests_and_response_access",
    ),
    (
        "plugins/test_configured_http_clients.py",
        "test_configured_clients_forbid_direct_response_body_access",
    ),
    (
        "plugins/test_fixed_origin_http_clients.py",
        "test_fixed_http_plugins_have_no_direct_response_body_reads",
    ),
    (
        "plugins/test_remaining_http_clients.py",
        "test_remaining_runtime_paths_have_no_direct_response_body_reads",
    ),
    (
        "plugins/test_simbad_bounded_http.py",
        "test_simbad_source_has_local_query_builder_and_bounded_transport",
    ),
    (
        "plugins/test_earthquake.py",
        "test_runtime_has_no_direct_unbounded_response_reads",
    ),
    (
        "test_bounded_http_adoption.py",
        "test_runtime_http_paths_have_no_direct_unbounded_response_reads",
    ),
    # 共享参数、内容边界与配置快照的采用门禁。
    ("test_args_adoption.py", "test_reviewed_integer_consumers_use_core_parser"),
    ("test_args_adoption.py", "test_reviewed_plugins_use_core_argument_layer"),
    (
        "test_external_content_adoption.py",
        "test_external_text_consumers_use_the_single_dual_budget_boundary",
    ),
    (
        "test_external_content_adoption.py",
        "test_image_consumers_do_not_reimplement_pillow_validation",
    ),
    (
        "test_settings_snapshot_adoption.py",
        "test_pendo_web_runtime_has_one_config_source",
    ),
    (
        "test_settings_snapshot_adoption.py",
        "test_plugins_do_not_bypass_the_atomic_settings_reader",
    ),
    (
        "test_settings_snapshot_adoption.py",
        "test_reviewed_runtime_readers_explicitly_use_settings_snapshots",
    ),
    # 控制面职责拆分、能力探测与私有 API 隔离。
    (
        "test_core_module_boundaries.py",
        "test_control_plane_facades_and_responsibility_modules_stay_bounded",
    ),
    (
        "test_core_module_boundaries.py",
        "test_control_plane_mixins_do_not_shadow_each_other",
    ),
    (
        "test_core_module_boundaries.py",
        "test_runtime_compatibility_uses_capabilities_instead_of_exact_versions",
    ),
    (
        "test_scheduler.py",
        "test_private_scheduler_api_is_confined_to_compat_adapter",
    ),
    # 删除边界、依赖清单与运行时包内容。
    (
        "plugins/test_plugin_resource_lifecycle.py",
        "test_disabled_adnmb_user_module_is_removed_from_runtime_tree",
    ),
    (
        "plugins/test_dict_color_contracts.py",
        "test_dict_uses_standard_library_and_package_inventory_is_complete",
    ),
    (
        "plugins/test_arxiv_training_utils.py",
        "test_arxiv_runtime_keeps_only_the_used_fetch_and_knn_scoring_entrypoints",
    ),
    # Web 登录与历史数据转义必须由源码/CSP 的组合契约保证。
    (
        "plugins/test_pendo_web_demo.py",
        "test_login_page_sources_offer_demo_entry",
    ),
    (
        "plugins/test_pendo_transfer_import.py",
        "test_pendo_item_id_attributes_escape_historical_untrusted_values_and_csp_blocks_inline_script",
    ),
    # 文档、项目元数据、脚本与最低 Python 版本的发布契约。
    ("test_docs_metadata.py", "test_project_urls_match_the_configured_origin"),
    (
        "test_docs_metadata.py",
        "test_active_pendo_dependency_guidance_matches_runtime_metadata",
    ),
    (
        "test_operational_truth.py",
        "test_python_functions_contain_no_misplaced_string_expressions",
    ),
    (
        "test_run_bot_monitor_script.py",
        "test_log_pump_requests_hidden_windows_children",
    ),
    (
        "test_tooling_config.py",
        "test_all_python_310_toml_entrypoints_have_tomli_fallback",
    ),
    (
        "test_tooling_config.py",
        "test_mypy_checks_runtime_trees_without_core_exclusions",
    ),
}
TRANSPORT_CONTRACT_TESTS = {
    "plugins/test_signin.py",
    "plugins/test_twitter.py",
    "plugins/test_wolframalpha.py",
}
MAX_TEST_MODULE_LINES = 1600
OPAQUE_REVIEW_FILENAME = re.compile(r"^test_cr\d", re.IGNORECASE)
PERSONAL_ABSOLUTE_PATH = re.compile(
    r"(?i)(?:[A-Z]:[/\\]Users[/\\][A-Za-z0-9._-]{1,64}|/home/[A-Za-z0-9._-]{1,64})"
)


def _test_functions() -> list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]]:
    functions: list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions.extend(
            (path, node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
    return functions


def _implicit_subprocess_text_decoders() -> list[str]:
    """列出依赖 Python 默认编码的文本子进程调用。"""

    violations: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
            text_mode = keywords.get("text") or keywords.get("universal_newlines")
            if not (
                isinstance(text_mode, ast.Constant)
                and text_mode.value is True
                and ("encoding" not in keywords or "errors" not in keywords)
            ):
                continue
            relative_path = path.relative_to(TESTS_ROOT).as_posix()
            violations.append(f"{relative_path}:{node.lineno}")
    return violations


def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _is_len_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
        and len(node.args) == 1
    )


def _is_zero(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value == 0


def _is_tautological_length_assert(node: ast.Assert) -> bool:
    comparison = node.test
    if not isinstance(comparison, ast.Compare) or len(comparison.ops) != 1:
        return False
    right = comparison.comparators[0]
    return (
        isinstance(comparison.ops[0], ast.GtE) and _is_len_call(comparison.left) and _is_zero(right)
    ) or (
        isinstance(comparison.ops[0], ast.LtE) and _is_zero(comparison.left) and _is_len_call(right)
    )


def _bind_source_expression(
    bindings: dict[str, list[ast.AST]], target: ast.AST, value: ast.AST
) -> None:
    """记录名称可能来自哪些表达式，保守合并分支和循环中的赋值。"""
    if isinstance(target, ast.Name):
        bindings.setdefault(target.id, []).append(value)
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            _bind_source_expression(bindings, element, value)


def _scope_source_bindings(
    tree: ast.Module, function: ast.FunctionDef | ast.AsyncFunctionDef
) -> dict[str, list[ast.AST]]:
    """收集模块和测试函数内的路径来源，不执行被审查的测试模块。"""
    bindings: dict[str, list[ast.AST]] = {}
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            if value is None:
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                _bind_source_expression(bindings, target, value)

    # 参数化值位于函数体之外；把装饰器表达式绑定给参数，后续再与 ROOT 合并。
    decorator_values = [
        decorator for decorator in function.decorator_list if _source_flags(decorator, bindings)[0]
    ]
    for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs):
        for decorator in decorator_values:
            _bind_source_expression(bindings, ast.Name(id=argument.arg), decorator)

    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            value = node.value
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                _bind_source_expression(bindings, target, value)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _bind_source_expression(bindings, node.target, node.iter)
        elif isinstance(node, ast.comprehension):
            _bind_source_expression(bindings, node.target, node.iter)
    return bindings


def _source_flags(
    node: ast.AST | None,
    bindings: dict[str, list[ast.AST]],
    resolving: frozenset[str] = frozenset(),
) -> tuple[bool, bool]:
    """返回表达式是否含 Python 路径、是否锚定当前项目。"""
    if node is None:
        return False, False
    has_python_path = (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (node.value.casefold().endswith(".py") or node.value.casefold().endswith(".pyw"))
    )
    project_anchored = isinstance(node, ast.Name) and node.id == "__file__"
    if isinstance(node, ast.Attribute) and node.attr == "__file__":
        # `Path(imported_module.__file__)` 已经是具体 Python 模块文件，而不是目录锚点。
        has_python_path = True
        project_anchored = True

    if isinstance(node, ast.Name) and node.id in bindings and node.id not in resolving:
        next_resolving = resolving | {node.id}
        for value in bindings[node.id]:
            value_python, value_project = _source_flags(value, bindings, next_resolving)
            has_python_path |= value_python
            project_anchored |= value_project

    for child in ast.iter_child_nodes(node):
        child_python, child_project = _source_flags(child, bindings, resolving)
        has_python_path |= child_python
        project_anchored |= child_project
    return has_python_path, project_anchored


def _function_reads_project_python_source(
    tree: ast.Module, function: ast.FunctionDef | ast.AsyncFunctionDef
) -> bool:
    bindings = _scope_source_bindings(tree, function)
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        target: ast.AST | None = None
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "open",
            "read_bytes",
            "read_text",
        }:
            target = node.func.value
        elif isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
            target = node.args[0]
        elif isinstance(node.func, ast.Name) and node.func.id in {"compile", "exec"}:
            # 源码先读入变量再执行时，赋值来源仍会把两个标志传到参数。
            target = node.args[0] if node.args else None
        if target is not None and _source_flags(target, bindings) == (True, True):
            return True
    return False


def test_test_functions_have_executable_bodies() -> None:
    empty: list[str] = []
    for path, function in _test_functions():
        statements = _without_docstring(function.body)
        if not statements or all(isinstance(statement, ast.Pass) for statement in statements):
            empty.append(f"{path.relative_to(TESTS_ROOT)}:{function.lineno} {function.name}")

    assert empty == [], "empty tests:\n" + "\n".join(empty)


def test_tests_do_not_use_constant_true_or_tautological_length_assertions() -> None:
    violations: list[str] = []
    for path, function in _test_functions():
        for node in ast.walk(function):
            if not isinstance(node, ast.Assert):
                continue
            constant_true = isinstance(node.test, ast.Constant) and node.test.value is True
            if constant_true or _is_tautological_length_assert(node):
                violations.append(f"{path.relative_to(TESTS_ROOT)}:{node.lineno} {function.name}")

    assert violations == [], "non-verifying assertions:\n" + "\n".join(violations)


def test_only_audited_contract_tests_read_project_python_source() -> None:
    readers: set[tuple[str, str]] = set()
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path == Path(__file__).resolve():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(TESTS_ROOT).as_posix()
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ):
            if _function_reads_project_python_source(tree, function):
                readers.add((relative_path, function.name))

    unexpected = sorted(readers - ALLOWED_PROJECT_SOURCE_READERS)
    stale = sorted(ALLOWED_PROJECT_SOURCE_READERS - readers)
    assert unexpected == [] and stale == [], (
        "unaudited project-source readers:\n"
        + "\n".join(f"  + {path}::{name}" for path, name in unexpected)
        + "\nstale source-reader allowlist entries:\n"
        + "\n".join(f"  - {path}::{name}" for path, name in stale)
    )


def test_plugin_http_tests_do_not_replace_the_bounded_transport() -> None:
    violations: list[str] = []
    for relative_path in sorted(TRANSPORT_CONTRACT_TESTS):
        path = TESTS_ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setattr"
                and any(
                    isinstance(argument, ast.Constant)
                    and argument.value == "aiohttp_request_bounded"
                    for argument in node.args
                )
            ):
                violations.append(f"{relative_path}:{node.lineno}")

    assert violations == [], "bounded transport replacements:\n" + "\n".join(violations)


def test_removed_runtime_modules_and_test_runner_stay_removed() -> None:
    existing = sorted(
        relative_path
        for relative_path in REMOVED_RUNTIME_PATHS
        if (PROJECT_ROOT / relative_path).exists()
    )

    assert existing == []


def test_runtime_code_does_not_call_removed_qingpet_database_apis() -> None:
    violations: list[str] = []
    for path in sorted((PROJECT_ROOT / "plugins").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in REMOVED_QINGPET_APIS
            ):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} {node.func.attr}"
                )

    assert violations == [], "removed QingPet API calls:\n" + "\n".join(violations)


def test_removed_dead_or_test_only_runtime_apis_stay_removed() -> None:
    """阻止死接口或测试便利函数重新进入生产模块。"""

    violations: list[str] = []
    for relative_path, removed_names in sorted(REMOVED_DEAD_OR_TEST_ONLY_RUNTIME_APIS.items()):
        path = PROJECT_ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        definitions = {
            node.name: node.lineno
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        violations.extend(
            f"{relative_path}:{definitions[name]} {name}"
            for name in sorted(removed_names & definitions.keys())
        )

    assert violations == [], "test-only runtime APIs restored:\n" + "\n".join(violations)


def test_runtime_sources_have_no_personal_absolute_paths() -> None:
    violations: list[str] = []
    for root_name in ("core", "plugins"):
        root = PROJECT_ROOT / root_name
        for path in sorted(root.rglob("*.py")):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if PERSONAL_ABSOLUTE_PATH.search(line):
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}:{line_number}")

    assert violations == [], "personal absolute paths in runtime sources:\n" + "\n".join(violations)


def test_pytest_modules_do_not_embed_manual_runners() -> None:
    violations: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            is_main_guard = (
                isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"
                and any(
                    isinstance(value, ast.Constant) and value.value == "__main__"
                    for value in node.test.comparators
                )
            )
            calls_pytest_main = any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "pytest"
                and child.func.attr == "main"
                for child in ast.walk(node)
            )
            if is_main_guard and calls_pytest_main:
                violations.append(f"{path.relative_to(TESTS_ROOT)}:{node.lineno}")

    assert violations == [], "manual pytest runners:\n" + "\n".join(violations)


def test_text_subprocesses_declare_encoding_and_decode_policy() -> None:
    """阻止系统默认编码再次泄漏到子进程 reader 线程。"""

    violations = _implicit_subprocess_text_decoders()
    assert violations == [], "implicit subprocess text decoding:\n" + "\n".join(violations)


def test_test_modules_remain_bounded_and_semantically_named() -> None:
    """阻止测试重新堆回巨型文件或只保留已失去上下文的审查编号。"""

    violations: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        relative_path = path.relative_to(TESTS_ROOT)
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > MAX_TEST_MODULE_LINES:
            violations.append(
                f"{relative_path}: {line_count} lines exceeds {MAX_TEST_MODULE_LINES}"
            )
        if OPAQUE_REVIEW_FILENAME.match(path.name):
            violations.append(f"{relative_path}: replace review number with a behavior name")

    assert violations == [], "unbounded or opaque test modules:\n" + "\n".join(violations)


def test_critical_coordinators_remain_split_into_bounded_phases() -> None:
    violations: list[str] = []
    for (relative_path, coordinator), maximum_lines in CRITICAL_COORDINATORS.items():
        path = PROJECT_ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        node = functions.get(coordinator)
        if node is None or node.end_lineno is None:
            violations.append(f"{relative_path}: missing {coordinator}")
            continue
        line_count = node.end_lineno - node.lineno + 1
        if line_count > maximum_lines:
            violations.append(f"{relative_path}:{node.lineno} {coordinator} has {line_count} lines")
        missing_phases = REQUIRED_COORDINATOR_PHASES[relative_path] - functions.keys()
        if missing_phases:
            violations.append(
                f"{relative_path}: missing phases {', '.join(sorted(missing_phases))}"
            )

    assert violations == [], "oversized or collapsed coordinators:\n" + "\n".join(violations)
