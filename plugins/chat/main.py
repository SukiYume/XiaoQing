"""
AI 对话插件 (Coze API)
提供与 AI 的对话功能
"""
import asyncio
import logging
from typing import Any, Optional

from core.plugin_base import segments
from core.args import parse

logger = logging.getLogger(__name__)

# ============================================================
# 常量配置
# ============================================================

COZE_API_URL = "https://api.coze.com/open_api/v2/chat"
DEFAULT_USER_ID = "123223"
REQUEST_TIMEOUT = 30  # 秒
MAX_QUERY_LENGTH = 2000  # 最大查询长度

# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    pass

# ============================================================
# 配置管理
# ============================================================

def get_config(context) -> dict[str, Any]:
    """获取并验证插件配置
    
    Args:
        context: 插件上下文
        
    Returns:
        配置字典
    """
    config = context.secrets.get("plugins", {}).get("chat", {})
    
    # 验证必需的配置项
    if not config:
        context.logger.warning("Chat 插件配置不存在")
    elif not config.get("token"):
        context.logger.warning("Chat 插件缺少 token 配置")
    elif not config.get("bot_id"):
        context.logger.warning("Chat 插件缺少 bot_id 配置")
    
    return config

def validate_config(config: dict[str, Any]) -> tuple[bool, Optional[str]]:
    """验证配置的完整性
    
    Args:
        config: 配置字典
        
    Returns:
        (是否有效, 错误信息)
    """
    if not config:
        return False, "插件配置为空，请在 secrets.json 中配置 chat 插件"
    
    if not config.get("token"):
        return False, "缺少 token 配置"
    
    if not config.get("bot_id"):
        return False, "缺少 bot_id 配置"
    
    return True, None

# ============================================================
# API 调用
# ============================================================

async def call_coze_api(
    query: str,
    config: dict[str, Any],
    context
) -> Optional[dict[str, Any]]:
    """调用 Coze API
    
    Args:
        query: 用户查询内容
        config: 插件配置
        context: 插件上下文
        
    Returns:
        API 响应数据，失败时返回 None
    """
    token = config.get("token")
    bot_id = config.get("bot_id")
    user_id = config.get("user", DEFAULT_USER_ID)
    proxy = config.get("proxy")
    stream = config.get("stream", False)
    
    # 构建请求
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Host": "api.coze.com",
        "Connection": "keep-alive",
    }
    
    payload = {
        "bot_id": bot_id,
        "user": user_id,
        "query": query,
        "stream": stream,
    }
    
    request_kwargs: dict[str, Any] = {
        "headers": headers,
        "json": payload,
        "timeout": REQUEST_TIMEOUT
    }
    
    if proxy:
        request_kwargs["proxy"] = proxy
        context.logger.debug(f"使用代理: {proxy}")
    
    # 发送请求
    try:
        context.logger.info(f"调用 Coze API，查询长度: {len(query)} 字符")
        async with context.http_session.post(COZE_API_URL, **request_kwargs) as response:
            if response.status != 200:
                error_text = await response.text()
                context.logger.error(f"Coze API 返回错误: HTTP {response.status}, {error_text[:200]}")
                return None
            
            data = await response.json()
            context.logger.info(f"Coze API 调用成功，响应消息数: {len(data.get('messages', []))}")
            return data
            
    except asyncio.TimeoutError:
        context.logger.error(f"Coze API 请求超时 ({REQUEST_TIMEOUT}s)")
        return None
    except Exception as exc:
        context.logger.error(f"Coze API 请求异常: {type(exc).__name__}: {exc}", exc_info=True)
        return None

def extract_answer(data: dict[str, Any], context) -> Optional[str]:
    """从 API 响应中提取答案
    
    Args:
        data: API 响应数据
        context: 插件上下文
        
    Returns:
        答案文本，失败时返回 None
    """
    if not isinstance(data, dict):
        context.logger.error(f"API 响应格式错误: 期望字典，得到 {type(data)}")
        return None
    
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        context.logger.error(f"messages 字段格式错误: 期望列表，得到 {type(messages)}")
        return None
    
    # 提取类型为 "answer" 的消息
    answers = [msg for msg in messages if isinstance(msg, dict) and msg.get("type") == "answer"]
    
    if not answers:
        context.logger.warning(f"未找到答案消息，消息总数: {len(messages)}")
        # 尝试查看是否有其他类型的消息
        message_types = [msg.get("type") for msg in messages if isinstance(msg, dict)]
        context.logger.debug(f"消息类型: {message_types}")
        return None
    
    answer_content = answers[0].get("content", "").strip()
    if not answer_content:
        context.logger.warning("答案内容为空")
        return None
    
    context.logger.debug(f"提取到答案，长度: {len(answer_content)} 字符")
    return answer_content

# ============================================================
# 命令处理
# ============================================================

async def handle(
    command: str, 
    args: str, 
    event: dict[str, Any], 
    context
) -> list[dict[str, Any]]:
    """命令处理入口"""
    try:
        parsed = parse(args)
        
        # 解析子命令
        if parsed and parsed.first:
            subcommand = parsed.first.lower()
            
            if subcommand == "help" or subcommand == "帮助":
                return segments(_show_help())
        
        # 解析参数
        query = args.strip()
        if not query:
            return segments("💬 请输入要对话的内容\n\n用法: /chat <你的问题>")
        
        # 限制查询长度
        if len(query) > MAX_QUERY_LENGTH:
            return segments(f"❌ 查询内容过长（{len(query)} 字符），最多支持 {MAX_QUERY_LENGTH} 字符")
        
        # 获取并验证配置
        config = get_config(context)
        is_valid, error_msg = validate_config(config)
        if not is_valid:
            logger.error(f"Chat 插件配置无效: {error_msg}")
            return segments(f"❌ 配置错误: {error_msg}")
        
        logger.info(f"用户查询: {query[:50]}{'...' if len(query) > 50 else ''}")
        
        # 调用 API
        data = await call_coze_api(query, config, context)
        if data is None:
            return segments("❌ AI 对话失败，请稍后重试")
        
        # 提取答案
        answer = extract_answer(data, context)
        if answer is None:
            return segments("❌ 未能获取到有效回答")
        
        # 返回答案
        return segments(answer)
        
    except Exception as e:
        logger.exception("Chat handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")

def _show_help() -> str:
    """显示帮助信息"""
    return """
💬 **AI 对话助手**

与 AI 进行智能对话

**使用方法:**
• /chat <问题> - 向 AI 提问
• /gpt <问题> - 向 AI 提问（别名）
• /chat help - 显示帮助信息

**示例:**
• /chat 什么是快速射电暴？
• /gpt 解释一下黑洞的形成过程

**功能特点:**
- 基于先进的 AI 模型
- 支持自然语言对话
- 快速响应

输入 /chat <你的问题> 开始对话
""".strip()
