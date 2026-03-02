"""
Wolfram|Alpha 插件

调用 Wolfram|Alpha API 进行计算和查询。
"""

import asyncio
import logging
from typing import Any
from xml.etree import ElementTree

import aiohttp

from core.plugin_base import segments
from core.args import parse

logger = logging.getLogger(__name__)

def init(context=None) -> None:
    """插件初始化"""
    pass

async def handle(command: str, args: str, event: dict, context) -> list:
    """命令处理入口"""
    try:
        parsed = parse(args)
        
        # 检查帮助命令
        if not parsed or parsed.has("h") or parsed.has("help") or parsed.first.lower() in ["help", "帮助"]:
            return segments(_show_help())
        
        # 获取问题内容
        question = parsed.rest()
        if not question:
            return segments("请输入问题\n输入 /alpha help 查看帮助")
        
        # 获取 App ID
        appid = _get_appid(context)
        if not appid:
            return segments("❌ Wolfram|Alpha 未配置 appid\n请在 secrets.json 中配置 plugins.wolframalpha.appid")
        
        # 执行查询
        return await _get_answer(question, appid, context)
        
    except Exception as e:
        logger.exception("WolframAlpha handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")

def _show_help() -> str:
    """显示帮助信息"""
    return """
🧮 **Wolfram|Alpha 万能计算器**

**基本用法:**
• /alpha <问题> - 查询或计算
• /alpha help - 显示此帮助

**特殊后缀:**
• step - 显示步骤解答
  示例: /alpha integrate x^2 step
  
• cp - 仅返回完整结果
  示例: /alpha 1+1 cp

**查询示例:**
• /alpha 1+1 - 简单计算
• /alpha sin(pi/4) - 三角函数
• /alpha integrate x^2 - 积分
• /alpha solve x^2+2x+1=0 - 方程求解
• /alpha derivative of sin(x) - 求导
• /alpha population of China - 查询数据
• /alpha weather in Beijing - 天气查询
• /alpha convert 100 USD to CNY - 单位转换

**支持的内容:**
• 数学计算（代数、微积分、统计）
• 物理公式和常数
• 化学数据
• 单位转换
• 日期和时间计算
• 地理和天文数据
• 语言翻译

输入 /alpha help 查看此帮助
""".strip()

# ============================================================
# 配置获取
# ============================================================

def _get_appid(context) -> str:
    """获取 App ID"""
    return context.secrets.get("plugins", {}).get("wolframalpha", {}).get("appid", "")

# ============================================================
# 查询处理函数
# ============================================================

async def _get_answer(question: str, appid: str, context) -> list:
    """执行 Wolfram|Alpha 查询"""
    session = context.http_session
    if not session:
        return segments("❌ HTTP 会话未初始化")

    try:
        # 检查是否需要步骤解答
        if question.strip().endswith("step"):
            result = await _query_step(question[:-4].strip(), appid, session)
            return segments(f"📝 **步骤解答:**\n\n{result}")

        # 检查是否需要完整结果
        if question.strip().endswith("cp"):
            result = await _query_complete(question[:-2].strip(), appid, session)
            return segments(f"🔢 **计算结果:**\n\n{result}")

        # 简单查询 - 使用 v1/result API (最快速)
        url = "http://api.wolframalpha.com/v1/result"
        params = {"appid": appid, "i": question}
        
        async with session.get(url, params=params, timeout=30) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error("WolframAlpha API error: status=%d, body=%s", resp.status, error_text)
                return segments(f"❌ 查询失败: {error_text}")
            result = await resp.text()
        
        return segments(f"🔢 **{question}**\n\n{result}")
        
    except asyncio.TimeoutError:
        logger.error("WolframAlpha query timeout")
        return segments("❌ 查询超时，请稍后重试")
    except aiohttp.ClientError as e:
        logger.exception("WolframAlpha network error: %s", e)
        return segments(f"❌ 网络错误: {str(e)}")
    except Exception as e:
        logger.exception("WolframAlpha query failed: %s", e)
        return segments(f"❌ 查询失败: {str(e)}")

async def _query_step(question: str, appid: str, session) -> str:
    """获取步骤解答"""
    url = f"http://api.wolframalpha.com/v2/query?appid={appid}"
    data = {
        "input": question,
        "podstate": "Result__Step-by-step solution",
        "format": "plaintext",
    }
    
    async with session.post(url, data=data, timeout=30) as resp:
        if resp.status != 200:
            error_text = await resp.text()
            raise ValueError(f"API returned status {resp.status}: {error_text}")
        payload = await resp.text()
    
    root = ElementTree.fromstring(payload)
    lines = []
    for item in root.iter("plaintext"):
        if item.text:
            lines.append(item.text.strip())
    
    if not lines:
        return "未找到步骤解答"
    
    return "\n\n".join(lines)

async def _query_complete(question: str, appid: str, session) -> str:
    """获取完整结果"""
    url = "http://api.wolframalpha.com/v2/query"
    data = {
        "appid": appid,
        "input": question,
        "includepodid": "Result",
        "format": "plaintext",
        "output": "json",
    }
    
    async with session.post(url, data=data, timeout=30) as resp:
        if resp.status != 200:
            error_text = await resp.text()
            raise ValueError(f"API returned status {resp.status}: {error_text}")
        payload = await resp.json()
    
    # 提取结果
    try:
        result = payload["queryresult"]["pods"][0]["subpods"][0]["plaintext"]
        if not result:
            return "未找到结果"
        return result
    except (KeyError, IndexError) as e:
        logger.error("Failed to parse WolframAlpha response: %s", e)
        return "结果解析失败"
