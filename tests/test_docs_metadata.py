"""校验公开文档、配置示例、项目元数据和生成产物边界。"""

from __future__ import annotations

import ast
import json
import re
import textwrap
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib

from core.models import (
    PluginCommandManifest,
    PluginCommandNodeManifest,
    PluginDependencyManifest,
    PluginManifest,
    PluginScheduleManifest,
    PluginServiceManifest,
)

ROOT = Path(__file__).resolve().parents[1]


def test_project_urls_match_the_configured_origin() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["urls"] == {
        "Homepage": "https://github.com/SukiYume/XiaoQing",
        "Documentation": "https://github.com/SukiYume/XiaoQing#readme",
        "Repository": "https://github.com/SukiYume/XiaoQing.git",
        "Issues": "https://github.com/SukiYume/XiaoQing/issues",
    }
    fetcher = (
        ROOT
        / "plugins"
        / "arxiv_filter"
        / "train_model"
        / "data_prep"
        / "step2_fetch_all_astro_ph.py"
    ).read_text(encoding="utf-8")
    assert "github.com/SukiYume/XiaoQing" in fetcher
    assert "github.com/xiaoqing-bot/xiaoqing" not in fetcher


def test_root_secret_example_matches_voice_and_signin_runtime_schema() -> None:
    document = json.loads((ROOT / "config" / "secrets.json.example").read_text(encoding="utf-8"))
    plugins = document["plugins"]

    assert set(plugins["voice"]) == {
        "subscription_key",
        "region",
        "voice_name",
        "style",
        "role",
        "proxy",
    }
    assert set(plugins["signin"]) == {"yingshijufeng"}
    assert set(plugins["signin"]["yingshijufeng"]) == {
        "app_id",
        "kdt_id",
        "access_token",
        "sid",
    }


def test_root_secret_example_uses_current_standard_glm_api() -> None:
    config = json.loads((ROOT / "config" / "config.json.example").read_text(encoding="utf-8"))
    secrets = json.loads((ROOT / "config" / "secrets.json.example").read_text(encoding="utf-8"))
    provider = config["ai"]["providers"]["zhipu"]
    model = config["ai"]["models"]["glm-5.2"]

    assert provider["api_base"] == "https://open.bigmodel.cn/api/paas/v4"
    assert provider["endpoint_path"] == "/chat/completions"
    assert provider["proxy"] == ""
    assert model["provider"] == "zhipu"
    assert model["model"] == "glm-5.2"
    assert set(secrets["ai"]["providers"]["zhipu"]) == {"api_key"}
    assert "/api/coding/" not in json.dumps([provider, model])


def test_active_pendo_dependency_guidance_matches_runtime_metadata() -> None:
    active_guidance = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "README.md",
            ROOT / "docs" / "01-getting-started.md",
            ROOT / "plugins" / "pendo" / "README.md",
            ROOT / "plugins" / "pendo" / "handlers" / "web.py",
        )
    )
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]

    assert "passlib" not in active_guidance.casefold()
    assert any(requirement.startswith("fastapi") for requirement in dependencies)
    assert any(requirement.startswith("uvicorn") for requirement in dependencies)
    assert any(requirement.startswith("PyJWT") for requirement in dependencies)


def test_signin_manifest_and_docs_only_advertise_current_runtime() -> None:
    manifest = json.loads((ROOT / "plugins" / "signin" / "plugin.json").read_text(encoding="utf-8"))
    manual = (ROOT / "docs" / "09-plugins.md").read_text(encoding="utf-8")
    readme = (ROOT / "plugins" / "signin" / "README.md").read_text(encoding="utf-8")

    assert manifest["commands"][0]["admin_only"] is True
    assert "sony" not in manifest["description"].casefold()
    assert "sony" not in manual.casefold()
    assert "sony" not in readme.casefold()


def test_generated_coverage_is_absent_and_ignored() -> None:
    ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert not (ROOT / "coverage.json").exists()
    assert "coverage*.json" in ignore_rules
    assert "coverage-reports/" in ignore_rules


def test_plugin_concurrency_documentation_matches_manifest_contract() -> None:
    plugin_doc = (ROOT / "docs" / "03-plugin-development.md").read_text(encoding="utf-8")
    concurrency_row = next(
        line for line in plugin_doc.splitlines() if line.startswith("| `concurrency`")
    )

    assert "`parallel`" in concurrency_row
    assert "`sequential`" in concurrency_row
    assert "`serial`" not in concurrency_row
    assert "`shared`" not in concurrency_row


def test_plugin_development_manifest_reference_covers_the_live_schema() -> None:
    plugin_doc = (ROOT / "docs" / "03-plugin-development.md").read_text(encoding="utf-8")
    schema_models = (
        PluginManifest,
        PluginCommandManifest,
        PluginCommandNodeManifest,
        PluginScheduleManifest,
        PluginDependencyManifest,
        PluginServiceManifest,
    )

    for model in schema_models:
        for field_name in model.model_fields:
            assert f"`{field_name}`" in plugin_doc, f"missing {model.__name__}.{field_name}"


def test_plugin_development_uses_current_safe_extension_boundaries() -> None:
    plugin_doc = (ROOT / "docs" / "03-plugin-development.md").read_text(encoding="utf-8")

    assert "context.metrics.increment_plugin_call" not in plugin_doc
    assert "context.http_session.get(" not in plugin_doc
    assert "aiohttp_request_bounded" in plugin_doc
    assert "fetch_public_html" in plugin_doc
    assert "bounded_external_text" in plugin_doc
    assert "validate_image_bytes" in plugin_doc
    assert "public_error_response" in plugin_doc
    assert "log_sensitive_operation" in plugin_doc
    assert "config.plugins.<plugin_name>.ai.routes.<route_name>" in plugin_doc


def test_plugin_development_python_examples_are_syntax_checked() -> None:
    plugin_doc = (ROOT / "docs" / "03-plugin-development.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", plugin_doc, flags=re.DOTALL)

    assert blocks
    for index, block in enumerate(blocks, start=1):
        filename = f"docs/03-plugin-development.md#python-{index}"
        try:
            compile(block, filename, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        except SyntaxError:
            wrapped = "async def _documented_example():\n" + textwrap.indent(block, "    ")
            compile(wrapped, filename, "exec")


def test_configuration_reference_covers_the_shipped_examples() -> None:
    configuration = (ROOT / "docs" / "06-configuration.md").read_text(encoding="utf-8")
    public_example = json.loads(
        (ROOT / "config" / "config.json.example").read_text(encoding="utf-8")
    )
    secret_example = json.loads(
        (ROOT / "config" / "secrets.json.example").read_text(encoding="utf-8")
    )

    for key in public_example:
        assert f"`{key}`" in configuration, f"missing public config key {key}"
    for plugin_name in public_example["plugins"]:
        assert f"`plugins.{plugin_name}" in configuration, (
            f"missing public plugin namespace {plugin_name}"
        )
    for key in ("onebot_token", "inbound_token", "admin_user_ids"):
        assert key in secret_example
        assert f"`{key}`" in configuration
    for plugin_name in secret_example["plugins"]:
        assert f"`plugins.{plugin_name}" in configuration, (
            f"missing secret plugin namespace {plugin_name}"
        )


def test_configuration_reference_states_runtime_boundaries_and_budgets() -> None:
    configuration = (ROOT / "docs" / "06-configuration.md").read_text(encoding="utf-8")

    assert "`inbound_http_base` 使用 `http://`" in configuration
    assert "`inbound_ws_uri` 使用 `ws://`" in configuration
    assert "`plugin_poll_interval` | number | `3600`，`0.01..86400`" in configuration
    assert "`inbound_ws_max_workers + ws_queue_size`" in configuration
    assert "单个配置文件上限为 8 MiB" in configuration
    assert "JSON 树深度上限为 64" in configuration
    assert "节点上限为 100000" in configuration
    assert "### 生效矩阵" in configuration


def test_sensitive_codex_command_remains_private_and_admin_only() -> None:
    manifest = json.loads((ROOT / "plugins" / "codex" / "plugin.json").read_text(encoding="utf-8"))
    command = next(item for item in manifest["commands"] if item["name"] == "codex")

    assert command["admin_only"] is True
    assert command["contexts"] == ["private"]


def test_inbound_docs_require_token_before_listener_start() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    configuration = (ROOT / "docs" / "06-configuration.md").read_text(encoding="utf-8")

    assert "启用 HTTP 或 WebSocket Inbound 时，请设置非空 `inbound_token`" in readme
    assert "`secrets.json` 顶层 `inbound_token` 使用非空字符串" in configuration
    assert "默认 Listener 地址使用 loopback" in configuration


def test_api_reference_covers_current_public_types_and_delivery_contract() -> None:
    api = (ROOT / "docs" / "05-api-reference.md").read_text(encoding="utf-8")

    for marker in (
        "atomic_write_bytes(path: Path, payload: bytes) -> None",
        "atomic_write_text(path: Path, payload: str) -> None",
        "await context.send_action(action: dict[str, Any]) -> bool | None",
        "### `PluginSettingsSnapshot`",
        "### `PluginPrincipal`",
        "### `AIModelInfo`",
        "### `AICompletionResult`",
        "### `CommandCatalogNode`",
        "### `CommandInvocation`",
        "Core 同时接受相同参数签名的同步 `def` 回调",
    ):
        assert marker in api
    assert "重载操作由命令权限与插件 capability 共同约束" not in api


def test_inbound_and_deployment_docs_match_runtime_entrypoints() -> None:
    architecture = (ROOT / "docs" / "02-architecture.md").read_text(encoding="utf-8")
    core_modules = (ROOT / "docs" / "04-core-modules.md").read_text(encoding="utf-8")
    api = (ROOT / "docs" / "05-api-reference.md").read_text(encoding="utf-8")
    advanced = (ROOT / "docs" / "07-advanced.md").read_text(encoding="utf-8")
    message_flow = (ROOT / "docs" / "08-message-flow.md").read_text(encoding="utf-8")

    assert "`GET /metrics`" in architecture
    assert "`/metrics`" in core_modules
    assert '`{"actions": [...]}`' in api
    assert "Authorization: Bearer <inbound_token>" in api
    assert "OneBot secret 对应头" not in api
    assert "`/health`、`/metrics`、`POST /event`" in api
    assert "ExecStart=python main.py" in advanced
    assert "/usr/bin/python3" not in advanced
    assert "OneBotHttpSender" in message_flow
    assert "OneBotHttpClient" not in message_flow


def test_reload_documentation_uses_the_argument_free_runtime_command() -> None:
    documents = (
        ROOT / "README.md",
        ROOT / "docs" / "02-architecture.md",
        ROOT / "docs" / "06-configuration.md",
        ROOT / "plugins" / "bot_core" / "README.md",
    )

    for path in documents:
        text = path.read_text(encoding="utf-8")
        assert "`/reload`" in text or "/reload\n" in text
        assert "/reload config" not in text


def test_plugin_manual_covers_current_commands_defaults_and_data_paths() -> None:
    manual = (ROOT / "docs" / "09-plugins.md").read_text(encoding="utf-8")
    chime_readme = (ROOT / "plugins" / "chime" / "README.md").read_text(encoding="utf-8")

    for marker in (
        "/pendo settings view",
        "/pendo settings reminder on|off",
        "/pendo settings daily_report 08:00",
        "/astro formula calc <schwarzschild|luminosity|lifetime> <太阳质量>",
        "/astro formula [schwarzschild|stellar_luminosity|stellar_lifetime]",
        "/宠物 rename [群号] <新名字>",
        "/宠物 title",
        "发行配置示例设为 `0`，字段缺失时运行时默认值为 `0.2`",
    ):
        assert marker in manual
    for document in (manual, chime_readme):
        assert "`data/chime/chime_history.json`" in document
        assert "`data/chime/chime_delivery.json`" in document
        assert "`data/chime_history.json`" not in document
        assert "`data/chime_delivery.json`" not in document


def test_pendo_scriptable_guide_matches_client_layout_and_calendar_contract() -> None:
    guide = (ROOT / "docs" / "pendo-scriptable-widget.md").read_text(encoding="utf-8")

    for marker in (
        "section=tasks|ledger|notes|all|auto",
        "const BASE_URL = normalizeBaseUrl('https://example.com/pendo');",
        "固定请求 `section=auto`",
        "固定请求 `section=all`",
        "组件参数只控制 `medium`",
        "const SYNC_CALENDAR_NAME = 'Pendo';",
        "同步仅在 Scriptable App 内直接运行脚本时执行",
        "标题 + 开始时间",
        "采用仅新增策略",
    ):
        assert marker in guide
