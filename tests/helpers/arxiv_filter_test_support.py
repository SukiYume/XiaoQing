"""arxiv_filter 插件测试共用的假 arXiv 世界：页面、模型目录、句向量编码器。

真实边界只有三处：arXiv 页面（``requests.request``）、可选的 sentence_transformers / torch
库、插件自己的 config.json 位置。其余——日期解析、模型类型选择、k-NN 打分、结果格式化、
缓存与投递——都走插件的真实代码。
"""

from __future__ import annotations

import datetime
import importlib
import json
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
import requests

arxiv_filter       = importlib.import_module("plugins.arxiv_filter.main")
arxiv_filter_utils = importlib.import_module("plugins.arxiv_filter.utils")

_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class RawBody:
    """``requests`` 响应的 ``raw`` 流：按生产有界读取要求只接受 ``decode_content=False``。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks                     = chunks
        self.stream_calls                = 0
        self.decode_content: bool | None = None

    def stream(self, _chunk_size: int, *, decode_content: bool) -> Iterator[bytes]:
        self.stream_calls += 1
        assert decode_content is False
        yield from self._chunks


class RequestsResponse:
    """arXiv 列表页的 ``requests`` 响应：状态 200，响应体经 ``raw`` 流式提供。"""

    def __init__(self, *, body: bytes, content_type: str) -> None:
        self.status_code = 200
        self.url         = "https://arxiv.org/list/astro-ph/new"
        self.headers     = {"Content-Type": content_type}
        self.raw         = RawBody([body])
        self.closed      = False

    def close(self) -> None:
        self.closed = True


class FakeEncoderLog:
    """记录假编码器被构造了几次，并让测试能令它报错或少返回一行。"""

    def __init__(self) -> None:
        self.loads: list[str]               = []
        self.encode_error: Exception | None = None
        self.drop_last_row                  = False


def install_fake_encoder(
    monkeypatch: pytest.MonkeyPatch,
    vectors: Mapping[str, Sequence[float]] | None = None,
) -> FakeEncoderLog:
    """用最小替身顶替可选依赖 sentence_transformers 与 torch。

    文本里含 ``vectors`` 的某个关键词（不分大小写）就得到对应向量，默认 ``pulsar`` 对应
    [1, 0]；其余文本得到 [0, 1]。真实 CUDA 探测在没有 torch 的环境里无法运行，所以 torch 也替换成
    只会回答“没有 CUDA”的模块。
    """

    keyed = {
        keyword.lower(): np.array(vector, dtype=np.float32)
        for keyword, vector in (vectors if vectors is not None else {"pulsar": (1.0, 0.0)}).items()
    }
    log = FakeEncoderLog()

    class SentenceTransformer:
        max_seq_length: int | None = None

        def __init__(self, name: str) -> None:
            log.loads.append(name)

        def get_sentence_embedding_dimension(self) -> int:
            return 2

        def encode(self, texts: list[str], **_kwargs: object) -> np.ndarray:
            if log.encode_error is not None:
                raise log.encode_error
            rows = []
            for text in texts:
                lowered = text.lower()
                rows.append(
                    next(
                        (vector for keyword, vector in keyed.items() if keyword in lowered),
                        np.array([0.0, 1.0], dtype=np.float32),
                    )
                )
            return np.stack(rows[:-1] if log.drop_last_row else rows)

    sentence_transformers                     = ModuleType("sentence_transformers")
    sentence_transformers.SentenceTransformer = SentenceTransformer  # type: ignore[attr-defined]
    torch                                     = ModuleType("torch")
    torch.cuda = SimpleNamespace(is_available=lambda: False)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", sentence_transformers)
    monkeypatch.setitem(sys.modules, "torch", torch)
    return log


def write_knn_model(
    model_dir: Path,
    *,
    threshold: float = 0.5,
    version: int     = 1,
    input_mode: str  = "title_abstract",
) -> None:
    """写出一个能被真实 k-NN 后端加载的最小模型：兴趣库只有 [1, 0] 一条。"""

    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "training_config.json").write_text(
        json.dumps({"model_type": "knn", "optimal_threshold": threshold, "input_mode": input_mode}),
        encoding="utf-8",
    )
    (model_dir / "meta.json").write_text(
        json.dumps(
            {
                "encoder_name": "fake-encoder",
                "embed_dim"   : 2,
                "k"           : 1,
                "neg_k"       : 0,
                "columns"     : {},
                "version"     : version,
            }
        ),
        encoding="utf-8",
    )
    np.save(model_dir / "pos_embeddings.npy", np.array([[1.0, 0.0]], dtype=np.float32))


def listing_html(list_date: str, titles: Sequence[str]) -> bytes:
    """arXiv astro-ph/new 页面：日期标题加每篇论文的标题和摘要；没有论文时不含 <dl>。"""

    day     = datetime.date.fromisoformat(list_date)
    heading = f"Showing new listings for Monday, {day.day} {_MONTH_NAMES[day.month - 1]} {day.year}"
    entries = "".join(
        f'<dt><a href="/abs/2607.{index:05d}">arXiv:2607.{index:05d}</a></dt>'
        f'<dd><div class="list-title">Title: {title}</div>'
        f'<p class="mathjax">Abstract of {title}.</p></dd>'
        for index, title in enumerate(titles, 1)
    )
    body = f"<dl>{entries}</dl>" if titles else ""
    return f"<!doctype html><html><body><h3>{heading}</h3>{body}</body></html>".encode()


def use_plugin_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config: Mapping[str, object],
) -> Path:
    """让真实的 ``load_plugin_config`` 读取临时目录里的 config.json。

    插件只从 utils.py 旁边读这一个文件，没有别的注入口；返回的路径可在测试里重写。
    """

    config_dir = tmp_path / "plugin-config"
    config_dir.mkdir(exist_ok=True)
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(arxiv_filter_utils, "__file__", str(config_dir / "utils.py"))
    return config_path


class ArxivWorld:
    """页面上有哪些论文、模型是什么，都由测试决定；默认页面只有一篇不含 pulsar 的论文，
    即没有任何正例。``requests`` 记录每次抓取。"""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        list_date: str,
        titles: Sequence[str] = ("Quiet galaxy survey",),
    ) -> None:
        self.list_date                                  = list_date
        self.titles                                     = tuple(titles)
        self.requests: list[str]                        = []
        self.request_barrier: Callable[[], None] | None = None
        self.request_error: Exception | None            = None
        self.model_dir                                  = tmp_path / "model"
        write_knn_model(self.model_dir)
        self.encoder = install_fake_encoder(monkeypatch)
        monkeypatch.setenv("ARXIV_MODEL_PATH", str(self.model_dir))
        monkeypatch.delenv("ARXIV_PROXY", raising=False)
        monkeypatch.setattr(requests, "request", self._request)
        # 新一代插件加载时的状态：推理函数、当日结果和模型指纹都是空的。
        monkeypatch.setattr(arxiv_filter, "_inference_func", None)
        monkeypatch.setattr(arxiv_filter, "_FILTER_CACHE", {})
        monkeypatch.setattr(arxiv_filter, "_MODEL_FINGERPRINT_CACHE", {})

    def _request(self, _method: str, url: str, **_kwargs: object) -> RequestsResponse:
        self.requests.append(url)
        if self.request_barrier is not None:
            self.request_barrier()
        if self.request_error is not None:
            raise self.request_error
        return RequestsResponse(
            body         = listing_html(self.list_date, self.titles),
            content_type = "text/html; charset=UTF-8",
        )
