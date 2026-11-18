"""SciX 检索、摘要、统计、引用导出、引用网络、相关论文与期刊查询命令。"""

import asyncio
import math
import re
from dataclasses import dataclass
from typing import Any

from core.delivery import CommandOutcome, DeliverySegments
from core.plugin_base import Segments, segments

from .abstract_sources import Abstract, resolve_abstracts
from .constants import (
    ABSTRACT_EXCERPT_CHARS,
    DEFAULT_SEARCH_ROWS,
    JOURNAL_ROWS,
    MAX_DETAILED_ROWS,
    MAX_HIGHLIGHT_SNIPPETS,
    MAX_PAPER_IDS,
    MAX_SEARCH_ROWS,
    MAX_SEARCH_START,
    NETWORK_PREVIEW_ROWS,
    RELATED_ROWS,
    STATS_FACET_ROWS,
    STATS_YEAR_ROWS,
)
from .formatting import (
    clean_text,
    format_authors,
    format_paper,
    highlight_text,
    paper_list_text,
    paper_title,
    short_title,
)
from .identifiers import PaperId, parse_paper_id
from .scix_client import (
    EXPORT_FORMATS,
    PAPER_DETAIL_FIELDS,
    PAPER_LIST_FIELDS,
    SLOW_QUERY_TIMEOUT,
    SciXClient,
    escape_query_term,
)

_ID_USAGE = "<Bibcode / arXiv ID / DOI / 链接>"

# 排序写法 -> (SciX sort 表达式, 展示名)
_SORTS = {
    "relevance": ("score desc,citation_count desc", "相关度"),
    "citations": ("citation_count desc,date desc", "被引次数"),
    "date"     : ("date desc,bibcode asc", "发表时间"),
    "reads"    : ("read_count desc,date desc", "近期阅读量"),
}
# 正文片段排在前面：摘要可以用 abstract 命令完整取到，正文里的原句只有这里能看到。
_HIGHLIGHT_LABELS  = {"body": "正文", "abstract": "摘要"}
_OPTION_PATTERN    = re.compile(r"(?<!\S)--([A-Za-z][\w-]*)(?:=(\S*))?(?!\S)")
_NEXT_TOKEN        = re.compile(r"\s+(?!--)(\S+)")
_QUOTED_PHRASE     = re.compile(r'"([^"]+)"')
_ID_SEPARATOR      = re.compile(r"[\s，、]+")
_LIST_VALUE_OPTION = frozenset({"rows", "page", "sort"})
_LIST_FLAGS        = frozenset({"abs", "hl"})
# 分面 doctype_facet_hier 第二层的叶子名
_FACET_DOCTYPE_LABELS = {
    "Journal Article"    : "期刊论文",
    "e-print"            : "预印本",
    "Proceedings Article": "会议论文",
    "Abstract"           : "会议摘要",
    "Book Chapter"       : "书章",
    "Thesis"             : "学位论文",
    "Proposal"           : "观测申请",
    "Dataset"            : "数据集",
    "Software"           : "软件",
    "Catalog"            : "星表",
    "Circular"           : "通告",
    "PhD Thesis"         : "博士论文",
    "Masters Thesis"     : "硕士论文",
    "Book"               : "专著",
    "Press Release"      : "新闻稿",
    "Newsletter"         : "通讯",
    "Other"              : "其他",
}


def _completed(text: str, **outcome: Any) -> Segments:
    return DeliverySegments(segments(text), outcome=CommandOutcome("completed", **outcome))


def split_options(
    args: str, value_options: frozenset[str], flags: frozenset[str]
) -> tuple[str, dict[str, str], str]:
    """把 ``--名称 值`` 和 ``--开关`` 从参数里取出，其余文本原样留给调用方。

    返回 (剩余文本, 选项, 错误说明)。SciX 查询语法不使用 ``--``，所以查询里出现
    不认识的 ``--词`` 一律按写错的选项报告。
    """

    options: dict[str, str] = {}
    kept   : list[str]      = []
    position                = 0
    for match in _OPTION_PATTERN.finditer(args):
        name, value = match.group(1).lower(), match.group(2)
        end = match.end()
        if name in flags:
            value = ""
        elif name in value_options:
            if value is None:
                following = _NEXT_TOKEN.match(args, end)
                if following is None:
                    return "", {}, f"❌ --{name} 后面要跟一个值"
                value, end = following.group(1), following.end()
        else:
            known = "、".join(f"--{item}" for item in sorted(value_options | flags))
            return "", {}, f"❌ 无法识别的选项 --{name}；可用选项：{known}"
        options[name] = value
        # 选项前的空白随选项一起去掉，剩余文本内部的空白保持原样。
        kept.append(args[position : match.start()].rstrip())
        position = end
    kept.append(args[position:])
    return "".join(kept).strip(), options, ""


@dataclass(frozen=True, slots=True)
class ListOptions:
    rows     : int
    page     : int
    sort     : str
    abstracts: bool
    highlight: bool

    def suffix(self, default_sort: str) -> str:
        """除页码外、与默认值不同的选项，用来拼下一页命令。"""

        parts = [
            f"--rows {self.rows}" if self.rows != DEFAULT_SEARCH_ROWS else "",
            f"--sort {self.sort}" if self.sort != default_sort else "",
            "--abs" if self.abstracts else "",
            "--hl" if self.highlight else "",
        ]
        return "".join(f" {part}" for part in parts if part)


def _bounded_int(raw: str, name: str, upper: int) -> tuple[int, str]:
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if 1 <= value <= upper:
        return value, ""
    return 0, f"❌ --{name} 取 1–{upper} 的整数"


def parse_list_options(args: str, default_sort: str) -> tuple[str, ListOptions | None, str]:
    """解析列表类命令的选项，返回 (查询文本, 选项, 错误说明)。"""

    rest, raw, error = split_options(args, _LIST_VALUE_OPTION, _LIST_FLAGS)
    if error:
        return "", None, error
    detailed = "abs" in raw or "hl" in raw
    rows, error = _bounded_int(
        raw.get("rows", str(DEFAULT_SEARCH_ROWS)),
        "rows",
        MAX_DETAILED_ROWS if detailed else MAX_SEARCH_ROWS,
    )
    if error:
        if detailed:
            error += f"（带 --abs 或 --hl 时每页最多 {MAX_DETAILED_ROWS} 篇，其余用 --page 翻页）"
        return "", None, error
    last_page = MAX_SEARCH_START // rows + 1
    page, error = _bounded_int(raw.get("page", "1"), "page", last_page)
    if error:
        return "", None, error
    sort = raw.get("sort", default_sort).lower()
    if sort not in _SORTS:
        return "", None, f"❌ --sort 可选：{'、'.join(_SORTS)}"
    return rest, ListOptions(rows, page, sort, "abs" in raw, "hl" in raw), ""


def _highlight_terms(query: str) -> str:
    """要标出的词：查询里带引号的短语；没有短语时用整条查询。"""

    phrases = [phrase.strip() for phrase in _QUOTED_PHRASE.findall(query) if phrase.strip()]
    return " OR ".join(f'"{phrase}"' for phrase in phrases) or query


def _abstract_excerpt(abstract: Abstract | None) -> str:
    if abstract is None:
        return "   📝 没有可用的公开摘要"
    text = abstract.text
    if len(text) > ABSTRACT_EXCERPT_CHARS:
        text = f"{text[:ABSTRACT_EXCERPT_CHARS]}…"
    source = "" if abstract.source == "SciX" else f"（{abstract.source}）"
    return f"   📝 {text}{source}"


def _highlight_lines(snippets: dict[str, list[str]]) -> list[str]:
    lines = [
        f"   💬 {label}：…{highlight_text(snippet)}…"
        for name, label in _HIGHLIGHT_LABELS.items()
        for snippet in snippets.get(name, [])
    ]
    return lines[:MAX_HIGHLIGHT_SNIPPETS]


async def _list_papers(
    client: SciXClient,
    query: str,
    options: ListOptions,
    *,
    command: str,
    default_sort: str,
    title: str,
    empty_text: str,
) -> Segments:
    """执行一页检索并排版；command 是不带选项的原命令，用来给出下一页。"""

    start = (options.page - 1) * options.rows
    page  = await client.search(
        query,
        fields    = PAPER_DETAIL_FIELDS if options.abstracts else PAPER_LIST_FIELDS,
        rows      = options.rows,
        start     = start,
        sort      = _SORTS[options.sort][0],
        highlight = _highlight_terms(query) if options.highlight else "",
    )
    if not page.docs:
        if page.total == 0:
            return _completed(empty_text, has_more=False, page=options.page)
        return _completed(
            f"🔍 共 {page.total} 篇，第 {options.page} 页没有记录",
            has_more = False,
            page     = options.page,
        )

    abstracts = await resolve_abstracts(client.session, page.docs) if options.abstracts else None
    blocks    = []
    for offset, paper in enumerate(page.docs):
        lines = [f"{start + offset + 1}. {format_paper(paper)}"]
        if abstracts is not None:
            lines.append(_abstract_excerpt(abstracts[offset]))
        lines.extend(_highlight_lines(page.highlights.get(str(paper.get("bibcode")), {})))
        blocks.append("\n".join(lines))

    total_pages = math.ceil(page.total / options.rows)
    has_more    = options.page < total_pages and start + options.rows <= MAX_SEARCH_START
    heading     = (
        f"{title}：共 {page.total} 篇，第 {start + 1}–{start + len(page.docs)} 篇，"
        f"按{_SORTS[options.sort][1]}排序"
    )
    footer       = f"第 {options.page}/{total_pages} 页"
    next_command = ""
    if has_more:
        next_command = f"{command} --page {options.page + 1}{options.suffix(default_sort)}"
        footer       = f"{footer}\n下一页：{next_command}"
    return _completed(
        "\n\n".join([heading, *blocks, footer]),
        next_command = next_command,
        has_more     = has_more,
        page         = options.page,
    )


async def cmd_search(client: SciXClient, args: str) -> Segments:
    query, options, error = parse_list_options(args, "relevance")
    if options is None:
        return segments(error)
    if not query:
        return segments("❌ 请提供检索式\n用法: /paper search <SciX 检索式> [选项]")
    return await _list_papers(
        client,
        query,
        options,
        command      = f"/paper search {query}",
        default_sort = "relevance",
        title        = "📚 SciX 检索",
        empty_text   = f"🔍 SciX 没有匹配的记录：{query}",
    )


async def cmd_author(client: SciXClient, args: str) -> Segments:
    author, options, error = parse_list_options(args, "date")
    if options is None:
        return segments(error)
    if not author:
        return segments("❌ 请提供作者姓名\n用法: /paper author <作者姓名> [选项]")
    return await _list_papers(
        client,
        f'author:"{escape_query_term(author)}"',
        options,
        command      = f"/paper author {author}",
        default_sort = "date",
        title        = f"👤 作者 '{author}' 的论文",
        empty_text   = f"🔍 SciX 里没有作者 '{author}' 的论文",
    )


async def resolve_papers(
    client: SciXClient,
    args: str,
    command: str,
    *,
    limit: int = MAX_PAPER_IDS,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把参数里的论文标识换成 SciX 记录。

    返回 (找到的记录, 问题说明)；记录按给出的顺序排列并去重，
    写法认不出或 SciX 未收录的标识各占一条问题说明。
    """

    tokens = [token.rstrip(",") for token in _ID_SEPARATOR.split(args.strip()) if token]
    usage  = f"用法: /paper {command} {_ID_USAGE}"
    if not tokens:
        return [], [f"❌ 请提供论文标识\n{usage}"]
    if len(tokens) > limit:
        return [], [f"❌ 这条命令一次最多 {limit} 篇\n{usage}"]

    problems: list[str]     = []
    wanted  : list[PaperId] = []
    for token in tokens:
        paper_id = parse_paper_id(token)
        if paper_id is None:
            problems.append(f"❌ 无法识别的论文标识：{token}")
        elif paper_id not in wanted:
            wanted.append(paper_id)

    papers: list[dict[str, Any]] = []
    for paper_id, paper in zip(wanted, await client.lookup(wanted), strict=True):
        if paper is None:
            problems.append(f"❌ SciX 未收录：{paper_id.identifier}")
        elif paper not in papers:
            papers.append(paper)
    return papers, problems


async def resolve_paper(
    client: SciXClient, args: str, command: str
) -> tuple[dict[str, Any] | None, Segments]:
    """只接受一个标识的命令用它取回记录；失败时第二项是给用户的错误消息。"""

    papers, problems = await resolve_papers(client, args, command, limit=1)
    if papers:
        return papers[0], []
    return None, segments("\n".join(problems))


def _abstract_block(index: int, paper: dict[str, Any], abstract: Abstract | None) -> str:
    lines = [f"{index}. {format_paper(paper, with_doi=True)}"]
    subjects = paper.get("uat") or paper.get("keyword")
    if isinstance(subjects, list) and subjects:
        names = [clean_text(item) for item in subjects[:8] if isinstance(item, str)]
        lines.append(f"   🏷️ {'；'.join(name for name in names if name)}")
    if abstract is None:
        lines.append(
            "   📝 摘要：SciX 接口不提供这家出版商的摘要，arXiv 和 Europe PMC 也没有收录这篇"
        )
    else:
        lines.append(f"   📝 摘要（来源：{abstract.source}）：\n{abstract.text}")
    return "\n".join(lines)


async def cmd_abstract(client: SciXClient, args: str) -> Segments:
    """给出一到多篇论文的完整信息与摘要原文。"""

    papers, problems = await resolve_papers(client, args, "abstract")
    if not papers:
        return segments("\n".join(problems))
    abstracts = await resolve_abstracts(client.session, papers)
    blocks    = [
        _abstract_block(index, paper, abstract)
        for index, (paper, abstract) in enumerate(zip(papers, abstracts, strict=True), 1)
    ]
    return _completed("\n\n".join([*blocks, "\n".join(problems)]).rstrip())


def _facet_line(label: str, counts: list[tuple[str, int]]) -> str:
    return f"{label}：{'｜'.join(f'{name} {count}' for name, count in counts)}"


def _year_counts(counts: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """年份升序；年份太多时把较早的合并成一项。"""

    by_year = sorted((int(name), count) for name, count in counts if name.isdigit())
    recent  = by_year[-STATS_YEAR_ROWS:]
    earlier = sum(count for _year, count in by_year[:-STATS_YEAR_ROWS])
    result  = [(str(year), count) for year, count in recent]
    if earlier:
        result.insert(0, (f"{recent[0][0] - 1} 年及以前", earlier))
    return result


def _doctype_counts(counts: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """doctype_facet_hier 的取值形如 "1/Article/Journal Article"，只取第二层的叶子。"""

    result = []
    for name, count in counts:
        level, _, path = name.partition("/")
        if level == "1":
            leaf = path.rsplit("/", 1)[-1]
            result.append((_FACET_DOCTYPE_LABELS.get(leaf, leaf), count))
    return result


async def cmd_stats(client: SciXClient, args: str) -> Segments:
    """一条检索式的整体画像：总数、被引统计、年份、类型、期刊与关键词分布。"""

    query = args.strip()
    if not query:
        return segments("❌ 请提供检索式\n用法: /paper stats <SciX 检索式>")
    (total, citations), years, doctypes, properties, journals, keywords = await asyncio.gather(
        client.citation_stats(query),
        client.facet(query, "year", 300),
        client.facet(query, "doctype_facet_hier", 40),
        client.facet(query, "property", 40),
        client.facet(query, "bibstem_facet", STATS_FACET_ROWS),
        client.facet(query, "keyword_facet", STATS_FACET_ROWS),
    )
    if total == 0:
        return _completed(f"🔍 SciX 没有匹配的记录：{query}")

    lines         = [f"📊 SciX 统计：{query}", f"共 {total} 篇"]
    citation_sum  = citations.get("sum")
    citation_mean = citations.get("mean")
    citation_max  = citations.get("max")
    if all(
        isinstance(value, (int, float)) for value in (citation_sum, citation_mean, citation_max)
    ):
        lines.append(
            f"被引：合计 {citation_sum:.0f}，篇均 {citation_mean:.1f}，单篇最高 {citation_max:.0f}"
        )
    flags = dict(properties)
    lines.append(
        f"同行评议 {flags.get('refereed', 0)} 篇，开放获取 {flags.get('openaccess', 0)} 篇"
    )
    for label, counts in (
        ("📅 年份", _year_counts(years)),
        ("📑 类型", _doctype_counts(doctypes)),
        (f"📚 期刊（前 {STATS_FACET_ROWS}，bibstem）", journals),
        (f"🏷️ 关键词（前 {STATS_FACET_ROWS}）", keywords),
    ):
        if counts:
            lines.append(_facet_line(label, counts))
    return _completed("\n".join(lines))


async def cmd_cite(client: SciXClient, args: str) -> Segments:
    rest, options, error = split_options(args, frozenset({"format"}), frozenset())
    if error:
        return segments(error)
    export_format = options.get("format", "bibtex").lower()
    if export_format not in EXPORT_FORMATS:
        return segments(f"❌ --format 可选：{'、'.join(EXPORT_FORMATS)}")
    papers, problems = await resolve_papers(client, rest, "cite")
    if not papers:
        return segments("\n".join(problems))
    bibcodes = [str(paper["bibcode"]) for paper in papers]
    text     = await client.export(bibcodes, export_format)
    if not text:
        return segments(f"❌ SciX 没有返回引用内容：{'、'.join(bibcodes)}")
    heading = f"📎 引用（{export_format}，{len(bibcodes)} 篇）:"
    return _completed("\n\n".join([heading, text, "\n".join(problems)]).rstrip())


def _relation_preview(papers: list[dict[str, Any]]) -> list[str]:
    """引用关系里的一行一篇：短标题、前两位作者、年份、bibcode。"""

    lines = []
    for index, paper in enumerate(papers, 1):
        byline = " ".join(
            part
            for part in (format_authors(paper, max_authors=2), str(paper.get("year", "")))
            if part
        )
        lines.append(f"  {index}. {short_title(paper)} - {byline} · {paper.get('bibcode', '')}")
    return lines


async def cmd_cite_network(client: SciXClient, args: str) -> Segments:
    paper, error = await resolve_paper(client, args, "cite-network")
    if paper is None:
        return error
    bibcode = str(paper["bibcode"])
    citations, references = await asyncio.gather(
        *(
            client.search(
                f"{relation}(bibcode:{bibcode})",
                rows = NETWORK_PREVIEW_ROWS,
                sort = _SORTS["citations"][0],
            )
            for relation in ("citations", "references")
        )
    )
    lines = ["📊 引用网络", f"📄 {paper_title(paper)}", f"📎 {bibcode}", ""]
    for page, heading, relation in (
        (citations, "🔗 被 {} 篇论文引用", "citations"),
        (references, "📖 参考文献 {} 篇", "references"),
    ):
        lines.append(heading.format(page.total))
        if page.docs:
            lines.append(f"被引最多的 {len(page.docs)} 篇：")
            lines.extend(_relation_preview(page.docs))
            lines.append(f"完整清单：/paper search {relation}(bibcode:{bibcode})")
        lines.append("")
    return _completed("\n".join(lines).rstrip())


async def cmd_related(client: SciXClient, args: str) -> Segments:
    """按摘要文本相似度找相关论文。"""

    paper, error = await resolve_paper(client, args, "related")
    if paper is None:
        return error
    bibcode = str(paper["bibcode"])
    page    = await client.search(
        f"similar(bibcode:{bibcode}) -bibcode:{bibcode}",
        rows    = RELATED_ROWS,
        sort    = "score desc",
        timeout = SLOW_QUERY_TIMEOUT,
    )
    if not page.docs:
        return _completed("🔍 未找到相关论文")
    return _completed(paper_list_text(page.docs, f"🔗 与 '{short_title(paper)}' 内容相近的论文："))


async def cmd_journal(client: SciXClient, args: str) -> Segments:
    """按刊名或缩写查 bibstem，检索时用 bibstem:<缩写> 限定期刊。"""

    term = args.strip()
    if not term:
        return segments("❌ 请提供刊名或缩写\n用法: /paper journal <刊名或缩写>")
    journals = await client.journals(term)
    if not journals:
        return _completed(f"🔍 SciX 期刊库里没有匹配 '{term}' 的期刊")
    lines = [f"📚 匹配 '{term}' 的期刊（bibstem — 刊名）："]
    lines.extend(f"  {bibstem} — {name}" for bibstem, name in journals[:JOURNAL_ROWS])
    if len(journals) > JOURNAL_ROWS:
        lines.append(f"共 {len(journals)} 种，这里列出前 {JOURNAL_ROWS} 种")
    return _completed("\n".join(lines))
