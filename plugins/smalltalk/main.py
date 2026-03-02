"""
闲聊插件

实现以下功能：
1. 当消息只有 bot_name 时，随机回复
2. 群聊中没有 bot_name 时，有概率（5%）随机回复
3. 有 bot_name 但不是命令时，调用闲聊处理
4. 私聊不是命令时，调用闲聊处理
5. 支持自定义问答对
6. 支持一定概率将文字回复转换为语音
"""

# 标准库
import json
import logging
import random
from pathlib import Path

# 本地导入
from core.args import parse
from core.plugin_base import ensure_dir, load_json, segments, write_json


logger = logging.getLogger(__name__)


# ============================================================
# 常量配置
# ============================================================

GROUP_RANDOM_REPLY_RATE = 0.05  # 群聊随机回复概率 5%


def init(context=None) -> None:
    """插件初始化"""
    logger.info("Smalltalk plugin initialized")


# ============================================================
# 随机回复（只叫 bot_name）
# ============================================================

# 默认回复列表
DEFAULT_RESPONSES = [
    "叫我干嘛",
    "嗯嗯，我就是小青",
    "我是小青，叫我有什么事情吗？",
    "在的在的",
    "嗯？",
    "有什么事吗？",
    "我在~",
    "叫我干啥",
    "干嘛干嘛~",
]


def _load_responses(context) -> list:
    """加载随机回复列表"""
    # 尝试加载 小青.json
    xiaoqing_file = context.data_dir / "小青.json"
    if xiaoqing_file.exists():
        data = load_json(xiaoqing_file, {})
        responses = data.get("小青", DEFAULT_RESPONSES)
        if responses:
            return responses
    
    # 尝试加载 responses.json
    data_file = context.data_dir / "responses.json"
    if data_file.exists():
        data = load_json(data_file, {})
        return data.get("responses", DEFAULT_RESPONSES)
    
    return DEFAULT_RESPONSES


def call_bot_name_only(context) -> list:
    """当消息只有 bot_name 时的随机回复"""
    responses = _load_responses(context)
    response = random.choice(responses)
    return segments(response)


# ============================================================
# 问答对管理
# ============================================================

def _load_qa(context) -> dict:
    """加载问答对"""
    qa_file = context.data_dir / "QA.json"
    return load_json(qa_file, {})


def _save_qa(context, data: dict) -> None:
    """保存问答对"""
    ensure_dir(context.data_dir)
    qa_file = context.data_dir / "QA.json"
    write_json(qa_file, data)


def get_qa_answer(context, question: str) -> str | None:
    """查找问答对"""
    data = _load_qa(context)
    if question in data:
        answers = data[question]
        return random.choice(answers)
    return None


# ============================================================
# 命令处理
# ============================================================

async def handle(command: str, args: str, event: dict, context) -> list[dict]:
    """
    命令处理入口
    
    Args:
        command: plugin.json 中定义的 command name (如 'qa', 'qa_list', 'qa_remove')
        args: 用户输入的参数字符串
        event: 原始事件数据
        context: 插件上下文
    """
    try:
        parsed = parse(args)
        
        # 子命令路由
        if command == "qa":
            # 帮助命令
            if parsed and parsed.first.lower() in {"help", "帮助", "?"}:
                return segments(_show_help_qa())
            return _add_qa(context, args)
        
        elif command == "qa_list":
            # 帮助命令
            if parsed and parsed.first.lower() in {"help", "帮助", "?"}:
                return segments(_show_help_qa_list())
            return _list_qa(context, args)
        
        elif command == "qa_remove":
            # 帮助命令
            if parsed and parsed.first.lower() in {"help", "帮助", "?"}:
                return segments(_show_help_qa_remove())
            return _remove_qa(context, args)
        
        return segments("未知命令")
        
    except Exception as e:
        logger.exception("Smalltalk handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")


def _show_help_qa() -> str:
    """显示问答对添加帮助"""
    return """
💬 **问答对添加**

**基本用法:**
• /记忆 <问题> <回答> - 添加问答对
• /学习 <问题> <回答> - 添加问答对
• /记忆 help - 显示帮助

**示例:**
• /记忆 你好 你好呀
• /学习 晚安 晚安啦~

**功能说明:**
✨ 同一问题可以有多个回答
🎲 回复时会随机选择一个
🔍 问题必须完全匹配

输入 /记忆 <问题> <回答> 添加问答
""".strip()


def _show_help_qa_list() -> str:
    """显示问答对查询帮助"""
    return """
📜 **问答对查询**

**基本用法:**
• /对话 - 查看所有问题
• /对话 <问题> - 查看指定问题的回答
• /对话 help - 显示帮助

**示例:**
• /对话
• /对话 你好

输入 /对话 查看所有问答对
""".strip()


def _show_help_qa_remove() -> str:
    """显示问答对删除帮助"""
    return """
🗑️ **问答对删除**

**基本用法:**
• /删除对话 <问题> - 删除整个问题
• /删除对话 <问题> <回答> - 删除特定回答
• /删除对话 help - 显示帮助

**示例:**
• /删除对话 你好
• /删除对话 你好 你好呀

**注意:**
⚠️ 此命令仅管理员可用

输入 /删除对话 <问题> 删除问答对
""".strip()


def _add_qa(context, args: str) -> list[dict]:
    """添加问答对"""
    parts = args.split(None, 1)  # 只分割成两部分
    if len(parts) < 2:
        return segments("格式: 记忆 问题 回答")
    
    question, answer = parts[0], parts[1]
    data = _load_qa(context)
    
    if question in data:
        if answer in data[question]:
            return segments("这个我已经知道了。")
        data[question].append(answer)
    else:
        data[question] = [answer]
    
    _save_qa(context, data)
    return segments("对话添加成功了！")


def _list_qa(context, args: str) -> list[dict]:
    """列出问答对"""
    data = _load_qa(context)
    question = args.strip()
    
    if question:
        if question in data:
            answers = "\n".join(data[question])
            return segments(f"{question}：\n{answers}")
        else:
            return segments("没有这个问题的回答")
    else:
        if data:
            questions = "\n".join(list(data.keys()))
            return segments(f"问答列表:\n{questions}")
        else:
            return segments("还没有任何问答对")


def _remove_qa(context, args: str) -> list[dict]:
    """删除问答对"""
    parts = args.split()
    if not parts:
        return segments("要删除哪个对话？格式: 删除对话 问题 [回答]")
    
    question = parts[0]
    answer = parts[1] if len(parts) > 1 else None
    
    data = _load_qa(context)
    
    if question not in data:
        return segments("似乎没有这个对话呢")
    
    if answer:
        # 删除特定回答
        if answer in data[question]:
            data[question].remove(answer)
            if not data[question]:
                del data[question]
            _save_qa(context, data)
            return segments(f"对话: {question} - {answer} 删除成功了。")
        else:
            return segments("没有这个回答")
    else:
        # 删除整个问题
        removed = data.pop(question)
        _save_qa(context, data)
        return segments(f"对话: {question} - {'|'.join(removed)} 删除成功了。")


# ============================================================
# 导出函数（供 dispatcher 调用）
# ============================================================

async def handle_smalltalk(text_content: str, event: dict, context) -> list[dict] | None:
    """
    处理闲聊消息
    
    返回 None 表示不处理，返回消息段列表表示回复
    """
    # 检查是否在问答对中
    answer = get_qa_answer(context, text_content)
    if answer:
        reply = segments(answer)
        # 有概率转换为语音
        reply = await _maybe_convert_to_voice(reply, context)
        return reply
    
    # 调用 Coze API 进行闲聊
    reply = await _call_chat_api(text_content, context)
    # 有概率转换为语音
    reply = await _maybe_convert_to_voice(reply, context)
    return reply


async def _maybe_convert_to_voice(reply: list[dict], context) -> list[dict]:
    """
    以一定概率将文字回复转换为语音
    
    参数:
        reply: 原始回复消息段列表
        context: 插件上下文
        
    返回:
        可能转换为语音的消息段列表
    """
    # 获取配置中的语音转换概率（默认 20%）
    plugin_config = context.config.get("plugins", {}).get("smalltalk", {})
    voice_probability = plugin_config.get("voice_probability", 0.2)
    
    # 判断是否触发语音转换
    if random.random() > voice_probability:
        return reply
    
    # 尝试导入 voice 插件
    try:
        from plugins.voice import main as voice_plugin
    except ImportError:
        logger.warning("Voice plugin not found, cannot convert to voice")
        return reply
    
    # 提取文字内容
    text_content = ""
    for seg in reply:
        if seg.get("type") == "text":
            text_content += seg.get("data", {}).get("text", "")
    
    if not text_content:
        return reply
    
    # 转换为语音
    try:
        voice_reply = await voice_plugin.convert_text_to_voice(text_content, context)
        if voice_reply:
            logger.info("Converted text to voice: %s...", text_content[:30])
            return voice_reply
    except Exception as exc:
        logger.error("Failed to convert to voice: %s", exc)
    
    # 转换失败，返回原始文字
    return reply


async def _call_chat_api(text_content: str, context) -> list[dict]:
    """
    调用 chat 插件进行对话
    
    复用 chat 插件的 Coze API 调用逻辑，避免代码重复
    """
    try:
        # 动态导入 chat 插件
        from plugins.chat import main as chat_plugin
        
        # 构造一个空事件（chat 插件不使用 event）
        dummy_event: dict = {}
        
        # 调用 chat 插件的 handle 函数
        result = await chat_plugin.handle("chat", text_content, dummy_event, context)
        return result
    except ImportError:
        logger.warning("Chat plugin not available")
        return segments("暂时无法回复~")
    except Exception as exc:
        logger.warning("Chat plugin call failed: %s", exc)
        return segments("暂时无法回复，请稍后再试~")
