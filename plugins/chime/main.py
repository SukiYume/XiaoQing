"""
CHIME FRB 重复暴监测插件
提供 CHIME FRB 重复暴观测数据的获取和监测功能
"""

# 标准库
import logging
import re
from pathlib import Path

# 本地导入
from core.args import parse
from core.plugin_base import ensure_dir, load_json, segments, write_json


logger = logging.getLogger(__name__)


# ============================================================
# 常量配置
# ============================================================

CHIME_API_URL = 'https://catalog.chime-frb.ca/repeaters'
PULSE_DATE_PATTERN = r'\d{6}'  # 用于匹配脉冲日期键（如 "201225"）
MAX_DISPLAY_FRBS = 5  # 最多显示的 FRB 数量


# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    pass


# ============================================================
# 数据模型
# ============================================================

class FRBData:
    """FRB 数据模型"""
    def __init__(self, name: str, info: dict):
        self.name = name
        self.raw_info = info
        self._parse_data()
    
    def _parse_data(self):
        """解析 FRB 数据"""
        # 提取所有脉冲日期
        self.pulses = sorted([k for k in self.raw_info.keys() if re.match(PULSE_DATE_PATTERN, k)])
        
        if self.pulses:
            self.latest_pulse = self.pulses[-1]
            pulse_data = self.raw_info.get(self.latest_pulse, {})
            
            # 提取各字段的值，支持嵌套结构
            self.timestamp = self._get_value(pulse_data, 'timestamp')
            self.dm = self._get_value(pulse_data, 'dm')
            self.snr = self._get_value(pulse_data, 'snr')
        else:
            self.latest_pulse = None
            self.timestamp = None
            self.dm = None
            self.snr = None
        
        # 位置信息
        self.ra = self._get_value(self.raw_info, 'ra')
        self.dec = self._get_value(self.raw_info, 'dec')
    
    def _get_value(self, data: dict, key: str) -> str:
        """安全获取嵌套字典中的值"""
        item = data.get(key, {})
        if isinstance(item, dict):
            return str(item.get('value', 'N/A'))
        return str(item) if item else 'N/A'
    
    def format_info(self) -> str:
        """格式化 FRB 信息为可读文本"""
        lines = [
            f"FRB: {self.name}",
            f"Time: {self.timestamp}",
            f"DM: {self.dm}",
            f"RA: {self.ra}",
            f"DEC: {self.dec}",
            f"SNR: {self.snr}"
        ]
        return "\n".join(lines)
    
    def is_valid(self) -> bool:
        """检查数据是否有效"""
        return self.latest_pulse is not None and self.timestamp is not None


# ============================================================
# 数据获取
# ============================================================

async def fetch_chime_repeaters(context) -> dict | None:
    """从 CHIME FRB 网站获取重复暴数据
    
    Args:
        context: 插件上下文
        
    Returns:
        包含 FRB 数据的字典，失败时返回 None
    """
    try:
        context.logger.info(f"正在请求 CHIME API: {CHIME_API_URL}")
        async with context.http_session.post(CHIME_API_URL, json={}, timeout=30) as response:
            if response.status == 200:
                data = await response.json()
                if not isinstance(data, dict):
                    context.logger.error(f"CHIME API 返回数据格式错误: 期望字典，得到 {type(data)}")
                    return None
                context.logger.info(f"成功获取 CHIME 数据，共 {len(data)} 个重复暴")
                return data
            else:
                context.logger.warning(f"CHIME API 请求失败: HTTP {response.status}")
                return None
    except asyncio.TimeoutError:
        context.logger.error("CHIME API 请求超时")
        return None
    except Exception as exc:
        context.logger.error(f"CHIME API 请求异常: {type(exc).__name__}: {exc}", exc_info=True)
        return None


# ============================================================
# 数据处理
# ============================================================

def parse_frb_data(data: dict, context) -> list:
    """解析 FRB 数据，提取关键信息
    
    Args:
        data: 从 CHIME API 获取的原始数据
        context: 插件上下文
        
    Returns:
        FRBData 对象列表
    """
    frb_list = []
    
    for name, info in data.items():
        try:
            frb = FRBData(name, info)
            if frb.is_valid():
                frb_list.append(frb)
            else:
                context.logger.debug(f"跳过无效 FRB 数据: {name}")
        except Exception as exc:
            context.logger.warning(f"解析 FRB {name} 数据时出错: {exc}")
            continue
    
    return frb_list


def build_history_mapping(frb_list: list) -> dict:
    """构建历史记录映射 {name: latest_timestamp}
    
    Args:
        frb_list: FRBData 对象列表
        
    Returns:
        名称到最新时间戳的映射
    """
    return {frb.name: frb.timestamp for frb in frb_list if frb.timestamp}


def find_updates(
    new_data: dict, 
    old_mapping: dict,
    context
) -> tuple:
    """比较新旧数据，找出更新
    
    Args:
        new_data: 新获取的原始数据
        old_mapping: 旧的历史记录映射
        context: 插件上下文
        
    Returns:
        (新重复暴列表, 新脉冲列表)
    """
    new_repeaters = []
    new_pulses = []
    
    for name, info in new_data.items():
        try:
            frb = FRBData(name, info)
            if not frb.is_valid():
                continue
            
            old_timestamp = old_mapping.get(name)
            
            if old_timestamp is None:
                # 新发现的重复暴
                new_repeaters.append(frb)
                context.logger.info(f"发现新重复暴: {name}")
            elif frb.timestamp != old_timestamp:
                # 已知重复暴有新脉冲
                new_pulses.append(frb)
                context.logger.info(f"检测到新脉冲: {name} ({old_timestamp} -> {frb.timestamp})")
        except Exception as exc:
            context.logger.warning(f"处理 FRB {name} 更新时出错: {exc}")
            continue
    
    return new_repeaters, new_pulses


def load_history(context) -> dict:
    """加载历史记录
    
    Args:
        context: 插件上下文
        
    Returns:
        历史记录映射
    """
    ensure_dir(context.data_dir)
    history_file = context.data_dir / "chime_history.json"
    return load_json(history_file, {})


def save_history(context, mapping: dict) -> bool:
    """保存历史记录
    
    Args:
        context: 插件上下文
        mapping: 要保存的映射
        
    Returns:
        是否保存成功
    """
    try:
        ensure_dir(context.data_dir)
        history_file = context.data_dir / "chime_history.json"
        write_json(history_file, mapping)
        context.logger.debug(f"历史记录已保存: {len(mapping)} 条")
        return True
    except Exception as exc:
        context.logger.error(f"保存历史记录失败: {exc}", exc_info=True)
        return False


def format_update_message(
    new_repeaters: list,
    new_pulses: list,
    is_scheduled: bool = False
) -> str:
    """格式化更新消息
    
    Args:
        new_repeaters: 新重复暴列表
        new_pulses: 新脉冲列表
        is_scheduled: 是否为定时检查
        
    Returns:
        格式化的消息文本
    """
    lines = []
    
    if is_scheduled:
        lines.append("🔔 **CHIME FRB 更新通知**")
        lines.append("")
    
    if new_repeaters:
        lines.append("🆕 **新发现的重复暴:**")
        for frb in new_repeaters[:MAX_DISPLAY_FRBS]:
            lines.append(frb.format_info())
            lines.append("")
        if len(new_repeaters) > MAX_DISPLAY_FRBS:
            lines.append(f"... 还有 {len(new_repeaters) - MAX_DISPLAY_FRBS} 个")
            lines.append("")
    
    if new_pulses:
        lines.append("📡 **检测到新脉冲:**")
        for frb in new_pulses[:MAX_DISPLAY_FRBS]:
            lines.append(frb.format_info())
            lines.append("")
        if len(new_pulses) > MAX_DISPLAY_FRBS:
            lines.append(f"... 还有 {len(new_pulses) - MAX_DISPLAY_FRBS} 个")
            lines.append("")
    
    return "\n".join(lines)


# ============================================================
# 命令处理
# ============================================================

async def handle(
    command: str, 
    args: str, 
    event: dict, 
    context
) -> list:
    """命令处理入口"""
    try:
        parsed = parse(args)
        
        # 解析子命令
        if parsed and parsed.first:
            subcommand = parsed.first.lower()
            
            if subcommand == "help" or subcommand == "帮助":
                return segments(_show_help())
        
        logger.info(f"处理 CHIME 命令: {command} {args}")
        
        # 获取最新数据
        data = await fetch_chime_repeaters(context)
        if not data:
            return segments("❌ 无法获取 CHIME FRB 数据，请稍后重试")
        
        # 加载历史数据
        old_mapping = load_history(context)
        
        # 解析新数据
        frb_list = parse_frb_data(data, context)
        if not frb_list:
            return segments("❌ 未能解析到有效的 FRB 数据")
        
        # 构建新的映射并保存
        new_mapping = build_history_mapping(frb_list)
        save_history(context, new_mapping)
        
        # 查找更新
        new_repeaters, new_pulses = find_updates(data, old_mapping, context)
        
        # 构建响应消息
        if new_repeaters or new_pulses:
            message = format_update_message(new_repeaters, new_pulses, is_scheduled=False)
            return segments(message)
        else:
            # 没有更新，显示最新的一个
            latest_frb = max(frb_list, key=lambda x: x.timestamp if x.timestamp else '')
            lines = [
                "📊 没有新的重复暴观测，目前最新的是:",
                latest_frb.format_info(),
                "",
                f"当前共追踪 {len(frb_list)} 个重复暴"
            ]
            return segments("\n".join(lines))
        
    except Exception as e:
        logger.exception("CHIME handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")


def _show_help() -> str:
    """显示帮助信息"""
    return """
📡 **CHIME FRB 重复暴监测**

**基本用法:**
• /chime - 检查 CHIME FRB 更新
• /chime help - 显示帮助信息

**功能特点:**
✨ 实时获取最新重复暴数据
🆕 自动检测新发现的重复暴
📡 自动检测新的脉冲事件
⏰ 支持定时检查和通知

**定时任务:**
每天 9:00 和 21:00 自动检查并通知更新

输入 /chime 查看最新数据
""".strip()


async def scheduled_check(context) -> list:
    """定时检查任务
    
    Args:
        context: 插件上下文
        
    Returns:
        消息段列表（如有更新）或空列表
    """
    context.logger.info("CHIME 定时检查开始")
    
    # 获取最新数据
    data = await fetch_chime_repeaters(context)
    if not data:
        context.logger.warning("CHIME 定时检查: 无法获取数据")
        return []
    
    # 加载历史数据
    old_mapping = load_history(context)
    
    # 解析新数据
    frb_list = parse_frb_data(data, context)
    if not frb_list:
        context.logger.warning("CHIME 定时检查: 未能解析到有效数据")
        return []
    
    # 构建新的映射并保存
    new_mapping = build_history_mapping(frb_list)
    save_history(context, new_mapping)
    
    # 查找更新
    new_repeaters, new_pulses = find_updates(data, old_mapping, context)
    
    # 如果没有更新，不发送消息
    if not new_repeaters and not new_pulses:
        context.logger.info("CHIME 定时检查: 没有新数据")
        return []
    
    # 构建通知消息
    context.logger.info(f"CHIME 定时检查: 发现更新 (新重复暴: {len(new_repeaters)}, 新脉冲: {len(new_pulses)})")
    message = format_update_message(new_repeaters, new_pulses, is_scheduled=True)
    return segments(message)
