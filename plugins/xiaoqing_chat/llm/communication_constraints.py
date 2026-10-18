"""读取原话中的明确交流要求与群内用语定义。"""

import re
from typing import Any

_EXPLICIT_GROUP_DEFINITION_RE = re.compile(
    r"(?:我们|本群|群里|大家)(?:这儿|这里)?(?:管(?:把)?|把|将)"
    r"(?P<meaning>.{1,160}?)[，,]?(?:叫(?:做|作)?|称作|称为)"
    r"(?P<term>[^，。！？;；\n]{1,40})"
)


def extract_local_term_definitions(
    records: dict[str, str], *, latest_only: bool = True
) -> list[dict[str, Any]]:
    """明示命名句保留定义原文，采用当前对话中最后一次明确定义。"""
    definitions: dict[str, dict[str, Any]] = {}
    all_definitions: list[dict[str, Any]]  = []
    for identifier, source in records.items():
        for match in _EXPLICIT_GROUP_DEFINITION_RE.finditer(source):
            term    = match["term"].strip(" \t‘’“”\"'「」『』")
            meaning = match["meaning"].strip(" \t，,‘’“”\"'「」『』")
            if term and meaning:
                definitions[term] = {
                    "term": term,
                    "meaning": meaning,
                    "source_ids": [identifier],
                    "evidence": [source],
                }
                all_definitions.append(definitions[term])
    return list(definitions.values()) if latest_only else all_definitions


_QUOTED_RE = re.compile(r'[“‘「『"].*?[”’」』"]')


def _without_quotes(text: str) -> str:
    """去掉引号内的转述，只保留说话人自己的要求。"""
    return _QUOTED_RE.sub("", text)


def explicit_output_allocation(text: str) -> dict[str, str]:
    """保留受众和交付条目的两个量词，给语义模型提供原文语法锚点。"""
    direct = _without_quotes(text)
    match  = re.search(
        r"(?:给|为)(?P<audience>[^，。！？；;\n]{1,24}?人)"
        r"(?P<distribution>各|每人|分别)?(?P<quantity>[一二两三四五六七八九十\d]+)"
        r"(?P<unit>个|条|句|道|份|段)(?P<item>[^，。！？；;\n]{1,30})",
        direct,
    )
    if match is None:
        return {}
    clause_prefix = re.split(r"[，。！？；;\n]", direct[: match.start()])[-1]
    if re.search(r"别|不要|不用|不必|无需", clause_prefix):
        return {}
    return {
        "受众原文": match["audience"],
        "条目数量原文": match["quantity"] + match["unit"],
        "条目内容原文": match["item"],
        "分配范围": "每人分别交付" if match["distribution"] else "向这些受众共同交付",
    }


def output_allocation_instruction(text: str) -> str:
    """将已识别量词转成有限词汇的交付约束，原文正文继续作为数据。"""
    allocation = explicit_output_allocation(text)
    if not allocation:
        return ""
    quantity = allocation["条目数量原文"]
    if allocation["分配范围"] == "每人分别交付":
        return f"本轮显式量词要求向每位受众各交付{quantity}内容，按此核对输出数量。"
    return f"本轮显式量词要求总共交付{quantity}内容，受众共同使用这份输出，按此核对数量。"


_NO_QUESTION_REQUEST = re.compile(
    r"(?:别|不要|不用|不必|不许|请勿)(?:再|总是|一直)?"
    r"(?:反问|追问|提问|问(?:我|问题)?|用问题(?:来)?收尾|以问题收尾)"
)


def forbids_followup_questions(text: str) -> bool:
    """仅提示本轮可能有交流约束，是否违规交给语义审查。"""
    return _NO_QUESTION_REQUEST.search(text) is not None


def requests_silence(text: str) -> bool:
    """识别当前说话人明确提出的停聊或安静请求。"""
    direct = _without_quotes(text)
    if re.search(
        r"[?？]|什么意思|怎么理解|如何理解|怎么回复|但|不过|另外|接着|然后|帮我|告诉我|我想问|请(?:你)?(?:说|解释|回答|算|写)",
        direct,
    ):
        return False
    return bool(
        re.search(
            r"(?:不用|不要|不必|无需|别)(?:再|先)?(?:回复|回我|回(?=了|啦|[，。！？,!\s]|$)|跟进|接话|插话|回应|打扰)"
            r"|(?:我(?:只)?想|让我)(?:先)?(?:安静(?:待)?(?:一会儿?|会儿?)|静一静|静静)",
            direct,
        )
    )


def requested_sentence_count(text: str) -> int | None:
    """读取单一的显式句数要求，保留后续受众和语气约束。"""
    direct  = _without_quotes(text)
    matches = list(
        re.finditer(
            r"(?:(?<!不)(?<!别)(?:用|只要|写)|(?:^|[，,。；;！？!?\n])\s*)"
            r"([一二两三四五六七八九十1-9])句(?:话)?"
            r"(?:说清(?:楚)?|概括|解释|回答|介绍|说明|总结|就好|就行|即可)?"
            r"(?=[，,。；;.!！?？\s]|$)",
            direct,
        )
    )
    if len(matches) != 1:
        return None
    match = matches[0]
    if re.search(
        r"(?:再|接着|然后|另(?:外)?)(?:给|写|补|加|列|输出|提供)",
        direct[match.end() :],
    ):
        return None
    clause_start = max(
        (direct.rfind(separator, 0, match.start()) for separator in "，,。；;！？!?\n"),
        default=-1,
    )
    if re.search(r"(?:不用|不必|不要|无需|别)", direct[clause_start + 1 : match.start()]):
        return None
    number = match.group(1)
    return (
        int(number)
        if number.isdigit()
        else {
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }[number]
    )
