"""看视频（弹幕）、梗图、睡前整理：解析、核对、落盘、导出与预算。"""

from __future__ import annotations

import io
import json
import random
import zlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.xiaoqing_life import digest, memes, planner, wind_down
from plugins.xiaoqing_life.session import SessionDeps, run_session
from plugins.xiaoqing_life.sources import bilibili, hotlists, watch
from plugins.xiaoqing_life.sources.base import (
    BoundedFetcher,
    SourceComment,
    SourceDanmaku,
    SourceError,
    SourceItem,
)
from plugins.xiaoqing_life.stores import LifeStore, default_state
from tests.helpers.http import AiohttpResponse, QueuedAiohttpSession
from tests.plugins.xiaoqing_life.support import (
    FREE,
    FakeAI,
    FakeFetcher,
    at,
    bili_reply,
    bili_video,
    comments_payload,
    make_cfg,
    popular_payload,
)

MOMENT = at(23, 0)
NOW    = MOMENT.timestamp()
DAY    = 86400.0


def _store(tmp_path: Path) -> LifeStore:
    store = LifeStore(tmp_path / "life")
    store.load()
    return store


# ───────────────────────────── 弹幕 ─────────────────────────────


def _rows() -> list[tuple[float, str]]:
    rows: list[tuple[float, str]] = []
    rows += [
        (65.0 + n, "哈哈哈哈哈" if n % 2 else "哈哈哈哈哈哈哈") for n in range(6)
    ]  # 重复刷屏句合并
    rows += [(70.0, "前方高能")] * 4
    rows += [(200.0, "！！！???"), (200.0, "加微信领福利"), (201.0, "x" * 60), (202.0, "nmsl")]
    rows += [(400.0 + n, f"独特弹幕{n}") for n in range(30)]
    return rows


def test_danmaku_filtering_counting_refs_and_dense_moments() -> None:
    danmaku, dense = watch.process_danmaku(_rows(), 3, [])
    texts = [d.text for d in danmaku]
    assert len(danmaku) == 20 and danmaku[0].ref == "d3.1" and danmaku[-1].ref == "d3.20"
    assert texts[0].startswith("哈哈哈") and danmaku[0].count == 6  # 长短不一的哈哈哈合成一条
    assert texts[1] == "前方高能" and danmaku[1].count == 4
    assert not any(t in texts for t in ("！！！???", "加微信领福利", "nmsl")) and all(
        len(t) <= 40 for t in texts
    )
    assert dense == "1:00、6:30 前后弹幕最密" or dense.startswith("1:00")
    assert watch.process_danmaku([(1.0, "单条")], 1, [])[1] == ""  # 没有够密的时间点


def test_danmaku_blocked_keywords_from_config_are_dropped() -> None:
    danmaku, _ = watch.process_danmaku(
        [(1.0, "自定义屏蔽词弹幕"), (2.0, "正常弹幕")], 1, ["自定义屏蔽词"]
    )
    assert [d.text for d in danmaku] == ["正常弹幕"]


def test_danmaku_xml_parsing_tolerates_garbage() -> None:
    xml = '<?xml version="1.0"?><i><d p="12.5,1,25,16777215">你好</d><d p="bad">跳过</d><d p="3,1">再见</d></i>'
    assert watch.parse_danmaku_xml(xml.encode()) == [(12.5, "你好"), (3.0, "再见")]
    assert watch.parse_danmaku_xml(b"<i><d") == []


def _watch_routes(cid: int = 99) -> dict[str, Any]:
    xml = "<i>" + "".join(f'<d p="{65 + n},1">前方高能</d>' for n in range(5)) + "</i>"
    return {
        "/web-interface/view": {
            "code": 0,
            "data": {
                "desc"    : "详细简介 https://x.com",
                "owner"   : {"name": "UP主甲"},
                "duration": 345,
                "cid"     : cid,
            },
        },
        "/tag/archive/tags": {"code": 0, "data": [{"tag_name": "天文"}, {"tag_name": "摄影"}]},
        "/dm/list.so"      : xml.encode(),
    }


@pytest.mark.asyncio
async def test_watch_adds_details_tags_and_danmaku_without_claiming_more() -> None:
    [item] = await bilibili.popular(
        FakeFetcher({"/popular": popular_payload(bili_video(7, "标题", "短简介"))}), []
    )
    fetch = FakeFetcher(_watch_routes())
    seen  = await watch.watch(fetch, item, 2, [])
    assert (seen.uploader, seen.duration_s, seen.text) == ("UP主甲", 345, "详细简介")
    assert (
        {"天文", "摄影"} <= set(seen.tags)
        and seen.danmaku[0].ref == "d2.1"
        and seen.danmaku[0].count == 5
    )
    assert seen.dense_moment == "1:00 前后弹幕最密"
    urls = [c[0] for c in fetch.calls]
    assert any("web-interface/view" in u for u in urls) and fetch.calls[0][1] == {"bvid": "BV7"}
    assert [c[1] for c in fetch.calls if "dm/list" in c[0]] == [{"oid": "99"}]


@pytest.mark.asyncio
async def test_watch_failures_only_remove_material() -> None:
    [item] = await bilibili.popular(
        FakeFetcher({"/popular": popular_payload(bili_video(7, "标题", "短简介"))}), []
    )
    down = FakeFetcher(
        {"/web-interface/view": SourceError("x", "Timeout"), "/tag/archive": {"code": -404}}
    )
    assert await watch.watch(down, item, 1, []) == item
    no_dm   = FakeFetcher({**_watch_routes(), "/dm/list.so": SourceError("x", "HttpStatusError")})
    partial = await watch.watch(no_dm, item, 1, [])
    assert partial.uploader == "UP主甲" and partial.danmaku == ()
    assert (
        await watch.watch(
            FakeFetcher({}), SourceItem("zhihu_hot:1", "zhihu_hot", "t", "", "u", 1, ()), 1, []
        )
        is not None
    )


@pytest.mark.asyncio
async def test_bili_item_ids_do_not_depend_on_the_source() -> None:
    popular = await bilibili.popular(
        FakeFetcher({"/popular": popular_payload(bili_video(7, "同一个视频"))}), []
    )
    search = await bilibili.search(
        FakeFetcher(
            {
                "/search/type": {
                    "code": 0,
                    "data": {
                        "result": [
                            {"id": 7, "bvid": "BV7", "title": "同一个视频", "author": "作者乙"}
                        ]
                    },
                }
            }
        ),
        "q",
        [],
    )
    ranking = await bilibili.ranking(
        FakeFetcher({"/ranking/v2": popular_payload(bili_video(7, "同一个视频"))}), []
    )
    assert {i.item_id for i in popular + search + ranking} == {"bili:7"}
    assert [i.source for i in popular + search + ranking] == [
        "bili_popular",
        "bili_search",
        "bili_ranking",
    ]
    assert search[0].uploader == "作者乙" and popular[0].uploader == "up"


def test_danmaku_lines_are_valid_quote_sources_and_one_video_is_one_source() -> None:
    item = SourceItem(
        "bili:1",
        "bili_popular",
        "标题",
        "",
        "u",
        1,
        (),
        (SourceComment("c1.1", "评论里也有哈基米", 1),),
        danmaku=(SourceDanmaku("d1.1", "哈基米来了", 9),),
    )
    material = digest.material([item])["items"][0]
    assert material["danmaku"] == [{"id": "d1.1", "text": "哈基米来了", "count": 9}]
    raw = {
        "terms": [
            {"term": "哈基米", "meaning": "爱称", "quote": "哈基米来了", "refs": ["d1.1"]},
            {"term": "哈基米", "meaning": "爱称", "quote": "评论里也有哈基米", "refs": ["c1.1"]},
        ],
        "expressions": [
            {"situation": "当看到猫", "pattern": "喊一声", "quote": "哈基米来了", "refs": ["d1.1"]}
        ],
    }
    result = digest.validate(raw, [item], max_memes=0)
    assert [t.item_id for t in result.terms] == ["bili:1"] and result.expressions[
        0
    ].item_id == "bili:1"
    forged = {
        "terms": [{"term": "哈基米", "meaning": "m", "quote": "不存在的弹幕", "refs": ["d1.1"]}]
    }
    assert digest.validate(forged, [item], max_memes=0).terms == []
    assert (
        "没有看画面"
        in digest.build_messages(persona="p", items=[item], known_terms=[], thread_titles=[])[0][
            "content"
        ]
    )


@pytest.mark.asyncio
async def test_session_watches_clicked_videos_and_stores_dense_moment(tmp_path: Path) -> None:
    store  = _store(tmp_path)
    routes = {
        "/popular"   : popular_payload(bili_video(1, "视频一"), bili_video(2, "视频二")),
        "/reply/main": comments_payload(bili_reply("好看")),
        **_watch_routes(),
    }
    fetch = FakeFetcher(routes)

    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return json.dumps({"looked_at": [{"item": "i1", "reaction": "不错", "feeling": "like"}]})

    deps = SessionDeps(fetch=fetch, complete=complete, rng=random.Random(1), now=lambda: MOMENT)
    await run_session(
        store, make_cfg(), planner.Decision("hot_scroll", "s", {}, {"source": "bili_popular"}), deps
    )
    episode = store.episodes[0]
    assert episode["uploader"] == "UP主甲" and episode["duration_s"] == 345
    assert episode["dense_moment"] == "1:00 前后弹幕最密"
    assert sum("web-interface/view" in c[0] for c in fetch.calls) == 2  # 最多看两个


# ───────────────────────────── 有界 XML 与 deflate ─────────────────────────────


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


@pytest.mark.asyncio
async def test_bounded_fetcher_xml_accepts_raw_deflate_and_rejects_entities() -> None:
    xml = b'<i><d p="1,1">hi</d></i>'
    for body in (_raw_deflate(xml), zlib.compress(xml)):
        session = QueuedAiohttpSession(
            AiohttpResponse(body, media_type="text/xml", headers={"Content-Encoding": "deflate"})
        )
        got = await BoundedFetcher(session).get_xml(
            "https://api.bilibili.com/x/v1/dm/list.so", params={"oid": "1"}
        )
        assert got == xml and session.requests[0][2]["auto_decompress"] is False
    evil = b'<?xml version="1.0"?><!DOCTYPE i [<!ENTITY x "y">]><i>&x;</i>'
    session = QueuedAiohttpSession(AiohttpResponse(evil, media_type="text/xml"))
    with pytest.raises(SourceError):
        await BoundedFetcher(session).get_xml("https://api.bilibili.com/x/v1/dm/list.so")
    wrong = QueuedAiohttpSession(AiohttpResponse(xml, media_type="image/png"))
    with pytest.raises(SourceError):
        await BoundedFetcher(wrong).get_xml("https://api.bilibili.com/x/v1/dm/list.so")
    with pytest.raises(SourceError, match="host_not_allowed"):
        await BoundedFetcher(QueuedAiohttpSession()).get_xml("https://evil.example.com/x")


@pytest.mark.asyncio
async def test_baidu_uses_the_pc_board_with_heat_and_description() -> None:
    payload = {
        "data": {"cards": [{"content": [{"word": "热词", "desc": "说明", "hotScore": "7808520"}]}]}
    }
    fetch = FakeFetcher({"top.baidu.com": payload})
    [item] = await hotlists.baidu(fetch, [])
    assert item.heat == 7808520 and item.text == "说明" and fetch.calls[0][1]["platform"] == "pc"


# ───────────────────────────── 梗图 ─────────────────────────────


def _png(color: int = 0) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (color, 10, 20)).save(buffer, format="PNG")
    return buffer.getvalue()


def _meme_items(*pictures: str) -> list[SourceItem]:
    comment = SourceComment("c1.1", "这表情太绝了", 3, tuple(pictures))
    return [SourceItem("bili:1", "bili_popular", "视频", "", "u", 1, (), (comment,))]


GOOD = {
    "is_reaction_meme": True,
    "safe_for_group"  : True,
    "label"           : "点赞认可",
    "description"     : "动漫女孩竖起大拇指",
    "visible_text"    : "",
    "emotion_tags"    : ["认可", "开心", "赞", "多余"],
    "usage"           : "夸人的时候发",
}


class MemeEnv:
    def __init__(self, verdicts: list[Any], images: dict[str, bytes]) -> None:
        self.verdicts, self.images = list(verdicts), images
        self.downloads: list[str] = []
        self.vision_calls         = 0

    async def download(self, url: str) -> tuple[bytes, str] | None:
        self.downloads.append(url)
        data = self.images.get(url)
        return None if data is None else (data, ".png")

    async def vision(self, messages: list[dict[str, Any]]) -> str:
        self.vision_calls += 1
        assert messages[1]["content"][1]["image_url"]["url"].startswith("data:image/")
        verdict = self.verdicts.pop(0)
        return verdict if isinstance(verdict, str) else json.dumps(verdict)


async def _collect(
    store: LifeStore, env: MemeEnv, items: list[SourceItem], refs: list[str], **cfg: Any
) -> int:
    picks = [(r, "像反应图") for r in refs]
    return await memes.collect(
        store,
        make_cfg(**cfg),
        items,
        picks,
        download = env.download,
        vision   = env.vision,
        day      = "2026-11-05",
        now      = lambda: NOW,
    )


def test_verdict_must_be_strictly_true_clean_and_unblocked() -> None:
    verdict = memes.parse_verdict(GOOD, [])
    assert (
        verdict and verdict.label == "点赞认可" and verdict.emotion_tags == ("认可", "开心", "赞")
    )
    for bad in (
        {**GOOD, "safe_for_group": "true"},
        {**GOOD, "is_reaction_meme": 1},
        {**GOOD, "safe_for_group": False},
        {**GOOD, "label": ""},
        {**GOOD, "description": "加微信领福利的图"},
        {**GOOD, "visible_text": "nmsl"},
    ):
        assert memes.parse_verdict(bad, []) is None
    assert memes.parse_verdict({**GOOD, "usage": "自定义词"}, ["自定义词"]) is None


@pytest.mark.asyncio
async def test_collect_accepts_stores_dedupes_and_skips_unsafe(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a, b, c = _png(1), _png(2), _png(3)
    items = _meme_items(
        "https://i0.hdslb.com/a.png",
        "https://i0.hdslb.com/b.png",
        "https://i0.hdslb.com/c.png",
        "https://i0.hdslb.com/a.png",
    )
    env = MemeEnv(
        [GOOD, {**GOOD, "safe_for_group": False}, "不是 json"],
        {
            "https://i0.hdslb.com/a.png": a,
            "https://i0.hdslb.com/b.png": b,
            "https://i0.hdslb.com/c.png": c,
        },
    )
    added = await _collect(
        store,
        env,
        items,
        ["c1.1#0", "c1.1#1", "c1.1#2", "c1.1#3", "c9.9#0"],
        max_vision_per_session=4,
    )
    assert added == 1 and env.vision_calls == 3 and len(store.memes) == 1
    [record] = store.memes.values()
    assert record["status"] == "ready" and record["approved_at"] == NOW and record["ext"] == ".png"
    assert (store.data_dir / "memes" / f"{record['sha256']}.png").read_bytes() == a
    assert store.state["llm_calls_today"] == 3  # 每次视觉调用都先计预算
    # 同一张图再来：不再调视觉
    again = await _collect(
        store, MemeEnv([GOOD], {"https://i0.hdslb.com/a.png": a}), items, ["c1.1#0"]
    )
    assert again == 0 and store.state["llm_calls_today"] == 3


@pytest.mark.asyncio
async def test_collect_respects_budget_per_session_cap_and_switches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    url   = "https://i0.hdslb.com/a.png"
    items, images = _meme_items(url), {url: _png(5)}
    assert (
        await _collect(store, MemeEnv([GOOD], images), items, ["c1.1#0"], max_llm_calls_per_day=0)
        == 0
    )
    assert (
        await _collect(store, MemeEnv([GOOD], images), items, ["c1.1#0"], memes_enabled=False) == 0
    )
    assert await _collect(store, MemeEnv([GOOD], images), items, ["c1.1#0"], max_memes=0) == 0
    env = MemeEnv([GOOD], {})
    assert (
        await _collect(store, env, items, ["c1.1#0"]) == 0 and env.vision_calls == 0
    )  # 下载失败不调视觉
    pending = await _collect(
        store, MemeEnv([GOOD], images), items, ["c1.1#0"], meme_auto_approve=False
    )
    assert pending == 1 and next(iter(store.memes.values()))["status"] == "pending"
    assert (await memes.export_page(store, {"since": 0}, NOW))["memes"] == []  # 待审的不导出


@pytest.mark.asyncio
async def test_eviction_removes_the_longest_unexported_and_leaves_a_revocation(
    tmp_path: Path,
) -> None:
    store  = _store(tmp_path)
    urls   = [f"https://i0.hdslb.com/{n}.png" for n in range(3)]
    images = {u: _png(10 + n) for n, u in enumerate(urls)}
    stamps = iter([NOW, NOW + 10, NOW + 20])

    async def add(n: int) -> None:
        await memes.collect(
            store,
            make_cfg(max_memes=2),
            _meme_items(urls[n]),
            [("c1.1#0", "w")],
            download = MemeEnv([], images).download,
            vision   = MemeEnv([GOOD], images).vision,
            day      = "2026-11-05",
            now      = lambda: next(stamps),
        )

    await add(0)
    await add(1)
    first = next(iter(store.memes))
    await add(2)
    live = [m for m in store.memes.values() if m["status"] == "ready"]
    assert (
        len(live) == 2
        and store.memes[first]["status"] == "evicted"
        and store.memes[first]["revoked_at"] == NOW + 20
    )
    assert not (store.data_dir / "memes" / f"{first}.png").exists()
    page = await memes.export_page(store, {"since": 0, "limit": 10}, NOW + 30)
    assert first in page["revoked"] and len(page["memes"]) == 2


@pytest.mark.asyncio
async def test_export_pagination_revocation_and_switches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for n in range(3):
        payload = _png(40 + n)
        sha     = __import__("hashlib").sha256(payload).hexdigest()
        (store.data_dir / "memes").mkdir(parents=True, exist_ok=True)
        (store.data_dir / "memes" / f"{sha}.png").write_bytes(payload)
        store.memes[sha] = {
            "sha256"       : sha,
            "ext"          : ".png",
            "label"        : f"名字{n}",
            "description"  : "描述",
            "visible_text" : "",
            "emotion_tags" : ["开心"],
            "usage"        : "用法",
            "status"       : "ready",
            "created_at"   : NOW,
            "approved_at"  : NOW + n,
            "last_exported": 0.0,
        }
    first = await memes.export_page(store, {"since": 0, "limit": 2}, NOW + 100)
    assert [m["label"] for m in first["memes"]] == ["名字0", "名字1"] and first[
        "next_since"
    ] == NOW + 1
    assert {
        "sha256",
        "label",
        "description",
        "visible_text",
        "emotion_tags",
        "usage",
        "mime",
        "data_b64",
        "approved_at",
    } == set(first["memes"][0])
    assert first["memes"][0]["mime"] == "image/png" and first["revoked"] == []
    second = await memes.export_page(store, {"since": first["next_since"], "limit": 2}, NOW + 100)
    assert [m["label"] for m in second["memes"]] == ["名字2"] and second["next_since"] == NOW + 2
    assert (await memes.export_page(store, {"since": second["next_since"]}, NOW + 100))[
        "memes"
    ] == []

    sha = first["memes"][0]["sha256"]
    assert await memes.set_enabled(store, sha[:6], False, NOW + 200) == "已关闭。"
    after = await memes.export_page(store, {"since": NOW + 150, "limit": 10}, NOW + 300)
    assert after["revoked"] == [sha] and after["next_since"] == NOW + 200 and after["memes"] == []
    assert sha[:6] in memes.list_text(store) and "off" in memes.list_text(store)
    assert await memes.set_enabled(store, sha[:6], True, NOW + 400) == "已开启。"
    back = await memes.export_page(store, {"since": NOW + 300}, NOW + 500)
    assert [m["sha256"] for m in back["memes"]] == [sha]  # 重新开启后会被再次导出
    assert await memes.set_enabled(store, "zzzzzz", True, NOW) == "没有这张梗图。"
    assert (await memes.export_page(store, {"since": "x", "limit": 99}, NOW))[
        "memes"
    ]  # 非法参数按默认处理


@pytest.mark.asyncio
async def test_download_image_uses_the_safe_downloader_with_a_fixed_host_and_validates(
    monkeypatch,
) -> None:
    calls: dict[str, Any] = {}

    async def fake_fetch(url: str, **kw: Any):
        calls.update(url=url, **kw)
        return SimpleNamespace(body=calls["body"])

    import core.safe_http as safe_http

    monkeypatch.setattr(safe_http, "fetch_public_bytes", fake_fetch)
    calls["body"] = _png(7)
    got           = await memes.download_image("http://i0.hdslb.com/a.png", 1_000_000)
    assert got == (calls["body"], ".png")
    assert calls["url"].startswith("https://") and "i0.hdslb.com" in calls["allowed_hosts"]
    assert calls["headers"]["Referer"] == memes.MEME_REFERER and calls["max_bytes"] == 1_000_000
    calls["body"] = b"not an image at all"
    assert await memes.download_image("https://i0.hdslb.com/a.png", 1_000_000) is None

    async def refuse(url: str, **kw: Any):
        raise safe_http.UnsafeUrlError("blocked")

    monkeypatch.setattr(safe_http, "fetch_public_bytes", refuse)
    assert await memes.download_image("https://evil.example.com/a.png", 1_000_000) is None


# ───────────────────────────── 睡前整理 ─────────────────────────────


def _episode(n: int, ago: float = 600.0) -> dict[str, Any]:
    return {
        "id"      : f"e{n}",
        "at"      : NOW - ago,
        "day"     : "2026-11-05",
        "title"   : f"视频{n}",
        "reaction": "有意思",
        "feeling" : "like",
        "gist"    : "",
        "topics"  : [],
    }


def _thread(tid: str, status: str, notes: list[str]) -> dict[str, Any]:
    return {
        "id"            : tid,
        "title"         : f"课题{tid}",
        "why"           : "",
        "next_query"    : "q",
        "status"        : status,
        "steps"         : 1,
        "empty_streak"  : 0,
        "cooldown_until": 0.0,
        "notes"         : [{"episode_id": n, "note": "要点"} for n in notes],
        "takeaway"      : "",
        "created_at"    : NOW - DAY,
        "updated_at"    : NOW - 100,
    }


async def _run(store: LifeStore, ai: FakeAI, moment: datetime = MOMENT, **cfg: Any):
    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        return (await ai.complete(route, messages)).content

    return await wind_down.run_wind_down(
        store,
        make_cfg(**cfg),
        moment,
        complete,
        persona      = "你是小青。",
        moment_line  = "现在是晚上。",
        reasoning_ok = True,
    )


@pytest.mark.asyncio
async def test_wind_down_writes_a_validated_diary_and_finishes_threads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.episodes += [_episode(1), _episode(2)]
    store.threads += [
        _thread("t1", "reviewing", ["e1"]),
        _thread("t2", "open", ["e2"]),
        _thread("t3", "open", []),
    ]
    reply = {
        "diary": {
            "date"       : "2026-11-05",
            "text"       : "今天刷了两个视频，挺有意思。",
            "episode_ids": ["e1", "e2"],
        },
        "threads": [
            {"id": "t1", "action": "done", "takeaway": "查明白了", "episode_ids": ["e1"]},
            {"id": "t2", "action": "continue"},
            {"id": "t3", "action": "drop"},
        ],
    }
    ai     = FakeAI(reply)
    result = await _run(store, ai)
    assert (result.status, result.diary, result.done, result.dropped) == ("ok", True, 1, 1)
    state = store.state
    assert state["wound_down_day"] == "2026-11-05" and state["diary"]["episode_ids"] == ["e1", "e2"]
    assert state["llm_calls_today"] == 1
    statuses = {t["id"]: t["status"] for t in store.threads}
    assert statuses == {"t1": "done", "t2": "open", "t3": "dropped"}
    assert store.threads[0]["takeaway"] == "查明白了"
    assert (
        store.journal[-1]["kind"] == "diary"
        and store.terms == {}
        and store.expressions == []
        and store.interests == {}
    )
    user = json.loads(ai.calls[0][1][1]["content"])
    assert "untrusted_record" in user and "现在是晚上" in ai.calls[0][1][0]["content"]


@pytest.mark.asyncio
async def test_diary_must_cite_todays_real_episodes_and_the_right_date(tmp_path: Path) -> None:
    for diary in (
        {"date": "2026-11-04", "text": "日期不对", "episode_ids": ["e1"]},
        {"date": "2026-11-05", "text": "引用不存在", "episode_ids": ["e9"]},
        {"date": "2026-11-05", "text": "没有引用", "episode_ids": []},
        {"date": "2026-11-05", "text": "", "episode_ids": ["e1"]},
        {"date": "2026-11-05", "text": "含屏蔽词 自定义词", "episode_ids": ["e1"]},
    ):
        store = _store(tmp_path / str(abs(hash(json.dumps(diary)))))
        store.episodes.append(_episode(1))
        result = await _run(store, FakeAI({"diary": diary}), blocked_keywords=["自定义词"])
        assert result.status == "failed" and result.reason == "diary_invalid"
        assert (
            store.state["wound_down_day"] == ""
            and store.journal == []
            and store.state["diary"] == {}
        )
        assert store.state["llm_calls_today"] == 1  # 失败也计预算


@pytest.mark.asyncio
async def test_reviewing_thread_needs_a_takeaway_citing_its_own_episodes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.episodes.append(_episode(1))
    store.threads += [
        _thread("t1", "reviewing", ["e1"]),
        _thread("t2", "reviewing", ["e1"]),
        _thread("t3", "reviewing", ["e1"]),
    ]
    reply = {
        "diary"  : {"date": "2026-11-05", "text": "今天刷了视频。", "episode_ids": ["e1"]},
        "threads": [
            {"id": "t1", "action": "done", "takeaway": "没有依据", "episode_ids": ["e-other"]},
            {"id": "t2", "action": "done", "takeaway": "", "episode_ids": ["e1"]},
            {"id": "t3", "action": "drop"},  # reviewing 不能直接 drop
        ],
    }
    result = await _run(store, FakeAI(reply))
    assert result.done == 0 and {t["status"] for t in store.threads} == {"reviewing"}


@pytest.mark.asyncio
async def test_wind_down_skips_without_episodes_or_budget_and_never_writes_on_model_failure(
    tmp_path: Path,
) -> None:
    empty = _store(tmp_path / "a")
    ai    = FakeAI()
    assert (await _run(empty, ai)).reason == "no_episodes" and ai.calls == []
    tomorrow = _store(tmp_path / "b")
    tomorrow.episodes.append({**_episode(1), "day": "2026-11-04"})
    assert (await _run(tomorrow, ai)).reason == "no_episodes"
    future = _store(tmp_path / "c")
    future.episodes.append(_episode(1, ago=-600))  # 还没刷到的不算
    assert (await _run(future, ai)).reason == "no_episodes"
    broke = _store(tmp_path / "d")
    broke.episodes.append(_episode(1))
    assert (
        await _run(broke, ai, max_llm_calls_per_day=0)
    ).reason == "llm_exhausted" and ai.calls == []
    for reply in (RuntimeError("boom"), "不是 json"):
        store = _store(tmp_path / str(id(reply)))
        store.episodes.append(_episode(1))
        out = await _run(store, FakeAI(reply))
        assert (
            out.status == "failed" and store.journal == [] and store.state["wound_down_day"] == ""
        )


@pytest.mark.asyncio
async def test_weekly_note_on_sunday_cites_diary_dates_and_episode_ids(tmp_path: Path) -> None:
    sunday = at(23, 0, day=8)  # 2026-11-08 周日
    assert sunday.weekday() == 6
    store = _store(tmp_path)
    store.episodes.append({**_episode(5), "at": sunday.timestamp() - 600, "day": "2026-11-08"})
    store.journal += [
        {
            "kind"       : "diary",
            "at"         : sunday.timestamp() - 2 * DAY,
            "day"        : "2026-11-06",
            "text"       : "周五看了猫",
            "episode_ids": ["e1"],
        },
        {
            "kind"       : "diary",
            "at"         : sunday.timestamp() - DAY,
            "day"        : "2026-11-07",
            "text"       : "周六看了星星",
            "episode_ids": ["e2", "e3"],
        },
    ]
    reply = {
        "diary" : {"date": "2026-11-08", "text": "周日看了点东西。", "episode_ids": ["e5"]},
        "weekly": {
            "text"       : "这周看了猫和星星。",
            "dates"      : ["2026-11-06", "2026-11-07"],
            "episode_ids": ["e1", "e3"],
        },
    }
    ai  = FakeAI(reply)
    out = await _run(store, ai, sunday)
    assert (
        out.weekly
        and store.journal[-1]["kind"] == "weekly"
        and store.journal[-1]["dates"] == ["2026-11-06", "2026-11-07"]
    )
    assert json.loads(ai.calls[0][1][1]["content"])["untrusted_record"]["weekly_requested"] is True
    again = _store(tmp_path / "again")
    again.episodes.append({**_episode(5), "at": sunday.timestamp() - 600, "day": "2026-11-08"})
    again.journal += store.journal[:2]
    bad = {
        **reply,
        "weekly": {"text": "引用错了。", "dates": ["2026-10-01"], "episode_ids": ["e1"]},
    }
    out = await _run(again, FakeAI(bad), sunday)
    assert out.diary and not out.weekly and [j["kind"] for j in again.journal].count("weekly") == 0
    weekday = _store(tmp_path / "weekday")
    weekday.episodes.append(_episode(1))
    weekday.journal += store.journal[:2]
    fake = FakeAI(
        {
            "diary" : reply["diary"] | {"date": "2026-11-05", "episode_ids": ["e1"]},
            "weekly": reply["weekly"],
        }
    )
    assert not (await _run(weekday, fake)).weekly  # 不是周日不写周记


def test_journal_is_kept_eight_weeks_and_wind_down_passes_the_llm_gate() -> None:
    journal = [{"kind": "weekly", "at": NOW - 57 * DAY}, {"kind": "weekly", "at": NOW - 10 * DAY}]
    wind_down.prune_journal(journal, NOW)
    assert len(journal) == 1
    cfg = make_cfg(choice_temperature=0.5)
    near  = type(FREE)("free", "", 20)
    today = [{"id": "e1", "day": "2026-11-05"}]
    state = default_state() | {"day": "2026-11-05", "llm_calls_today": 30}
    out   = planner.decide(cfg, state, today, [], {}, MOMENT, near, random.Random(0))
    assert out.reason == "llm_exhausted"
    state["llm_calls_today"] = 3
    assert (
        planner.decide(cfg, state, today, [], {}, MOMENT, near, random.Random(0)).choice
        == planner.WIND_DOWN
    )
