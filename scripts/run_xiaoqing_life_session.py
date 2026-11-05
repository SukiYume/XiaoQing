"""真实验证：用真实端点和真实模型跑一次 xiaoqing_life 的上网（或睡前整理）。

在隔离的数据目录（默认 ``work/xiaoqing_life/<时间戳>/``）里运行，不碰线上数据，也不修改
``config/config.json`` 与 ``config/secrets.json``（只读）。本地配置里没有 ``plugins.xiaoqing_life`` 时，
用 ``--routes-from xiaoqing_chat`` 在内存里复用那个插件的 ``reasoning`` / ``vision`` 路由。

    python scripts/run_xiaoqing_life_session.py --activity hot_scroll
    python scripts/run_xiaoqing_life_session.py --activity interest_dive --query 天文摄影
    python scripts/run_xiaoqing_life_session.py --data-dir work/xiaoqing_life/x --activity hot_scroll   # 第二次
    python scripts/run_xiaoqing_life_session.py --no-ai --activity hot_scroll                          # 只看取材
    python scripts/run_xiaoqing_life_session.py --activity wind_down --advance-minutes 40

输出取材条目（数量与几个标题）、模型原始 JSON、被本地核对丢弃的条目及原因、最终落盘的内容，
并写 ``result.json``。不打印任何密钥。
"""

from __future__ import annotations

# ruff: noqa: E402  (需要先把仓库根目录放进 sys.path 再导入项目模块)
import argparse
import asyncio
import copy
import dataclasses
import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402

from core.ai import complete_configured_route  # noqa: E402
from core.clock import now_in_configured_timezone  # noqa: E402
from plugins.xiaoqing_life import digest as digest_mod  # noqa: E402
from plugins.xiaoqing_life import memes as memes_mod  # noqa: E402
from plugins.xiaoqing_life import (
    persona_bridge,  # noqa: E402
    planner,  # noqa: E402
)
from plugins.xiaoqing_life.config import parse_life_config, route_configured  # noqa: E402
from plugins.xiaoqing_life.lifecycle import seed_interests  # noqa: E402
from plugins.xiaoqing_life.safety import is_blocked, is_profane, quote_in_source  # noqa: E402
from plugins.xiaoqing_life.session import (  # noqa: E402
    SessionDeps,
    _gather,
    _open_items,
    _topics,
    run_session,
)
from plugins.xiaoqing_life.sources.base import BoundedFetcher, SourceItem  # noqa: E402
from plugins.xiaoqing_life.stores import LifeStore  # noqa: E402
from plugins.xiaoqing_life.wind_down import run_wind_down  # noqa: E402

PLUGIN     = "xiaoqing_life"
ACTIVITIES = ("hot_scroll", "interest_dive", "thread_continue", "term_lookup", "wind_down")


def _load(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _build_config(routes_from: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取只读配置；需要时把别的插件的路由复制成 xiaoqing_life 的（仅内存）。"""

    config, secrets = _load("config.json"), _load("secrets.json")
    plugins         = config.setdefault("plugins", {})
    life            = plugins.setdefault(PLUGIN, {})
    life["enabled"] = True
    if routes_from:
        source     = plugins.get(routes_from, {}).get("ai", {})
        routes     = source.get("routes", {})
        life["ai"] = {
            "model_aliases": copy.deepcopy(source.get("model_aliases", {})),
            "routes": {k: copy.deepcopy(routes[k]) for k in ("reasoning", "vision") if k in routes},
        }
        secret_plugins = secrets.setdefault("plugins", {})
        if routes_from in secret_plugins:
            secret_plugins[PLUGIN] = copy.deepcopy(secret_plugins[routes_from])
    return config, secrets


def _stub_context(config: dict[str, Any]) -> Any:
    snapshot = SimpleNamespace(config=config, secrets={})
    return SimpleNamespace(get_settings_snapshot=lambda: snapshot)


def _drops(
    raw: dict[str, Any], items: list[SourceItem], result: digest_mod.DigestResult
) -> dict[str, Any]:
    """把模型候选与通过核对的结果对照，说明每个被丢弃的候选为什么没通过。"""

    texts                         = digest_mod.ref_texts(items)
    kept_terms                    = {t.term for t in result.terms}
    reasons: list[dict[str, str]] = []
    for row in raw.get("terms") or []:
        if not isinstance(row, dict) or str(row.get("term", ""))[:12] in kept_terms:
            continue
        refs = row.get("refs") if isinstance(row.get("refs"), list) else []
        if not refs or any(r not in texts for r in refs):
            why = "bad_refs"
        elif not quote_in_source(str(row.get("quote", "")), " ".join(texts[r] for r in refs)):
            why = "quote_not_in_source"
        elif not quote_in_source(str(row.get("term", "")), str(row.get("quote", ""))):
            why = "term_not_in_quote"
        elif is_profane(str(row.get("term", ""))) or is_blocked(str(row.get("term", ""))):
            why = "blocked_or_profane"
        else:
            why = "other(dup/limit/empty)"
        reasons.append({"kind": "term", "value": str(row.get("term"))[:20], "why": why})
    kept_patterns = {e.pattern for e in result.expressions}
    for row in raw.get("expressions") or []:
        if not isinstance(row, dict) or str(row.get("pattern", ""))[:100] in kept_patterns:
            continue
        refs = row.get("refs") if isinstance(row.get("refs"), list) else []
        if not refs or any(r not in texts for r in refs):
            why = "bad_refs"
        elif not quote_in_source(str(row.get("quote", "")), " ".join(texts[r] for r in refs)):
            why = "quote_not_in_source"
        else:
            why = "other(blocked/dup/limit)"
        reasons.append({"kind": "expression", "value": str(row.get("pattern"))[:30], "why": why})
    counts = {
        key: (len(raw.get(key) or []) if isinstance(raw.get(key), list) else 0)
        for key in ("looked_at", "terms", "expressions", "meme_pictures")
    }
    kept = {
        "looked_at"    : len(result.looked_at),
        "terms"        : len(result.terms),
        "expressions"  : len(result.expressions),
        "meme_pictures": len(result.memes),
    }
    return {"raw_counts": counts, "kept_counts": kept, "dropped": reasons}


def _snapshot(store: LifeStore) -> dict[str, Any]:
    return {
        "episodes"   : len(store.episodes),
        "terms"      : len(store.terms),
        "expressions": len(store.expressions),
        "interests"  : {
            k: round(len(v.get("evidence", [])), 0)
            for k, v in store.interests.items()
            if v.get("evidence")
        },
        "threads"        : [{"title": t["title"], "status": t["status"]} for t in store.threads],
        "seen"           : len(store.seen),
        "memes"          : {s[:6]: m["status"] for s, m in store.memes.items()},
        "snapshots"      : len(store.snapshots),
        "journal"        : len(store.journal),
        "llm_calls_today": store.state.get("llm_calls_today"),
    }


async def _main(args: argparse.Namespace) -> int:
    config, secrets = _build_config(args.routes_from)
    life_cfg = parse_life_config({**config["plugins"][PLUGIN], "enabled": True})
    context  = _stub_context(config)
    stamp    = time.strftime("%Y%m%d-%H%M%S")
    data_dir = Path(args.data_dir) if args.data_dir else ROOT / "work" / "xiaoqing_life" / stamp
    data_dir.mkdir(parents=True, exist_ok=True)
    store = LifeStore(data_dir / "data")
    store.load()
    offset = timedelta(minutes=args.advance_minutes)
    clock                  = lambda: now_in_configured_timezone(context) + offset  # noqa: E731
    rng                    = random.Random(args.seed)
    result: dict[str, Any] = {
        "data_dir": str(data_dir),
        "activity": args.activity,
        "no_ai"   : args.no_ai,
    }
    before = _snapshot(store)

    async with aiohttp.ClientSession() as session:
        fetch = BoundedFetcher(session)

        async def complete(
            route: str, messages: list[dict[str, Any]], modalities: tuple[str, ...] = ("text",)
        ) -> str:
            reply = await complete_configured_route(
                session             = session,
                config              = config,
                secrets             = secrets,
                plugin_name         = PLUGIN,
                route_name          = route,
                messages            = messages,
                required_modalities = modalities,
            )
            return str(reply.content)

        moment                  = clock()
        presence                = persona_bridge.availability(context, life_cfg, moment)
        raw_box: dict[str, Any] = {}

        def on_items(items: list[SourceItem]) -> None:
            result["items"] = {
                "count"  : len(items),
                "titles" : [i.title[:40] for i in items[:6]],
                "sources": sorted({i.source for i in items}),
            }
            print(f"取到条目 {len(items)} 条：", " | ".join(i.title[:24] for i in items[:5]))

        def on_digest(raw: dict[str, Any], items: list[SourceItem]) -> None:
            raw_box["raw"], raw_box["items"] = raw, items
            print("模型原始 JSON：", json.dumps(raw, ensure_ascii=False)[:3500])

        async def vision(messages: list[dict[str, Any]]) -> str:
            return await complete("vision", messages, ("text", "image"))

        can_see = route_configured(context, "vision") and not args.no_ai
        deps    = SessionDeps(
            fetch         = fetch,
            complete      = lambda r, m: complete(r, m),
            rng           = rng,
            now           = clock,
            persona       = persona_bridge.persona_facts(context),
            reasoning_ok  = route_configured(context, "reasoning") and not args.no_ai,
            activity_text = presence.activity,
            download      = memes_mod.download_image if can_see else None,
            vision        = vision if can_see else None,
            on_items      = on_items,
            on_digest     = on_digest,
        )
        seed_interests(store.interests, life_cfg.seed_interests, moment.timestamp())

        if args.activity == "wind_down":
            line    = digest_mod.moment_line(moment, presence.activity)
            outcome = await run_wind_down(
                store,
                life_cfg,
                moment,
                complete,
                persona      = deps.persona,
                moment_line  = line,
                reasoning_ok = deps.reasoning_ok,
            )
            result["wind_down"] = dataclasses.asdict(outcome) | {
                "diary"  : store.state.get("diary"),
                "journal": store.journal[-2:],
            }
        else:
            decision           = _decision(args, store, life_cfg, moment, presence, rng)
            result["decision"] = decision.record(moment.timestamp()) | {"params": decision.params}
            print("决策：", result["decision"])
            if args.no_ai:
                items, failures = await _gather(life_cfg, decision, fetch)
                on_items(items)
                opened = await _open_items(
                    life_cfg,
                    fetch,
                    items[: life_cfg.max_items_per_session],
                    _topics(store, life_cfg, moment.timestamp()),
                )
                result["opened"] = [
                    {
                        "title"           : i.title[:30],
                        "uploader"        : i.uploader,
                        "duration_s"      : i.duration_s,
                        "tags"            : list(i.tags),
                        "dense_moment"    : i.dense_moment,
                        "danmaku_top"     : [(d.text, d.count) for d in i.danmaku[:5]],
                        "comments"        : len(i.comments),
                        "comment_pictures": sum(len(c.pictures) for c in i.comments),
                    }
                    for i in opened
                    if i.comments or i.danmaku
                ]
                result["source_failures"] = failures
            else:
                outcome           = await run_session(store, life_cfg, decision, deps)
                result["outcome"] = dataclasses.asdict(outcome)
                if raw_box:
                    verdict = digest_mod.validate(
                        raw_box["raw"],
                        raw_box["items"],
                        max_memes = life_cfg.max_vision_per_session,
                        blocked   = life_cfg.blocked_keywords,
                    )
                    result["validation"] = _drops(raw_box["raw"], raw_box["items"], verdict)
                    print("核对：", json.dumps(result["validation"], ensure_ascii=False))
                if args.force_memes and raw_box and deps.download and deps.vision:
                    pics = [
                        f"{c.ref}#{n}"
                        for i in raw_box["items"]
                        for c in i.comments
                        for n in range(len(c.pictures))
                    ]
                    picks           = [(ref, "脚本强制送审") for ref in pics[: args.force_memes]]
                    life_cfg_forced = parse_life_config(
                        {
                            **config["plugins"][PLUGIN],
                            "enabled"               : True,
                            "max_vision_per_session": args.force_memes,
                        }
                    )
                    added = await memes_mod.collect(
                        store,
                        life_cfg_forced,
                        raw_box["items"],
                        picks,
                        download = lambda u: deps.download(u, life_cfg.max_meme_bytes),
                        vision   = deps.vision,
                        day      = moment.strftime("%Y-%m-%d"),
                        now      = lambda: clock().timestamp(),
                    )
                    result["forced_memes"] = {"submitted": len(picks), "accepted": added}
                    print("强制送审梗图：", result["forced_memes"])
                result["committed"] = {
                    "episodes": [
                        {
                            k: e.get(k)
                            for k in (
                                "title",
                                "uploader",
                                "gist",
                                "reaction",
                                "feeling",
                                "topics",
                                "dense_moment",
                            )
                        }
                        for e in store.episodes[-4:]
                    ],
                    "terms": [
                        {
                            "term"   : t["term"],
                            "meaning": t["meaning"],
                            "status" : t["status"],
                            "sources": len(t["sources"]),
                        }
                        for t in list(store.terms.values())[-5:]
                    ],
                    "expressions": [
                        {"situation": e["situation"], "pattern": e["pattern"]}
                        for e in store.expressions[-3:]
                    ],
                    "memes": [
                        {
                            "label"      : m["label"],
                            "description": m["description"],
                            "status"     : m["status"],
                        }
                        for m in store.memes.values()
                    ],
                }
    result["before"], result["after"] = before, _snapshot(store)
    await store.flush()
    (data_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {k: result[k] for k in ("activity", "outcome", "wind_down", "after") if k in result},
            ensure_ascii = False,
            indent       = 2,
            default      = str,
        )
    )
    print("结果已写入", data_dir / "result.json")
    return 0


def _decision(
    args: Any, store: LifeStore, cfg: Any, moment: datetime, presence: Any, rng: random.Random
) -> planner.Decision:
    """``--activity`` 强制选择；否则走真实的门控与打分（视同手动运行）。"""

    if args.activity == "hot_scroll":
        return planner.Decision("hot_scroll", "forced", {}, {"source": args.source})
    if args.activity == "interest_dive":
        query = args.query or planner.choose_topic(
            cfg, store.state, store.interests, moment.timestamp(), rng
        )
        return planner.Decision("interest_dive", "forced", {}, {"query": query})
    if args.activity == "thread_continue":
        open_threads = [t for t in store.threads if t["status"] == "open"]
        thread       = open_threads[0] if open_threads else None
        if thread is None:
            raise SystemExit("没有进行中的小课题可续做")
        return planner.Decision(
            "thread_continue",
            "forced",
            {},
            {"thread_id": thread["id"], "query": args.query or thread["next_query"]},
        )
    if args.activity == "term_lookup":
        terms                       = [t for t in (args.terms or "").split(",") if t]
        store.state["lookup_queue"] = list(dict.fromkeys([*store.state["lookup_queue"], *terms]))
        return planner.Decision("term_lookup", "forced", {}, {"terms": terms[:3]})
    return planner.decide(
        cfg,
        store.state,
        store.episodes,
        store.threads,
        store.interests,
        moment,
        presence,
        rng,
        manual=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--activity", choices=ACTIVITIES, default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--source", default="bili_popular")
    parser.add_argument("--terms", default="")
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument(
        "--force-memes",
        type    = int,
        default = 0,
        help    = "消化没选图时，强制把前 N 张评论图送去视觉判定（验证用）",
    )
    parser.add_argument(
        "--routes-from", default="", help="复用该插件（如 xiaoqing_chat）的 reasoning/vision 路由"
    )
    parser.add_argument("--data-dir", default="")
    parser.add_argument(
        "--advance-minutes",
        type    = float,
        default = 0.0,
        help    = "把脚本时钟向前拨（睡前整理要看到今天的经历）",
    )
    parser.add_argument("--seed", type=int, default=None)
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
