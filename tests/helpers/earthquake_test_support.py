"""earthquake 插件测试共用的微博有界 HTTP 替身与地震快讯卡片。

真实边界是 ``requests_request_bounded``（微博的访客脚本、配置和索引接口）；快讯解析、
去重游标、投递与持久化都走插件的真实代码。
"""

from __future__ import annotations

import threading

import pytest

from core.bounded_http import BoundedHttpResponse
from plugins.earthquake import main as earthquake
from tests.helpers.http import bounded_json_response

EARTHQUAKE_TEXT = (
    '#地震快讯#<a href="/123">中国地震台网正式测定</a>'
    '：01月01日00:00在四川发生4.5级地震（ <a href="/location">震源深度10公里</a>）'
)


def weibo_card(
    post_id: str = "1234567890",
    *,
    magnitude: str        = "4.5",
    image_url: str | None = None,
) -> dict:
    """索引里的一张地震快讯卡片：``post_id`` 是微博 id，``magnitude`` 替换正文里的震级，
    给出 ``image_url`` 时带一张原图。"""

    raw_text = EARTHQUAKE_TEXT.replace("4.5", magnitude)
    mblog    = {"id": post_id, "text": raw_text}
    if image_url is not None:
        mblog["original_pic"] = image_url
    return {"mblog": mblog}


def weibo_index(cards: list[dict]) -> dict:
    """微博 ``getIndex`` 接口的响应体：成功标记加卡片列表。"""

    return {"ok": 1, "data": {"cards": cards}}


def _javascript_response(url: str, payload: bytes) -> BoundedHttpResponse:
    """已经完成有界读取的 ``text/javascript`` 响应（访客脚本）。"""

    return BoundedHttpResponse(
        url           = url,
        status        = 200,
        body          = payload,
        media_type    = "text/javascript",
        charset       = "utf-8",
        headers       = {"Content-Type": "text/javascript"},
        wire_bytes    = len(payload),
        decoded_bytes = len(payload),
    )


class Weibo:
    """微博有界 HTTP 请求的替身：按 URL 返回访客脚本、配置和索引，并记录每次调用。"""

    def __init__(
        self,
        cards: list[dict] | None = None,
        *,
        error: BaseException | None         = None,
        visitor_error: BaseException | None = None,
        index_error: BaseException | None   = None,
        config_payload: object              = None,
    ) -> None:
        self.cards          = cards or []
        self.error          = error
        self.visitor_error  = visitor_error
        self.index_error    = index_error
        self.config_payload = (
            {"ok": 1, "data": {"login": False}} if config_payload is None else config_payload
        )
        self.calls: list[tuple[str, str, object, int, dict]] = []

    def __call__(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs["session"], threading.get_ident(), kwargs))
        if self.error is not None:
            raise self.error
        if "genvisitor2" in url:
            if self.visitor_error is not None:
                raise self.visitor_error
            return _javascript_response(url, b"visitor_gray_callback({});")
        if url.endswith("/api/config"):
            return bounded_json_response(self.config_payload, url=url)
        if self.index_error is not None:
            raise self.index_error
        return bounded_json_response(weibo_index(self.cards), url=url)

    @property
    def index_calls(self) -> list[tuple[str, str, object, int, dict]]:
        return [call for call in self.calls if call[1].endswith("/getIndex")]


def serve_weibo(monkeypatch: pytest.MonkeyPatch, *cards: dict, **failures: object) -> Weibo:
    """把插件的微博有界 HTTP 请求换成 ``Weibo``；``cards`` 是索引卡片，``failures`` 传给 ``Weibo``。"""

    weibo = Weibo(list(cards), **failures)
    monkeypatch.setattr(earthquake, "requests_request_bounded", weibo)
    return weibo
