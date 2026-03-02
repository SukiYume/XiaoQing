"""
语音功能插件

实现功能：
1. 文字转语音(TTS) - 使用 Azure 认知服务
2. 语音转文字(STT) - 使用 Azure 认知服务
3. 提供转换接口供其他插件调用
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from core.plugin_base import segments, PluginContextProtocol
from core.args import parse


logger = logging.getLogger(__name__)


def init(context=None) -> None:
    """插件初始化"""
    logger.info("语音功能插件已加载 (Voice Plugin Loaded)")


def _show_help() -> str:
    """返回帮助信息"""
    return (
        "🔊 语音功能使用方法：\n\n"
        "1. 文字转语音：\n"
        "   /语音 <文本> 或 /念 <文本> 或 /tts <文本>\n"
        "   例：/语音 你好，我是小青\n\n"
        "2. 查看帮助：\n"
        "   /语音 help\n\n"
        "💡 插件使用 Azure 认知服务，支持多种声音和风格"
    )


# ============================================================
# TTS (文字转语音)
# ============================================================

async def text_to_speech(text: str, context: PluginContextProtocol) -> Optional[str]:
    """
    将文字转换为语音文件
    
    参数:
        text: 要转换的文字
        context: 插件上下文
        
    返回:
        生成的音频文件路径，失败返回 None
    """
    # 获取配置
    voice_config = context.secrets.get("plugins", {}).get("voice", {})
    subscription_key = voice_config.get("subscription_key")
    region = voice_config.get("region", "southeastasia")
    voice_name = voice_config.get("voice_name", "zh-CN-XiaomoNeural")
    style = voice_config.get("style", "cheerful")
    role = voice_config.get("role", "Girl")
    
    if not subscription_key:
        logger.warning("Azure TTS 未配置 subscription_key")
        return None
    
    # 生成唯一文件名（基于文本内容）
    text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()[:8]
    audio_dir = context.data_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    output_file = audio_dir / f"tts_{text_hash}.mp3"
    
    # 如果文件已存在，直接返回
    if output_file.exists():
        logger.info(f"使用缓存音频: {output_file}")
        return str(output_file.absolute())
    
    # 构建 SSML（对用户输入进行 XML 转义，防止 SSML 注入）
    safe_text = xml_escape(text)
    ssml = f'''
    <speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis"
           xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="zh-CN">
        <voice name="{voice_name}">
            <mstts:express-as role="{role}" style="{style}">
                {safe_text}
            </mstts:express-as>
        </voice>
    </speak>
    '''.strip()
    
    # 调用 Azure TTS API
    url = f'https://{region}.tts.speech.microsoft.com/cognitiveservices/v1'
    headers = {
        'Ocp-Apim-Subscription-Key': subscription_key,
        'X-Microsoft-OutputFormat': 'audio-24khz-96kbitrate-mono-mp3',
        'Content-Type': 'application/ssml+xml',
        'User-Agent': 'XiaoQing/1.0'
    }
    
    try:
        async with context.http_session.post(url, data=ssml.encode('utf-8'), headers=headers) as response:
            if response.status == 200:
                content = await response.read()
                output_file.write_bytes(content)
                logger.info(f"生成音频: {output_file}")
                return str(output_file.absolute())
            else:
                error_text = await response.text()
                logger.error(f"Azure TTS API 错误 {response.status}: {error_text}")
                return None
    except Exception as exc:
        logger.error(f"Azure TTS API 调用失败: {exc}", exc_info=True)
        return None


# ============================================================
# STT (语音转文字)
# ============================================================

async def speech_to_text(audio_path: str, context: PluginContextProtocol) -> Optional[Tuple[str, str]]:
    """
    将语音文件转换为文字
    
    参数:
        audio_path: 音频文件路径
        context: 插件上下文
        
    返回:
        (分词文本, 完整文本) 元组，失败返回 None
    """
    # 获取配置
    voice_config = context.secrets.get("plugins", {}).get("voice", {})
    subscription_key = voice_config.get("subscription_key")
    region = voice_config.get("region", "eastasia")  # STT 使用 eastasia
    
    if not subscription_key:
        logger.warning("Azure STT 未配置 subscription_key")
        return None
    
    # 调用 Azure STT API
    url = f'https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1'
    url += '?language=zh-CN&format=detailed'
    
    headers = {
        'Ocp-Apim-Subscription-Key': subscription_key,
        'Content-type': 'audio/wav; codecs=audio/pcm; samplerate=16000',
        'Accept': 'application/json'
    }
    
    try:
        audio_data = await asyncio.to_thread(Path(audio_path).read_bytes)
        
        async with context.http_session.post(url, data=audio_data, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                if 'NBest' in data and len(data['NBest']) > 0:
                    lexical = data['NBest'][0].get('Lexical', '')
                    display = data['NBest'][0].get('Display', '')
                    logger.info(f"语音识别成功: {display}")
                    return (lexical, display)
                else:
                    logger.warning("未识别到语音内容")
                    return None
            else:
                error_text = await response.text()
                logger.error(f"Azure STT API 错误 {response.status}: {error_text}")
                return None
    except Exception as exc:
        logger.error(f"Azure STT API 调用失败: {exc}", exc_info=True)
        return None


# ============================================================
# 命令处理
# ============================================================

async def handle(command: str, args: str, event: dict, context: PluginContextProtocol) -> list:
    """
    命令处理入口
    
    参数:
        command: plugin.json 中定义的 command name
        args: 命令后的参数字符串
        event: 原始 OneBot 事件
        context: 插件上下文
        
    返回:
        消息段列表
    """
    try:
        # 使用 parse 解析参数
        parsed = parse(args)
        
        if command == "tts":
            # 检查是否请求帮助
            if parsed and parsed.first.lower() in ["help", "帮助"]:
                return segments(_show_help())
            
            return await _handle_tts(args, context)
        
        return segments("未知命令")
    
    except Exception as exc:
        logger.error(f"处理命令时出错: {exc}", exc_info=True)
        return segments(f"❌ 处理失败: {str(exc)}")


async def _handle_tts(args: str, context: PluginContextProtocol) -> list[dict[str, Any]]:
    """处理 TTS 命令"""
    text = args.strip()
    
    if not text:
        return segments("请输入要转换的文字，例如: 语音 你好")
    
    # 调用 TTS
    audio_path = await text_to_speech(text, context)
    
    if not audio_path:
        return segments("语音合成失败")
    
    # 返回语音消息
    return [
        {"type": "record", "data": {"file": f"file:///{audio_path}"}}
    ]


# ============================================================
# 工具函数（供其他插件调用）
# ============================================================

async def convert_text_to_voice(text: str, context: PluginContextProtocol) -> Optional[list[dict[str, Any]]]:
    """
    将文本转换为语音消息段
    
    供其他插件调用的工具函数
    
    参数:
        text: 要转换的文字
        context: 插件上下文
        
    返回:
        语音消息段列表，失败返回 None
    """
    audio_path = await text_to_speech(text, context)
    
    if not audio_path:
        return None
    
    return [
        {"type": "record", "data": {"file": f"file:///{audio_path}"}}
    ]
