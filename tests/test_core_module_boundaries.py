"""Architecture gates for the split application and plugin control planes."""

from __future__ import annotations

import ast
from pathlib import Path

from core.app import XiaoQingApp
from core.plugin_manager import PluginManager

ROOT = Path(__file__).resolve().parents[1]


def _declared_methods(source: str, class_name: str) -> set[str]:
    tree = ast.parse(source)
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_control_plane_facades_and_responsibility_modules_stay_bounded() -> None:
    limits = {
        "core/app.py": 2500,
        "core/plugin_manager.py": 2000,
        "core/app_delivery.py": 1000,
        "core/app_identity.py": 250,
        "core/app_ingress.py": 1000,
        "core/app_plugin_watch.py": 500,
        "core/app_scheduling.py": 500,
        "core/app_support.py": 500,
        "core/plugin_data.py": 750,
        "core/plugin_generation.py": 3000,
        "core/plugin_manager_support.py": 1500,
        "core/plugin_runtime.py": 1000,
        "core/plugin_watcher.py": 2000,
        "core/scheduler.py": 750,
        "core/scheduler_compat.py": 500,
    }

    violations = []
    for relative, maximum in limits.items():
        line_count = len((ROOT / relative).read_text(encoding="utf-8").splitlines())
        if line_count > maximum:
            violations.append(f"{relative}: {line_count} > {maximum}")

    assert not violations, (
        "control-plane module grew past its responsibility budget:\n" + "\n".join(violations)
    )


def test_plugin_manager_methods_are_owned_by_focused_modules() -> None:
    expected_owners = {
        "reload_plugin": "core.plugin_generation",
        "reconcile_plugins": "core.plugin_watcher",
        "resolve_service": "core.plugin_runtime",
        "_ensure_plugin_data_dir": "core.plugin_data",
    }

    assert {
        name: getattr(PluginManager, name).__module__ for name in expected_owners
    } == expected_owners


def test_application_methods_are_owned_by_focused_modules() -> None:
    expected_owners = {
        "_send_action": "core.app_delivery",
        "_reconcile_inbound_manager": "core.app_ingress",
        "_configure_plugin_watch": "core.app_plugin_watch",
        "_reschedule": "core.app_scheduling",
    }

    assert {
        name: getattr(XiaoQingApp, name).__module__ for name in expected_owners
    } == expected_owners


def test_control_plane_mixins_do_not_shadow_each_other() -> None:
    groups = (
        (
            ("core/plugin_manager.py", "PluginManager"),
            ("core/plugin_runtime.py", "PluginRuntimeMixin"),
            ("core/plugin_watcher.py", "PluginWatcherMixin"),
            ("core/plugin_generation.py", "PluginGenerationMixin"),
            ("core/plugin_data.py", "PluginDataMixin"),
        ),
        (
            ("core/app.py", "XiaoQingApp"),
            ("core/app_delivery.py", "AppDeliveryMixin"),
            ("core/app_ingress.py", "AppIngressMixin"),
            ("core/app_plugin_watch.py", "AppPluginWatchMixin"),
            ("core/app_scheduling.py", "AppSchedulingMixin"),
        ),
    )

    for group in groups:
        seen: set[str] = set()
        for relative, class_name in group:
            source = (ROOT / relative).read_text(encoding="utf-8")
            methods = _declared_methods(source, class_name)
            assert seen.isdisjoint(methods), f"{relative} shadows {sorted(seen & methods)}"
            seen.update(methods)


def test_runtime_compatibility_uses_capabilities_instead_of_exact_versions() -> None:
    plugin_support = (ROOT / "core/plugin_manager_support.py").read_text(encoding="utf-8")
    scheduler = (ROOT / "core/scheduler.py").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "sys.version_info" not in plugin_support
    assert "apscheduler.__version__" not in scheduler
    assert 'requires-python = ">=3.10"' in project
    assert '"apscheduler>=3.11,<4"' in project
    assert "apscheduler>=3.11,<4" in requirements.splitlines()
    assert "^core/app\\\\.py$" not in project
