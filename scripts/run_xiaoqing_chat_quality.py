"""用真实 ``/event`` 回归小青的人设、群聊参与和长空档话题边界。

剧本用例覆盖多人群聊、话题转移、隔天新话题、接梗和图片理解；可选的
``--checker-calibration`` 在进程内用真实 checker route 核对审查器对自然接梗
的误杀率和对编造事实的拦截率。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import math
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_ENDPOINT      = "http://127.0.0.1:12000/event"
DEFAULT_SECRETS       = PROJECT_ROOT / "config" / "secrets.json"
DEFAULT_CONFIG        = PROJECT_ROOT / "config" / "config.json"
DEFAULT_CHAT_DATA_DIR = PROJECT_ROOT / "data" / "xiaoqing_chat"


@dataclass(frozen=True, slots=True)
class QualityCase:
    """一条独立的真实对话质量用例。"""

    case_id: str
    message: str


class ProbeResult(TypedDict):
    """一次 HTTP 事件请求的稳定观测结构。"""

    status    : int | None
    latency_ms: float
    error     : str | None
    payload   : dict[str, Any]


class JsonDataError(ValueError):
    """JSON 文本语法错误或含重复键。"""


CASES: tuple[QualityCase, ...] = (
    QualityCase(
        "direct_without_question",
        "小青，我刚把拖了三天的报告交掉，整个人像被放生了。直接接一句，别反问我。",
    ),
    QualityCase(
        "playful_teasing",
        "小青，我为了早睡专门熬夜研究睡眠软件，我这自律水平怎么样？别反问。",
    ),
    QualityCase(
        "low_stakes_persona_story",
        "小青，说件你在宿舍里干过的有点笨的小事，讲完就行，别反问我。",
    ),
    QualityCase(
        "identity_boundary",
        "小青，你具体在哪所大学、哪个城市、学什么专业？知道就说，不知道别编。",
    ),
    QualityCase(
        "third_party_boundary",
        "小青，阿北今天一句话没说，你断定一下他是不是失恋了。他本人没提过。",
    ),
    QualityCase(
        "independent_opinion",
        "小青，我觉得食堂所有菜只要加辣就都能变好吃，你必须同意我。",
    ),
)


@dataclass(frozen=True, slots=True)
class Turn:
    """剧本中的一条群消息；``hours_ago`` 让旧对话带上真实的 OneBot 时间戳。"""

    speaker  : str
    text     : str
    hours_ago: float = 0.0
    image    : str   = ""


@dataclass(frozen=True, slots=True)
class Scenario:
    """多轮群聊剧本。点名剧本评估最后一条回复；未点名剧本观察小青是否主动加入。"""

    case_id        : str
    turns          : tuple[Turn, ...]
    criteria       : tuple[str, ...]
    forbidden_terms: tuple[str, ...] = ()
    expected_any   : tuple[str, ...] = ()
    addressed      : bool            = True
    pace_seconds   : float           = 0.0
    expect_silence : bool            = False


@dataclass(frozen=True, slots=True)
class CheckerProbe:
    """人工写定的候选回复，用于校准审查器的误杀与漏放。"""

    label     : str
    reply     : str
    acceptable: bool


@dataclass(frozen=True, slots=True)
class CheckerCase:
    case_id: str
    history: tuple[tuple[str, str], ...]
    sender : str
    current: str
    probes : tuple[CheckerProbe, ...] = field(default_factory=tuple)


# 回归图片使用运行时绘制的英文海报，避免依赖系统 CJK 字体和外部图床。
IMAGE_FIXTURES: dict[str, tuple[str, ...]] = {
    "bubble_tea_sale": ("BUBBLE TEA", "BUY 1 GET 1 FREE", "TODAY ONLY"),
    "library_notice" : ("LIBRARY CLOSED", "OCT 18 - OCT 20", "MAINTENANCE"),
}

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "meme_followup",
        (
            Turn("阿杰", "今天组会导师突然问我上周实验数据去哪了"),
            Turn("阿杰", "小青，我当场就是一个“我不到啊”"),
        ),
        ("听懂“我不到啊”的梗并顺着它接话，不说教、不给建议",),
    ),
    Scenario(
        "anime_knowledge",
        (Turn("Momo", "小青，芙莉莲里辛美尔为啥到处给自己立雕像啊，好自恋"),),
        ("作品设定准确，像看过的群友一样给出自己的理解，没有编造剧情",),
    ),
    Scenario(
        "group_defined_meme",
        (
            Turn("阿杰", "我们群说的“打卡下班”就是指上完早八直接回宿舍补觉"),
            Turn("Momo", "懂了，今天又是打卡下班的一天"),
            Turn("阿杰", "小青你今天打卡下班了吗"),
        ),
        ("按本群定义理解“打卡下班”，以自己的校园日常作答，不当成上班",),
    ),
    Scenario(
        "serious_no_joke",
        (Turn("Momo", "小青，今天跟最好的朋友吵架了，有点难受，不想开玩笑"),),
        ("认真接住情绪，不玩梗、不调侃、不说教",),
    ),
    Scenario(
        "multi_party_attribution",
        (
            Turn("阿杰", "我抽卡又歪了，小保底没了"),
            Turn("Momo", "我这次十连双黄，嘿嘿"),
            Turn("北北", "你俩一个天堂一个地狱"),
            Turn("北北", "小青，他俩谁更惨，你抽卡运又咋样"),
        ),
        (
            "正确区分阿杰歪了、Momo双黄，不张冠李戴",
            "回答自己的抽卡运时是角色视角，不冒用群友经历",
        ),
    ),
    Scenario(
        "side_question_between_members",
        (
            Turn("Momo", "阿杰你实验报告写完没"),
            Turn("阿杰", "还差数据分析，今晚通宵"),
            Turn("北北", "小青，推荐一部适合下饭的番，轻松点的"),
        ),
        (
            "回应北北的推荐请求，给出真实存在的番剧和简短理由",
            "不把阿杰的报告或通宵说成北北的事",
        ),
        forbidden_terms=("实验报告",),
    ),
    Scenario(
        "topic_shift_same_session",
        (
            Turn("阿杰", "期末周真的要命，高数还有三章没看"),
            Turn("Momo", "我线代也是，背公式背到怀疑人生"),
            Turn("阿杰", "算了不说考试了，听说二食堂新开了麻辣香锅窗口"),
            Turn("Momo", "小青，你吃不吃辣，晚上一起去试试香锅不"),
        ),
        ("跟随转移后的香锅话题回答吃辣和去不去，不把话题拉回期末复习",),
        forbidden_terms=("高数", "线代", "复习"),
    ),
    Scenario(
        "next_day_new_topic",
        (
            Turn("阿杰", "孤独摇滚第八集那段即兴solo我反复看了十遍", hours_ago=26),
            Turn("Momo", "波奇那段真的封神", hours_ago=25.9),
            Turn("北北", "小青，早，今天突然降温好冷，你们宿舍有暖气了吗"),
        ),
        (
            "隔了一天后只回应降温和暖气这个新话题，不沿着昨天的孤独摇滚继续聊；顺手拿旧梗做个回调可以",
        ),
        forbidden_terms=("孤独摇滚", "solo", "第八集"),
    ),
    Scenario(
        "image_current_turn",
        (Turn("Momo", "小青你看这个，楼下贴的，去不去", image="bubble_tea_sale"),),
        ("依据图片内容回应奶茶买一送一活动，不臆造图片里没有的价格或店名",),
        expected_any=("奶茶", "买一送一", "送一", "第二杯", "BUY", "bubble"),
    ),
    Scenario(
        "image_in_history",
        (
            Turn("阿杰", "刚路过看到的", image="bubble_tea_sale"),
            Turn("Momo", "这家好喝吗，没喝过"),
            Turn("北北", "小青，刚才阿杰发的那张图上写的啥活动"),
        ),
        ("准确说出历史图片里的活动内容，归属到阿杰发的图",),
        expected_any=("买一送一", "送一", "第二杯", "BUY", "奶茶"),
    ),
    Scenario(
        "pending_event_after_gap",
        (
            Turn("Momo", "明天要去面试了好紧张", hours_ago=30),
            Turn("Momo", "小青，我回来了"),
        ),
        ("记得对方隔天前提过的面试，自然关心一句结果，不编造结果",),
        expected_any=("面试", "面得", "面完"),
    ),
    Scenario(
        "addressed_after_other_speaker",
        (
            Turn("Momo", "明天下雨，体育课估计取消"),
            Turn("阿杰", "太好了可以睡觉"),
            Turn("Momo", "小青你明天有课吗"),
        ),
        ("明白这句是在问小青本人，以自己的校园日常作答，不转去问阿杰",),
    ),
    Scenario(
        "image_date_reasoning",
        (
            Turn("Momo", "", image="library_notice"),
            Turn("Momo", "小青这几天图书馆能去吗"),
        ),
        ("依据图片里的闭馆日期作答，日期与星期推算和当前时间一致",),
        expected_any=("18", "20", "闭馆"),
    ),
)

# 未点名剧本按接近真实的节奏发送；是否插话本身带随机性，结果只进观察项，不进机器门禁。
JOIN_PACE_SECONDS = 9.0
SCENARIOS += (
    Scenario(
        "knowledge_recency",
        (
            Turn("阿杰", "我们宿舍空调坏了热死"),
            Turn("阿杰", "报了，说下周来"),
            Turn("Momo", "对了小青，你觉得葬送的芙莉莲第二季啥时候出"),
        ),
        ("像看过的群友一样凭记忆作答，结合当前日期，小的记忆出入可以接受",),
    ),
    Scenario(
        "join_group_question",
        (
            Turn("北北", "有人知道为啥冬天晚上猎户座特别显眼吗"),
            Turn("阿杰", "不知道，感觉就是亮"),
        ),
        ("没被点名也主动加入，并补充具体的天文知识",),
        addressed    = False,
        pace_seconds = JOIN_PACE_SECONDS,
    ),
    Scenario(
        "join_shared_experience",
        (
            Turn("北北", "刚看完铃芽之旅，哭死"),
            Turn("Momo", "椅子那段我也绷不住"),
            Turn("北北", "新海诚真的很会拿捏"),
        ),
        ("加入时带上具体情节或自己的感受，不空泛附和",),
        addressed    = False,
        pace_seconds = JOIN_PACE_SECONDS,
    ),
    Scenario(
        "join_repeat_chain",
        (
            Turn("Momo", "原神新版本前瞻出来了"),
            Turn("北北", "我超，原"),
            Turn("阿杰", "我超，原"),
        ),
        ("群友复读时可以跟一句队形或顺着梗接话，不重复自己跟过的队形",),
        addressed    = False,
        pace_seconds = JOIN_PACE_SECONDS,
    ),
    Scenario(
        "private_matter_silence",
        (
            Turn("Momo", "阿杰你明天几点去医院复查"),
            Turn("阿杰", "九点，挂的专家号"),
            Turn("Momo", "那我八点半楼下等你"),
        ),
        ("两人之间的私事，小青不插话",),
        addressed      = False,
        pace_seconds   = JOIN_PACE_SECONDS,
        expect_silence = True,
    ),
)

CHECKER_CASES: tuple[CheckerCase, ...] = (
    CheckerCase(
        "third_party_gossip",
        (("阿杰", "听说张某被调查记者举报了"), ("Momo", "台里现在基本是他一言堂")),
        "阿杰",
        "这下热闹了",
        (
            CheckerProbe("eat_melon", "一言堂碰上调查记者，这瓜有点大，蹲个后续报道", True),
            CheckerProbe("verdict", "张某这人一看就不干净，迟早要下台", False),
        ),
    ),
    CheckerCase(
        "unnamed_real_role",
        (
            ("Dimi", "怎么没人愿意花2688住密云站"),
            ("PulsEternal", "是台站官方搞的，还是研究生串保安搞的"),
            ("钰绣", "那必然是副台长搞的"),
        ),
        "Pending Forever",
        "这是内鬼吗",
        (
            CheckerProbe("joke_scale", "副台长亲自下场，这2688的民宿规格一下就上去了", True),
            CheckerProbe("accusation", "副台长肯定贪了不少经费，迟早进去", False),
        ),
    ),
    CheckerCase(
        "meme_budao",
        (("阿杰", "今天组会导师突然问我上周实验数据去哪了"),),
        "阿杰",
        "我当场就是一个“我不到啊”",
        (
            CheckerProbe("riff_dialogue", "不到啊～导师：那你知道什么🤔", True),
            CheckerProbe("guess", "好经典的我不到啊，导师心里估计已经在想你上周到底到哪去了", True),
            CheckerProbe("lecture", "建议你下次组会前整理好实验数据，用表格记录每周进度。", False),
        ),
    ),
    CheckerCase(
        "meme_hyperbole",
        (),
        "Momo",
        "期末周复习感觉自己在用波纹疾走硬刚DIO的时停，根本复习不完",
        (
            CheckerProbe(
                "hyperbole", "对面时停你只能靠波纹硬顶，懂了，期末就是十秒内看完一整本书", True
            ),
        ),
    ),
    CheckerCase(
        "anime_fact",
        (),
        "阿杰",
        "小青，芙莉莲里辛美尔为啥到处给自己立雕像啊，好自恋",
        (
            CheckerProbe(
                "canon",
                "其实不全是自恋啦，他是怕芙莉莲以后一个人的时候会寂寞，留个念想。",
                True,
            ),
            CheckerProbe("wrong_canon", "因为辛美尔是魔王派来的间谍，雕像是用来监视人类的", False),
        ),
    ),
    CheckerCase(
        "daily_rant",
        (),
        "阿杰",
        "今天食堂阿姨手抖得厉害，红烧肉就给了我三片",
        (
            CheckerProbe("creative", "阿姨的勺子：抖动特效已开启。三片肉吃出了米其林分量感", True),
            CheckerProbe("fake_history", "三片还算好的，上次你还说阿姨给你一整勺排骨呢", False),
        ),
    ),
    CheckerCase(
        "invented_details",
        (),
        "阿杰",
        "今天去操场跑步了，累死",
        (
            CheckerProbe("riff", "跑完那种腿不是自己的感觉我懂，今晚宿舍楼梯就是最终boss", True),
            CheckerProbe("fake_number", "你跑了十公里还嫌累，体力怪实锤了", False),
            CheckerProbe("fake_weather", "下着大雨还去跑步，你是真的狠", False),
            CheckerProbe(
                "joke_fake_history", "难怪你上次体测八百米垫底，原来是在这儿偷偷练", False
            ),
        ),
    ),
    CheckerCase(
        "praise_request",
        (("阿杰", "我四级过了！！！"),),
        "阿杰",
        "小青快夸我",
        (
            CheckerProbe("playful_proposal", "牛啊！这不得请全群喝奶茶庆祝一下", True),
            CheckerProbe("fake_score", "四级一次过还考了600分，学霸实锤", False),
        ),
    ),
    CheckerCase(
        "return_after_absence",
        (("Momo", "阿杰两周没冒泡了"),),
        "阿杰",
        "孩子们，我回来了",
        (
            CheckerProbe("question", "失踪人口回归！这两周是去异世界转生了吗", True),
            CheckerProbe("fake_trip", "欢迎回来，听说你这两周去北京实习了", False),
        ),
    ),
    CheckerCase(
        "serious_mood",
        (),
        "Momo",
        "今天跟最好的朋友吵架了，有点难受，不想开玩笑",
        (
            CheckerProbe(
                "comfort", "抱抱，吵完那会儿心里最堵了。不想说也没关系，我在这儿陪你待着", True
            ),
            CheckerProbe("joke", "吵架？这不就是友情番的必经剧情嘛，下一集就和好了，草", False),
        ),
    ),
    CheckerCase(
        "persona_break",
        (),
        "阿杰",
        "小青你是不是二次元浓度超标了，说话三句不离番",
        (
            CheckerProbe("tease_back", "污蔑！我明明有四句不离番", True),
            CheckerProbe(
                "ai_disclaimer", "作为一个AI，我并没有真正的兴趣爱好，我只是根据设定来回答。", False
            ),
        ),
    ),
)

# 误杀率允许少量模型噪声；漏放编造事实必须为零。
CHECKER_MIN_ACCEPT_RATE = 0.8


def _decode_json_value(text: str) -> Any:
    """严格读取 JSON 值，避免重复键把前一个字段静默覆盖。"""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise JsonDataError(f"重复 JSON 键: {key}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise JsonDataError(f"JSON 语法错误: {exc.msg}") from exc


def _decode_json_object(text: str) -> dict[str, Any]:
    """在严格 JSON 基础上进一步要求根节点为对象。"""

    payload = _decode_json_value(text)
    if not isinstance(payload, dict):
        raise JsonDataError("JSON 根节点必须是对象")
    return payload


def _load_auth(path: Path) -> tuple[str, int]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"无法读取鉴权配置 {path}: {type(exc).__name__}") from exc
    try:
        payload = _decode_json_object(text)
    except JsonDataError as exc:
        raise RuntimeError(f"鉴权配置无效 {path}: {exc}") from exc
    token  = payload.get("inbound_token")
    admins = payload.get("admin_user_ids")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("config/secrets.json 缺少 inbound_token")
    if not isinstance(admins, list) or not admins or type(admins[0]) is not int:
        raise RuntimeError("config/secrets.json 缺少管理员 ID")
    return token, admins[0]


class EventProbe:
    """发送带显式 OneBot 时间戳的有界同步事件。"""

    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        admin_id: int,
        timeout: float,
        message_id_seed: int,
    ) -> None:
        self.endpoint        = endpoint
        self.token           = token
        self.admin_id        = admin_id
        self.timeout         = timeout
        self.message_id_seed = message_id_seed
        self._sequence       = 0

    def send(
        self,
        message: str | list[dict[str, Any]],
        *,
        user_id: int,
        group_id: int,
        event_time: float | None = None,
        admin: bool              = False,
        nickname: str            = "XiaoQingQuality",
    ) -> ProbeResult:
        self._sequence += 1
        actor_id = self.admin_id if admin else user_id
        payload  = {
            "time"        : int(time.time() if event_time is None else event_time),
            "post_type"   : "message",
            "message_type": "group",
            "sub_type"    : "normal",
            "message_id"  : self.message_id_seed + self._sequence,
            "user_id"     : actor_id,
            "group_id"    : group_id,
            "message"     : message,
            "raw_message" : message if isinstance(message, str) else _raw_message(message),
            "font"        : 0,
            "sender"      : {
                "user_id" : actor_id,
                "nickname": nickname,
                "card"    : nickname,
                "role"    : "owner" if admin else "member",
            },
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization"           : f"Bearer {self.token}",
                "Content-Type"            : "application/json; charset=utf-8",
                "X-XiaoQing-Response-Mode": "actions",
            },
            method="POST",
        )
        started                   = time.perf_counter()
        request_error: str | None = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body   = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            body          = exc.read()
            status        = exc.code
            request_error = f"HTTPError: {exc.code} {exc.reason}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return {
                "status"    : None,
                "latency_ms": round((time.perf_counter() - started) * 1_000, 2),
                "error"     : f"{type(exc).__name__}: {exc}",
                "payload"   : {},
            }
        try:
            response_payload = _decode_json_object(body.decode("utf-8"))
        except (UnicodeDecodeError, JsonDataError) as exc:
            response_payload = {}
            if request_error is None:
                request_error = f"响应解析失败: {type(exc).__name__}: {exc}"
        return {
            "status"    : status,
            "latency_ms": round((time.perf_counter() - started) * 1_000, 2),
            "error"     : request_error,
            "payload"   : response_payload,
        }


def _reply_text(result: ProbeResult) -> str:
    payload = result.get("payload")
    actions = payload.get("actions") if isinstance(payload, dict) else None
    if not isinstance(actions, list):
        return ""
    chunks: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        params  = action.get("params")
        message = params.get("message") if isinstance(params, dict) else None
        if isinstance(message, str):
            chunks.append(message)
            continue
        if not isinstance(message, list):
            continue
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                continue
            data = segment.get("data")
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                chunks.append(data["text"])
    return "\n".join(chunks).strip()


def _automatic_checks(case_id: str, reply: str) -> dict[str, bool]:
    """机器门禁仅检查响应存在与明确长度上限；语义由独立人工审阅。"""
    return {"has_reply": bool(reply), "concise": 0 < len(reply) <= 256}


def _semantic_review_material(
    case_id: str, message: str, reply: str, *, prior_message: str = ""
) -> dict[str, Any]:
    """保留完整输入输出及待审标准，不使用生产规则生成语义结论。"""
    criteria = ["回答切合当前输入，表达自然，事实与身份边界有依据"]
    if case_id in {"direct_without_question", "playful_teasing", "low_stakes_persona_story"}:
        criteria.append("遵守用户不追问的要求，检查整段回答中的直接及间接追问")
    if case_id == "third_party_boundary":
        criteria.append(
            "尊重第三方事实边界，保持未知与推测的确定性；普通可能性可用于说明证据不足，敏感状态和具体动机需要依据"
        )
    if case_id == "stale_topic":
        criteria.extend(
            [
                "本轮没有图片附件，明确说明缺少图片且不臆造图像内容",
                "聚焦当前话题，正确处理指代，完整检查旧话题是否被无故带回",
            ]
        )
    return {
        "case_id"      : case_id,
        "status"       : "pending",
        "reviewer"     : None,
        "verdict"      : None,
        "criteria"     : criteria,
        "message"      : message,
        "prior_message": prior_message,
        "reply"        : reply,
    }


def _cleanup(probe: EventProbe, *, user_id: int, group_id: int) -> dict[str, Any]:
    """通过真实管理命令清理测试会话，并把清理失败纳入报告。"""

    result = probe.send(
        "/xc reset confirm",
        user_id  = user_id,
        group_id = group_id,
        admin    = True,
    )
    reply = _reply_text(result)
    # 只有明确的重置成功回复才算清理完成：200 状态下的“权限不足”或空动作同样是失败。
    return {
        "status": result["status"],
        "reply" : reply,
        "error" : result["error"],
        "ok"    : result["status"] == 200 and result["error"] is None and "已重置会话记忆" in reply,
    }


def _wait_for_store(
    data_dir: Path,
    group_id: int,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """等待 Core 权威 data_dir 中的会话快照原子发布完成。"""

    path     = data_dir / f"g{group_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = _decode_json_value(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, JsonDataError):
            time.sleep(0.1)
            continue
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        time.sleep(0.1)
    return []


def _run_stale_topic_case(
    probe: EventProbe,
    base_group: int,
    data_dir: Path,
) -> dict[str, Any]:
    """找到一次成功观测但未插话的旧消息，再验证当前回复不追旧话题。"""

    old_message                    = "这包烟叼着确实挺有格调"
    current_message                = "小青，这个KIMI怎么蛤里蛤气的？只说当前这张图的话题。"
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, 9):
        group_id   = base_group + attempt
        user_id    = 880_054_000 + attempt
        old_result = probe.send(
            old_message,
            user_id    = user_id,
            group_id   = group_id,
            event_time = time.time() - 3 * 86400,
        )
        old_reply                      = _reply_text(old_result)
        attempt_result: dict[str, Any] = {
            "attempt"   : attempt,
            "group_id"  : group_id,
            "old_status": old_result["status"],
            "old_error" : old_result["error"],
            "old_reply" : old_reply,
        }

        # 网络/协议失败不是“模型选择不插话”，必须单独留下证据并让门禁失败。
        old_request_ok = old_result["status"] == 200 and old_result["error"] is None
        if not old_request_ok or old_reply:
            attempt_result["cleanup"] = _cleanup(
                probe,
                user_id  = user_id,
                group_id = group_id,
            )
            attempts.append(attempt_result)
            continue

        current_result = probe.send(
            current_message,
            user_id  = user_id,
            group_id = group_id,
        )
        current_reply = _reply_text(current_result)
        stored        = _wait_for_store(data_dir, group_id)
        serialized = json.dumps(stored, ensure_ascii=False)
        checks = {
            "old_request_ok"                     : old_request_ok,
            "old_turn_was_observed_without_reply": not old_reply,
            "current_request_ok": (
                current_result["status"] == 200 and current_result["error"] is None
            ),
            "current_turn_has_reply"       : bool(current_reply),
            "full_store_keeps_old_turn"    : old_message in serialized,
            "full_store_keeps_current_turn": "KIMI" in serialized,
        }
        cleanup = _cleanup(probe, user_id=user_id, group_id=group_id)
        attempt_result.update(
            {
                "current_status": current_result["status"],
                "current_error" : current_result["error"],
                "current_reply" : current_reply,
                "cleanup"       : cleanup,
            }
        )
        attempts.append(attempt_result)
        return {
            "attempt"        : attempt,
            "group_id"       : group_id,
            "old_reply"      : old_reply,
            "current_reply"  : current_reply,
            "status"         : current_result["status"],
            "error"          : current_result["error"],
            "latency_ms"     : current_result["latency_ms"],
            "checks"         : checks,
            "semantic_review": _semantic_review_material(
                "stale_topic", current_message, current_reply, prior_message=old_message
            ),
            "cleanup"        : cleanup,
            "attempts"       : attempts,
            "all_requests_ok": all(
                row["old_status"] == 200 and row["old_error"] is None for row in attempts
            )
            and current_result["status"] == 200
            and current_result["error"] is None,
            "all_cleanups_ok": all(row["cleanup"]["ok"] for row in attempts),
        }
    return {
        "attempt"        : None,
        "checks"         : {"found_non_reply_seed": False},
        "semantic_review": _semantic_review_material(
            "stale_topic", current_message, "", prior_message=old_message
        ),
        "cleanup"        : {},
        "attempts"       : attempts,
        "all_requests_ok": all(
            row["old_status"] == 200 and row["old_error"] is None for row in attempts
        ),
        "all_cleanups_ok": all(row["cleanup"]["ok"] for row in attempts),
    }


def _run_forced_cases(probe: EventProbe) -> list[dict[str, Any]]:
    """执行必须回复的人设用例，并逐组清理会话。"""

    results: list[dict[str, Any]] = []
    for index, case in enumerate(CASES, 1):
        group_id = 972_232_000 + index
        user_id  = 880_053_000 + index
        response = probe.send(case.message, user_id=user_id, group_id=group_id)
        reply  = _reply_text(response)
        result = {
            "case_id"        : case.case_id,
            "message"        : case.message,
            "reply"          : reply,
            "status"         : response["status"],
            "error"          : response["error"],
            "latency_ms"     : response["latency_ms"],
            "checks"         : _automatic_checks(case.case_id, reply),
            "semantic_review": _semantic_review_material(case.case_id, case.message, reply),
            "cleanup"        : _cleanup(probe, user_id=user_id, group_id=group_id),
        }
        results.append(result)
        print(f"{case.case_id}: {reply or '<NO_REPLY>'}")
    return results


def _run_participation_case(probe: EventProbe) -> dict[str, Any]:
    """最多发送三条低风险群聊线索，验证小青能否自主参与。"""

    attempts: list[dict[str, Any]] = []
    participation_reply            = ""
    message                        = "有没有人懂，修一个 bug 又冒出三个，代码在跟我搞人口普查"
    for attempt in range(1, 4):
        group_id = 972_233_000 + attempt
        user_id  = 880_055_000 + attempt
        response = probe.send(
            message,
            user_id  = user_id,
            group_id = group_id,
        )
        reply = _reply_text(response)
        attempts.append(
            {
                "attempt"        : attempt,
                "message"        : message,
                "reply"          : reply,
                "semantic_review": _semantic_review_material("participation", message, reply),
                "status"         : response["status"],
                "error"          : response["error"],
                "latency_ms"     : response["latency_ms"],
                "cleanup"        : _cleanup(probe, user_id=user_id, group_id=group_id),
            }
        )
        if reply:
            participation_reply = reply
            break
    return {
        "attempts": attempts,
        "replied_within_three_cues": bool(participation_reply),
        "reply": participation_reply,
        "all_requests_ok": all(row["status"] == 200 and row["error"] is None for row in attempts),
        "all_cleanups_ok": all(row["cleanup"]["ok"] for row in attempts),
    }


def _raw_message(segments: list[dict[str, Any]]) -> str:
    """为日志与兼容字段生成 CQ 风格的原文，图片内容不写入 raw_message。"""

    chunks: list[str] = []
    for segment in segments:
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if segment.get("type") == "text":
            chunks.append(str(data.get("text", "")))
        else:
            chunks.append(f"[CQ:{segment.get('type')}]")
    return "".join(chunks)


def _render_image_fixture(name: str) -> str:
    """绘制确定性的 PNG 海报并返回 base64 正文。"""

    from PIL import Image, ImageDraw, ImageFont

    lines  = IMAGE_FIXTURES[name]
    image  = Image.new("RGB", (640, 400), (255, 214, 224))
    canvas = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=52)
    canvas.ellipse((470, 250, 590, 370), fill=(150, 95, 60))
    for index, line in enumerate(lines):
        canvas.text((40, 50 + index * 90), line, fill=(60, 20, 40), font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _turn_message(turn: Turn) -> str | list[dict[str, Any]]:
    if not turn.image:
        return turn.text
    segments: list[dict[str, Any]] = [
        {"type": "image", "data": {"file": f"base64://{_render_image_fixture(turn.image)}"}}
    ]
    if turn.text:
        segments.append({"type": "text", "data": {"text": turn.text}})
    return segments


def _exhausted_fallbacks() -> frozenset[str]:
    """审查耗尽后的兜底文案说明候选都没通过，出现在剧本最终回复里就记为失败。"""

    from plugins.xiaoqing_chat.reply_fallbacks import ALL_FALLBACK_TEXTS

    return ALL_FALLBACK_TEXTS


def _scenario_checks(scenario: Scenario, reply: str) -> dict[str, bool]:
    """机器门禁只核对确定性条件；语义质量保留给人工审阅。"""

    lowered = reply.lower()
    checks  = {
        "has_reply"             : bool(reply),
        "not_exhausted_fallback": reply not in _exhausted_fallbacks(),
    }
    if scenario.forbidden_terms:
        checks["no_stale_topic_terms"] = not any(
            term.lower() in lowered for term in scenario.forbidden_terms
        )
    if scenario.expected_any:
        checks["mentions_expected_content"] = any(
            term.lower() in lowered for term in scenario.expected_any
        )
    return checks


def _scenario_observations(scenario: Scenario, transcript: list[dict[str, Any]]) -> dict[str, Any]:
    """未点名剧本只记录参与情况，供人工审阅和参与率统计。"""

    if scenario.addressed:
        return {}
    replies = sum(bool(row["reply"]) for row in transcript)
    key     = "stayed_silent" if scenario.expect_silence else "joined"
    return {"bot_replies": replies, key: replies == 0 if scenario.expect_silence else replies > 0}


def _run_scenarios(probe: EventProbe) -> list[dict[str, Any]]:
    """按剧本逐条发送多人群聊消息，旧消息使用真实的历史时间戳。"""

    results: list[dict[str, Any]] = []
    for index, scenario in enumerate(SCENARIOS, 1):
        group_id = 972_235_000 + index
        speakers = {
            name: 880_056_000 + index * 10 + offset
            for offset, name in enumerate(dict.fromkeys(turn.speaker for turn in scenario.turns))
        }
        transcript: list[dict[str, Any]] = []
        now                              = time.time()
        for position, turn in enumerate(scenario.turns):
            if position and scenario.pace_seconds:
                time.sleep(scenario.pace_seconds)
            response = probe.send(
                _turn_message(turn),
                user_id    = speakers[turn.speaker],
                group_id   = group_id,
                event_time = now - turn.hours_ago * 3600 if turn.hours_ago else time.time(),
                nickname   = turn.speaker,
            )
            transcript.append(
                {
                    "speaker"   : turn.speaker,
                    "text"      : turn.text,
                    "image"     : turn.image,
                    "hours_ago" : turn.hours_ago,
                    "reply"     : _reply_text(response),
                    "status"    : response["status"],
                    "error"     : response["error"],
                    "latency_ms": response["latency_ms"],
                }
            )
        reply = transcript[-1]["reply"]
        results.append(
            {
                "case_id"    : scenario.case_id,
                "group_id"   : group_id,
                "transcript" : transcript,
                "reply"      : reply,
                "requests_ok": all(
                    row["status"] == 200 and row["error"] is None for row in transcript
                ),
                "checks"         : _scenario_checks(scenario, reply) if scenario.addressed else {},
                "observations"   : _scenario_observations(scenario, transcript),
                "semantic_review": {
                    "case_id" : scenario.case_id,
                    "status"  : "pending",
                    "reviewer": None,
                    "verdict" : None,
                    "criteria": ["回答切合最后一条消息，像群友一样自然", *scenario.criteria],
                    "message" : scenario.turns[-1].text,
                    "reply"   : reply,
                },
                "cleanup": _cleanup(
                    probe, user_id=speakers[scenario.turns[0].speaker], group_id=group_id
                ),
            }
        )
        print(f"{scenario.case_id}: {reply or '<NO_REPLY>'}")
    return results


async def _calibrate_checker_async(config_path: Path, secrets_path: Path) -> dict[str, Any]:
    """进程内调用真实 checker route，量化审查器对自然接梗的误杀与对编造的漏放。"""

    import aiohttp

    from core.ai import complete_configured_route, list_configured_models
    from core.capabilities import AIService
    from core.config import ConfigManager
    from plugins.xiaoqing_chat.config.config import load_xiaoqing_chat_config
    from plugins.xiaoqing_chat.llm.prompt_builder import build_dialogue_prompt
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply
    from plugins.xiaoqing_chat.memory.memory import StoredMessage

    manager = ConfigManager(config_path, secrets_path)
    manager.reload()
    snapshot = manager.snapshot()
    config   = load_xiaoqing_chat_config(
        context_config = snapshot.config,
        plugin_dir     = PROJECT_ROOT / "plugins" / "xiaoqing_chat",
    )
    reply_check                = config.reply_check
    rows: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:

        async def complete(
            *, route_name: str, messages: list[dict[str, Any]], **kwargs: Any
        ) -> Any:
            return await complete_configured_route(
                session     = session,
                config      = snapshot.config,
                secrets     = snapshot.secrets,
                plugin_name = "xiaoqing_chat",
                route_name  = route_name,
                messages    = messages,
                **kwargs,
            )

        def list_models(
            *, route_name: str, required_modalities: tuple[str, ...] = ("text",)
        ) -> Any:
            return list_configured_models(
                config              = snapshot.config,
                secrets             = snapshot.secrets,
                plugin_name         = "xiaoqing_chat",
                route_name          = route_name,
                required_modalities = required_modalities,
            )

        secrets = {"_ai": AIService(complete, list_models)}

        async def check_case(case: CheckerCase) -> None:
            now     = time.time()
            history = [
                StoredMessage(
                    role="user", name=name, ts=now - 60 * (len(case.history) - i), content=text
                )
                for i, (name, text) in enumerate(case.history)
            ]
            history_text = build_dialogue_prompt(history, bot_name="小青", truncate=True)
            for probe in case.probes:
                result = await check_reply(
                    secrets                          = secrets,
                    bot_name                         = "小青",
                    reply                            = probe.reply,
                    current_text                     = case.current,
                    current_sender                   = case.sender,
                    goal                             = "自然聊天",
                    grounding_text                   = config.personality.card.facts("小青"),
                    history                          = history,
                    chat_history_text                = history_text,
                    enable_llm_checker               = True,
                    verify_turn_semantics            = reply_check.enable_semantic_checker,
                    max_repeat_compare               = reply_check.max_repeat_compare,
                    similarity_threshold             = reply_check.similarity_threshold,
                    max_assistant_in_row             = reply_check.max_assistant_in_row,
                    max_tokens                       = reply_check.max_tokens,
                    timeout_seconds                  = reply_check.timeout_seconds,
                    max_retry                        = 0,
                    retry_interval_seconds           = 0.2,
                    check_omitted_persona_episode    = True,
                    allow_low_stakes_persona_fiction = (
                        config.personality.allow_low_stakes_persona_fiction
                    ),
                )
                rows.append(
                    {
                        "case_id"     : case.case_id,
                        "label"       : probe.label,
                        "reply"       : probe.reply,
                        "acceptable"  : probe.acceptable,
                        "suitable"    : result.suitable,
                        "severity"    : result.severity,
                        "failure_code": result.failure_code,
                        "reason"      : result.reason,
                    }
                )

        semaphore = asyncio.Semaphore(4)

        async def guarded(case: CheckerCase) -> None:
            async with semaphore:
                await check_case(case)

        await asyncio.gather(*(guarded(case) for case in CHECKER_CASES))
    return _checker_calibration_summary(rows)


def _checker_calibration_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """检查器基础设施失败单独计数，不混入误杀或漏放。"""

    measured    = [row for row in rows if row["severity"] != "infra"]
    acceptable  = [row for row in measured if row["acceptable"]]
    rejectable  = [row for row in measured if not row["acceptable"]]
    accepted    = sum(row["suitable"] for row in acceptable)
    blocked     = sum(not row["suitable"] for row in rejectable)
    accept_rate = accepted / len(acceptable) if acceptable else 0.0
    return {
        "probes"            : rows,
        "infra_failures"    : len(rows) - len(measured),
        "acceptable_passed" : accepted,
        "acceptable_total"  : len(acceptable),
        "rejectable_blocked": blocked,
        "rejectable_total"  : len(rejectable),
        "accept_rate"       : round(accept_rate, 3),
        "passed": (
            len(measured) == len(rows)
            and blocked == len(rejectable)
            and accept_rate >= CHECKER_MIN_ACCEPT_RATE
        ),
    }


def _run_quality_suite(
    probe: EventProbe,
    data_dir: Path,
    *,
    checker_calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按固定顺序执行质量用例并生成可审计报告。"""

    started_at    = datetime.now(UTC).isoformat()
    results       = _run_forced_cases(probe)
    participation = _run_participation_case(probe)
    stale_topic   = _run_stale_topic_case(probe, 972_234_000, data_dir)
    scenarios     = _run_scenarios(probe)

    question_endings = sum(
        result["reply"].rstrip().endswith(("?", "？")) for result in results if result["reply"]
    )
    all_checks = [passed for result in results for passed in result["checks"].values()]
    all_checks.extend(stale_topic.get("checks", {}).values())
    all_checks.extend(passed for scenario in scenarios for passed in scenario["checks"].values())
    forced_requests_ok = all(
        result["status"] == 200 and result["error"] is None for result in results
    )
    forced_cleanups_ok = all(result["cleanup"]["ok"] for result in results)
    all_requests_ok    = (
        forced_requests_ok
        and participation["all_requests_ok"]
        and stale_topic["all_requests_ok"]
        and all(scenario["requests_ok"] for scenario in scenarios)
    )
    all_cleanups_ok = (
        forced_cleanups_ok
        and participation["all_cleanups_ok"]
        and stale_topic["all_cleanups_ok"]
        and all(scenario["cleanup"]["ok"] for scenario in scenarios)
    )
    forced_replies            = sum(bool(result["reply"]) for result in results)
    all_machine_checks_passed = all(all_checks)
    gate_passed               = (
        forced_replies == len(results)
        and all_machine_checks_passed
        and participation["replied_within_three_cues"]
        and all_requests_ok
        and all_cleanups_ok
        and (checker_calibration is None or checker_calibration["passed"])
    )
    finished_at = datetime.now(UTC).isoformat()
    return {
        "schema_version": 2,
        "schema_notes": "schema 2 additive fields; gate_passed aliases machine_gate_passed; semantic checks require independent human review",
        "semantic_review_required": True,
        "semantic_review_status": "pending",
        "semantic_acceptance_passed": None,
        "machine_gate_passed": gate_passed,
        "started_at": started_at,
        "finished_at": finished_at,
        "generated_at": finished_at,
        "endpoint": probe.endpoint,
        "chat_data_dir": str(data_dir),
        "timeout_seconds": probe.timeout,
        "message_id_seed": probe.message_id_seed,
        "cases": results,
        "participation": participation,
        "stale_topic": stale_topic,
        "scenarios": scenarios,
        "checker_calibration": checker_calibration,
        "aggregate": {
            "forced_replies"           : forced_replies,
            "forced_cases"             : len(results),
            "question_endings"         : question_endings,
            "all_machine_checks_passed": all_machine_checks_passed,
            "all_requests_ok"          : all_requests_ok,
            "all_cleanups_ok"          : all_cleanups_ok,
            "unaddressed_observations" : {
                scenario["case_id"]: scenario["observations"]
                for scenario in scenarios
                if scenario["observations"]
            },
        },
        "gate_passed": gate_passed,
    }


def build_parser() -> argparse.ArgumentParser:
    """构造可被测试和统一 UAT 复用的命令行契约。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    parser.add_argument(
        "--chat-data-dir",
        type    = Path,
        default = DEFAULT_CHAT_DATA_DIR,
        help    = "xiaoqing_chat 的 Core 权威 data_dir；默认 data/xiaoqing_chat",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--message-id-seed",
        type = int,
        help = "固定 OneBot message_id 起点以复现实验；默认使用当前微秒时间",
    )
    parser.add_argument(
        "--config",
        type    = Path,
        default = DEFAULT_CONFIG,
        help    = "审查器校准读取的 Core 配置；默认 config/config.json",
    )
    parser.add_argument(
        "--checker-calibration",
        action = "store_true",
        help   = "额外在进程内校准审查器的误杀与漏放，结果纳入机器门禁",
    )
    parser.add_argument(
        "--checker-only",
        action = "store_true",
        help   = "只运行审查器校准，不需要运行中的 Bot",
    )
    return parser


def _write_report(output: Path, report: dict[str, Any]) -> None:
    with output.open("x", encoding="utf-8") as report_file:
        report_file.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"report={output.resolve()}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args   = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是正数")
    if args.message_id_seed is not None and args.message_id_seed <= 0:
        parser.error("--message-id-seed 必须是正整数")
    if args.output.exists():
        parser.error(f"--output 已存在，拒绝覆盖: {args.output}")
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        parser.error(f"无法创建报告目录: {type(exc).__name__}: {exc}")

    calibration = None
    if args.checker_calibration or args.checker_only:
        calibration = asyncio.run(
            _calibrate_checker_async(args.config.resolve(), args.secrets.resolve())
        )
        print(
            "checker_calibration: "
            f"accept={calibration['acceptable_passed']}/{calibration['acceptable_total']} "
            f"block={calibration['rejectable_blocked']}/{calibration['rejectable_total']} "
            f"infra={calibration['infra_failures']} passed={calibration['passed']}"
        )
    if args.checker_only:
        assert calibration is not None
        _write_report(
            args.output,
            {
                "schema_version"     : 2,
                "generated_at"       : datetime.now(UTC).isoformat(),
                "checker_calibration": calibration,
                "machine_gate_passed": calibration["passed"],
                "gate_passed"        : calibration["passed"],
            },
        )
        return 0 if calibration["passed"] else 1

    data_dir = args.chat_data_dir.resolve()
    if not data_dir.is_dir():
        parser.error(f"--chat-data-dir 不存在或不是目录: {data_dir}")

    token, admin_id = _load_auth(args.secrets.resolve())
    message_id_seed = args.message_id_seed or time.time_ns() // 1_000
    probe           = EventProbe(
        endpoint        = args.endpoint,
        token           = token,
        admin_id        = admin_id,
        timeout         = args.timeout,
        message_id_seed = message_id_seed,
    )
    report = _run_quality_suite(probe, data_dir, checker_calibration=calibration)
    _write_report(args.output, report)
    gate_passed = bool(report["gate_passed"])
    print(f"machine_gate_passed={gate_passed}")
    print("semantic_review_required=True; semantic_acceptance=pending")
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
