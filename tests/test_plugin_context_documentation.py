"""Executable guards for the public PluginContext documentation contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.interfaces import PluginContextProtocol

ROOT = Path(__file__).resolve().parent.parent
CONTEXT_DOCS = (
    ROOT / "docs" / "00-overview.md",
    ROOT / "docs" / "03-plugin-development.md",
    ROOT / "docs" / "04-core-modules.md",
    ROOT / "docs" / "05-api-reference.md",
    ROOT / "docs" / "06-configuration.md",
    ROOT / "docs" / "07-advanced.md",
)


@pytest.mark.parametrize("path", CONTEXT_DOCS, ids=lambda path: path.name)
def test_context_docs_do_not_teach_privilege_bypasses(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "context.secrets.get" not in text
    assert "context.app" not in text
    assert "secrets.json 完整内容" not in text


def test_context_docs_use_real_mute_and_expiry_units() -> None:
    api = (ROOT / "docs" / "05-api-reference.md").read_text(encoding="utf-8")
    advanced = (ROOT / "docs" / "07-advanced.md").read_text(encoding="utf-8")

    assert "剩余静音时间（分钟）" in api
    assert "剩余静音时间（秒）" not in api
    assert "后续插件可再次调用 `create_session()` 建立新会话" in api
    assert "context.update_session(callback)" in api
    assert "context.app.plugin_manager" not in advanced


def test_public_docs_state_the_trusted_plugin_model_and_review_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    plugin_guide = (ROOT / "docs" / "03-plugin-development.md").read_text(encoding="utf-8")

    assert "请安装经过审查的第一方插件或可信插件" in readme
    assert "与 Bot 共享 Python 进程和操作系统权限" in readme
    assert "独立 Manifest、配置命名空间、数据目录和生命周期" in readme
    assert "| 插件执行隔离 |" not in readme
    assert "同进程、同操作系统权限运行的受信任 Python 扩展" in plugin_guide
    assert "部署者负责插件来源与代码审查" in plugin_guide
    assert "限定在当前插件命名空间" in plugin_guide


async def _type_checked_context_example(context: PluginContextProtocol) -> bool:
    """Keep the documented getter/session/mute calls compatible with the protocol."""

    context.get_config("provider.model")
    context.get_secret("provider.api_key")
    context.is_global_admin()
    context.get_mute_remaining(123456)
    session = await context.get_session()
    if session is None:
        await context.create_session({"step": 1}, timeout=300.0)
    else:
        await context.update_session(lambda working: working.set("step", 2))
    return await context.has_session()


def test_documented_context_example_is_executable() -> None:
    assert callable(_type_checked_context_example)


def test_protocol_declares_every_request_scoped_attribute_used_by_the_guide() -> None:
    annotations = PluginContextProtocol.__annotations__

    assert {"metrics", "request_id", "command_invocation"} <= annotations.keys()
