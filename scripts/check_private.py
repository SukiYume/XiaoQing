"""提交前检查：要提交的文件里不能出现本机私有配置里的值。

私有值从三处读，都只在本机、不进仓库：

- ``config/secrets.json`` 里的凭据和身份（长度够的字符串、数字）；
- ``config/config.json`` 里的群号、账号这类长数字，以及本机路径里的用户目录；
- ``config/private_terms.txt``（可选）：一行一个不该公开的词，比如群友的昵称、单位名。

两份 ``.example`` 里本来就公开的值不算。发现时只报文件和标签，不打印值本身。

文本文件整篇按文字查。二进制文件（图片、PSD 这类，内容里有 NUL 字节）按字节查，元数据里常见的
UTF-8 和 UTF-16 两种写法都查；命中处前后紧挨着字母或数字的不算，那是压缩像素数据里碰巧出现
的一段（比如一条渐变里的连续数字），不是写在文件里的一个值。

用法::

    python scripts/check_private.py            # 检查暂存区（pre-commit 钩子用）
    python scripts/check_private.py --all      # 检查全部已跟踪和未跟踪的文件
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

REPO_DIR    = Path(__file__).resolve().parents[1]
CONFIG_DIR  = REPO_DIR / "config"
TERMS_FILE  = "private_terms.txt"
_MIN_SECRET = 8
_LONG_ID_RE = re.compile(r"^\d{6,}$")
_HOME_RE    = re.compile(r"[/\\](Users|home)[/\\]([^/\\]+)", re.IGNORECASE)
_SKIP_HOMES = {"user", "username", "you", "testuser", "example", "public", "default"}
# 同一个用户目录在文件里的三种写法：正斜杠、反斜杠，以及 Python / JSON 字符串里转义过的反斜杠。
_HOME_SEPARATORS = {"/": "", "\\": ", backslash", "\\\\": ", escaped backslash"}
# 二进制文件里文字的写法：元数据多是 UTF-8 或 UTF-16（PSD 的图层名是大端，Windows 上的多是小端）。
_BINARY_TEXT_ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")


def _leaves(value: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _leaves(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, f"{path}[{index}]")
    else:
        yield path, value


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _public_values(config_dir: Path) -> set[str]:
    public: set[str] = set()
    for name in ("config.json.example", "secrets.json.example"):
        public |= {str(value) for _, value in _leaves(_read_json(config_dir / name))}
    return public


def private_needles(config_dir: Path = CONFIG_DIR) -> dict[str, str]:
    """标签 → 私有值。标签只是键路径或名单行号，可以打印。"""

    public                  = _public_values(config_dir)
    needles: dict[str, str] = {}
    for path, value in _leaves(_read_json(config_dir / "secrets.json")):
        if isinstance(value, bool) or value is None:
            continue
        text = str(value).strip()
        if text in public:
            continue
        if (isinstance(value, str) and len(text) >= _MIN_SECRET) or _LONG_ID_RE.match(text):
            needles[f"secrets.{path}"] = text
    for path, value in _leaves(_read_json(config_dir / "config.json")):
        if isinstance(value, bool) or value is None:
            continue
        text = str(value).strip()
        if text in public:
            continue
        if _LONG_ID_RE.match(text):
            needles[f"config.{path}"] = text
        for root, home in _HOME_RE.findall(text):
            if home.lower() in _SKIP_HOMES:
                continue
            for separator, variant in _HOME_SEPARATORS.items():
                needles[f"config.{path} (user directory{variant})"] = f"{root}{separator}{home}"
    terms = config_dir / TERMS_FILE
    if terms.is_file():
        for number, line in enumerate(terms.read_text(encoding="utf-8").splitlines(), start=1):
            term = line.strip()
            if term and not term.startswith("#"):
                needles[f"{TERMS_FILE}:{number}"] = term
    return needles


def _is_word_unit(unit: bytes, encoding: str) -> bool:
    try:
        character = unit.decode(encoding)
    except UnicodeDecodeError:
        return False
    return len(character) == 1 and character.isascii() and character.isalnum()


def _stands_alone(blob: bytes, start: int, end: int, encoding: str) -> bool:
    """命中处前后不紧挨着字母或数字。"""

    width  = 1 if encoding == "utf-8" else 2
    before = blob[max(0, start - width) : start]
    after  = blob[end : end + width]
    return not (_is_word_unit(before, encoding) or _is_word_unit(after, encoding))


def _in_binary(blob: bytes, value: str) -> bool:
    for encoding in _BINARY_TEXT_ENCODINGS:
        needle = value.encode(encoding)
        start  = blob.find(needle)
        while start >= 0:
            if _stands_alone(blob, start, start + len(needle), encoding):
                return True
            start = blob.find(needle, start + 1)
    return False


def find_leaks(
    files: Mapping[str, str | bytes], needles: Mapping[str, str]
) -> list[tuple[str, str]]:
    """（文件, 标签），按文件名排序。内容是字节且含 NUL 的按二进制文件查，其余按文本查。"""

    found: list[tuple[str, str]] = []
    for name in sorted(files):
        content = files[name]
        if isinstance(content, bytes) and b"\0" in content:
            blob = content
            found.extend(
                (name, label) for label, value in needles.items() if _in_binary(blob, value)
            )
            continue
        text = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else content
        found.extend((name, label) for label, value in needles.items() if value in text)
    return found


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_DIR, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout


def _staged_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0"):
        if not name:
            continue
        blob = subprocess.run(
            ["git", "show", f":{name}"], cwd=REPO_DIR, capture_output=True, check=False
        ).stdout
        files[name] = blob
    return files


def _all_files() -> dict[str, bytes]:
    names = _git("ls-files", "-z").split("\0") + _git(
        "ls-files", "--others", "--exclude-standard", "-z"
    ).split("\0")
    files: dict[str, bytes] = {}
    for name in names:
        path = REPO_DIR / name
        if name and path.is_file():
            files[name] = path.read_bytes()
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true", help="检查全部已跟踪和未跟踪的文件")
    args    = parser.parse_args(argv)
    needles = private_needles()
    if not needles:
        return 0
    leaks = find_leaks(_all_files() if args.all else _staged_files(), needles)
    if not leaks:
        return 0
    print("这些文件里出现了本机私有配置里的值（只列标签，不打印值）：", file=sys.stderr)
    for name, label in leaks:
        print(f"  {name}: {label}", file=sys.stderr)
    print(
        "换成虚构的内容后再提交；确实不私密的词从 config/private_terms.txt 里删掉。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
