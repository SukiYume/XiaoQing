"""取材适配器：字段解析、清洗、屏蔽、非 0 返回码、超限正文与非 JSON。"""

from __future__ import annotations

import pytest

from plugins.xiaoqing_life.sources import baike, bilibili, hotlists
from plugins.xiaoqing_life.sources.base import BoundedFetcher, SourceError
from tests.helpers.http import (
    QueuedAiohttpSession,
    aiohttp_json_response,
    aiohttp_text_response,
)
from tests.plugins.xiaoqing_life.support import (
    FakeFetcher,
    bili_reply,
    bili_video,
    comments_payload,
    popular_payload,
)

pytestmark = pytest.mark.asyncio


async def test_bilibili_popular_parses_cleans_and_drops_blocked() -> None:
    fetch = FakeFetcher(
        {
            "/popular": popular_payload(
                bili_video(1, "<em>猫猫</em>的一天 [doge]", "详情 https://a.b/c"),
                bili_video(2, "国务院发布会"),
                bili_video(3, "加微信领福利视频"),
                {"title": "没有 aid"},
            )
        }
    )
    items = await bilibili.popular(fetch, [])
    assert [i.item_id for i in items] == ["bili_popular:1"]
    item = items[0]
    assert item.title == "猫猫的一天" and item.text == "详情" and item.tags == ("科技",)
    assert item.heat == 1001 and item.url.endswith("BV1")
    assert fetch.calls[0][1] == {"ps": "20", "pn": "1"}


@pytest.mark.parametrize("fn", ["popular", "ranking"])
async def test_bilibili_nonzero_code_is_a_source_failure(fn: str) -> None:
    fetch = FakeFetcher({"/x/web-interface": {"code": -352, "message": "x"}})
    with pytest.raises(SourceError) as info:
        await getattr(bilibili, fn)(fetch, [])
    assert "352" in info.value.reason


async def test_bilibili_ranking_takes_the_first_thirty_only() -> None:
    videos = [bili_video(i, f"视频{i}") for i in range(1, 60)]
    fetch  = FakeFetcher({"/ranking/v2": popular_payload(*videos)})
    items = await bilibili.ranking(fetch, [], limit=100)
    assert len(items) == 30


async def test_bilibili_search_strips_keyword_markup_and_reads_result_rows() -> None:
    payload = {
        "code": 0,
        "data": {
            "result": [
                {
                    "id"         : 77,
                    "bvid"       : "BV77",
                    "title"      : '<em class="keyword">星云</em>拍摄教程',
                    "description": "赤道仪",
                    "tag"        : "天文,摄影,教程,多余",
                    "play"       : 5000,
                    "typename"   : "科技",
                }
            ]
        },
    }
    fetch = FakeFetcher({"/search/type": payload})
    [item] = await bilibili.search(fetch, "星云", [])
    assert item.title == "星云拍摄教程" and item.heat == 5000
    assert item.tags[:3] == ("科技", "天文", "摄影")
    assert fetch.calls[0][1]["keyword"] == "星云"


async def test_bilibili_comments_cleaned_numbered_and_budgeted() -> None:
    fetch = FakeFetcher(
        {
            "/reply/main": comments_payload(
                bili_reply(
                    "哈基米 [doge] 好可爱",
                    99,
                    ("https://i0.hdslb.com/a.jpg",),
                    ("楼中楼一", "楼中楼二"),
                ),
                bili_reply("国务院评论"),
                bili_reply("第三条", 3, (), ("独占",)),
                bili_reply("", 1),  # 无文字无图：丢弃
                bili_reply("第四条"),
            )
        }
    )
    [item] = await bilibili.popular(
        FakeFetcher({"/popular": popular_payload(bili_video(9, "视频"))}), []
    )
    comments = await bilibili.hot_comments(fetch, item, 2, [], limit=4)
    assert [c.ref for c in comments] == ["c2.1", "c2.2"]
    assert comments[0].text == "哈基米 好可爱" and comments[0].pictures == (
        "https://i0.hdslb.com/a.jpg",
    )
    assert comments[0].replies == ("楼中楼一", "楼中楼二")
    # 评论与楼中楼合计不超过上限：1+2+1=4，第二条的楼中楼被裁掉。
    assert comments[1].replies == () and sum(1 + len(c.replies) for c in comments) == 4
    assert fetch.calls[0][1]["oid"] == "9"
    assert await bilibili.hot_comments(fetch, item, 1, [], limit=0) == ()


async def test_zhihu_hot_parses_target_and_heat() -> None:
    payload = {
        "data": [
            {
                "target"     : {"id": 11, "title": "如何评价某事", "excerpt": "摘要"},
                "detail_text": "1234 万热度",
            },
            {
                "target"     : {"id": 12, "title": "色情内容讨论", "excerpt": ""},
                "detail_text": "5 万热度",
            },
        ]
    }
    items = await hotlists.zhihu(FakeFetcher({"hot-lists": payload}), [])
    assert [i.item_id for i in items] == ["zhihu_hot:11"]
    assert items[0].heat == 1234 and items[0].text == "摘要"


async def test_weibo_reads_realtime_only_and_skips_ads_and_hotgov() -> None:
    payload = {
        "ok"  : 1,
        "data": {
            "hotgov"  : {"word": "时政置顶", "num": 9},
            "realtime": [
                {"word": "猫咪学会开门", "num": 500000, "label_name": "新"},
                {"word": "某品牌新品", "num": 1, "label_name": "商"},
                {"word": "总统访问", "num": 3},
                {"word": "月全食", "note": "月全食", "num": 200},
            ],
        },
    }
    items = await hotlists.weibo(FakeFetcher({"hotSearch": payload}), [])
    assert [i.title for i in items] == ["猫咪学会开门", "月全食"]
    assert items[0].tags == ("新",) and items[0].heat == 500000


async def test_baidu_walks_nested_cards() -> None:
    payload = {
        "data": {
            "cards": [
                {"content": [{"word": "热词甲", "desc": "说明甲", "hotScore": "99"}]},
                {"content": [{"content": [{"word": "热词乙", "hotScore": 7}]}]},
            ]
        }
    }
    items = await hotlists.baidu(FakeFetcher({"top.baidu.com": payload}), [])
    assert [i.title for i in items] == ["热词甲", "热词乙"]
    assert items[0].text == "说明甲" and items[0].heat == 99


async def test_baike_only_counts_slang_entries() -> None:
    slang = {"title": "哈基米", "desc": "网络流行语", "abstract": "对猫咪的爱称。"}
    plain = {"title": "猫", "desc": "哺乳动物", "abstract": "家猫是一种小型动物。"}
    entry = await baike.lookup(FakeFetcher({"Baike": slang}), "哈基米", [])
    assert entry is not None and entry.meaning.startswith("对猫咪")
    assert await baike.lookup(FakeFetcher({"Baike": plain}), "猫", []) is None
    assert await baike.lookup(FakeFetcher({"Baike": {}}), "不存在", []) is None
    assert await baike.lookup(FakeFetcher({"Baike": slang}), "哈基米", ["猫咪"]) is None
    with pytest.raises(SourceError):
        await baike.lookup(FakeFetcher({"Baike": SourceError("x", "Timeout")}), "词", [])


# ───────────────────────────── 有界 HTTP ─────────────────────────────


async def test_bounded_fetcher_sends_no_cookies_and_parses_json() -> None:
    session = QueuedAiohttpSession(aiohttp_json_response({"code": 0}))
    data    = await BoundedFetcher(session).get_json(
        "https://api.bilibili.com/x/web-interface/popular",
        params  = {"ps": "20"},
        referer = "https://www.bilibili.com/",
    )
    assert data == {"code": 0}
    method, _url, kwargs = session.requests[0]
    assert method == "GET" and kwargs["allow_redirects"] is False
    headers = {k.lower(): v for k, v in kwargs["headers"].items()}
    assert headers["referer"] == "https://www.bilibili.com/" and "cookie" not in headers


async def test_bounded_fetcher_rejects_unknown_hosts_before_any_request() -> None:
    session = QueuedAiohttpSession()
    with pytest.raises(SourceError, match="host_not_allowed"):
        await BoundedFetcher(session).get_json("https://evil.example.com/x")
    assert session.requests == []


async def test_bounded_fetcher_maps_bad_bodies_to_source_errors() -> None:
    not_json = QueuedAiohttpSession(aiohttp_text_response("<html>", media_type="text/html"))
    with pytest.raises(SourceError):
        await BoundedFetcher(not_json).get_json("https://api.zhihu.com/x")
    wrong_mime = QueuedAiohttpSession(aiohttp_text_response("{}", media_type="image/png"))
    with pytest.raises(SourceError):
        await BoundedFetcher(wrong_mime).get_json("https://api.zhihu.com/x")
    huge = QueuedAiohttpSession(
        aiohttp_json_response({"data": "x" * (9 * 1024 * 1024)}),
    )
    with pytest.raises(SourceError):
        await BoundedFetcher(huge).get_json("https://api.zhihu.com/x")
    error_status = QueuedAiohttpSession(aiohttp_json_response({}, status=503))
    with pytest.raises(SourceError):
        await BoundedFetcher(error_status).get_json("https://api.zhihu.com/x")
