"""校验公开文档、配置示例、项目元数据和生成产物边界。"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib

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
    plugin_doc = (ROOT / "docs" / "03-plugin-development.md").read_text(
        encoding="utf-8"
    )
    concurrency_row = next(
        line for line in plugin_doc.splitlines() if line.startswith("| `concurrency`")
    )

    assert "`parallel`" in concurrency_row
    assert "`sequential`" in concurrency_row
    assert "`serial`" not in concurrency_row
    assert "`shared`" not in concurrency_row


def test_sensitive_codex_command_remains_private_and_admin_only() -> None:
    manifest = json.loads(
        (ROOT / "plugins" / "codex" / "plugin.json").read_text(encoding="utf-8")
    )
    command = next(item for item in manifest["commands"] if item["name"] == "codex")

    assert command["admin_only"] is True
    assert command["contexts"] == ["private"]


def test_inbound_docs_require_token_before_listener_start() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    configuration = (ROOT / "docs" / "06-configuration.md").read_text(
        encoding="utf-8"
    )

    assert "启动时就必须提供非空 `inbound_token`" in readme
    assert "首次启动就要求非空字符串" in configuration
    assert "不表示匿名开放" in configuration
