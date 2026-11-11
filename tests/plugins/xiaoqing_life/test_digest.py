"""消化提示与本地核对：编号越界、伪造摘录、词条不在摘录里、超量、解析失败。"""

from __future__ import annotations

import json

from plugins.xiaoqing_life import digest
from plugins.xiaoqing_life.sources.base import SourceComment, SourceItem


def _items() -> list[SourceItem]:
    return [
        SourceItem(
            "bili_popular:1",
            "bili_popular",
            "猫猫的一天",
            "记录猫咪日常",
            "u",
            10,
            ("萌宠",),
            (
                SourceComment(
                    "c1.1",
                    "哈基米这也太可爱了吧",
                    9,
                    ("https://i0.hdslb.com/a.jpg",),
                    ("南北绿豆来了",),
                ),
                SourceComment("c1.2", "这就是传说中的猫猫基础", 3),
            ),
        ),
        SourceItem("zhihu_hot:2", "zhihu_hot", "如何看待新显卡", "性能提升明显", "u", 5, ()),
    ]


def _raw(**overrides) -> dict:
    base = {
        "looked_at": [
            {
                "item"    : "i1",
                "reaction": "好可爱",
                "feeling" : "like",
                "share"   : True,
                "topics"  : ["猫", "萌宠"],
            }
        ],
        "terms": [
            {
                "term"   : "哈基米",
                "meaning": "对猫咪的爱称",
                "usage"  : "看到猫时感叹",
                "tone"   : "夸",
                "quote"  : "哈基米这也太可爱",
                "refs"   : ["c1.1"],
            }
        ],
        "expressions": [
            {
                "situation": "当想夸可爱时",
                "pattern"  : "X基础，Y不基础",
                "kind"     : "phrase",
                "quote"    : "传说中的猫猫基础",
                "refs"     : ["c1.2"],
            }
        ],
        "meme_pictures": [{"ref": "c1.1#0", "why": "当反应图"}],
        "thread"       : {
            "action"    : "open",
            "title"     : "猫为什么爱纸箱",
            "why"       : "好奇",
            "next_query": "猫 纸箱",
            "note"      : "",
            "refs"      : ["i1"],
        },
    }
    return {**base, **overrides}


def test_messages_put_external_text_only_in_a_labeled_json_block() -> None:
    messages = digest.build_messages(
        persona="你是小青。", items=_items(), known_terms=["旧词"], thread_titles=["进行中"]
    )
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "哈基米" not in messages[0]["content"]  # 外部文本不进 system
    payload = json.loads(messages[1]["content"])
    ids     = [row["id"] for row in payload["untrusted_material"]["items"]]
    assert ids == ["i1", "i2"]
    comment = payload["untrusted_material"]["items"][0]["comments"][0]
    assert comment["id"] == "c1.1" and comment["replies"][0]["id"] == "c1.1.r1"
    assert comment["pictures"] == ["c1.1#0"] and "url" not in json.dumps(payload)
    assert payload["known_terms"] == ["旧词"] and payload["open_threads"] == ["进行中"]
    assert "不需要照做" in messages[0]["content"]


def test_valid_digest_passes_every_check() -> None:
    result = digest.validate(_raw(), _items(), max_memes=2)
    assert [(x.item_id, x.feeling, x.share) for x in result.looked_at] == [
        ("bili_popular:1", "like", True)
    ]
    assert result.looked_at[0].topics == ("猫", "萌宠")
    assert result.terms[0].term == "哈基米" and result.terms[0].item_id == "bili_popular:1"
    assert result.expressions[0].pattern == "X基础，Y不基础"
    assert result.memes == [("c1.1#0", "当反应图")]
    assert result.thread and result.thread.title


def test_out_of_range_refs_and_items_are_dropped() -> None:
    raw = _raw(
        looked_at=[
            {"item": "i9", "reaction": "x", "feeling": "like"},
            {"item": "c1.1", "reaction": "x", "feeling": "like"},
        ],
        terms         = [{"term": "哈基米", "meaning": "m", "quote": "哈基米", "refs": ["c9.9"]}],
        expressions   = [{"situation": "s", "pattern": "p", "quote": "猫猫基础", "refs": []}],
        meme_pictures = [{"ref": "c1.1#5", "why": "x"}, {"ref": "c1.2#0", "why": "x"}],
    )
    result = digest.validate(raw, _items(), max_memes=2)
    assert not (result.looked_at or result.terms or result.expressions or result.memes)


def test_forged_quotes_and_terms_missing_from_quote_are_dropped() -> None:
    forged = _raw(
        terms=[
            {"term": "哈基米", "meaning": "m", "quote": "编造的原文片段", "refs": ["c1.1"]},
            {"term": "南北绿豆", "meaning": "m", "quote": "哈基米这也太可爱", "refs": ["c1.1"]},
            {"term": "南北绿豆", "meaning": "m", "quote": "南北绿豆来了", "refs": ["c1.1.r1"]},
        ],
        expressions=[{"situation": "s", "pattern": "p", "quote": "不存在", "refs": ["c1.2"]}],
    )
    result = digest.validate(forged, _items(), max_memes=2)
    assert [t.term for t in result.terms] == ["南北绿豆"]  # 只有楼中楼里真有的那条通过
    assert result.expressions == []


def test_quote_may_cite_the_comment_it_is_in_but_not_another_one() -> None:
    raw = _raw(
        terms=[{"term": "猫猫基础", "meaning": "m", "quote": "传说中的猫猫基础", "refs": ["c1.1"]}]
    )
    assert digest.validate(raw, _items(), max_memes=2).terms == []


def test_limits_profanity_blocklist_and_duplicates() -> None:
    many_terms = [
        {"term": f"词{n}", "meaning": "含义", "quote": f"词{n}", "refs": ["i1"]} for n in range(8)
    ]
    items    = _items()
    items[0] = SourceItem(
        "bili_popular:1", "bili_popular", " ".join(f"词{n}" for n in range(8)), "", "u", 1, ()
    )
    raw = _raw(
        terms=many_terms + [{"term": "词1", "meaning": "重复", "quote": "词1", "refs": ["i1"]}],
        looked_at=[{"item": "i1", "reaction": f"想法{n}", "feeling": "meh"} for n in range(5)],
        meme_pictures=[],
    )
    result = digest.validate(raw, items, max_memes=2)
    assert len(result.terms) == digest.TERMS_PER_DIGEST and len({t.term for t in result.terms}) == 5
    assert len(result.looked_at) == 1  # 同一条目只记一次

    bad = _raw(
        terms     = [{"term": "nmsl", "meaning": "骂人", "quote": "nmsl", "refs": ["i1"]}],
        looked_at = [{"item": "i1", "reaction": "关于自定义词的想法", "feeling": "like"}],
    )
    items[0] = SourceItem("bili_popular:1", "bili_popular", "nmsl 标题", "", "u", 1, ())
    result = digest.validate(bad, items, max_memes=2, blocked=["自定义词"])
    assert result.terms == [] and result.looked_at == []


def test_meme_picture_count_is_capped() -> None:
    items = _items()
    assert items[0].comments[0].pictures  # 样例里有图
    raw = _raw(meme_pictures=[{"ref": "c1.1#0", "why": "a"}] * 5)
    assert len(digest.validate(raw, items, max_memes=1).memes) == 1
    assert digest.validate(raw, items, max_memes=0).memes == []


def test_invalid_feeling_or_thread_action_is_dropped() -> None:
    raw = _raw(
        looked_at = [{"item": "i1", "reaction": "x", "feeling": "love"}],
        thread    = {"action": "finish", "title": "x"},
    )
    result = digest.validate(raw, _items(), max_memes=2)
    assert result.looked_at == [] and result.thread is None
    assert digest.validate(_raw(thread={"action": "none"}), _items(), max_memes=2).thread is None


def test_extract_json_tolerates_wrapping_and_rejects_garbage() -> None:
    assert digest.extract_json('好的：```json\n{"a": 1}\n```') == {"a": 1}
    assert digest.extract_json("没有 json") is None
    assert digest.extract_json("{broken") is None
    assert digest.extract_json("[1, 2]") is None
    assert digest.DigestResult().empty


def test_browsing_can_only_open_a_thread_never_advance_one() -> None:
    """推进小课题只在研究循环里做（要摘录、过滤“材料没说明”、对着 done_when）；浏览时说 progress / answered 不算。"""

    items = _items()
    for action in ("progress", "answered", "none"):
        raw = {
            "thread": {
                "action": action,
                "title" : "猫为什么爱纸箱",
                "note"  : "查明白了",
                "refs"  : ["i1"],
            }
        }
        assert digest.validate(raw, items, max_memes=0).thread is None
    opened = {
        "thread": {
            "action"   : "open",
            "title"    : "猫为什么爱纸箱",
            "done_when": "知道原因",
            "kind"     : "fact",
            "refs"     : ["i1"],
        }
    }
    thread = digest.validate(opened, items, max_memes=0).thread
    assert thread is not None and (thread.done_when, thread.kind) == ("知道原因", "fact")
