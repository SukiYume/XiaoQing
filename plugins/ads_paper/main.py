"""ADS Paper 插件入口：解析命令并把职责分派到本地存储或远程 ADS 服务。"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from core.plugin_base import PluginContextProtocol, Segments, segments
from core.public_errors import public_error_response

from . import ai_commands, note_commands, paper_commands
from .ads_client import ADSClient
from .storage import PaperStorage

logger = logging.getLogger(__name__)


def init(context: PluginContextProtocol | None = None) -> None:
    """记录插件代际初始化；当前插件没有需要预热的独占资源。"""

    # 保留 context 参数是插件生命周期契约的一部分，资源由每次命令按需取得。
    del context
    logger.info("ADS Paper 插件已初始化")


def _get_ads_token(context: PluginContextProtocol) -> str:
    """从当前不可变配置快照读取插件私有 ADS Token。"""
    token = context.get_settings_snapshot().plugin_secrets("ads_paper").get("ads_token", "")
    if isinstance(token, str) and token:
        logger.debug("成功获取 ADS Token，长度: %s", len(token))
        return token
    if token:
        logger.warning("ADS Token 配置必须是字符串")
    else:
        logger.warning("未配置 ADS Token")
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
    """处理 ``/paper`` 命令；本地功能不依赖 ADS Token。"""
    try:
        logger.info("收到 ADS Paper 命令: %s %s", command, args)
        command_parts = args.strip().split(maxsplit=1)
        if not command_parts:
            logger.debug("无参数，显示帮助信息")
            return _show_help()

        subcommand      = command_parts[0].casefold()
        subcommand_args = command_parts[1] if len(command_parts) == 2 else ""
        logger.info("执行子命令: %s", subcommand)

        if subcommand in {"help", "帮助"}:
            if subcommand_args.strip():
                return segments("❌ 用法: /paper help")
            return _show_help()

        storage = _get_storage(context)
        user_id = _require_user_id(event)
        logger.debug("用户 ID: %s", user_id)

        # 本地命令只访问按用户隔离的文件，不应因 ADS 未配置而失效。
        if subcommand == "refs":
            if subcommand_args.strip():
                return segments("❌ 用法: /paper refs")
            return await ai_commands.cmd_refs(storage, context, user_id)

        local_commands: dict[str, Callable[[], Awaitable[Segments]]] = {
            "note": lambda: note_commands.cmd_note(storage, subcommand_args, user_id),
            "writing": lambda: note_commands.cmd_writing(storage, subcommand_args, user_id),
            "topics": lambda: note_commands.cmd_topics(storage, subcommand_args, user_id),
            "deadline": lambda: note_commands.cmd_deadline(storage, subcommand_args, user_id),
        }
        local_handler = local_commands.get(subcommand)
        if local_handler is not None:
            return await local_handler()

        remote_commands = {
            "search",
            "author",
            "cite",
            "cite-network",
            "related",
            "summarize",
            "daily",
            "ref_add",
        }
        if subcommand not in remote_commands:
            return segments(f"未知命令: {subcommand}\n输入 /paper help 查看帮助")

        if subcommand == "daily" and subcommand_args.strip():
            return segments("❌ 用法: /paper daily")

        token = _get_ads_token(context)
        if not token:
            logger.error("ADS Token 未配置")
            return segments(
                '❌ 未配置 ADS Token\n请在 secrets.json 中配置:\n  "plugins": {"ads_paper": {"ads_token": "your-token"}}'
            )

        # 只有远程子命令才创建 ADS 客户端，并复用核心提供的共享 HTTP 会话。
        client = ADSClient(token, context.http_session, context)
        remote_handlers: dict[str, Callable[[], Awaitable[Segments]]] = {
            "search": lambda: paper_commands.cmd_search(client, subcommand_args),
            "author": lambda: paper_commands.cmd_author(client, subcommand_args),
            "cite": lambda: paper_commands.cmd_cite(client, subcommand_args),
            "cite-network": lambda: paper_commands.cmd_cite_network(client, subcommand_args),
            "related": lambda: paper_commands.cmd_related(client, subcommand_args),
            "summarize": lambda: ai_commands.cmd_summarize(client, subcommand_args, context),
            "daily": lambda: ai_commands.cmd_daily(client, storage, user_id),
            "ref_add": lambda: ai_commands.cmd_ref_add(
                client,
                subcommand_args,
                storage,
                context,
                user_id,
            ),
        }
        return await remote_handlers[subcommand]()

    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "ads_paper.handle",
        )


def _show_help() -> Segments:
    """显示帮助信息"""
    help_text = (
        "📚 **论文与文献管理助手**\n\n"
        "📖 论文搜索:\n"
        "  /paper search <关键词>      - 搜索论文\n"
        "  /paper author <作者>        - 查找作者论文\n"
        "  /paper cite <ID>            - 获取 BibTeX (支持 arXiv ID/链接/Bibcode)\n"
        "  /paper cite-network <ID>    - 查看引用网络 (支持 arXiv ID/链接/Bibcode)\n"
        "  /paper related <ID>         - 查找相关论文 (支持 arXiv ID/链接/Bibcode)\n\n"
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
        "  /paper topics clear          - 清空关键词\n\n"
        "📅 截稿提醒:\n"
        "  /paper deadline add <名称> <日期> - 添加截稿日期\n"
        "  /paper deadline del <序号>        - 删除截稿日期\n"
        "  /paper deadline                   - 查看截稿日期\n\n"
        "🤖 AI 功能:\n"
        "  /paper summarize <ID>       - AI 生成论文摘要 (支持 arXiv ID/链接/Bibcode)\n"
        "  /paper daily                - 基于关键词推荐今日论文\n\n"
        "📚 文献库:\n"
        "  /paper ref_add <ID>         - 添加引用到文献库 (支持 arXiv ID/链接/Bibcode)\n"
        "  /paper refs                 - 查看文献库\n\n"
        "💡 示例:\n"
        '  /paper search "fast radio burst"\n'
        "  /paper cite 2401.12345\n"
        "  /paper topics add fast radio burst\n"
        "  /paper daily"
    )
    return segments(help_text)
