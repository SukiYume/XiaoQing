"""
猜数字游戏插件

演示如何使用 SessionManager 实现多轮对话。

游戏规则：
1. 用户发送 /猜数字 开始游戏
2. 系统随机生成一个 1-100 的数字
3. 用户猜测数字，系统提示大了/小了
4. 猜对或次数用尽时游戏结束

多轮对话实现要点：
1. 使用 context.create_session() 创建会话
2. 实现 handle_session() 函数处理会话消息
3. 使用 session.get()/set() 存取会话数据
4. 使用 context.end_session() 结束会话
"""

# 标准库
import logging
import random

# 本地导入
from core.args import parse
from core.plugin_base import segments


logger = logging.getLogger(__name__)


# ============================================================
# 游戏配置
# ============================================================

MIN_NUMBER = 1
MAX_NUMBER = 100
MAX_ATTEMPTS = 7
SESSION_TIMEOUT = 180.0  # 3 分钟


# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    logger.info("GuessNumber plugin initialized")


# ============================================================
# 主命令入口
# ============================================================

async def handle(command: str, args: str, event: dict, context) -> list:
    """
    命令处理入口
    
    处理初始命令和子命令。
    后续的猜测消息由 handle_session() 处理。
    """
    try:
        parsed = parse(args)
        
        # 如果有参数，检查是否为子命令
        if parsed and parsed.first:
            subcommand = parsed.first.lower()
            
            # 帮助命令
            if subcommand in {"help", "帮助", "?"}:
                return segments(_show_help())
            
            # 状态查询
            if subcommand in {"status", "状态", "info", "信息"}:
                return await _handle_status(context)
            
            # 重新开始（结束当前游戏并开始新游戏）
            if subcommand in {"restart", "重新开始", "重开"}:
                await context.end_session()
                # 不带参数重新开始
                return await _start_game("", context)
            
            # 如果不是子命令，作为难度参数处理
            return await _start_game(subcommand, context)
        
        # 无参数，直接开始游戏
        return await _start_game("", context)
        
    except Exception as e:
        logger.exception("GuessNumber handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")


async def _start_game(difficulty: str, context) -> list[dict]:
    """开始新游戏"""
    # 检查是否已有进行中的游戏
    existing_session = await context.get_session()
    if existing_session:
        return segments(
            "🎮 你已经有一个进行中的游戏！\n"
            f"当前范围: {existing_session.get('hint', '1-100')}\n"
            f"剩余次数: {existing_session.get('remaining', MAX_ATTEMPTS)}\n"
            "\n发送数字继续猜测\n"
            "发送「退出」/「取消」放弃游戏\n"
            "发送 /猜数字 restart 重新开始"
        )
    
    # 解析难度参数
    min_num, max_num, max_attempts = _parse_difficulty(difficulty)
    
    # 生成目标数字
    target = random.randint(min_num, max_num)
    
    # 创建会话
    await context.create_session(
        initial_data={
            "target": target,
            "min": min_num,
            "max": max_num,
            "attempts": 0,
            "max_attempts": max_attempts,
            "remaining": max_attempts,
            "hint": f"{min_num}-{max_num}",
            "history": [],
            "difficulty": difficulty or "normal",
        },
        timeout=SESSION_TIMEOUT,
    )
    
    logger.info(
        "Game started: target=%d, range=%d-%d, max_attempts=%d, user=%s",
        target, min_num, max_num, max_attempts, context.current_user_id
    )
    
    difficulty_text = _get_difficulty_name(difficulty)
    
    return segments(
        f"🎮 猜数字游戏开始！\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"难度: {difficulty_text}\n"
        f"范围: {min_num} 到 {max_num}\n"
        f"机会: {max_attempts} 次\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 请发送一个数字开始猜测\n"
        f"💡 输入「退出」/「取消」可以放弃游戏"
    )


async def _handle_status(context) -> list[dict]:
    """查询当前游戏状态"""
    session = await context.get_session()
    if not session:
        return segments(
            "📊 当前没有进行中的游戏\n"
            "发送 /猜数字 开始新游戏"
        )
    
    history = session.get("history", [])
    history_str = " → ".join(str(g) for g in history) if history else "（尚未开始）"
    
    return segments(
        f"📊 游戏状态\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"当前范围: {session.get('hint', 'N/A')}\n"
        f"剩余次数: {session.get('remaining', 0)}/{session.get('max_attempts', 0)}\n"
        f"已猜次数: {session.get('attempts', 0)}\n"
        f"猜测历史: {history_str}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"继续发送数字猜测"
    )


# ============================================================
# 会话处理（多轮对话核心）
# ============================================================

async def handle_session(
    text: str,
    event: dict,
    context,
    session,
) -> list[dict]:
    """
    处理会话消息
    
    当用户有活跃会话时，Dispatcher 会调用这个函数处理后续消息。
    框架已自动处理退出命令（退出/取消/exit/quit/q），插件无需再处理。
    
    参数:
        text: 用户发送的原始文本
        event: OneBot 事件
        context: 插件上下文
        session: 当前会话对象
    """
    # 获取会话数据
    target = session.get("target")
    min_num = session.get("min", MIN_NUMBER)
    max_num = session.get("max", MAX_NUMBER)
    attempts = session.get("attempts", 0)
    max_attempts = session.get("max_attempts", MAX_ATTEMPTS)
    history = session.get("history", [])
    
    # 解析用户输入
    guess_text = text.strip()
    
    # 检查特殊命令
    if guess_text.lower() in {"status", "状态", "info", "信息", "?"}:
        history_str = " → ".join(str(g) for g in history) if history else "（尚未开始）"
        return segments(
            f"📊 游戏状态\n"
            f"当前范围: {session.get('hint')}\n"
            f"剩余次数: {session.get('remaining', 0)}/{max_attempts}\n"
            f"猜测历史: {history_str}"
        )
    
    # 尝试解析为数字
    try:
        guess = int(guess_text)
    except ValueError:
        return segments(
            f"❓ 请输入一个数字（{min_num}-{max_num}）\n"
            f"💡 输入「退出」/「取消」可以放弃游戏"
        )
    
    # 验证范围
    if guess < min_num or guess > max_num:
        return segments(
            f"⚠️ 请输入 {min_num} 到 {max_num} 之间的数字！"
        )
    
    # 更新尝试次数
    attempts += 1
    remaining = max_attempts - attempts
    history.append(guess)
    
    session.set("attempts", attempts)
    session.set("history", history)
    session.set("remaining", remaining)
    
    # 判断结果
    if guess == target:
        # 猜对了！
        await context.end_session()
        
        # 生成历史记录展示
        history_str = " → ".join(str(g) for g in history)
        
        logger.info(
            "Game won: user=%s, attempts=%d, target=%d",
            context.current_user_id, attempts, target
        )
        
        return segments(
            f"🎉 恭喜你猜对了！\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"答案是: {target}\n"
            f"尝试次数: {attempts} 次\n"
            f"猜测历史: {history_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{_get_rating(attempts, max_attempts)}"
        )
    
    # 检查是否用尽次数
    if remaining <= 0:
        await context.end_session()
        history_str = " → ".join(str(g) for g in history)
        
        logger.info(
            "Game lost: user=%s, attempts=%d, target=%d",
            context.current_user_id, attempts, target
        )
        
        return segments(
            f"😢 游戏结束，次数用尽！\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"正确答案是: {target}\n"
            f"你的猜测: {history_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"再接再厉！发送 /猜数字 开始新游戏"
        )
    
    # 给出提示
    if guess < target:
        hint_emoji = "📈"
        hint_text = "太小了！"
        # 更新范围下限
        new_min = max(min_num, guess + 1)
        session.set("min", new_min)
        session.set("hint", f"{new_min}-{max_num}")
    else:
        hint_emoji = "📉"
        hint_text = "太大了！"
        # 更新范围上限
        new_max = min(max_num, guess - 1)
        session.set("max", new_max)
        session.set("hint", f"{min_num}-{new_max}")
    
    return segments(
        f"{hint_emoji} {hint_text}\n"
        f"剩余次数: {remaining}\n"
        f"当前范围: {session.get('hint')}"
    )


# ============================================================
# 辅助函数
# ============================================================

def _show_help() -> str:
    """显示帮助信息"""
    return """
🎮 **猜数字游戏**

**游戏规则:**
• 系统随机生成一个数字
• 你输入猜测的数字，系统提示大了/小了
• 在有限次数内猜对即可获胜

**基本命令:**
• /猜数字 - 开始游戏（默认难度）
• /猜数字 help - 显示帮助
• /猜数字 status - 查看当前游戏状态
• /猜数字 restart - 重新开始游戏

**难度选择:**
• /猜数字 简单 - 1-50，10次机会
• /猜数字 普通 - 1-100，7次机会（默认）
• /猜数字 困难 - 1-200，8次机会
• /猜数字 地狱 - 1-1000，10次机会

**游戏中命令:**
• 输入数字 - 进行猜测
• 输入「退出」/「取消」- 放弃游戏
• 输入「状态」/「info」- 查看状态

**提示:**
• 游戏会动态缩小猜测范围
• 剩余次数显示在每次提示中
• 3分钟无操作自动结束会话
""".strip()


def _get_difficulty_name(difficulty: str) -> str:
    """获取难度名称"""
    difficulty_map = {
        "简单": "简单 ⭐",
        "easy": "简单 ⭐",
        "e": "简单 ⭐",
        "困难": "困难 ⭐⭐⭐",
        "hard": "困难 ⭐⭐⭐",
        "h": "困难 ⭐⭐⭐",
        "地狱": "地狱 ⭐⭐⭐⭐⭐",
        "hell": "地狱 ⭐⭐⭐⭐⭐",
        "nightmare": "地狱 ⭐⭐⭐⭐⭐",
    }
    return difficulty_map.get(difficulty.lower(), "普通 ⭐⭐")


def _parse_difficulty(difficulty: str) -> tuple:
    """
    解析难度参数
    
    Returns:
        (min_num, max_num, max_attempts) 元组
    """
    if difficulty in {"简单", "easy", "e"}:
        return 1, 50, 10
    elif difficulty in {"困难", "hard", "h"}:
        return 1, 200, 8
    elif difficulty in {"地狱", "hell", "nightmare"}:
        return 1, 1000, 10
    else:
        # 默认难度
        return MIN_NUMBER, MAX_NUMBER, MAX_ATTEMPTS


def _get_rating(attempts: int, max_attempts: int) -> str:
    """根据尝试次数给出评价"""
    ratio = attempts / max_attempts
    
    if attempts == 1:
        return "🏆 难以置信！一发入魂！"
    elif ratio <= 0.3:
        return "⭐⭐⭐⭐⭐ 太厉害了！"
    elif ratio <= 0.5:
        return "⭐⭐⭐⭐ 表现优秀！"
    elif ratio <= 0.7:
        return "⭐⭐⭐ 不错哦~"
    elif ratio <= 0.9:
        return "⭐⭐ 还可以更好！"
    else:
        return "⭐ 险胜！"

