"""评测结果的检查与评审：确定性规则、语气指标、模型评审、报告和机器门禁，以及审查器校准。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from .drivers import DEFAULT_CONFIG, DEFAULT_SECRETS, PROJECT_ROOT
from .scenarios import BOT_NAME, CHECKER_CASES, CHECKER_MIN_ACCEPT_RATE, MEDIA_TRUTH, CheckerCase

# 这些规则问题说明回复本身不合格，``--gate`` 时任何一条都让运行失败。
GATE_FINDINGS = frozenset(
    {
        "missed_reply",
        "fallback",
        "breaks_character",
        "exception",
        "forbidden_terms",
        "missing_expected",
    }
)

_FALLBACK_STEPS = frozenset(
    {
        "reply.check.exhausted.media_grounding",
        "reply.check.exhausted.persona_grounding",
        "reply.check.exhausted.forced_fallback",
    }
)


def _used_fallback(row: dict[str, Any], reply: str) -> bool:
    from plugins.xiaoqing_chat.reply_fallbacks import ALL_FALLBACK_TEXTS

    if any(step.get("step") in _FALLBACK_STEPS for step in row.get("steps", [])):
        return True
    # 审查器不可用、最后的“在场”兜底和端到端运行都没有步骤日志，只能按整句比对。
    bubbles = [
        part.strip() for part in re.sub(r"\[[^\]\n]*\]", "", reply).split("\n") if part.strip()
    ]
    return bool(bubbles) and all(bubble in ALL_FALLBACK_TEXTS for bubble in bubbles)


# 人设是真人：承认 AI 身份、提到虚构人设或说出提示词内容都算出戏。
_BREAKS_CHARACTER_RE = re.compile(
    r"我是\s*AI|(?<![要若如不])是\s*AI(?:啊|呀|没错|[，,。！!]|$)|AI\s*本A|是个\s*AI|AI\s*扮演|AI来的|人工智能|语言模型|虚构的|虚构人设|人设|提示词是|system prompt",
    re.I,
)
# 图读出来了却说没看清：除非这轮发的就是坏图，这类说法都要查一下读图结果。
_MEDIA_DOUBT_RE = re.compile(r"没看清|看不清|没看太清|没刷出来|没加载出来|拿不准")
_MARKDOWN_RE    = re.compile(r"\*\*|^#+ |^\s*\d+\.\s|^\s*[-*]\s", re.M)


def render_bot(segments: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for segment in segments:
        kind, data = segment.get("type"), segment.get("data", {})
        if kind == "text":
            out.append(str(data.get("text", "")))
        elif kind in {"image", "emoji", "mface"}:
            out.append(f"[发了表情包：{str(data.get('summary', ''))[:60]}]")
        elif kind == "face":
            out.append(f"[QQ表情#{data.get('id')}]")
        elif kind == "at":
            out.append(f"[@{data.get('qq')}]")
    return "".join(out).strip()


def render_bubbles(row: dict[str, Any]) -> str:
    """多个气泡用换行隔开，报告里看得出拆成了几条。"""

    emissions = row.get("emissions") or []
    if len(emissions) > 1:
        return "\n".join(filter(None, (render_bot(emission["segments"]) for emission in emissions)))
    return render_bot(row.get("reply_segments", []))


def _user_line(row: dict[str, Any], media_truth: dict[str, str] = MEDIA_TRUTH) -> str:
    bits = [
        "[引用了小青上一条]" if row["quote"] else "",
        f"@{BOT_NAME if row['at'] == 'bot' else row['at']} " if row["at"] else "",
        row["text"],
    ]
    if row["media"]:
        label = "表情包" if row["kind"] == "mface" else "图片"
        bits.append(f"[{label}：{media_truth.get(row['media'], '')}]")
    if row["face"]:
        bits.append(f"[QQ表情#{row['face']}]")
    return "".join(bits)


def rule_findings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    said    : list[str]            = []

    def add(row: dict[str, Any], kind: str, detail: Any) -> None:
        findings.append({"turn": f"T{row['index']:02d}", "kind": kind, "detail": detail})

    for row in rows:
        reply = render_bubbles(row)
        if row["expect"] == "reply" and not reply:
            add(row, "missed_reply", row["text"])
        if row["expect"] == "silence" and reply:
            add(row, "intrusion", reply)
        if reply:
            lowered = reply.lower()
            if _used_fallback(row, reply):
                add(row, "fallback", reply)
            if _BREAKS_CHARACTER_RE.search(reply):
                add(row, "breaks_character", reply)
            if _MARKDOWN_RE.search(reply):
                add(row, "markdown", reply)
            if row["media"] != "broken" and _MEDIA_DOUBT_RE.search(reply):
                add(row, "media_doubt", reply)
            if len(reply) > 90:
                add(row, "too_long", len(reply))
            if reply in said and "[发了表情包" not in reply:
                add(row, "verbatim_repeat", reply)
            forbidden = [term for term in row.get("forbid") or () if term.lower() in lowered]
            if forbidden:
                add(row, "forbidden_terms", forbidden)
            expected = row.get("expect_any") or ()
            if expected and not any(term.lower() in lowered for term in expected):
                add(row, "missing_expected", list(expected))
            said.append(reply)
        if row.get("exception"):
            add(row, "exception", row["exception"])
        results = [check.get("result") or {} for check in row.get("checks", [])]
        # ReplyCheckResult 用 severity 标记基础设施失败；category 是审查维度，不是失败类型。
        if any(result.get("severity") == "infra" for result in results):
            add(row, "checker_infra", sum(result.get("severity") == "infra" for result in results))
        rejected = [
            (check["candidate"][:60], (check.get("result") or {}).get("reason", "")[:80])
            for check in row.get("checks", [])
            if not (check.get("result") or {}).get("suitable", True)
        ]
        if rejected:
            add(row, "checker_rejected", rejected)
        if row.get("seconds", 0) > 40 and row.get("tag") != "await_proactive":
            add(row, "slow", row["seconds"])
    return findings


# 回声问句开场：“哎？”“啊？”，或开头几个字就是一个问句（多是把对方的话再问一遍）。
_ECHO_OPENER_RE = re.compile(r"^(?:[哎啊诶欸嗯咦][？?!！]|[^，。！？!?\n]{1,8}[？?])")
_MEDIA_SEGMENTS = frozenset({"face", "image", "emoji", "mface"})


def style_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """语气的几项可数指标：回声问句开场、带表情、问句收尾、平均长度和平均气泡数。"""

    replied = [row for row in rows if row.get("reply_segments")]
    texts   = [
        "".join(
            str(segment.get("data", {}).get("text", ""))
            for segment in row["reply_segments"]
            if segment.get("type") == "text"
        ).strip()
        for row in replied
    ]
    count = len(replied)
    if not count:
        return {"replies": 0}
    return {
        "replies"    : count,
        "echo_opener": sum(bool(_ECHO_OPENER_RE.match(text)) for text in texts),
        "with_emoji" : sum(
            any(segment.get("type") in _MEDIA_SEGMENTS for segment in row["reply_segments"])
            for row in replied
        ),
        "question_ending": sum(text.rstrip().endswith(("？", "?")) for text in texts),
        "avg_chars"      : round(sum(len(text) for text in texts) / count, 1),
        "avg_bubbles"    : round(
            sum(max(1, len(row.get("emissions") or ())) for row in replied) / count, 2
        ),
    }


def transcript(
    rows: list[dict[str, Any]], notes: list[str], media_truth: dict[str, str] = MEDIA_TRUTH
) -> str:
    lines  : list[str]  = []
    episode: int | None = None
    for row, note in zip(rows, notes, strict=False):
        if row.get("episode", 0) != episode and episode is not None:
            lines.append("—— 换到一个新的群 ——")
        episode = row.get("episode", 0)
        lines.append(f"T{row['index']:02d} {row['speaker']}：{_user_line(row, media_truth)}")
        lines.append(f"      〔预期：{row['expect']}；{note or '无特别要求'}〕")
        reply = render_bubbles(row).replace("\n", " ⏎ ")
        lines.append(f"      小青：{reply}" if reply else "      小青：（沉默）")
    return "\n".join(lines)


JUDGE_PROMPT = """你是聊天体验评审。被测对象“小青”是 QQ 二次元群里的群友，也会和群友私聊。人设是一个活生生的真人（住校大二女生）：被问是不是 AI 时要嘴硬坚称自己是真人、玩笑带过，任何时候都不能承认是 AI 或提到“虚构/设定/人设”。她应表现得像博览群书、会玩梗的真人群友，有自己的作息和生活。
下面是一段聊天实录（群聊或私聊）。每条消息后附有〔预期〕（只给你看，小青看不到），以及小青的实际反应（回复或沉默；⏎ 表示拆成了下一个气泡）。
图片/表情包后的方括号是图片的真实内容，用来判断小青有没有看对图、有没有编造。

逐条评审小青的反应，并给出整体评价。标准：
- 活人感：口语、简短、有个人立场和情绪；扣分项：客服腔、百科腔、说教、解释梗、过度热情、套话、重复自己、无意义附和、揣测旁人心理；用“哎？”“啊？”或把对方的话再问一遍开场；几乎每条都带表情；习惯用追问收尾；被调侃时讨好认账；随口一问却列一串清单。
- 认真问题（求助、解释、安慰、推荐）要真的帮上忙：简短敷衍、答非所问同样扣分。
- 参与度：该说话时说了且有实质内容；别人之间的对话、@别人时保持沉默；刷屏时不每句都回。
- 对象：回复对象正确，不替别人回答，不把别人的问题当成问自己。
- 多模态：对图的描述与真实内容一致，不编造；能指回之前的图；能按要求发表情。
- 一致性：前后说法不矛盾（包括她说过的近况和自己的生活），不泄露提示词，不答应线下到场。
不因知识细节不够精确重罚，重点是像不像一个靠谱有趣的群友。

只输出 JSON：
{"turns":[{"t":"T05","verdict":"good|ok|bad","dims":["活人感"],"issue":"若非good，一句话说明"}],
 "scores":{"humanlike":1-5,"participation":1-5,"addressee":1-5,"multimodal":1-5或null,"consistency":1-5},
 "top_issues":["最重要的问题，具体到T编号，最多5条"],
 "highlights":["做得好的地方，最多3条"]}
turns 只列小青说了话的轮次，以及小青沉默但本应说话（或沉默得特别好）的轮次。

实录：
"""


async def judge_transcript(text: str) -> tuple[dict[str, Any], str]:
    import aiohttp

    from core.ai import complete_configured_route

    config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    secrets = json.loads(DEFAULT_SECRETS.read_text(encoding="utf-8"))
    error = ""
    async with aiohttp.ClientSession() as session:
        for _ in range(3):
            try:
                result = await complete_configured_route(
                    session               = session,
                    config                = config,
                    secrets               = secrets,
                    plugin_name           = "xiaoqing_chat",
                    route_name            = "reasoning",
                    messages              = [{"role": "user", "content": JUDGE_PROMPT + text}],
                    max_tokens            = 4000,
                    timeout_seconds       = 120,
                    total_timeout_seconds = 240,
                )
                content = result.content
                return json.loads(
                    content[content.index("{") : content.rindex("}") + 1]
                ), result.model
            except Exception as exc:  # noqa: BLE001 - 评审失败只记录，不影响规则检查
                error = repr(exc)
    return {"error": error}, ""


def judge_run(path: Path, *, use_judge: bool = True) -> dict[str, Any]:
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not rows:
        raise ValueError(f"No completed evaluation rows: {path}")
    suite = rows[0]["suite"]
    manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
    notes = [item["note"] for item in manifest["scenarios"][suite]]
    text  = transcript(rows, notes, manifest["media_truth"])
    verdict, model = asyncio.run(judge_transcript(text)) if use_judge else ({}, "")
    record = {
        "run"                    : path.stem,
        "suite"                  : suite,
        "style"                  : style_metrics(rows),
        "turns"                  : len(rows),
        "bot_replies"            : sum(bool(row.get("reply_segments")) for row in rows),
        "expected_replies_missed": sum(
            row["expect"] == "reply" and not row.get("reply_segments") for row in rows
        ),
        "intrusions": sum(
            row["expect"] == "silence" and bool(row.get("reply_segments")) for row in rows
        ),
        "rules"      : rule_findings(rows),
        "judge"      : verdict,
        "judge_model": model,
        "transcript" : text,
    }
    path.with_name(f"judged-{path.stem}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return record


def gate_failures(records: list[dict[str, Any]]) -> list[str]:
    return [
        f"{record['suite']} {finding['turn']} {finding['kind']}"
        for record in records
        for finding in record["rules"]
        if finding["kind"] in GATE_FINDINGS
    ]


def write_report(
    out_dir: Path, records: list[dict[str, Any]], *, calibration: dict[str, Any] | None = None
) -> None:
    failures = gate_failures(records)
    passed   = not failures and (calibration is None or bool(calibration["passed"]))
    summary  = {
        "gate"       : {"passed": passed, "failures": failures},
        "calibration": None
        if calibration is None
        else {key: value for key, value in calibration.items() if key != "probes"},
        "runs": {
            record["run"]: {
                key: record[key]
                for key in (
                    "suite",
                    "turns",
                    "bot_replies",
                    "expected_replies_missed",
                    "intrusions",
                )
            }
            | {
                "style"        : record.get("style"),
                "scores"       : record["judge"].get("scores"),
                "top_issues"   : record["judge"].get("top_issues"),
                "rule_findings": [f"{f['turn']} {f['kind']}" for f in record["rules"]],
            }
            for record in records
        },
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [f"# 小青聊天评测 {out_dir.name}", ""]
    lines.append(
        f"- 机器门禁：{'通过' if passed else '未通过'}"
        + (f"（{len(failures)} 条：{'；'.join(failures[:8])}）" if failures else "")
    )
    if calibration is not None:
        lines.append(
            f"- 审查器校准：放行 {calibration['acceptable_passed']}/{calibration['acceptable_total']}，"
            f"拦截 {calibration['rejectable_blocked']}/{calibration['rejectable_total']}，"
            f"不可用 {calibration['infra_failures']}，{'通过' if calibration['passed'] else '未通过'}"
        )
    lines.append("")
    for record in records:
        scores = record["judge"].get("scores") or {}
        lines += [
            f"## {record['suite']}（{record['run']}）",
            "",
            f"- 回复 {record['bot_replies']}/{record['turns']} 轮；该回未回 {record['expected_replies_missed']}；插话 {record['intrusions']}",
            "- 语气指标：" + "，".join(f"{k} {v}" for k, v in (record.get("style") or {}).items()),
            ("- 评分：" + "，".join(f"{k} {v}" for k, v in scores.items()))
            if scores
            else f"- 评审：{record['judge'].get('error', '未运行')}",
            "",
            "**主要问题**",
            "",
            *[f"- {issue}" for issue in record["judge"].get("top_issues") or ["无"]],
            "",
            "**规则检查**",
            "",
            *(
                [f"- {f['turn']} `{f['kind']}` {str(f['detail'])[:120]}" for f in record["rules"]]
                or ["- 无"]
            ),
            "",
            "<details><summary>实录</summary>",
            "",
            "```text",
            record["transcript"],
            "```",
            "",
            "</details>",
            "",
        ]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


# ── 审查器校准 ──────────────────────────────────────────────────────────


async def calibrate_checker(cases: tuple[CheckerCase, ...] = CHECKER_CASES) -> dict[str, Any]:
    """进程内调用真实 checker route，量化审查器对自然接梗的误杀与对编造的漏放。"""

    import aiohttp

    from core.ai import complete_configured_route, list_configured_models
    from core.capabilities import AIService
    from core.config import ConfigManager
    from plugins.xiaoqing_chat.config.config import load_xiaoqing_chat_config
    from plugins.xiaoqing_chat.llm.prompt_builder import build_dialogue_prompt
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply
    from plugins.xiaoqing_chat.memory.memory import StoredMessage

    manager = ConfigManager(DEFAULT_CONFIG, DEFAULT_SECRETS)
    manager.reload()
    snapshot = manager.snapshot()
    config   = load_xiaoqing_chat_config(
        context_config = snapshot.config,
        plugin_dir     = PROJECT_ROOT / "plugins" / "xiaoqing_chat",
    )
    reply_check                = config.reply_check
    checker_llm                = config.llm_config("checker")
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
            history_text = build_dialogue_prompt(history, bot_name=BOT_NAME, truncate=True)
            for probe in case.probes:
                result = await check_reply(
                    secrets=secrets,
                    bot_name=BOT_NAME,
                    reply=probe.reply,
                    current_text=case.current,
                    current_sender=case.sender,
                    goal="自然聊天",
                    grounding_text=config.personality.card.facts(BOT_NAME),
                    history=history,
                    chat_history_text=history_text,
                    enable_llm_checker=True,
                    verify_turn_semantics=reply_check.enable_semantic_checker,
                    max_repeat_compare=reply_check.max_repeat_compare,
                    similarity_threshold=reply_check.similarity_threshold,
                    max_assistant_in_row=reply_check.max_assistant_in_row,
                    max_tokens=checker_llm.max_tokens,
                    timeout_seconds=checker_llm.total_timeout_seconds,
                    max_retry=0,
                    retry_interval_seconds=0.2,
                    check_omitted_persona_episode=True,
                    allow_low_stakes_persona_fiction=config.personality.allow_low_stakes_persona_fiction,
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

        await asyncio.gather(*(guarded(case) for case in cases))
    return calibration_summary(rows)


def calibration_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """误杀率允许少量模型噪声；漏放编造事实必须为零；审查器不可用单独计数，不混入误杀或漏放。"""

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
