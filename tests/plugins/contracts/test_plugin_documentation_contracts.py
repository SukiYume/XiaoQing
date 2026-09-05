from __future__ import annotations

import hashlib
import json

from plugins.choice.main import make_choice
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


def _read(plugin: str, filename: str = "README.md") -> str:
    return (ROOT / "plugins" / plugin / filename).read_text(encoding="utf-8")


def _manifest(plugin: str) -> dict:
    return json.loads(_read(plugin, "plugin.json"))


def test_adnmb_docs_match_read_and_feed_capabilities() -> None:
    readme = _read("adnmb")
    assert "/adnmb -t" in readme and "/adnmb -a" in readme
    assert "命令目录只包含公开内容浏览与匿名 Feed 管理" in readme
    assert "allowed_users" not in readme


def test_flickr_docs_describe_all_license_mode_and_attribution() -> None:
    readme = _read("flickr")

    assert "license=any" in readme
    assert "作者、许可" in readme
    assert "data/flickr/images/" in readme


def test_ads_paper_docs_use_current_command_and_secret_keys() -> None:
    readme = _read("ads_paper")
    for marker in (
        "/paper search",
        "ads_token",
        "config.ai.models",
        "config.ai.providers",
        "secrets.ai.providers",
        "summary",
        "user_id",
    ):
        assert marker in readme
    assert "/ads " not in readme


def test_apod_docs_describe_current_nasa_page_and_empty_argument_contract() -> None:
    readme = _read("apod")
    assert "apod.nasa.gov/apod/astropix.html" in readme
    assert "`/apod` 使用空参数调用" in readme
    assert "UCL" not in readme


def test_astro_docs_list_only_router_subcommands() -> None:
    readme = _read("astro_tools")
    for command in ("time", "coord", "convert", "redshift", "formula", "obj", "const"):
        assert f"/astro {command}" in readme
    assert "/astro object" not in readme
    assert "FK4" not in readme


def test_dictionary_manifest_matches_packaged_files() -> None:
    manifest = json.loads(_read("dict", "assets/manifest.json"))
    for spec in manifest["files"].values():
        path = ROOT / "plugins" / "dict" / "assets" / spec["filename"]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == spec["sha256"]
    assert "assets/manifest.json" in _read("dict")


def test_github_docs_describe_range_and_fresh_fetch_contracts() -> None:
    readme = _read("github")
    assert "命令接受一个完整的时间范围参数" in readme
    assert "每次命令和定时任务执行一次完整抓取事务" in readme
    assert all(range_name in readme for range_name in ("daily", "weekly", "monthly"))


def test_guess_number_docs_follow_session_message_flow() -> None:
    readme = _read("guess_number")
    assert "直接发送 ASCII 十进制数字" in readme
    assert "Core Session" in readme
    assert "退出" in readme and "3 分钟" in readme


def test_signin_docs_and_manifest_match_shared_admin_account_model() -> None:
    readme   = _read("signin")
    manifest = _manifest("signin")
    assert manifest["commands"][0]["admin_only"] is True
    assert all("group_ids" not in schedule for schedule in manifest["schedule"])
    for marker in ("共享", "app_id", "kdt_id", "access_token", "sid", "h5.youzan.com"):
        assert marker in readme


def test_smalltalk_docs_cover_manifest_commands_and_provider_boundary() -> None:
    readme   = _read("smalltalk")
    manifest = _manifest("smalltalk")
    assert all(command["admin_only"] for command in manifest["commands"])
    for marker in ("/记忆", "/对话", "/删除对话", "chat.reply", "voice.synthesize_text"):
        assert marker in readme
    assert len(manifest["commands"]) == 3
    assert "笑话" not in readme


def test_voice_docs_use_exact_secret_hierarchy() -> None:
    readme = _read("voice")
    for marker in ("config/secrets.json", '"voice"', '"subscription_key"', '"region"'):
        assert marker in readme
    assert _manifest("voice")["commands"][0]["admin_only"] is True


def test_choice_docs_and_runtime_define_replacement_modes() -> None:
    readme = _read("choice")
    assert "有放回抽样" in readme and "唯一项多选" in readme
    assert len(make_choice(["a", "b"], 5, unique=False)) == 5
    try:
        make_choice(["a", "b"], 3, unique=True)
    except ValueError as exc:
        assert "不同选项数量" in str(exc)
    else:  # pragma: no cover - contract failure branch
        raise AssertionError("unique overdraw must fail")
