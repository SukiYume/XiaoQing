"""ADS Paper 的命令入口、SciX 检索、摘要来源、按用户存储及错误边界测试。"""

import json
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit

import pytest

from core.public_errors import PUBLIC_ERROR_PREFIX
from plugins.ads_paper import main as ads_main
from plugins.ads_paper import note_commands
from plugins.ads_paper.storage import PaperStorage
from tests.helpers.assertions import text_segments_text
from tests.helpers.http import (
    AiohttpResponse,
    QueuedAiohttpSession,
    aiohttp_json_response,
    aiohttp_text_response,
)
from tests.helpers.settings_snapshot import with_settings_reader

CLAY = {
    "bibcode"       : "2018NatAs...2..206B",
    "title"         : ["Surface clay formation on a cold ancient Mars"],
    "author"        : ["Bishop, Janice L.", "Fairén, Alberto G.", "Michalski, Joseph R."],
    "author_count"  : 8,
    "year"          : "2018",
    "pub"           : "Nature Astronomy",
    "doctype"       : "article",
    "property"      : ["ARTICLE", "REFEREED"],
    "citation_count": 153,
    "identifier"    : ["10.1038/s41550-017-0377-9", "2018NatAs...2..206B"],
    "doi"           : ["10.1038/s41550-017-0377-9"],
    "uat"           : ["mars", "planetary geology"],
}
RADAR = {
    "bibcode"       : "2021NatAs...5...63L",
    "title"         : ["Multiple subglacial water bodies below the south pole of Mars"],
    "author"        : ["Lauro, Sebastian Emanuel", "Pettinelli, Elena"],
    "author_count"  : 2,
    "year"          : "2021",
    "pub"           : "Nature Astronomy",
    "doctype"       : "article",
    "property"      : ["REFEREED"],
    "citation_count": 151,
    "identifier"    : [
        "2020arXiv201000870E",
        "arXiv:2010.00870",
        "10.48550/arXiv.2010.00870",
        "10.1038/s41550-020-1200-6",
        "2021NatAs...5...63L",
    ],
    "doi": ["10.1038/s41550-020-1200-6", "10.48550/arXiv.2010.00870"],
}
TALK = {
    "bibcode"       : "2022EPSC...16..198F",
    "title"         : "Phosphine routes on Venus",
    "author"        : ["Ferus, Martin"],
    "year"          : "2022",
    "pub"           : "European Planetary Science Congress",
    "doctype"       : "abstract",
    "citation_count": 0,
    "identifier"    : ["2022EPSC...16..198F"],
}
_ARXIV_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2010.00870v1</id>
    <title>Multiple subglacial water bodies</title>
    <summary>  New MARSIS data show
      bright reflectors with permittivity &gt;15 at the base of the deposits.  </summary>
  </entry>
</feed>"""


def _search_response(*docs: Any, total: int | None = None, **extra: Any) -> AiohttpResponse:
    """SciX 检索接口的 JSON 响应；total 缺省为本页条数。"""

    found = len(docs) if total is None else total
    return aiohttp_json_response({"response": {"numFound": found, "docs": list(docs)}, **extra})


class _RoutedSession:
    """按请求内容应答的 HTTP 会话，供并发发出多个请求的命令使用。"""

    def __init__(self, respond: Callable[[str, dict[str, Any]], AiohttpResponse]) -> None:
        self.respond                                         = respond
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> AiohttpResponse:
        self.requests.append((method.upper(), url, dict(kwargs)))
        response     = self.respond(url, dict(kwargs))
        response.url = url
        return response


def _context(tmp_path: Path, session: Any, *, token: str = "test-token", ai: Any = None) -> Any:
    """带 SciX Token 和给定 HTTP 会话的插件上下文。"""

    secrets = {"ads_token": token} if token else {}
    return cast(
        Any,
        with_settings_reader(
            SimpleNamespace(
                secrets      = {"plugins": {"ads_paper": secrets}},
                state        = {},
                data_dir     = tmp_path,
                http_session = session,
                request_id   = None,
                capabilities=SimpleNamespace(ai=ai),
            )
        ),
    )


async def _paper(context: Any, args: str, *, user_id: int = 10001) -> tuple[str, Any]:
    """从真实 ``/paper`` 入口执行一条命令，返回回复文本和原始结果。"""

    result = await ads_main.handle(
        "paper", args, {"user_id": user_id, "message_type": "private"}, context
    )
    return text_segments_text(result, separator="\n"), result


def _queries(session: Any) -> list[str]:
    return [
        request[2]["params"]["q"]
        for request in session.requests
        if urlsplit(request[1]).path.endswith("/search/query")
    ]


# ──────────────────── 检索 ────────────────────


@pytest.mark.asyncio
async def test_search_reports_total_page_and_each_paper_with_scix_links(tmp_path: Path) -> None:
    session = QueuedAiohttpSession(_search_response(CLAY, TALK, total=108))
    context = _context(tmp_path, session)

    text, result = await _paper(context, "search bibstem:NatAs Mars")

    method, url, kwargs = session.requests[0]
    assert (method, url) == ("GET", "https://api.scixplorer.org/v1/search/query")
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    params = kwargs["params"]
    assert (params["q"], params["rows"], params["start"]) == ("bibstem:NatAs Mars", 10, 0)
    assert params["sort"].startswith("score desc")

    assert "共 108 篇" in text and "第 1–2 篇" in text
    assert "Surface clay formation on a cold ancient Mars" in text
    assert "Bishop, Janice L.; Fairén, Alberto G.; Michalski, Joseph R. 等 8 人" in text
    assert "2018 · Nature Astronomy · 同行评议 · 被引 153" in text
    assert "https://scixplorer.org/abs/2018NatAs...2..206B/abstract" in text
    # 会议摘要标出类型，零被引照常显示，没有的字段不占位；列表不列 DOI。
    assert "2022 · European Planetary Science Congress · 会议摘要 · 被引 0" in text
    assert "Ferus, Martin\n" in text
    assert "doi:" not in text

    outcome = result.command_outcome
    assert (outcome.state, outcome.page, outcome.has_more) == ("completed", 1, True)
    assert outcome.next_command == "/paper search bibstem:NatAs Mars --page 2"
    assert outcome.next_command in text


@pytest.mark.asyncio
async def test_search_options_select_page_size_page_and_order(tmp_path: Path) -> None:
    session = QueuedAiohttpSession(_search_response(CLAY, total=12))
    context = _context(tmp_path, session)

    text, result = await _paper(
        context, "search bibstem:NatAs --rows 5 --sort citations year:2018 --page 3"
    )

    params = session.requests[0][2]["params"]
    assert params["q"] == "bibstem:NatAs year:2018"
    assert (params["rows"], params["start"]) == (5, 10)
    assert params["sort"].startswith("citation_count desc")
    assert "第 11–11 篇" in text and "11. 📄" in text
    assert "第 3/3 页" in text
    assert (result.command_outcome.has_more, result.command_outcome.next_command) == (False, "")


@pytest.mark.asyncio
async def test_next_page_command_keeps_the_options_of_the_current_page(tmp_path: Path) -> None:
    session = QueuedAiohttpSession(_search_response(TALK, total=40))
    context = _context(tmp_path, session)

    _text, result = await _paper(context, "search mars --rows 5 --sort date --abs --hl")

    assert result.command_outcome.next_command == (
        "/paper search mars --page 2 --rows 5 --sort date --abs --hl"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        "search mars --rows 99",
        "search mars --rows 20 --abs",
        "search mars --rows 11 --hl",
        "search mars --rows many",
        "search mars --rows",
        "search mars --page 0",
        "search mars --sort newest",
        "search mars --verbose",
        "search --rows 5",
        "cite 2018NatAs...2..206B --format word",
    ],
)
async def test_malformed_options_are_reported_without_calling_scix(
    tmp_path: Path, args: str
) -> None:
    session = QueuedAiohttpSession()

    text, _result = await _paper(_context(tmp_path, session), args)

    assert text.startswith("❌")
    assert session.requests == []


@pytest.mark.asyncio
async def test_search_without_matches_is_a_completed_empty_result(tmp_path: Path) -> None:
    session = QueuedAiohttpSession(_search_response(), _search_response(total=12))
    context = _context(tmp_path, session)
    query   = '"Nature Astronomy planetary science"'

    text, result = await _paper(context, f"search {query}")
    past_end, past_out = await _paper(context, "search mars --page 5")

    assert query in text
    assert "📄" not in text
    assert (result.command_outcome.state, result.command_outcome.has_more) == ("completed", False)
    # 翻过末页时说明总数，和没有命中区分开。
    assert "共 12 篇" in past_end and "第 5 页" in past_end
    assert (past_out.command_outcome.has_more, past_out.command_outcome.next_command) == (False, "")


@pytest.mark.asyncio
async def test_free_text_commands_preserve_quotes_backslashes_and_spacing(
    tmp_path: Path,
) -> None:
    session = QueuedAiohttpSession(_search_response({"title": "ok"}))
    context = _context(tmp_path, session)
    query   = r'"fast radio burst" -3σ  C:\papers'

    text, _result = await _paper(context, f"search {query}")

    assert "ok" in text
    assert _queries(session) == [query]

    content   = r'-3σ  偏差\路径 "保留引号"'
    note_text = (await _paper(context, f"note 2401.12345 {content}"))[0]

    assert "已添加笔记" in note_text
    notes = context.state["paper_storage"].get_paper_notes("2401.12345", 10001)
    assert notes[0]["content"] == content


@pytest.mark.asyncio
async def test_author_search_escapes_the_name_and_lists_newest_first(tmp_path: Path) -> None:
    session = QueuedAiohttpSession(_search_response(CLAY, total=30))
    context = _context(tmp_path, session)

    _text, result = await _paper(context, r'author  A\B"C  --rows 5')

    params = session.requests[0][2]["params"]
    assert params["q"] == r'author:"A\\B\"C"'
    assert params["sort"].startswith("date desc")
    assert result.command_outcome.next_command == r'/paper author A\B"C --page 2 --rows 5'


@pytest.mark.asyncio
async def test_search_highlights_show_matching_sentences_from_abstract_and_body(
    tmp_path: Path,
) -> None:
    session = QueuedAiohttpSession(
        _search_response(
            {**RADAR, "id": "19738644"},
            {**CLAY, "id": "7"},
            highlighting={
                "19738644": {
                    "abstract": ["extent of <em>subglacial water</em> in this region"],
                    "body"    : ["used to detect <em>subglacial water.</em> East Antarctica"],
                },
                "7": {},
            },
        )
    )

    text, _result = await _paper(
        _context(tmp_path, session), 'search full:"subglacial water" bibstem:NatAs --hl'
    )

    params = session.requests[0][2]["params"]
    assert params["hl"] == "true"
    assert params["hl.q"] == '"subglacial water"'
    assert "id" in params["fl"].split(",")
    body     = text.index("正文：…used to detect 【subglacial water.】 East Antarctica…")
    abstract = text.index("摘要：…extent of 【subglacial water】 in this region…")
    assert text.index(RADAR["title"][0]) < body < abstract < text.index(CLAY["title"][0])


# ──────────────────── 摘要 ────────────────────


def _abstract_session(*, europe_pmc_status: int = 200) -> _RoutedSession:
    """SciX 只给第一篇摘要；第二篇在 arXiv，第三篇在 Europe PMC，第四篇哪里都没有。"""

    with_abstract = {
        **TALK,
        "abstract": "We measure CO<SUB>2</SUB> at 10<SUP>16</SUP> cm<SUP>-2</SUP> &amp; more.",
    }
    nowhere = {
        **TALK,
        "bibcode"   : "2021NatGe..14..225R",
        "identifier": ["2021NatGe..14..225R"],
        "author"    : [],
    }

    def respond(url: str, kwargs: dict[str, Any]) -> AiohttpResponse:
        host = urlsplit(url).hostname
        if host == "api.scixplorer.org":
            return _search_response(with_abstract, RADAR, CLAY, nowhere)
        if host == "export.arxiv.org":
            return aiohttp_text_response(_ARXIV_FEED, media_type="application/atom+xml")
        assert host == "www.ebi.ac.uk"
        return aiohttp_json_response(
            {
                "resultList": {
                    "result": [
                        {
                            "doi"         : "10.1038/S41550-017-0377-9",
                            "abstractText": "The rock record is at odds<sup>1</sup> with models.",
                        }
                    ]
                }
            },
            status=europe_pmc_status,
        )

    return _RoutedSession(respond)


@pytest.mark.asyncio
async def test_abstract_command_fills_missing_abstracts_from_public_sources(
    tmp_path: Path,
) -> None:
    session = _abstract_session()
    context = _context(tmp_path, session)

    text, result = await _paper(
        context,
        "abstract 2022EPSC...16..198F arXiv:2010.00870v2 "
        "https://doi.org/10.1038/s41550-017-0377-9 2021NatGe..14..225R "
        "not-an-id 2099ApJ...999..999Z",
    )

    assert _queries(session) == [
        'identifier:("2022EPSC...16..198F" OR "arXiv:2010.00870" OR '
        '"10.1038/s41550-017-0377-9" OR "2021NatGe..14..225R" OR "2099ApJ...999..999Z")'
    ]
    assert "摘要（来源：SciX）：\nWe measure CO2 at 10^16 cm^-2 & more." in text
    assert (
        "摘要（来源：arXiv 预印本）：\n"
        "New MARSIS data show bright reflectors with permittivity >15 at the base of the deposits."
    ) in text
    assert "摘要（来源：Europe PMC）：\nThe rock record is at odds^1 with models." in text
    assert "mars；planetary geology" in text
    assert "2018NatAs...2..206B · doi:10.1038/s41550-017-0377-9" in text
    # 没有作者的记录不留空的作者行。
    assert text.count("📄") == 4 and text.count("👤") == 3
    assert text.count("📝 摘要：SciX 接口不提供这家出版商的摘要") == 1
    assert "无法识别的论文标识：not-an-id" in text
    assert "SciX 未收录：2099ApJ...999..999Z" in text
    assert result.command_outcome.state == "completed"

    by_host = {urlsplit(url).hostname: kwargs for _method, url, kwargs in session.requests}
    assert by_host["export.arxiv.org"]["params"]["id_list"] == "2010.00870"
    assert by_host["www.ebi.ac.uk"]["params"]["query"] == 'DOI:"10.1038/s41550-017-0377-9"'
    # SciX Token 只发给 SciX。
    assert "Authorization" in by_host["api.scixplorer.org"]["headers"]
    assert "Authorization" not in by_host["export.arxiv.org"]["headers"]
    assert "Authorization" not in by_host["www.ebi.ac.uk"]["headers"]


@pytest.mark.asyncio
async def test_a_failing_public_source_only_leaves_that_paper_without_abstract(
    tmp_path: Path,
) -> None:
    session = _abstract_session(europe_pmc_status=503)

    text, result = await _paper(
        _context(tmp_path, session), "abstract arXiv:2010.00870 10.1038/s41550-017-0377-9"
    )

    assert "摘要（来源：arXiv 预印本）" in text
    assert "Europe PMC）" not in text
    assert text.count("📝 摘要：SciX 接口不提供这家出版商的摘要") == 1
    assert result.command_outcome.state == "completed"


@pytest.mark.asyncio
async def test_search_with_abstracts_appends_an_excerpt_and_its_source(tmp_path: Path) -> None:
    session = _abstract_session()

    text, _result = await _paper(_context(tmp_path, session), "search mars --abs")

    assert "abstract" in session.requests[0][2]["params"]["fl"].split(",")
    assert "📝 We measure CO2 at 10^16 cm^-2 & more.\n" in text
    assert "at the base of the deposits.（arXiv 预印本）" in text
    assert "📝 没有可用的公开摘要" in text

    long_paper = {**TALK, "abstract": "sentence " * 200}
    long_text  = (
        await _paper(
            _context(tmp_path, QueuedAiohttpSession(_search_response(long_paper))),
            "search mars --abs",
        )
    )[0]
    [excerpt] = [line for line in long_text.splitlines() if "📝" in line]
    assert excerpt.endswith("…") and excerpt.count("sentence") < 60


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("paper_id", "identifier"),
    [
        ("2018NatAs...2..206B", "2018NatAs...2..206B"),
        ("2021A&A...645A...7K", "2021A&A...645A...7K"),
        ("2401.12345", "arXiv:2401.12345"),
        ("2401.12345v2", "arXiv:2401.12345"),
        ("arXiv:2401.12345", "arXiv:2401.12345"),
        ("astro-ph/0701089", "arXiv:astro-ph/0701089"),
        ("https://arxiv.org/abs/2401.12345v2", "arXiv:2401.12345"),
        ("https://arxiv.org/pdf/2401.12345.pdf", "arXiv:2401.12345"),
        ("https://arxiv.org/abs/astro-ph/0701089v3", "arXiv:astro-ph/0701089"),
        ("10.1038/s41550-017-0377-9", "10.1038/s41550-017-0377-9"),
        ("doi:10.1038/s41550-017-0377-9", "10.1038/s41550-017-0377-9"),
        ("https://doi.org/10.1038/s41550-017-0377-9", "10.1038/s41550-017-0377-9"),
        ("https://scixplorer.org/abs/2021A%26A...645A...7K/abstract", "2021A&A...645A...7K"),
        ("https://ui.adsabs.harvard.edu/abs/2018NatAs...2..206B/abstract", "2018NatAs...2..206B"),
        ("https://scixplorer.org/abs/arXiv:2401.12345/abstract", "arXiv:2401.12345"),
    ],
)
async def test_every_supported_identifier_form_is_looked_up_by_scix_identifier(
    tmp_path: Path, paper_id: str, identifier: str
) -> None:
    session = QueuedAiohttpSession(_search_response())

    text, _result = await _paper(_context(tmp_path, session), f"cite-network {paper_id}")

    assert _queries(session) == [f'identifier:("{identifier}")']
    assert f"SciX 未收录：{identifier}" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        'cite-network 2026ApJ...001..001A" OR *:*',
        "related 2026ApJ...001..001A*",
        "summarize citations(bibcode:2026ApJ...001..001A)",
        "cite-network 2018NatAs...2..206B 2021NatAs...5...63L",
        "abstract " + " ".join(f"24{index:02d}.12345" for index in range(1, 10)),
        "cite-network",
    ],
)
async def test_unrecognized_or_excess_identifiers_never_reach_the_query(
    tmp_path: Path, args: str
) -> None:
    session = QueuedAiohttpSession()

    text, _result = await _paper(_context(tmp_path, session), args)

    assert text.startswith("❌")
    assert session.requests == []


@pytest.mark.asyncio
async def test_preprint_identifier_resolves_to_the_published_record(tmp_path: Path) -> None:
    session = QueuedAiohttpSession(
        _search_response(RADAR),
        aiohttp_json_response({"export": " @ARTICLE{2021NatAs...5...63L, title={Radar}} "}),
    )
    context = _context(tmp_path, session)

    added, _result = await _paper(context, "ref_add 2010.00870")
    again, _result = await _paper(
        _context(
            tmp_path,
            QueuedAiohttpSession(
                _search_response(RADAR),
                aiohttp_json_response({"export": "@ARTICLE{2021NatAs...5...63L, title={Radar}}"}),
            ),
        ),
        "ref_add 2021NatAs...5...63L",
    )
    refs, _result = await _paper(context, "refs")

    method, url, kwargs = session.requests[1]
    assert (method, url) == ("POST", "https://api.scixplorer.org/v1/export/bibtex")
    assert kwargs["json"] == {"bibcode": ["2021NatAs...5...63L"]}
    assert "已添加到文献库" in added
    assert "已在文献库中" in again
    assert "文献库 (1 条引用)" in refs and "Radar" in refs


# ──────────────────── 引用、网络、相关、统计、期刊 ────────────────────


@pytest.mark.asyncio
async def test_cite_exports_several_papers_in_the_requested_format(tmp_path: Path) -> None:
    export = aiohttp_json_response({"msg": "Retrieved 2 abstracts", "export": "\\bibitem{a}\n"})
    # 导出端点返回合法 JSON 却不附 Content-Type。
    del export.headers["Content-Type"]
    session = QueuedAiohttpSession(_search_response(CLAY, RADAR), export)

    text, _result = await _paper(
        _context(tmp_path, session),
        "cite 2018NatAs...2..206B arXiv:2010.00870 2099ApJ...999..999Z --format aastex",
    )

    method, url, kwargs = session.requests[1]
    assert (method, url) == ("POST", "https://api.scixplorer.org/v1/export/aastex")
    assert kwargs["json"] == {"bibcode": ["2018NatAs...2..206B", "2021NatAs...5...63L"]}
    assert "aastex，2 篇" in text and "\\bibitem{a}" in text
    assert "SciX 未收录：2099ApJ...999..999Z" in text


@pytest.mark.asyncio
async def test_cite_network_reports_true_totals_and_most_cited_neighbours(
    tmp_path: Path,
) -> None:
    def respond(_url: str, kwargs: dict[str, Any]) -> AiohttpResponse:
        query = kwargs["params"]["q"]
        if query.startswith("citations("):
            return _search_response(CLAY, total=151)
        if query.startswith("references("):
            return _search_response(total=0)
        return _search_response(RADAR)

    session = _RoutedSession(respond)

    text, _result = await _paper(_context(tmp_path, session), "cite-network 2021NatAs...5...63L")

    assert set(_queries(session)[1:]) == {
        "citations(bibcode:2021NatAs...5...63L)",
        "references(bibcode:2021NatAs...5...63L)",
    }
    assert "被 151 篇论文引用" in text
    assert "Bishop, Janice L.; Fairén, Alberto G. 等 8 人 2018 · 2018NatAs...2..206B" in text
    assert "/paper search citations(bibcode:2021NatAs...5...63L)" in text
    assert "参考文献 0 篇" in text
    assert "/paper search references(" not in text


@pytest.mark.asyncio
async def test_related_ranks_by_text_similarity_and_excludes_the_paper_itself(
    tmp_path: Path,
) -> None:
    session = QueuedAiohttpSession(_search_response(RADAR), _search_response(CLAY, total=9000))

    text, _result = await _paper(_context(tmp_path, session), "related 2021NatAs...5...63L")

    params = session.requests[1][2]["params"]
    assert params["q"] == "similar(bibcode:2021NatAs...5...63L) -bibcode:2021NatAs...5...63L"
    assert params["sort"] == "score desc"
    assert CLAY["title"][0] in text


@pytest.mark.asyncio
async def test_stats_profiles_a_query_by_citations_year_type_journal_and_keyword(
    tmp_path: Path,
) -> None:
    facets = {
        "year"              : ["2024", 8, "2017", 4, "2025", 6],
        "doctype_facet_hier": [
            "0/Article",
            40,
            "1/Article/Journal Article",
            38,
            "1/Article/e-print",
            2,
        ],
        "property"     : ["article", 40, "refereed", 30, "openaccess", 25, "notrefereed", 10],
        "bibstem_facet": ["NatAs", 38, "arXiv", 2],
        "keyword_facet": ["solar system general", 12],
    }

    def respond(_url: str, kwargs: dict[str, Any]) -> AiohttpResponse:
        params = kwargs["params"]
        assert params["q"] == "bibstem:NatAs collection:planetary"
        assert params["rows"] == 0
        if "stats.field" in params:
            return _search_response(
                total = 44,
                stats = {
                    "stats_fields": {"citation_count": {"sum": 597.0, "mean": 13.568, "max": 201.0}}
                },
            )
        name = params["facet.field"]
        return _search_response(total=44, facet_counts={"facet_fields": {name: facets[name]}})

    session = _RoutedSession(respond)

    text, result = await _paper(
        _context(tmp_path, session), "stats bibstem:NatAs collection:planetary"
    )

    assert "共 44 篇" in text
    assert "合计 597，篇均 13.6，单篇最高 201" in text
    assert "同行评议 30 篇，开放获取 25 篇" in text
    assert "年份：2017 4｜2024 8｜2025 6" in text
    assert "类型：期刊论文 38｜预印本 2" in text
    assert "NatAs 38｜arXiv 2" in text
    assert "solar system general 12" in text
    assert result.command_outcome.state == "completed"


@pytest.mark.asyncio
async def test_stats_without_matches_reports_the_empty_query(tmp_path: Path) -> None:
    def respond(_url: str, kwargs: dict[str, Any]) -> AiohttpResponse:
        name = kwargs["params"].get("facet.field", "")
        return _search_response(total=0, facet_counts={"facet_fields": {name: []}}, stats={})

    text, _result = await _paper(_context(tmp_path, _RoutedSession(respond)), "stats journal:x")

    assert "journal:x" in text
    assert "共" not in text


@pytest.mark.asyncio
async def test_journal_lookup_lists_bibstems_for_a_name(tmp_path: Path) -> None:
    session = QueuedAiohttpSession(
        aiohttp_json_response(
            {
                "journal": [
                    {"bibstem": "ApJ", "name": "The Astrophysical Journal"},
                    {"bibstem": "ApJL", "name": "The Astrophysical Journal Letters"},
                ]
            }
        ),
        aiohttp_json_response({"journal": []}),
    )
    context = _context(tmp_path, session)

    found, _result = await _paper(context, "journal astrophysical journal")
    missing, _result = await _paper(context, "journal zzz/qqq")

    assert session.requests[0][1] == (
        "https://api.scixplorer.org/v1/journals/journal/astrophysical%20journal"
    )
    assert session.requests[1][1].endswith("/journals/journal/zzz%2Fqqq")
    assert "ApJ — The Astrophysical Journal\n" in found
    assert "ApJL — The Astrophysical Journal Letters" in found
    assert "zzz/qqq" in missing and "—" not in missing


# ──────────────────── AI 摘要与每日推荐 ────────────────────


@pytest.mark.asyncio
async def test_summary_feeds_the_resolved_abstract_to_the_core_ai_route(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class AI:
        async def complete(self, route, messages):
            captured["route"]    = route
            captured["messages"] = messages
            return SimpleNamespace(content="统一摘要")

    def respond(url: str, _kwargs: dict[str, Any]) -> AiohttpResponse:
        if urlsplit(url).hostname == "export.arxiv.org":
            return aiohttp_text_response(_ARXIV_FEED, media_type="application/atom+xml")
        return _search_response(RADAR)

    text, _result = await _paper(
        _context(tmp_path, _RoutedSession(respond), ai=AI()), "summarize 2021NatAs...5...63L"
    )

    assert captured["route"] == "summary"
    prompt = captured["messages"][0]["content"]
    assert RADAR["title"][0] in prompt
    assert "bright reflectors with permittivity >15" in prompt
    assert "统一摘要" in text and "arXiv 预印本" in text


@pytest.mark.asyncio
async def test_summary_without_ai_or_with_failing_ai_shows_the_original_abstract(
    tmp_path: Path,
) -> None:
    class BrokenAI:
        async def complete(self, _route, _messages):
            raise RuntimeError("model down")

    paper = {**CLAY, "abstract": "Clays formed in short warm periods."}

    plain, _result = await _paper(
        _context(tmp_path, QueuedAiohttpSession(_search_response(paper))),
        "summarize 2018NatAs...2..206B",
    )
    failed, _result = await _paper(
        _context(tmp_path, QueuedAiohttpSession(_search_response(paper)), ai=BrokenAI()),
        "summarize 2018NatAs...2..206B",
    )

    assert "Clays formed in short warm periods." in plain
    assert "Clays formed in short warm periods." in failed
    assert PUBLIC_ERROR_PREFIX in failed and "model down" not in failed


@pytest.mark.asyncio
async def test_summary_says_so_when_no_public_abstract_exists(tmp_path: Path) -> None:
    session = QueuedAiohttpSession(_search_response(TALK))

    text, _result = await _paper(_context(tmp_path, session), "summarize 2022EPSC...16..198F")

    assert text.startswith("⚠️") and "Phosphine routes on Venus" in text
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_daily_reads_topics_off_the_event_loop_thread(tmp_path: Path) -> None:
    threads: dict[str, int] = {}

    class Storage(PaperStorage):
        def get_topics(self, user_id):
            threads["get_topics"] = threading.get_ident()
            return super().get_topics(user_id)

    (tmp_path / "research_topics.json").write_text(
        json.dumps({"keywords": [{"value": "LLM", "user": 1}, {"value": "agents", "user": 1}]}),
        encoding="utf-8",
    )
    session                        = QueuedAiohttpSession(_search_response())
    context                        = _context(tmp_path, session)
    context.state["paper_storage"] = Storage(tmp_path)

    text, _result = await _paper(context, "daily", user_id=1)

    params = session.requests[0][2]["params"]
    assert '"LLM" OR "agents"' in params["q"]
    assert "entdate:" in params["q"]
    assert params["rows"] > 0
    assert "entdate" in params["fl"].split(",")
    assert params["sort"] == "entdate desc,bibcode asc"
    assert threads["get_topics"] != threading.get_ident()
    assert "未找到" in text


# ──────────────────── 错误边界 ────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"error": "upstream error"},
        {"response": {"docs": None, "numFound": 1}},
        {"response": {"docs": []}},
    ],
)
async def test_invalid_service_payload_is_an_error_instead_of_empty_results(
    tmp_path: Path, payload: Any
) -> None:
    session = QueuedAiohttpSession(aiohttp_json_response(payload))

    text, result = await _paper(_context(tmp_path, session), "search FRB")

    assert result.command_outcome.state == "error"
    assert text.startswith(PUBLIC_ERROR_PREFIX)


@pytest.mark.asyncio
async def test_search_timeout_reaches_command_error_boundary(tmp_path: Path) -> None:
    class TimeoutSession:
        def request(self, *_args, **_kwargs):
            raise TimeoutError

    text, result = await _paper(_context(tmp_path, TimeoutSession()), "search FRB")

    assert result.command_outcome.state == "error"
    assert text.startswith(PUBLIC_ERROR_PREFIX)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, "检索式"),
        (401, "https://scixplorer.org/user/settings/token"),
        (429, "额度"),
    ],
)
async def test_scix_statuses_the_caller_can_act_on_get_their_own_reply(
    tmp_path: Path, status: int, expected: str
) -> None:
    session = QueuedAiohttpSession(aiohttp_json_response({"error": "nope"}, status=status))

    text, result = await _paper(_context(tmp_path, session), "search ((mars")

    assert result.command_outcome.state == "error"
    assert expected in text and f"HTTP {status}" in text
    assert not text.startswith(PUBLIC_ERROR_PREFIX)


@pytest.mark.asyncio
async def test_other_scix_statuses_are_public_errors(tmp_path: Path) -> None:
    session = QueuedAiohttpSession(aiohttp_json_response({}, status=503))

    text, result = await _paper(_context(tmp_path, session), "search mars")

    assert result.command_outcome.state == "error"
    assert text.startswith(PUBLIC_ERROR_PREFIX)


@pytest.mark.asyncio
async def test_remote_commands_need_a_token_while_help_and_local_data_do_not(
    tmp_path: Path,
) -> None:
    session = QueuedAiohttpSession()
    context = _context(tmp_path, session, token="")

    help_text, _result = await _paper(context, "help")
    topics_text, _result = await _paper(context, "topics")
    search_text, _result = await _paper(context, "search mars")
    alias_text, _result = await _paper(context, "abs 2401.12345")

    assert help_text == (await _paper(context, ""))[0] != ""
    assert "暂无研究兴趣关键词" in topics_text
    for text in (search_text, alias_text):
        assert "https://scixplorer.org/user/settings/token" in text
    assert session.requests == []


@pytest.mark.asyncio
async def test_local_resource_commands_share_one_user_lifecycle_and_reject_bad_mutations(
    tmp_path: Path,
) -> None:
    """所有本地资源都从真实 ``handle`` 入口完成增查错删闭环。"""

    context = _context(tmp_path, object(), token="")

    async def call(args: str) -> str:
        return (await _paper(context, args))[0]

    # 论文笔记：错误序号必须拒绝，且不能误删同一论文的真实笔记。
    assert "已添加笔记" in await call("note 2401.12345 第一条笔记")
    assert "第一条笔记" in await call("note 2401.12345")
    assert "删除失败" in await call("note del 2401.12345 99")
    assert "第一条笔记" in await call("note 2401.12345")
    assert "已删除" in await call("note del 2401.12345 1")
    assert "暂无笔记" in await call("note 2401.12345")

    # 写作灵感：同一章节贯穿添加、查询、错误删除和正确删除。
    assert "已添加" in await call("writing 引言 解释研究动机")
    assert "解释研究动机" in await call("writing 引言")
    assert "序号必须是数字" in await call("writing del 引言 bad")
    assert "解释研究动机" in await call("writing 引言")
    assert "已删除" in await call("writing del 引言 1")
    assert "暂无灵感" in await call("writing 引言")

    # 关键词：重复添加和删除不存在项都不应改变集合。
    assert "已添加关键词" in await call("topics add Fast Radio Burst")
    assert "已存在" in await call("topics add Fast Radio Burst")
    assert "fast radio burst" in (await call("topics")).casefold()
    assert "不存在" in await call("topics remove missing-topic")
    assert "fast radio burst" in (await call("topics")).casefold()
    assert "已删除关键词" in await call("topics remove Fast Radio Burst")
    assert "暂无研究兴趣关键词" in await call("topics")

    # 截稿日期：非法日期必须在写入前返回，随后仍只存在原记录。
    assert "已添加截稿日期" in await call("deadline add FRB2027 2027-07-22")
    assert "FRB2027 - 2027-07-22" in await call("deadline")
    assert "日期格式错误" in await call("deadline add Broken tomorrow")
    deadline_text = await call("deadline")
    assert deadline_text.count("FRB2027 - 2027-07-22") == 1
    assert "Broken" not in deadline_text
    assert "已删除截稿日期" in await call("deadline del 1")
    assert "暂无截稿日期" in await call("deadline")

    storage_instance = context.state["paper_storage"]
    assert storage_instance.get_paper_notes("2401.12345", 10001) == []
    assert storage_instance.writing_section_counts(10001) == {}
    assert storage_instance.get_topics(10001) == []
    assert storage_instance.get_deadlines(10001) == []


def test_ads_paper_requires_exact_positive_user_id():
    for value in (None, 0, -1, True, "1"):
        with pytest.raises(ValueError, match="positive integer user_id"):
            ads_main._require_user_id({"user_id": value})
    assert ads_main._require_user_id({"user_id": 1}) == 1


# ──────────────────── 本地存储 ────────────────────


class TestAdsPaperAsyncStorage:
    """存储读写放到工作线程里执行，不占用事件循环线程。"""

    @pytest.mark.asyncio
    async def test_topics_read_runs_off_the_event_loop_thread(self, tmp_path):
        threads: dict[str, int] = {}

        class Storage(PaperStorage):
            def get_topics(self, user_id):
                threads["get_topics"] = threading.get_ident()
                return super().get_topics(user_id)

        await note_commands.cmd_topics(Storage(tmp_path), "", 1)

        assert threads["get_topics"] != threading.get_ident()

    @pytest.mark.asyncio
    async def test_note_write_runs_off_the_event_loop_thread(self, tmp_path):
        threads: dict[str, int] = {}

        class Storage(PaperStorage):
            def add_paper_note(self, paper_id, content, user_id):
                threads["add_paper_note"] = threading.get_ident()
                return super().add_paper_note(paper_id, content, user_id)

            def get_paper_notes(self, paper_id, user_id):
                threads["get_paper_notes"] = threading.get_ident()
                return super().get_paper_notes(paper_id, user_id)

        await note_commands.cmd_note(Storage(tmp_path), "paper-1 test", 1)

        assert {"add_paper_note", "get_paper_notes"} <= threads.keys()
        assert threading.get_ident() not in threads.values()


class TestPaperStorageBehavior:
    def test_storage_roundtrip_for_notes_topics_and_deadlines(self, tmp_path):
        storage = PaperStorage(tmp_path)

        storage.add_paper_note("paper-1", "first note", 10001)
        assert storage.get_paper_notes("paper-1", 10001)[0]["content"] == "first note"

        assert storage.add_topic("FRB", 10001) is True
        assert storage.get_topics(10001) == ["frb"]
        assert storage.remove_topic("FRB", 10001) is True
        assert storage.get_topics(10001) == []

        storage.add_deadline("submit", "2026-05-01", 10001)
        deadlines = storage.get_deadlines(10001)
        assert len(deadlines) == 1
        assert deadlines[0]["name"] == "submit"

    def test_storage_owner_accepts_only_integer_or_legacy_numeric_string(self, tmp_path):
        payload = {
            "paper-1": [
                {"content": "integer", "user": 1},
                {"content": "legacy-string", "user": "1"},
                {"content": "boolean", "user": True},
                {"content": "float", "user": 1.0},
                {"content": "malformed", "user": "owner-1"},
            ]
        }
        (tmp_path / "paper_notes.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        notes = PaperStorage(tmp_path).get_paper_notes("paper-1", 1)

        assert [note["content"] for note in notes] == ["integer", "legacy-string"]

    def test_storage_filters_notes_and_deadlines_by_user_and_delete_uses_visible_order(
        self, tmp_path
    ):
        storage = PaperStorage(tmp_path)
        storage.add_paper_note("paper-1", "note-user-1", 10001)
        storage.add_paper_note("paper-1", "note-user-2", 10002)
        storage.add_deadline("later", "2026-06-01", 10001)
        storage.add_deadline("earlier", "2026-05-01", 10001)
        storage.add_deadline("other-user", "2026-04-01", 10002)

        user_notes  = storage.get_paper_notes("paper-1", 10001)
        other_notes = storage.get_paper_notes("paper-1", 10002)
        assert [note["content"] for note in user_notes] == ["note-user-1"]
        assert [note["content"] for note in other_notes] == ["note-user-2"]

        deadlines = storage.get_deadlines(10001)
        assert [deadline["name"] for deadline in deadlines] == ["earlier", "later"]

        assert storage.delete_deadline(0, 10001) is True
        remaining = storage.get_deadlines(10001)
        assert [deadline["name"] for deadline in remaining] == ["later"]
        assert [deadline["name"] for deadline in storage.get_deadlines(10002)] == ["other-user"]

    def test_storage_instances_share_lock_and_do_not_lose_updates(self, tmp_path):
        from concurrent.futures import ThreadPoolExecutor

        def add(index):
            PaperStorage(tmp_path).add_paper_note("paper", f"note-{index}", 1)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(add, range(40)))

        notes = PaperStorage(tmp_path).get_paper_notes("paper", 1)
        assert len(notes) == 40

    def test_writing_topics_and_references_are_owner_scoped(self, tmp_path):
        storage = PaperStorage(tmp_path)
        storage.add_writing_idea("intro", "user-one", 1)
        storage.add_writing_idea("intro", "user-two", 2)
        storage.add_topic("FRB", 1)
        storage.add_topic("AGN", 2)
        storage.add_reference(1, "bib-1", "@article{bib-1, title={One}}")
        storage.add_reference(2, "bib-2", "@article{bib-2, title={Two}}")

        assert [item["content"] for item in storage.get_writing_ideas("intro", 1)] == ["user-one"]
        assert storage.get_topics(1) == ["frb"]
        assert "bib-1" in storage.get_references(1)
        assert "bib-2" not in storage.get_references(1)
