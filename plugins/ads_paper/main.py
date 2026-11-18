"""ADS Paper 插件入口：解析命令并把职责分派到本地存储或 SciX 检索服务。"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from core.bounded_http import HttpStatusError
from core.delivery import CommandOutcome, DeliverySegments
from core.log import LogEvent
from core.plugin_base import PluginContextProtocol, Segments, segments
from core.public_errors import public_error_response

from . import ai_commands, note_commands, paper_commands
from .scix_client import SCIX_TOKEN_URL, SciXClient
from .storage import PaperStorage

log = logging.getLogger(__name__)

# 需要 SciX Token 的远程子命令
_REMOTE_COMMANDS = frozenset(
    {
        "search",
        "author",
        "abstract",
        "stats",
        "journal",
        "cite",
        "cite-network",
        "related",
        "summarize",
        "daily",
        "ref_add",
    }
)
_ALIASES = {"abs": "abstract", "帮助": "help"}
# SciX 用这几个状态码表达调用方能自己处理的情况，各给一句对应的说明。
_SCIX_STATUS_REPLIES = {
    400: "❌ SciX 无法解析这条检索式（HTTP 400）：括号或引号不成对、字段名后缺内容、AND/OR 缺一侧都会这样",
    401: f"❌ SciX Token 无效或已过期（HTTP 401）\n请到 {SCIX_TOKEN_URL} 重新生成，并更新 secrets.json 的 plugins.ads_paper.ads_token",
    429: "⚠️ SciX 的请求额度已用完（HTTP 429），额度按天重置",
}


def _get_scix_token(context: PluginContextProtocol) -> str:
    """从当前不可变配置快照读取插件私有的 SciX Token（配置键沿用 ads_token）。"""
    token = context.get_settings_snapshot().plugin_secrets("ads_paper").get("ads_token", "")
    if isinstance(token, str) and token:
        return token
    if token:
        log.error(LogEvent("config.invalid", setting="ads_token", reason_code="not_string"))
    else:
        log.warning(LogEvent("config.missing", setting="ads_token"))
    return ""


def _require_user_id(event: dict[str, Any]) -> int:
    """返回有效 QQ 用户 ID，拒绝把缺失身份归入共享的零号存储。"""

    user_id = event.get("user_id")
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("ADS Paper requires a positive integer user_id")
    return user_id


def _get_storage(context: PluginContextProtocol) -> PaperStorage:
    """在当前插件代际内复用存储实例及其目录级锁。"""

    storage = context.state.get("paper_storage")
    if isinstance(storage, PaperStorage):
        return storage
    storage                        = PaperStorage(context.data_dir)
    context.state["paper_storage"] = storage
    return storage


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    """处理 ``/paper`` 命令；本地功能不依赖 SciX Token。"""
    try:
        command_parts = args.strip().split(maxsplit=1)
        if not command_parts:
            return segments(_HELP_TEXT)

        subcommand      = command_parts[0].casefold()
        subcommand      = _ALIASES.get(subcommand, subcommand)
        subcommand_args = command_parts[1] if len(command_parts) == 2 else ""
        if subcommand == "help":
            return segments("❌ 用法: /paper help" if subcommand_args.strip() else _HELP_TEXT)

        storage = _get_storage(context)
        user_id = _require_user_id(event)

        # 本地命令只访问按用户隔离的文件，不应因 SciX 未配置而失效。
        if subcommand == "refs":
            if subcommand_args.strip():
                return segments("❌ 用法: /paper refs")
            return await ai_commands.cmd_refs(storage, context, user_id)

        local_commands: dict[str, Callable[[], Awaitable[Segments]]] = {
            "note"    : lambda: note_commands.cmd_note(storage, subcommand_args, user_id),
            "writing" : lambda: note_commands.cmd_writing(storage, subcommand_args, user_id),
            "topics"  : lambda: note_commands.cmd_topics(storage, subcommand_args, user_id),
            "deadline": lambda: note_commands.cmd_deadline(storage, subcommand_args, user_id),
        }
        local_handler = local_commands.get(subcommand)
        if local_handler is not None:
            return await local_handler()

        if subcommand not in _REMOTE_COMMANDS:
            return segments(f"未知命令: {subcommand}\n输入 /paper help 查看帮助")

        if subcommand == "daily" and subcommand_args.strip():
            return segments("❌ 用法: /paper daily")

        token = _get_scix_token(context)
        if not token:
            return segments(
                "❌ 未配置 SciX Token\n"
                f"在 {SCIX_TOKEN_URL} 生成后写入 secrets.json:\n"
                '  "plugins": {"ads_paper": {"ads_token": "your-token"}}'
            )

        # 只有远程子命令才创建 SciX 客户端，并复用核心提供的共享 HTTP 会话。
        client = SciXClient(token, context.http_session)
        remote_handlers: dict[str, Callable[[], Awaitable[Segments]]] = {
            "search"      : lambda: paper_commands.cmd_search(client, subcommand_args),
            "author"      : lambda: paper_commands.cmd_author(client, subcommand_args),
            "abstract"    : lambda: paper_commands.cmd_abstract(client, subcommand_args),
            "stats"       : lambda: paper_commands.cmd_stats(client, subcommand_args),
            "journal"     : lambda: paper_commands.cmd_journal(client, subcommand_args),
            "cite"        : lambda: paper_commands.cmd_cite(client, subcommand_args),
            "cite-network": lambda: paper_commands.cmd_cite_network(client, subcommand_args),
            "related"     : lambda: paper_commands.cmd_related(client, subcommand_args),
            "summarize"   : lambda: ai_commands.cmd_summarize(client, subcommand_args, context),
            "daily"       : lambda: ai_commands.cmd_daily(client, storage, user_id),
            "ref_add"     : lambda: ai_commands.cmd_ref_add(
                client, subcommand_args, storage, context, user_id
            ),
        }
        return await remote_handlers[subcommand]()

    except HttpStatusError as exc:
        # 走到这里的状态码都来自 SciX：摘要的补充来源失败时在各自模块里就地降级。
        reply = _SCIX_STATUS_REPLIES.get(exc.status)
        if reply is None:
            return public_error_response(context, exc, component="ads_paper.handle")
        log.warning(LogEvent("scix.request_rejected", status=exc.status))
        return DeliverySegments(segments(reply), outcome=CommandOutcome("error"))
    except Exception as exc:
        return public_error_response(context, exc, component="ads_paper.handle")


_HELP_TEXT = (
    "📚 论文与文献管理助手（SciX，NASA ADS 的继任服务）\n\n"
    "🔍 检索与调研:\n"
    "  /paper search <检索式> [选项]  - 检索论文，给出命中总数\n"
    "  /paper author <作者> [选项]    - 作者的论文，新的在前\n"
    "  /paper abstract <ID>...        - 论文信息与摘要原文，一次最多 8 篇\n"
    "  /paper stats <检索式>          - 命中总数、被引、年份/类型/期刊/关键词分布\n"
    "  /paper journal <刊名>          - 查期刊缩写 bibstem\n"
    "  /paper cite-network <ID>       - 被引与参考文献概览\n"
    "  /paper related <ID>            - 内容相近的论文\n"
    "  /paper cite <ID>... [--format 格式] - 导出 BibTeX 等引用\n"
    "  /paper summarize <ID>          - AI 中文摘要\n"
    "  选项: --rows 1-25  --page N  --sort relevance|citations|date|reads\n"
    "        --abs 附摘要节选  --hl 附摘要和正文中的命中片段（带这两项时每页最多 10 篇）\n"
    "  ID: Bibcode、arXiv ID、DOI，或 SciX/ADS/arXiv/doi.org 链接\n\n"
    "🧭 检索式（SciX 语法，多个条件空格相连即同时满足）:\n"
    '  title:"…" abs:"…" full:"…"     标题 / 标题摘要关键词 / 全文\n'
    '  author:"Zhang, B" author:"^Zhang, B"  作者 / 第一作者\n'
    "  bibstem:NatAs year:2020-2024    期刊缩写 / 年份范围\n"
    "  property:refereed doctype:article collection:planetary\n"
    '  object:"M31" uat:"exoplanets"   天体 / UAT 主题词\n'
    "  citations(bibcode:X) references(bibcode:X) similar(bibcode:X)\n"
    "  reviews(…) trending(…) useful(…)  AND OR NOT 与括号\n\n"
    "📝 笔记管理:\n"
    "  /paper note <ID> <内容>     - 添加论文笔记\n"
    "  /paper note <ID>            - 查看论文笔记\n"
    "  /paper note del <ID> <序号>  - 删除笔记\n\n"
    "💡 写作灵感:\n"
    "  /paper writing <章节> <想法> - 添加写作灵感\n"
    "  /paper writing <章节>        - 查看章节灵感\n"
    "  /paper writing del <章节> <序号> - 删除灵感\n\n"
    "🏷️ 研究兴趣:\n"
    "  /paper topics                - 查看关键词\n"
    "  /paper topics add <关键词>   - 添加关键词\n"
    "  /paper topics remove <关键词> - 删除关键词\n"
    "  /paper topics clear          - 清空关键词\n"
    "  /paper daily                 - 基于关键词推荐今日论文\n\n"
    "📅 截稿提醒:\n"
    "  /paper deadline add <名称> <日期> - 添加截稿日期\n"
    "  /paper deadline del <序号>        - 删除截稿日期\n"
    "  /paper deadline                   - 查看截稿日期\n\n"
    "📚 文献库:\n"
    "  /paper ref_add <ID>         - 添加引用到文献库\n"
    "  /paper refs                 - 查看文献库\n\n"
    "💡 示例:\n"
    '  /paper search abs:"fast radio burst" year:2023-2025 property:refereed --sort citations\n'
    "  /paper search bibstem:NatAs collection:planetary --abs --page 2\n"
    "  /paper stats bibstem:NatAs collection:planetary\n"
    "  /paper abstract 2018NatAs...2..206B 2301.04191\n"
    "  /paper cite 2401.12345 --format aastex"
)
