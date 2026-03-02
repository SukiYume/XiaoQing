"""
随机选择插件
提供从多个选项中随机选择的功能
"""
import random
import re
import logging
from typing import Any, Optional

from core.plugin_base import segments
from core.args import parse

logger = logging.getLogger(__name__)

# ============================================================
# 常量配置
# ============================================================

MIN_OPTIONS = 2  # 至少需要的选项数
MAX_OPTIONS = 50  # 最多支持的选项数
MAX_CHOICES = 10  # 单次最多选择的数量
DEFAULT_CHOICES = 1  # 默认选择数量

CHOICE_EMOJIS = ["🎲", "🎯", "✨", "🌟", "💫", "🎰", "🔮", "🎪"]

# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    pass

# ============================================================
# 参数解析
# ============================================================

def parse_choice_args(args: str) -> tuple[Optional[str], list[str], int, bool]:
    """解析选择命令的参数
    
    Args:
        args: 命令参数字符串
        
    Returns:
        (问题, 选项列表, 选择数量, 是否去重)
    """
    if not args or not args.strip():
        return None, [], DEFAULT_CHOICES, False
    
    # 分割参数
    tokens = args.split()
    
    if len(tokens) < 2:
        return None, [], DEFAULT_CHOICES, False
    
    # 检查是否有 -n 参数指定选择数量
    choice_count = DEFAULT_CHOICES
    unique = False
    
    # 从后往前检查特殊参数
    filtered_tokens = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "-n" and i + 1 < len(tokens):
            # 尝试解析数量
            try:
                choice_count = int(tokens[i + 1])
                i += 2  # 跳过 -n 和数字
                continue
            except ValueError:
                pass
        elif token == "-u" or token == "--unique":
            # 去重选项
            unique = True
            i += 1
            continue
        
        filtered_tokens.append(token)
        i += 1
    
    if len(filtered_tokens) < 2:
        return None, [], DEFAULT_CHOICES, False
    
    # 第一个是问题，其余是选项
    question = filtered_tokens[0]
    options = filtered_tokens[1:]
    
    return question, options, choice_count, unique

def validate_options(options: list[str], choice_count: int, context) -> tuple[bool, Optional[str]]:
    """验证选项的有效性
    
    Args:
        options: 选项列表
        choice_count: 要选择的数量
        context: 插件上下文
        
    Returns:
        (是否有效, 错误信息)
    """
    if len(options) < MIN_OPTIONS:
        return False, f"至少需要 {MIN_OPTIONS} 个选项"
    
    if len(options) > MAX_OPTIONS:
        return False, f"选项过多，最多支持 {MAX_OPTIONS} 个选项"
    
    if choice_count < 1:
        return False, "选择数量必须至少为 1"
    
    if choice_count > MAX_CHOICES:
        return False, f"选择数量过多，最多支持一次选择 {MAX_CHOICES} 个"
    
    if choice_count > len(options):
        context.logger.warning(f"选择数量 ({choice_count}) 超过选项数量 ({len(options)})，将调整为选项数量")
    
    return True, None

# ============================================================
# 选择逻辑
# ============================================================

def make_choice(
    options: list[str], 
    count: int = 1, 
    unique: bool = False
) -> list[str]:
    """从选项中随机选择
    
    Args:
        options: 选项列表
        count: 选择数量
        unique: 是否去重（不重复选择同一项）
        
    Returns:
        选中的选项列表
    """
    # 限制选择数量不超过选项数量
    actual_count = min(count, len(options))
    
    if unique or actual_count >= len(options):
        # 去重模式或选择全部：使用 sample（无放回）
        if actual_count >= len(options):
            # 全选，直接打乱顺序
            result = options.copy()
            random.shuffle(result)
            return result[:actual_count]
        else:
            return random.sample(options, actual_count)
    else:
        # 允许重复：使用 choices（有放回）
        return random.choices(options, k=actual_count)

def format_choice_result(
    question: str,
    options: list[str],
    choices: list[str],
    total_options: int
) -> str:
    """格式化选择结果
    
    Args:
        question: 问题
        options: 原始选项列表
        choices: 选中的选项
        total_options: 总选项数
        
    Returns:
        格式化的结果文本
    """
    emoji = random.choice(CHOICE_EMOJIS)
    
    if len(choices) == 1:
        # 单个选择
        result = f"{emoji} {question}：**{choices[0]}**"
    else:
        # 多个选择
        result_lines = [f"{emoji} {question}："]
        for i, choice in enumerate(choices, 1):
            result_lines.append(f"  {i}. **{choice}**")
        result = "\n".join(result_lines)
    
    # 添加统计信息
    if len(choices) > 1:
        result += f"\n\n已从 {total_options} 个选项中选择 {len(choices)} 个"
    
    return result

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
        
        # 解析子命令或检查help
        if parsed and parsed.first:
            subcommand = parsed.first.lower()
            
            if subcommand == "help" or subcommand == "帮助":
                return segments(_show_help())
        
        # 解析参数
        question, options, choice_count, unique = parse_choice_args(args)
        
        if question is None or not options:
            return segments(_show_help())
        
        # 验证选项
        is_valid, error_msg = validate_options(options, choice_count, context)
        if not is_valid:
            return segments(f"❌ {error_msg}")
        
        # 记录日志
        unique_options = list(set(options))
        logger.info(
            f"随机选择: 问题='{question[:20]}...', "
            f"选项数={len(options)} (去重后{len(unique_options)}), "
            f"选择数={choice_count}, 去重={unique}"
        )
        
        # 执行选择
        choices = make_choice(options, choice_count, unique)
        
        # 格式化结果
        result = format_choice_result(question, options, choices, len(options))
        
        logger.debug(f"选择结果: {choices}")
        
        return segments(result)
        
    except Exception as e:
        logger.exception("Choice handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")

def _show_help() -> str:
    """显示帮助信息"""
    return """
🎲 **随机选择助手**

从多个选项中随机选择一个或多个

**基础用法:**
• /choice 问题 选项1 选项2 选项3
• /选择 吃啥 火锅 烤肉 披萨

**高级用法:**
• /choice 问题 选项1 选项2 -n 2    # 选择2个
• /choice 问题 选项1 选项2 -u      # 去重选择
• /choice 问题 选项1 选项1 选项2   # 加权选择

**示例:**
• /选择 今天吃什么 火锅 烤肉 日料 川菜
• /choice 抽奖 小明 小红 小张 -n 3
• /choice help - 显示帮助信息

**功能特点:**
- 支持多个选项随机选择
- 支持加权选择
- 支持去重或保留重复
- 友好的结果展示

输入 /choice 问题 选项1 选项2 开始选择
""".strip()
