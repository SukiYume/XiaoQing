"""在一份数据副本上跑统一记忆的迁移和整理，用真实模型，输出每个范围的数字和整理好的页面。

用法::

    python scripts/run_xiaoqing_memory_consolidate.py --data-dir <数据根目录的副本>

数据根目录里要有 ``xiaoqing_chat/``（聊天历史和旧数据），结果写进同一目录下的 ``xiaoqing_memory/``，
页面另存为 ``xiaoqing_memory/pages.txt``。只读聊天的旧文件，不改不删；请只在副本上运行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.xiaoqing_chat.memory.client import event_key, message_event  # noqa: E402
from plugins.xiaoqing_chat.memory.legacy import legacy_ops  # noqa: E402
from plugins.xiaoqing_chat.memory.memory import MemoryStore as ChatHistory  # noqa: E402
from plugins.xiaoqing_chat.memory.person_profile import get_profile_generation  # noqa: E402
from plugins.xiaoqing_memory.commands import render_page  # noqa: E402
from plugins.xiaoqing_memory.config import parse_memory_config  # noqa: E402
from plugins.xiaoqing_memory.consolidate.runner import (  # noqa: E402
    Deps,
    consolidate_scope,
    consolidate_self,
)
from plugins.xiaoqing_memory.model import GLOBAL  # noqa: E402
from plugins.xiaoqing_memory.ops import apply_record  # noqa: E402
from plugins.xiaoqing_memory.scopes import CHAT, Caller  # noqa: E402
from plugins.xiaoqing_memory.store import MemoryStore  # noqa: E402

DEFAULT_CONFIG  = PROJECT_ROOT / "config" / "config.json"
DEFAULT_SECRETS = PROJECT_ROOT / "config" / "secrets.json"


def migrate(chat_dir: Path, store: MemoryStore, bot_name: str) -> dict[str, dict[str, int]]:
    """把每个会话的近期历史作为事件、旧数据作为导入操作写进统一记忆。"""

    caller                            = Caller(CHAT, "system", "")
    history                           = ChatHistory(chat_dir)
    counts: dict[str, dict[str, int]] = {}
    for path in sorted(chat_dir.glob("[gu][0-9]*.json")):
        chat_id = path.stem
        if not chat_id[1:].isdigit():
            continue
        generation = get_profile_generation(chat_dir, chat_id)
        messages   = history.get(chat_id)
        events     = [
            event
            for message in messages
            if (event := message_event(message, event_key(generation, message), bot_name))
        ]
        ops      = legacy_ops(chat_dir, chat_id)
        rejected = 0
        result   = apply_record(store, {"events": events}, caller, chat_id)
        rejected += len(result["rejected"])
        for start in range(0, len(ops), 40):
            result = apply_record(store, {"ops": ops[start : start + 40]}, caller, chat_id)
            rejected += len(result["rejected"])
        counts[chat_id] = {"events": len(events), "imports": len(ops), "rejected": rejected}
    return counts


def make_deps(
    config: dict[str, Any], secrets: dict[str, Any], session: Any, bot_name: str
) -> tuple[Deps, list[float]]:
    """整理用的依赖，以及一份随模型调用累加的耗时（秒）列表。"""

    from core.ai import complete_configured_route

    calls: list[float] = []

    async def complete(messages: list[dict[str, str]]) -> str:
        started = time.monotonic()
        result  = await complete_configured_route(
            session     = session,
            config      = config,
            secrets     = secrets,
            plugin_name = "xiaoqing_memory",
            route_name  = "reasoning",
            messages    = messages,
        )
        calls.append(time.monotonic() - started)
        return str(result.content)

    zone = ZoneInfo(str(config.get("timezone") or "Asia/Shanghai"))
    return Deps(complete=complete, bot_name=bot_name, zone=zone), calls


async def run(args: argparse.Namespace) -> int:
    import aiohttp

    data_dir   = Path(args.data_dir).resolve()
    chat_dir   = data_dir / "xiaoqing_chat"
    memory_dir = data_dir / "xiaoqing_memory"
    if not chat_dir.is_dir():
        print(f"没有 {chat_dir}")
        return 1
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    secrets = json.loads(Path(args.secrets).read_text(encoding="utf-8"))
    store    = MemoryStore(memory_dir)
    started  = time.monotonic()
    bot_name = str(config.get("bot_name") or "小青")
    counts   = migrate(chat_dir, store, bot_name)
    print(json.dumps({"migrate": counts}, ensure_ascii=False))
    cfg = parse_memory_config(
        {**config.get("plugins", {}).get("xiaoqing_memory", {}), "max_llm_calls_per_day": 10_000}
    )
    scopes = [scope for scope in store.known_scopes() if scope != GLOBAL]
    if args.scope:
        scopes = [scope for scope in scopes if scope in args.scope]
    async with aiohttp.ClientSession() as session:
        deps, calls = make_deps(config, secrets, session, bot_name)
        for scope in scopes:
            result = await consolidate_scope(
                store, cfg, deps, scope, force=True, rewrites=args.rewrites
            )
            print(json.dumps({"scope": scope, **result}, ensure_ascii=False))
        print(
            json.dumps(
                {"scope": GLOBAL, **await consolidate_self(store, cfg, deps, force=True)},
                ensure_ascii=False,
            )
        )
    rows   : list[str]      = []
    summary: dict[str, Any] = {}
    for scope in [*scopes, GLOBAL]:
        pages          = store.pages(scope)
        notes          = store.notes(scope)
        summary[scope] = {
            "notes_active": sum(1 for note in notes.values() if note.status == "active"),
            "pages"       : len(pages),
            "page_chars"  : {subject: page.char_count() for subject, page in pages.items()},
        }
        for page in pages.values():
            rows.append(render_page(page))
            rows.append("")
    (memory_dir / "pages.txt").write_text("\n".join(rows), encoding="utf-8")
    print(
        json.dumps(
            {
                "summary"     : summary,
                "llm_calls"   : len(calls),
                "llm_seconds" : round(sum(calls), 1),
                "wall_seconds": round(time.monotonic() - started, 1),
            },
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", required=True, help="数据根目录的副本（含 xiaoqing_chat/）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    parser.add_argument("--scope", action="append", help="只整理这些范围（可重复）")
    parser.add_argument("--rewrites", type=int, default=12, help="每个范围最多重写几份页面")
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
