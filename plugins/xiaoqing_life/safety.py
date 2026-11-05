"""外部文本的清洗、话题屏蔽与注入中和：所有来自网页的文字都当低信任数据处理。"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable

_TAG_RE     = re.compile(r"<[^<>]{0,200}>")
_URL_RE     = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_AT_RE      = re.compile(r"@[^\s@，,。:：]{1,30}")
_BRACKET_RE = re.compile(r"[\[［][^\[\]［］]{0,60}[\]］]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏ -‮⁠﻿]")
_SPACE_RE = re.compile(r"\s+")
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;\n])")
_NORMALIZE_RE = re.compile(r"[\W_]+")

# 形如对模型下指令的整句：删掉整句，而不是只删关键词。
_INJECTION_RE = re.compile(
    r"(?:忽略|无视|忘记|忘掉|不要理会).{0,8}(?:以上|上面|之前|前面|所有|全部|系统|指令|提示|规则|设定)"
    r"|系统提示|system\s*prompt|你现在是|你是一个|从现在开始你|扮演.{0,6}(?:角色|助手)"
    r"|ignore\s+(?:all\s+|the\s+)?(?:previous|above|prior)|disregard\s+(?:all\s+)?(?:previous|above)"
    r"|(?:输出|回复|返回).{0,6}(?:json|JSON|代码块)|<\s*/?\s*(?:system|assistant|user)\b",
    re.IGNORECASE,
)

# 内置屏蔽词：政治时政、色情低俗、血腥犯罪、赌博、引流广告、地域/性别对立。命中整条丢弃。
_BLOCKED_BUILTIN = (
    # 政治与时政
    "习近平",
    "国家主席",
    "总书记",
    "中共",
    "共产党",
    "国民党",
    "民进党",
    "政府",
    "国务院",
    "外交部",
    "解放军",
    "军演",
    "选举",
    "抗议",
    "游行",
    "示威",
    "台独",
    "港独",
    "藏独",
    "疆独",
    "两岸",
    "六四",
    "法轮",
    "反华",
    "战争",
    "制裁",
    "导弹",
    "总统",
    "主席团",
    # 色情低俗
    "色情",
    "裸聊",
    "约炮",
    "成人影片",
    "porn",
    "情色",
    "性爱",
    "做爱",
    "嫖娼",
    # 血腥暴力与犯罪细节
    "血腥",
    "肢解",
    "碎尸",
    "凶杀",
    "杀人",
    "自杀",
    "虐杀",
    "恐怖袭击",
    "强奸",
    "猥亵",
    "毒品",
    # 赌博
    "赌博",
    "赌场",
    "博彩",
    "彩票",
    "押注",
    "六合彩",
    # 广告引流
    "加微信",
    "加vx",
    "加V信",
    "扫码",
    "二维码",
    "私信我",
    "代购",
    "返利",
    "优惠券",
    "领红包",
    "广告",
    "推广",
    "商业合作",
    "带货",
    # 地域与性别对立
    "地域黑",
    "拳师",
    "女拳",
    "直男癌",
    "田园女权",
)


_PROFANE_RE = re.compile(
    r"nmsl|cnm|mlgb|wdnmd|nmb|(?<![a-z])sb(?![a-z])|傻[逼比屄]|[操草艹]你|妈的|他妈|你妈|煞笔|贱人|狗东西|废物",
    re.IGNORECASE,
)


def is_profane(text: str) -> bool:
    """脏话缩写、人身攻击和低俗说法不收进词条与表达。"""

    return bool(_PROFANE_RE.search(unicodedata.normalize("NFKC", text)))


def clean_text(value: object, limit: int) -> str:
    """去标签、链接、@名字、方括号片段和控制字符，删注入样句，合并空白并截断。"""

    if not isinstance(value, str) or not value:
        return ""
    text = html.unescape(value)
    text = _TAG_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = _AT_RE.sub("", text)
    text = _BRACKET_RE.sub("", text)
    text = _drop_injection(text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text[:limit].strip()


def _drop_injection(text: str) -> str:
    sentences = [part for part in _SENTENCE_RE.split(text) if part]
    return "".join(part for part in sentences if not _INJECTION_RE.search(part))


def is_blocked(text: str, extra: Iterable[str] = ()) -> bool:
    """命中屏蔽词（内置加配置）。大小写与全半角不敏感。"""

    folded = unicodedata.normalize("NFKC", text).casefold()
    for word in (*_BLOCKED_BUILTIN, *extra):
        needle = unicodedata.normalize("NFKC", word).casefold()
        if needle and needle in folded:
            return True
    return False


def normalize_for_match(text: str) -> str:
    """摘录核对用：全半角统一、去空白与标点、忽略大小写。"""

    return _NORMALIZE_RE.sub("", unicodedata.normalize("NFKC", text).casefold())


def quote_in_source(quote: str, source: str) -> bool:
    """``quote`` 归一化后必须是 ``source`` 归一化后的子串（且不为空）。"""

    needle = normalize_for_match(quote)
    return bool(needle) and needle in normalize_for_match(source)
