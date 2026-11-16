"""两个插件的真实运行验证，作为 chat_eval 的 ``life`` 和 ``consolidate`` 子命令。

``life``：用真实端点和真实模型跑一次 xiaoqing_life 的上网（或睡前整理）。

在隔离的数据目录（默认 ``work/xiaoqing_life/<时间戳>/``）里运行，不碰线上数据，也不修改
``config/config.json`` 与 ``config/secrets.json``（只读）。本地配置里没有 ``plugins.xiaoqing_life`` 时，
用 ``--routes-from xiaoqing_chat`` 在内存里复用那个插件的 ``reasoning`` / ``vision`` 路由。

    python -m scripts.chat_eval life --activity hot_scroll
    python -m scripts.chat_eval life --activity interest_dive --query 天文摄影
    python -m scripts.chat_eval life --data-dir work/xiaoqing_life/x --activity hot_scroll   # 第二次
    python -m scripts.chat_eval life --no-ai --activity hot_scroll                          # 只看取材
    python -m scripts.chat_eval life --activity wind_down --advance-minutes 40
    python -m scripts.chat_eval life --activity thread_continue --thread "猎户座怎么拍"      # 研究循环
    python -m scripts.chat_eval life --activity thread_continue --thread "水珠为什么在热锅上跑" --kind fact --done-when "知道原因" --query "莱顿弗罗斯特效应"
    python -m scripts.chat_eval life --data-dir work/xiaoqing_life/x --activity work_on    # 整理作品
    python -m scripts.chat_eval life --activity thread_continue --thread "…" --bili-credential 凭证文件.json

``thread_continue --thread "<标题>"``：隔离目录里找标题匹配的小课题，没有就先开一个，再逐轮打印研究的行动、
模型原始 JSON 与本地核对结果（要点保留 / 丢弃、更正、下一步）。``work_on`` 要在已有进行中作品的数据目录里跑
（先用睡前整理开一个），打印条目的增删改和被丢弃的 op。``--bili-credential <文件>`` 只读地把一份已保存的
凭证文件复制到隔离目录的 secrets/ 下（用来验证字幕），**不打印它的任何内容**；原文件不会被修改。

输出取材条目（数量与几个标题）、模型原始 JSON、被本地核对丢弃的条目及原因、最终落盘的内容，
并写 ``result.json``。不打印任何密钥。

``consolidate``：在一份数据副本上跑统一记忆的迁移和整理，用真实模型，输出每个范围的数字和整理好的页面。

用法::

    python -m scripts.chat_eval consolidate --data-dir <数据根目录的副本>

数据根目录里要有 ``xiaoqing_chat/``（聊天历史和旧数据），结果写进同一目录下的 ``xiaoqing_memory/``，
页面另存为 ``xiaoqing_memory/pages.txt``。只读聊天的旧文件，不改不删；请只在副本上运行。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import dataclasses
import json
import os
import random
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from core.ai import complete_configured_route
from core.clock import now_in_configured_timezone
from plugins.xiaoqing_chat.memory.client import event_key, message_event
from plugins.xiaoqing_chat.memory.legacy import legacy_ops
from plugins.xiaoqing_chat.memory.memory import MemoryStore as ChatHistory
from plugins.xiaoqing_chat.memory.person_profile import get_profile_generation
from plugins.xiaoqing_life import digest as digest_mod
from plugins.xiaoqing_life import memes as memes_mod
from plugins.xiaoqing_life import persona_bridge, planner, research
from plugins.xiaoqing_life import threads as thread_ops
from plugins.xiaoqing_life.activity import SessionDeps
from plugins.xiaoqing_life.bili_auth import BiliCredential
from plugins.xiaoqing_life.config import SOURCE_DEFAULTS, parse_life_config, route_configured
from plugins.xiaoqing_life.fetching import gather, interest_topics, open_items
from plugins.xiaoqing_life.lifecycle import seed_interests
from plugins.xiaoqing_life.safety import is_blocked, is_profane, quote_in_source
from plugins.xiaoqing_life.session import run_session
from plugins.xiaoqing_life.sources.base import BoundedFetcher, SourceItem
from plugins.xiaoqing_life.stores import LifeStore
from plugins.xiaoqing_life.wind_down import run_wind_down
from plugins.xiaoqing_life.works import MAX_OPS
from plugins.xiaoqing_memory.commands import render_page
from plugins.xiaoqing_memory.config import parse_memory_config
from plugins.xiaoqing_memory.consolidate.runner import Deps, consolidate_scope, consolidate_self
from plugins.xiaoqing_memory.model import GLOBAL
from plugins.xiaoqing_memory.ops import apply_record
from plugins.xiaoqing_memory.scopes import CHAT, Caller
from plugins.xiaoqing_memory.store import MemoryStore

PROJECT_ROOT    = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG  = PROJECT_ROOT / "config" / "config.json"
DEFAULT_SECRETS = PROJECT_ROOT / "config" / "secrets.json"


def _read_json(path: Path) -> dict[str, Any]:
    """本机配置原样读出（只读）。"""

    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


# ──────────────────── life：xiaoqing_life 的一次上网 ────────────────────

PLUGIN       = "xiaoqing_life"
BILI_SOURCES = tuple(name for name in SOURCE_DEFAULTS if name.startswith("bili_"))
ACTIVITIES   = (
    "hot_scroll",
    "interest_dive",
    "thread_continue",
    "term_lookup",
    "work_on",
    "wind_down",
)


def _build_config(routes_from: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取只读配置；需要时把别的插件的路由复制成 xiaoqing_life 的（仅内存）。"""

    config, secrets = _read_json(DEFAULT_CONFIG), _read_json(DEFAULT_SECRETS)
    plugins         = config.setdefault("plugins", {})
    life            = plugins.setdefault(PLUGIN, {})
    life["enabled"] = True
    if routes_from:
        routes     = plugins.get(routes_from, {}).get("ai", {}).get("routes", {})
        life["ai"] = {
            "routes": {k: copy.deepcopy(routes[k]) for k in ("reasoning", "vision") if k in routes}
        }
    return config, secrets


def _stub_context(config: dict[str, Any]) -> Any:
    snapshot = SimpleNamespace(config=config, secrets={})
    return SimpleNamespace(get_settings_snapshot=lambda: snapshot)


def _drops(
    raw: dict[str, Any], items: list[SourceItem], result: digest_mod.DigestResult
) -> dict[str, Any]:
    """把模型候选与通过核对的结果对照，给每个被丢弃的候选一个大致的原因。

    只按核对里最常见的几步推断（引用、摘录、屏蔽词）；重复、暴力用语、配置里的屏蔽词、
    超出条数这些都归到 ``other``。真正的核对以 ``digest.validate`` 为准。
    """

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
            why = "other"
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
            why = "other"
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
        "threads"  : [{"title": t["title"], "status": t["status"]} for t in store.threads],
        "seen"     : len(store.seen),
        "memes"    : {s[:6]: m["status"] for s, m in store.memes.items()},
        "snapshots": len(store.snapshots),
        "works"    : [
            {"title": w["title"], "status": w["status"], "entries": len(w["entries"])}
            for w in store.works
        ],
        "methods"        : len(store.methods),
        "dossiers"       : len(store.dossiers),
        "journal"        : len(store.journal),
        "llm_calls_today": store.state.get("llm_calls_today"),
    }


async def _life_main(args: argparse.Namespace) -> int:
    config, secrets = _build_config(args.routes_from)
    options: dict[str, Any] = {**config["plugins"][PLUGIN], "enabled": True}
    if args.no_bili:
        # B站 风控期间：关掉全部 B站 来源，研究循环第一轮就会改查百科 / 维基。
        options["sources"] = {
            **(options.get("sources") or {}),
            **dict.fromkeys(BILI_SOURCES, False),
        }
    life_cfg = parse_life_config(options)
    context  = _stub_context(config)
    stamp    = time.strftime("%Y%m%d-%H%M%S")
    data_dir = (
        Path(args.data_dir) if args.data_dir else PROJECT_ROOT / "work" / "xiaoqing_life" / stamp
    )
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

    credential = BiliCredential(store.data_dir)
    if args.bili_credential:
        _install_credential(Path(args.bili_credential), credential)
        result["bili_credential"] = {"installed": True, "usable": credential.usable()}
        print("已复制凭证文件到隔离目录（内容不打印）；可用：", credential.usable())

    async with aiohttp.ClientSession() as session:
        fetch = BoundedFetcher(session, credential)

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
            raw_box["rounds"]                = raw_box.get("rounds", 0) + 1
            print(
                f"模型原始 JSON（第 {raw_box['rounds']} 次）：",
                json.dumps(raw, ensure_ascii=False)[:3500],
            )
            if args.activity == "thread_continue":
                thread = thread_ops.get_thread(store.threads, str(raw_box.get("thread_id")))
                if thread is not None:
                    report = _research_report(raw, items, thread, life_cfg.blocked_keywords)
                    result.setdefault("research_rounds", []).append(report)
                    print("本地核对：", json.dumps(report, ensure_ascii=False))

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
            subtitles_ok  = credential.usable,
        )
        seed_interests(store.interests, life_cfg.seed_interests, moment.timestamp())

        if args.activity == "wind_down":
            line  = digest_mod.moment_line(moment, presence.activity)
            wound = await run_wind_down(
                store,
                life_cfg,
                moment,
                complete,
                persona      = deps.persona,
                moment_line  = line,
                reasoning_ok = deps.reasoning_ok,
            )
            result["wind_down"] = dataclasses.asdict(wound) | {
                "diary"  : store.state.get("diary"),
                "journal": store.journal[-2:],
            }
        else:
            decision           = _decision(args, store, life_cfg, moment, presence, rng)
            result["decision"] = decision.record(moment.timestamp()) | {"params": decision.params}
            print("决策：", result["decision"])
            raw_box["thread_id"] = decision.params.get("thread_id", "")
            if args.activity in ("thread_continue", "work_on") and args.no_ai:
                raise SystemExit("--no-ai 只用于看取材；研究和整理作品需要模型")
            if args.activity == "thread_continue":
                thread = thread_ops.get_thread(store.threads, str(decision.params["thread_id"]))
                if thread is not None:
                    opening = research.first_action(
                        life_cfg, store.state, thread, moment.timestamp()
                    )
                    print("第一轮行动：", opening)
            work_before = copy.deepcopy(store.works)
            if args.no_ai:
                items, failures = await gather(life_cfg, decision, fetch)
                on_items(items)
                opened = await open_items(
                    life_cfg,
                    fetch,
                    items[: life_cfg.max_items_per_session],
                    interest_topics(store, life_cfg, moment.timestamp()),
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
                if args.activity == "thread_continue":
                    thread = thread_ops.get_thread(store.threads, str(decision.params["thread_id"]))
                    result["thread_after"] = {
                        "status"       : thread and thread["status"],
                        "actions"      : thread and thread.get("actions"),
                        "open_question": thread and thread.get("open_question"),
                        "notes"        : thread and [n.get("note") for n in thread["notes"]],
                        "corrections"  : thread and thread.get("corrections"),
                    }
                    print(
                        "研究结果：",
                        json.dumps(result["thread_after"], ensure_ascii=False, default=str),
                    )
                elif args.activity == "work_on":
                    result["work_ops"] = _work_report(
                        work_before,
                        store.works,
                        str(decision.params["work_id"]),
                        raw_box.get("raw"),
                    )
                    print("整理结果：", json.dumps(result["work_ops"], ensure_ascii=False))
                elif "raw" in raw_box:  # term_lookup 不调模型，raw_box 里只有 thread_id
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
                        download = deps.download,
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
    if args.activity == "interest_dive" and args.chat_topic:
        # 去看睡前挑出来的群聊话题：搜索词取她挑的时候写的（没有就用 --query / 话题本身）。
        picks                = store.state.get("chat_topics", [])
        pick: dict[str, Any] = next((p for p in picks if p.get("topic") == args.chat_topic), {})
        query                = args.query or str(pick.get("query") or args.chat_topic)
        return planner.Decision(
            "interest_dive",
            "forced",
            {},
            {"query": query, "explored": True, "chat_topic": args.chat_topic},
        )
    if args.activity == "interest_dive":
        query = (
            args.query
            or planner.choose_topic(cfg, store.state, store.interests, moment.timestamp(), rng)[0]
        )
        return planner.Decision("interest_dive", "forced", {}, {"query": query})
    if args.activity == "thread_continue":
        thread = _pick_thread(args, store, cfg, moment)
        return planner.Decision(
            "thread_continue",
            "forced",
            {},
            {"thread_id": thread["id"], "query": args.query or thread["next_query"]},
        )
    if args.activity == "work_on":
        mine = [w for w in store.works if w["status"] == "drafting"]
        if args.work:
            mine = [w for w in mine if args.work in (w["id"], w["title"])] or [
                w for w in mine if args.work in w["title"]
            ]
        if not mine:
            raise SystemExit("没有进行中的作品可整理（先用 wind_down 开一个，或换一个 --data-dir）")
        return planner.Decision(
            "work_on", "forced", {}, {"work_id": mine[0]["id"], "query": mine[0]["title"]}
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


def _pick_thread(args: Any, store: LifeStore, cfg: Any, moment: datetime) -> dict[str, Any]:
    """标题匹配的小课题；没有就在隔离目录里先开一个（没给 --thread 时取第一个可续做的）。"""

    live = [t for t in store.threads if t["status"] in ("open", "parked")]
    if args.thread:
        found = next(
            (t for t in live if args.thread in t["title"] or t["title"] in args.thread), None
        )
        if found is not None:
            return dict(found)
        opened = thread_ops.open_thread(
            store.threads,
            cfg,
            title=args.thread,
            why="脚本验证用",
            next_query=args.query or args.thread,
            now=moment.timestamp(),
            topic=thread_ops.pick_topic(store.interests, args.topic, args.thread, args.query),
            done_when=args.done_when,
            kind=args.kind,
        )
        if opened is None:
            raise SystemExit("小课题名额已满，无法开新的")
        print("已在隔离目录开了小课题：", opened["title"])
        return dict(opened)
    if not live:
        raise SystemExit('没有可续做的小课题；用 --thread "<标题>" 先开一个')
    return dict(live[0])


def _install_credential(source: Path, credential: BiliCredential) -> None:
    """只读地复制凭证文件到隔离目录（0600）；不解析、不打印内容，原文件不动。"""

    if not source.is_file():
        raise SystemExit("--bili-credential 指向的文件不存在")
    credential.path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, credential.path)
    os.chmod(credential.path, 0o600)


def _research_report(
    raw: dict[str, Any], items: list[SourceItem], thread: dict[str, Any], blocked: Any
) -> dict[str, Any]:
    """这一轮模型输出经本地核对后的样子：要点留了几条、更正、下一步、状态。"""

    verdict = research.validate_round(raw, items, thread, tuple(blocked))
    rows    = raw.get("findings")
    return {
        "findings_raw"  : len(rows) if isinstance(rows, list) else 0,
        "findings_kept" : [f.text for f in verdict.findings],
        "revises_valid" : [f.revises for f in verdict.findings if f.revises],
        "looked_at_kept": len(verdict.digest.looked_at),
        "status"        : verdict.status,
        "open_question" : verdict.open_question,
        "next"          : {"action": verdict.next.action, "arg": verdict.next.arg},
    }


def _work_report(
    before: list[dict[str, Any]], after: list[dict[str, Any]], work_id: str, raw: Any
) -> dict[str, Any]:
    """作品条目的增删改，以及被丢弃的 op 数。"""

    old: dict[str, Any] = next((w for w in before if w["id"] == work_id), {"entries": []})
    new: dict[str, Any] = next((w for w in after if w["id"] == work_id), {"entries": []})
    was                 = {e["id"]: e for e in old["entries"]}
    now                 = {e["id"]: e for e in new["entries"]}
    added               = [e["text"] for i, e in now.items() if i not in was]
    changed             = [e["text"] for i, e in now.items() if i in was and e != was[i]]
    removed             = [e["text"] for i, e in was.items() if i not in now]
    asked               = min(MAX_OPS, len(raw.get("ops") or [])) if isinstance(raw, dict) else 0
    kept                = len(added) + len(changed) + len(removed)
    return {
        "ops_asked"  : asked,
        "ops_kept"   : kept,
        "ops_dropped": max(0, asked - kept),
        "added"      : added,
        "revised"    : changed,
        "removed"    : removed,
        "status"     : new.get("status"),
        "intro"      : new.get("intro"),
    }


def add_life_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--activity",
        choices = ACTIVITIES,
        default = "",
        help    = "强制选择的活动；不给就走真实的门控与打分（视同手动运行）",
    )
    parser.add_argument("--query", default="")
    parser.add_argument("--source", default="bili_popular")
    parser.add_argument("--terms", default="")
    parser.add_argument(
        "--thread", default="", help="thread_continue：要续做（没有就先开）的小课题标题"
    )
    parser.add_argument(
        "--topic", default="", help="thread_continue：新开小课题的话题（正式流程里由消化给出）"
    )
    parser.add_argument(
        "--done-when", default="", help="thread_continue：新开小课题“弄明白到什么程度就算完”"
    )
    parser.add_argument(
        "--chat-topic",
        default = "",
        help    = "interest_dive：去看睡前挑出的群聊话题（搜索词取挑的时候写的，或 --query）",
    )
    parser.add_argument(
        "--kind",
        default = "",
        choices = ("", "fact", "practice"),
        help    = "thread_continue：新开小课题的问题类型（fact 先查百科维基，practice 先搜 B站）",
    )
    parser.add_argument(
        "--work", default="", help="work_on：作品 ID 或标题（缺省取第一个进行中的）"
    )
    parser.add_argument(
        "--bili-credential",
        default = "",
        help    = "已保存的 B站 凭证文件（只读复制到隔离目录，用来验证字幕；不打印内容）",
    )
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument(
        "--no-bili",
        action = "store_true",
        help   = "关掉全部 B站 来源（风控期间用，研究改查百科 / 维基）",
    )
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


def run_life(args: argparse.Namespace) -> int:
    return asyncio.run(_life_main(args))


# ──────────────────── consolidate：统一记忆的迁移和整理 ────────────────────


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


async def _consolidate_main(args: argparse.Namespace) -> int:
    data_dir   = Path(args.data_dir).resolve()
    chat_dir   = data_dir / "xiaoqing_chat"
    memory_dir = data_dir / "xiaoqing_memory"
    if not chat_dir.is_dir():
        print(f"没有 {chat_dir}")
        return 1
    config   = _read_json(Path(args.config))
    secrets  = _read_json(Path(args.secrets))
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


def add_consolidate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", required=True, help="数据根目录的副本（含 xiaoqing_chat/）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    parser.add_argument("--scope", action="append", help="只整理这些范围（可重复）")
    parser.add_argument("--rewrites", type=int, default=12, help="每个范围最多重写几份页面")


def run_consolidate(args: argparse.Namespace) -> int:
    return asyncio.run(_consolidate_main(args))
