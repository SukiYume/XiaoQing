"""
ads_paper 插件单元测试

测试 NASA ADS 论文管理插件的主要功能。
由于 ads_paper 插件使用相对导入，我们只测试文件结构和配置。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock

import pytest

from plugins.ads_paper import ai_commands, constants, note_commands, paper_commands, storage
from plugins.ads_paper import main as ads_main
from plugins.ads_paper.ads_client import ADSClient, paper_title
from plugins.ads_paper.storage import PaperStorage
from tests.helpers.assertions import text_segments_text
from tests.helpers.settings_snapshot import with_settings_reader

# 添加项目根目录到路径
ROOT = Path(__file__).resolve().parent.parent.parent


# ============================================================
# Tests
# ============================================================


class TestAdsPaperRuntimeContract:
    """Verify the installed package imports and exposes its real entrypoints."""

    def test_package_modules_and_entrypoints_import(self):
        assert ads_main.init() is None
        assert callable(ads_main.handle)
        assert callable(paper_commands.cmd_search)
        assert callable(note_commands.cmd_note)
        assert callable(ai_commands.cmd_summarize)
        assert constants.ARXIV_NEW_FORMAT_PATTERN.search("arXiv:2607.01234")
        assert storage.PaperStorage is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "paper_id",
    [
        "2401.12345",
        "2401.12345v2",
        "astro-ph/0701089",
        "https://arxiv.org/abs/2401.12345v2",
        "https://arxiv.org/abs/astro-ph/0701089v3",
    ],
)
async def test_resolve_missing_arxiv_identifier_never_falls_through_to_bibcode(
    paper_id: str,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.searched: list[str] = []

        async def search_by_arxiv_id(self, value: str):
            self.searched.append(value)
            return None

    client = Client()

    result = await paper_commands.resolve_paper_id_to_bibcode(cast(Any, client), paper_id)

    assert result is None
    assert client.searched == [paper_id]


@pytest.mark.asyncio
async def test_resolve_arxiv_identifier_returns_found_bibcode() -> None:
    class Client:
        async def search_by_arxiv_id(self, value: str):
            assert value == "2401.12345"
            return {"bibcode": "2024arXiv240112345A"}

    assert (
        await paper_commands.resolve_paper_id_to_bibcode(cast(Any, Client()), "2401.12345")
        == "2024arXiv240112345A"
    )


@pytest.mark.asyncio
async def test_resolve_real_ads_bibcode_bypasses_arxiv_search() -> None:
    class Client:
        async def search_by_arxiv_id(self, _value: str):
            raise AssertionError("ADS bibcodes must not be searched as arXiv identifiers")

    bibcode = "2026arXiv260122115P"
    assert await paper_commands.resolve_paper_id_to_bibcode(cast(Any, Client()), bibcode) == bibcode


@pytest.mark.asyncio
async def test_ads_query_builders_escape_author_and_reject_invalid_bibcodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ADSClient("token", object())
    captured: list[str] = []

    async def capture(query: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        del args, kwargs
        captured.append(query)
        return []

    monkeypatch.setattr(client, "_search_docs", capture)

    await client.search_by_author(r' A\B"C ')
    assert captured == [r'author:"A\\B\"C"']

    bibcode = "2026ApJ...001..001A"
    await client.get_paper_by_bibcode(bibcode)
    await client.get_citations(bibcode)
    await client.get_references(bibcode)
    assert captured[1:] == [
        f"bibcode:{bibcode}",
        f"citations(bibcode:{bibcode})",
        f"references(bibcode:{bibcode})",
    ]

    for method in (
        client.get_paper_by_bibcode,
        client.get_citations,
        client.get_references,
    ):
        with pytest.raises(ValueError, match="invalid ADS bibcode"):
            await method('2026ApJ...001..001A" OR *:*')


@pytest.mark.asyncio
async def test_resolve_invalid_non_arxiv_identifier_returns_none() -> None:
    class Client:
        async def search_by_arxiv_id(self, _value: str) -> None:
            raise AssertionError("invalid identifiers must not reach arXiv search")

    assert (
        await paper_commands.resolve_paper_id_to_bibcode(
            cast(Any, Client()), '2026ApJ...001..001A" OR *:*'
        )
        is None
    )


class TestAdsPaperConfig:
    """测试 ads_paper 配置"""

    def test_plugin_json_structure(self):
        """测试 plugin.json 结构"""
        plugin_json_path = ROOT / "plugins" / "ads_paper" / "plugin.json"
        with open(plugin_json_path, encoding="utf-8") as f:
            config = json.load(f)

        assert "name" in config
        assert "version" in config
        assert "description" in config
        assert "commands" in config

    def test_plugin_commands_have_help(self):
        """测试命令有帮助信息"""
        plugin_json_path = ROOT / "plugins" / "ads_paper" / "plugin.json"
        with open(plugin_json_path, encoding="utf-8") as f:
            config = json.load(f)

        for cmd in config.get("commands", []):
            if "name" in cmd:
                # 命令应该有描述或使用说明
                assert (
                    "help" in cmd or "usage" in cmd or "description" in cmd or cmd.get("help", "")
                )


@pytest.mark.asyncio
async def test_help_and_local_topics_do_not_require_ads_token(tmp_path):
    context = cast(
        Any,
        with_settings_reader(
            SimpleNamespace(
                secrets={"plugins": {"ads_paper": {}}},
                state={},
                data_dir=tmp_path,
                http_session=object(),
                request_id=None,
            )
        ),
    )
    help_result = await ads_main.handle("paper", "help", {}, context)
    topics_result = await ads_main.handle("paper", "topics", {"user_id": 1}, context)

    assert "论文与文献管理助手" in help_result[0]["data"]["text"]
    assert "暂无研究兴趣关键词" in topics_result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_free_text_commands_preserve_quotes_backslashes_and_spacing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = cast(
        Any,
        with_settings_reader(
            SimpleNamespace(
                secrets={"plugins": {"ads_paper": {"ads_token": "test-token"}}},
                state={},
                data_dir=tmp_path,
                http_session=object(),
                request_id="ads-free-text",
            )
        ),
    )
    event = {"user_id": 10001, "message_type": "private"}
    search = AsyncMock(return_value=[{"type": "text", "data": {"text": "ok"}}])
    monkeypatch.setattr(paper_commands, "cmd_search", search)
    query = r'"fast radio burst" -3σ  C:\papers'

    result = await ads_main.handle("paper", f"search {query}", event, context)

    assert "ok" in text_segments_text(result)
    search.assert_awaited_once()
    assert search.await_args.args[1] == query

    content = r'-3σ  偏差\路径 "保留引号"'
    note_result = await ads_main.handle(
        "paper",
        f"note 2401.12345 {content}",
        event,
        context,
    )

    assert "已添加笔记" in text_segments_text(note_result)
    notes = context.state["paper_storage"].get_paper_notes("2401.12345", 10001)
    assert notes[0]["content"] == content


@pytest.mark.asyncio
async def test_local_resource_commands_share_one_user_lifecycle_and_reject_bad_mutations(
    tmp_path: Path,
) -> None:
    """所有本地资源都从真实 ``handle`` 入口完成增查错删闭环。"""

    context = cast(
        Any,
        with_settings_reader(
            SimpleNamespace(
                secrets={"plugins": {"ads_paper": {}}},
                state={},
                data_dir=tmp_path,
                http_session=object(),
                request_id="ads-lifecycle",
            )
        ),
    )
    event = {"user_id": 10001, "message_type": "private"}

    async def call(args: str) -> str:
        return text_segments_text(
            await ads_main.handle("paper", args, event, context),
            separator="\n",
        )

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
    assert storage_instance.list_writing_sections(10001) == []
    assert storage_instance.get_topics(10001) == []
    assert storage_instance.get_deadlines(10001) == []


class TestAdsPaperAsyncStorage:
    def test_topics_read_uses_to_thread(self, monkeypatch):
        calls = []

        async def fake_to_thread(func, *args, **kwargs):
            calls.append(func.__name__)
            return func(*args, **kwargs)

        class Storage:
            def get_topics(self, user_id):
                return []

        monkeypatch.setattr(note_commands.asyncio, "to_thread", fake_to_thread)
        asyncio.run(note_commands.cmd_topics(cast(Any, Storage()), "", 1))

        assert "get_topics" in calls

    def test_note_write_uses_to_thread(self, monkeypatch):
        calls = []

        async def fake_to_thread(func, *args, **kwargs):
            calls.append(func.__name__)
            return func(*args, **kwargs)

        class Storage:
            def add_paper_note(self, paper_id, content, user_id):
                return True

            def get_paper_notes(self, paper_id, user_id=None):
                return [{"content": "c"}]

        monkeypatch.setattr(note_commands.asyncio, "to_thread", fake_to_thread)
        asyncio.run(note_commands.cmd_note(cast(Any, Storage()), "paper-1 test", 1))

        assert "add_paper_note" in calls
        assert "get_paper_notes" in calls

    def test_cmd_daily_reads_topics_in_thread(self, monkeypatch, tmp_path):
        from plugins.ads_paper import ai_commands

        calls = []

        async def fake_to_thread(func, *args, **kwargs):
            calls.append(func.__name__)
            return func(*args, **kwargs)

        class Client:
            async def search_papers(self, query, max_results, *, fields, sort):
                assert '"LLM" OR "agents"' in query
                assert "entdate:" in query
                assert max_results > 0
                assert "entdate" in fields
                assert sort == "entdate desc,bibcode asc"
                return []

        monkeypatch.setattr(ai_commands.asyncio, "to_thread", fake_to_thread)

        data_dir = tmp_path / "ads_paper"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "research_topics.json").write_text(
            json.dumps({"keywords": [{"value": "LLM", "user": 1}, {"value": "agents", "user": 1}]}),
            encoding="utf-8",
        )

        result = asyncio.run(ai_commands.cmd_daily(cast(Any, Client()), PaperStorage(data_dir), 1))

        assert "get_topics" in calls
        assert "未找到" in str(result)


class TestPaperStorageBehavior:
    def test_storage_roundtrip_for_notes_topics_and_deadlines(self, tmp_path):
        from plugins.ads_paper.storage import PaperStorage

        storage = PaperStorage(tmp_path)

        assert storage.add_paper_note("paper-1", "first note", 10001) is True
        assert storage.get_paper_notes("paper-1", 10001)[0]["content"] == "first note"

        assert storage.add_topic("FRB", 10001) is True
        assert storage.get_topics(10001) == ["frb"]
        assert storage.remove_topic("FRB", 10001) is True
        assert storage.get_topics(10001) == []

        assert storage.add_deadline("submit", "2026-05-01", 10001) is True
        deadlines = storage.get_deadlines(10001)
        assert len(deadlines) == 1
        assert deadlines[0]["name"] == "submit"

    def test_storage_filters_notes_and_deadlines_by_user_and_delete_uses_visible_order(
        self, tmp_path
    ):
        from plugins.ads_paper.storage import PaperStorage

        storage = PaperStorage(tmp_path)
        storage.add_paper_note("paper-1", "note-user-1", 10001)
        storage.add_paper_note("paper-1", "note-user-2", 10002)
        storage.add_deadline("later", "2026-06-01", 10001)
        storage.add_deadline("earlier", "2026-05-01", 10001)
        storage.add_deadline("other-user", "2026-04-01", 10002)

        user_notes = storage.get_paper_notes("paper-1", 10001)
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

        from plugins.ads_paper.storage import PaperStorage

        def add(index):
            return PaperStorage(tmp_path).add_paper_note("paper", f"note-{index}", 1)

        with ThreadPoolExecutor(max_workers=8) as pool:
            assert all(pool.map(add, range(40)))

        notes = PaperStorage(tmp_path).get_paper_notes("paper", 1)
        assert len(notes) == 40

    def test_writing_topics_and_references_are_owner_scoped(self, tmp_path):
        from plugins.ads_paper.storage import PaperStorage

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


def test_ads_paper_requires_exact_positive_user_id():
    for value in (None, 0, -1, True, "1"):
        with pytest.raises(ValueError, match="positive integer user_id"):
            ads_main._require_user_id({"user_id": value})
    assert ads_main._require_user_id({"user_id": 1}) == 1


@pytest.mark.asyncio
async def test_ads_search_filters_non_mapping_docs_and_formats_scalar_fields(monkeypatch):
    client = ADSClient("token", object())
    monkeypatch.setattr(
        client,
        "_request_json",
        AsyncMock(return_value={"response": {"docs": [None, "bad", {"title": "Full"}]}}),
    )

    assert await client.search_papers("frb") == [{"title": "Full"}]
    assert paper_title({"title": "Full title"}) == "Full title"
    assert ADSClient.format_paper_info({"title": "Full title", "author": "Solo"}).startswith(
        "📄 Full title\n   👤 Solo"
    )


@pytest.mark.asyncio
async def test_ads_client_search_passes_timeout():
    captured = {}

    class MockContent:
        async def iter_chunked(self, _size):
            yield b'{"response":{"docs":[]}}'

    class MockResponse:
        status = 200
        url = "https://api.adsabs.harvard.edu/v1/search/query"
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/json"}
        content_length = None
        content = MockContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def close(self):
            return None

    class MockSession:
        def request(self, *args, **kwargs):
            captured.update(kwargs)
            return MockResponse()

    client = ADSClient("token", MockSession())
    result = await client.search_papers("frb")

    assert result == []
    assert captured["timeout"].total == 30


@pytest.mark.asyncio
async def test_ads_bibtex_accepts_valid_json_without_content_type():
    """ADS BibTeX 端点偶尔省略 MIME，但正文仍是受限解析的合法 JSON。"""

    class MockContent:
        async def iter_chunked(self, _size):
            yield b'{"msg":"Retrieved 1 abstract","export":"@article{demo}"}'

    class MockResponse:
        status = 200
        url = "https://api.adsabs.harvard.edu/v1/export/bibtex"
        headers: ClassVar[dict[str, str]] = {}
        content_length = None
        content = MockContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def close(self):
            return None

    class MockSession:
        def request(self, *_args, **_kwargs):
            return MockResponse()

    client = ADSClient("token", MockSession())

    assert await client.get_bibtex("demo") == "@article{demo}"


@pytest.mark.asyncio
async def test_ads_summary_uses_core_ai_route_without_plugin_credentials():
    captured: dict[str, Any] = {}

    class Client:
        async def get_paper_by_bibcode(self, bibcode):
            assert bibcode == "2026ApJ...001..001A"
            return {"title": ["A title"], "abstract": "An abstract"}

    class AI:
        async def complete(self, route, messages):
            captured["route"] = route
            captured["messages"] = messages
            return SimpleNamespace(content="统一摘要")

    context = cast(
        Any,
        with_settings_reader(
            SimpleNamespace(
                capabilities=SimpleNamespace(ai=AI()),
                secrets={"plugins": {"ads_paper": {"ads_token": "token"}}},
                logger=SimpleNamespace(),
                request_id=None,
            )
        ),
    )

    result = await ai_commands.cmd_summarize(
        cast(Any, Client()),
        "2026ApJ...001..001A",
        context,
    )

    assert captured["route"] == "summary"
    assert "A title" in captured["messages"][0]["content"]
    assert "统一摘要" in result[0]["data"]["text"]
