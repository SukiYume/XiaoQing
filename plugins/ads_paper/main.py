"""
论文与文献管理插件
提供论文搜索、笔记管理、AI摘要等功能
"""
import logging
from typing import Any

from core.plugin_base import segments
from core.args import parse

from .ads_client import ADSClient
from .storage import PaperStorage
from . import paper_commands
from . import note_commands
from . import ai_commands

logger = logging.getLogger(__name__)

def init(context=None) -> None:
    """插件初始化"""
    logger.info("ADS Paper 插件已初始化")

def _get_ads_token(context) -> str:
    """获取ADS Token"""
    token = context.secrets.get("plugins", {}).get("ads_paper", {}).get("ads_token", "")
    if token:
        logger.debug(f"成功获取 ADS Token，长度: {len(token)}")
    else:
        logger.warning("未配置 ADS Token")
    return token

def _get_user_id(event: dict[str, Any]) -> int:
    """获取用户ID"""
    return event.get("user_id", 0)

async def handle(command: str, args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """命令处理入口"""
    try:
        logger.info(f"收到 ADS Paper 命令: {command} {args}")
        token = _get_ads_token(context)
        if not token:
            logger.error("ADS Token 未配置")
            return segments("❌ 未配置 ADS Token\n请在 secrets.json 中配置:\n  \"plugins\": {\"ads_paper\": {\"ads_token\": \"your-token\"}}")

        # Use shared HTTP session for connection pooling
        client = ADSClient(token, context.http_session)
        storage = PaperStorage(context.data_dir)
        user_id = _get_user_id(event)
        logger.debug(f"用户 ID: {user_id}")

        parsed = parse(args)

        if not parsed:
            logger.debug("无参数，显示帮助信息")
            return _show_help()

        subcommand = parsed.first.lower()
        logger.info(f"执行子命令: {subcommand}")

        if subcommand == "help" or subcommand == "帮助":
            return _show_help()

        COMMAND_MAP = {
            "search": lambda: paper_commands.cmd_search(client, parsed.rest(1)),
            "author": lambda: paper_commands.cmd_author(client, parsed.rest(1)),
            "cite": lambda: paper_commands.cmd_cite(client, parsed.rest(1)),
            "cite-network": lambda: paper_commands.cmd_cite_network(client, parsed.rest(1)),
            "related": lambda: paper_commands.cmd_related(client, parsed.rest(1)),
            "note": lambda: note_commands.cmd_note(storage, parsed.rest(1), user_id),
            "writing": lambda: note_commands.cmd_writing(storage, parsed.rest(1), user_id),
            "topics": lambda: note_commands.cmd_topics(storage, parsed.rest(1)),
            "deadline": lambda: note_commands.cmd_deadline(storage, parsed.rest(1), user_id),
            "summarize": lambda: ai_commands.cmd_summarize(client, parsed.rest(1), context),
            "daily": lambda: ai_commands.cmd_daily(client, context),
            "ref_add": lambda: ai_commands.cmd_ref_add(client, parsed.rest(1), context),
            "refs": lambda: ai_commands.cmd_refs(context),
        }

        handler = COMMAND_MAP.get(subcommand)
        if handler:
            return await handler()

        return segments(f"未知命令: {subcommand}\n输入 /paper help 查看帮助")
        
    except Exception as e:
        logger.exception("ADS Paper handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")

def _show_help() -> list[dict[str, Any]]:
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
        "  /paper search \"fast radio burst\"\n"
        "  /paper cite 2401.12345\n"
        "  /paper topics add fast radio burst\n"
        "  /paper daily"
    )
    return segments(help_text)
