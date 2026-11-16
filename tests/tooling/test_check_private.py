"""提交前检查：本机私有配置里的值出现在要提交的文件里就拦下，只报标签；公开示例里的值不算。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

# 检查脚本是 pre-commit 钩子的实现，放在 .githooks/ 里，不是可导入的包，按路径加载。
_SPEC = importlib.util.spec_from_file_location(
    "check_private", Path(__file__).resolve().parents[2] / ".githooks" / "check_private.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_check_private          = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _check_private
_SPEC.loader.exec_module(_check_private)
find_leaks      = _check_private.find_leaks
private_needles = _check_private.private_needles


def _config_dir(tmp_path: Path) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.json").write_text(
        json.dumps(
            {
                "default_group_ids": [123456789],
                "timezone"         : "Asia/Shanghai",
                "plugins"          : {
                    "codex"     : {"default_cwd": "C:/Users/alice/Work"},
                    "earthquake": {"schedules": {"poll": {"group_ids": [987654321]}}},
                },
            }
        ),
        encoding="utf-8",
    )
    (config / "secrets.json").write_text(
        json.dumps(
            {
                "admin_user_ids": [1122334455],
                "ai"            : {"providers": {"openai": {"api_key": "sk-real-secret-value"}}},
                "plugins"       : {"voice": {"region": "eastasia", "pin": "42"}},
            }
        ),
        encoding="utf-8",
    )
    (config / "config.json.example").write_text(
        json.dumps({"timezone": "Asia/Shanghai"}), encoding="utf-8"
    )
    (config / "secrets.json.example").write_text(
        json.dumps({"plugins": {"voice": {"region": "eastasia"}}}), encoding="utf-8"
    )
    (config / "private_terms.txt").write_text("# 注释行\n阿梨的真名\n\n", encoding="utf-8")
    return config


def test_private_values_come_from_local_config_secrets_and_terms(tmp_path: Path) -> None:
    needles = private_needles(_config_dir(tmp_path))
    assert set(needles.values()) == {
        "123456789",
        "987654321",
        "Users/alice",
        "Users\\alice",
        "Users\\\\alice",
        "1122334455",
        "sk-real-secret-value",
        "阿梨的真名",
    }
    # 示例里公开的值、太短的值都不算私有。
    assert "eastasia" not in needles.values() and "42" not in needles.values()


def test_user_directory_is_found_in_every_path_spelling(tmp_path: Path) -> None:
    config = _config_dir(tmp_path)
    (config / "config.json").write_text(
        json.dumps({"a": "C:\\Users\\alice\\Work", "b": "/home/bob/work", "c": "/home/user/x"}),
        encoding="utf-8",
    )
    needles = private_needles(config)
    files   = {
        "slash.md"  : "C:/Users/alice/Work",
        "back.md"   : "C:\\Users\\alice\\Work",
        "escaped.py": 'p = "C:\\\\Users\\\\alice"',
        "linux.md"  : "cd /home/bob/work",
        "public.md" : "cd /home/user/x",
    }
    assert {name for name, _ in find_leaks(files, needles)} == {
        "slash.md",
        "back.md",
        "escaped.py",
        "linux.md",
    }


def test_leaks_are_reported_by_label_without_the_value(tmp_path: Path) -> None:
    needles = private_needles(_config_dir(tmp_path))
    leaks   = find_leaks(
        {
            "tests/test_a.py": 'GROUP = 987654321  # 抄进来的真实群号\nname = "阿梨的真名"',
            "docs/b.md"      : "时区写 Asia/Shanghai，区域 eastasia",
        },
        needles,
    )
    assert leaks == [
        ("tests/test_a.py", "config.plugins.earthquake.schedules.poll.group_ids[0]"),
        ("tests/test_a.py", "private_terms.txt:2"),
    ]
    assert all("987654321" not in label for _, label in leaks)


def test_binary_files_are_searched_as_bytes_in_utf8_and_utf16(tmp_path: Path) -> None:
    """二进制文件里写着的值（元数据）照样查得到，UTF-16 的两种字节序也算。"""

    needles = private_needles(_config_dir(tmp_path))
    files   = {
        "utf8.psd"   : b"8BPS\0\x01<xmp:Author>987654321</xmp:Author>\0",
        "utf16be.psd": b"8BPS\0\x01luni" + "阿梨的真名".encode("utf-16-be") + b"\0\0",
        "utf16le.bin": b"MZ\0\0" + "id=987654321;".encode("utf-16-le"),
        "clean.png"  : b"\x89PNG\0\0\x10\x11\x12",
    }

    assert {name for name, _ in find_leaks(files, needles)} == {
        "utf8.psd",
        "utf16be.psd",
        "utf16le.bin",
    }


def test_digits_inside_a_longer_run_of_pixel_bytes_are_not_a_leak(tmp_path: Path) -> None:
    """压缩像素数据里碰巧出现的一段：命中处前后紧挨着字母或数字，不是写在文件里的一个值。"""

    needles = private_needles(_config_dir(tmp_path))
    pixels  = b"8BPS\0\x02" + b"/0123456789876543210:;<" + b"\0\xff"

    assert b"987654321" in pixels
    assert find_leaks({"layer.psd": pixels}, needles) == []
    # 同样的字节放在文本文件里，仍按文字整篇查。
    assert find_leaks({"notes.md": pixels.replace(b"\0", b" ")}, needles) != []
