from __future__ import annotations

import hashlib
import json
from pathlib import Path

from plugins.choice.main import make_choice

ROOT = Path(__file__).resolve().parents[2]


def _read(plugin: str, filename: str = "README.md") -> str:
    return (ROOT / "plugins" / plugin / filename).read_text(encoding="utf-8")


def _manifest(plugin: str) -> dict:
    return json.loads(_read(plugin, "plugin.json"))


def test_adnmb_docs_match_read_and_feed_capabilities() -> None:
    readme = _read("adnmb")
    assert "/adnmb -t" in readme and "/adnmb -a" in readme
    assert "不支持账号登录、Cookie 管理、发帖或回复" in readme
    assert "allowed_users" not in readme


def test_ads_paper_docs_use_current_command_and_secret_keys() -> None:
    readme = _read("ads_paper")
    for marker in ("/paper search", "ads_token", "api_base", "api_key", "model", "user_id"):
        assert marker in readme
    assert "/ads " not in readme


def test_apod_docs_describe_current_nasa_page_and_no_date_parameter() -> None:
    readme = _read("apod")
    assert "apod.nasa.gov/apod/astropix.html" in readme
    assert "不支持日期参数" in readme
    assert "UCL" in readme and "不使用" in readme


def test_astro_docs_list_only_router_subcommands() -> None:
    readme = _read("astro_tools")
    for command in ("time", "coord", "convert", "redshift", "formula", "obj", "const"):
        assert f"`{command}`" in readme
    assert "没有 `/astro object`" in readme
    assert "没有通用 FK4" in readme


def test_dictionary_manifest_matches_packaged_files() -> None:
    manifest = json.loads(_read("dict", "assets/manifest.json"))
    for spec in manifest["files"].values():
        path = ROOT / "plugins" / "dict" / "assets" / spec["filename"]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == spec["sha256"]
    assert "assets/manifest.json" in _read("dict")


def test_github_docs_do_not_claim_language_filter_or_network_cache() -> None:
    readme = _read("github")
    assert "不提供语言过滤参数" in readme
    assert "不是跳过网络请求的响应缓存" in readme
    assert all(range_name in readme for range_name in ("daily", "weekly", "monthly"))


def test_guess_number_docs_follow_session_message_flow() -> None:
    readme = _read("guess_number")
    assert "直接发送数字" in readme
    assert "不要重复命令前缀" in readme
    assert "退出" in readme and "3 分钟" in readme


def test_signin_docs_and_manifest_match_shared_admin_account_model() -> None:
    readme = _read("signin")
    manifest = _manifest("signin")
    assert manifest["commands"][0]["admin_only"] is True
    assert all("group_ids" not in schedule for schedule in manifest["schedule"])
    for marker in ("共享", "app_id", "kdt_id", "access_token", "sid", "h5.youzan.com"):
        assert marker in readme


def test_smalltalk_docs_cover_manifest_commands_and_provider_boundary() -> None:
    readme = _read("smalltalk")
    manifest = _manifest("smalltalk")
    assert all(command["admin_only"] for command in manifest["commands"])
    for marker in ("/记忆", "/对话", "/删除对话", "chat.reply", "voice.synthesize_text"):
        assert marker in readme
    assert "不存在笑话命令" in readme


def test_voice_docs_use_exact_secret_hierarchy() -> None:
    readme = _read("voice")
    for marker in ("config/secrets.json", '"voice"', '"subscription_key"', '"region"'):
        assert marker in readme
    assert _manifest("voice")["commands"][0]["admin_only"] is True


def test_choice_docs_and_runtime_define_replacement_modes() -> None:
    readme = _read("choice")
    assert "有放回抽样" in readme and "不放回抽样" in readme
    assert len(make_choice(["a", "b"], 5, unique=False)) == 5
    try:
        make_choice(["a", "b"], 3, unique=True)
    except ValueError as exc:
        assert "count <= number of options" in str(exc)
    else:  # pragma: no cover - contract failure branch
        raise AssertionError("unique overdraw must fail")
