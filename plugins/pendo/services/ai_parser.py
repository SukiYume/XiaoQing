"""
AI自然语言解析服务
使用LLM进行自然语言理解，失败时降级到规则解析
"""
import re
import json
import logging
import time
from typing import Any, Optional
from datetime import datetime, timedelta
from dateutil import parser
from collections import defaultdict
from .rule_parser import RuleParser

logger = logging.getLogger(__name__)

class RateLimiter:
    """简单的速率限制器"""
    
    def __init__(self, max_calls: int = 10, time_window: int = 60):
        """初始化速率限制器
        
        Args:
            max_calls: 时间窗口内最大调用次数
            time_window: 时间窗口（秒）
        """
        self.max_calls = max_calls
        self.time_window = time_window
        self.call_history = defaultdict(list)
    
    def check_rate_limit(self, user_id: str) -> tuple[bool, int]:
        """检查是否超过速率限制
        
        Args:
            user_id: 用户ID
            
        Returns:
            (是否允许调用, 剩余秒数)
        """
        now = time.time()
        history = self.call_history[user_id]
        
        # 移除超出时间窗口的记录
        history[:] = [t for t in history if now - t < self.time_window]
        
        if len(history) >= self.max_calls:
            oldest = history[0]
            remaining = int(oldest + self.time_window - now)
            return False, max(remaining, 0)
        
        history.append(now)
        return True, 0
    
    def reset(self, user_id: str = None):
        """重置速率限制
        
        Args:
            user_id: 用户ID，如果为None则重置所有用户
        """
        if user_id:
            self.call_history.pop(user_id, None)
        else:
            self.call_history.clear()

class AIParser:
    """AI解析器 - 专注于Event类型解析"""
    
    # 全局速率限制器实例
    _rate_limiter = RateLimiter(max_calls=20, time_window=60)
    
    # Event专用prompt模板
    PARSE_PROMPT_TEMPLATE = """解析日程信息，提取时间、地点、提醒等字段。

当前时间: {current_date} ({current_weekday})
用户输入: {text}

返回JSON:
{{
  "title": "简洁标题",
  "start_time": "YYYY-MM-DDTHH:MM:SS或null（有milestones时留null）",
  "end_time": "YYYY-MM-DDTHH:MM:SS或null（有milestones时留null）",
  "location": "地点或null",
  "category": "工作|学习|生活|健康|财务|社交",
  "remind_offsets": ["提前1天", "提前1小时"],
  "rrule": "RFC5545格式或null",
  "milestones": [
    {{"name": "节点名称", "time": "YYYY-MM-DDTHH:MM:SS"}}
  ],
  "notes": "备注内容或null"
}}

规则:
- 相对时间转绝对时间(明天→具体日期)
- 无时间则默认09:00
- 若用户描述多个具名时间点(如截止、开始、结束)，填milestones列表，start_time/end_time留null
- 普通单次事件milestones留空列表[]
- 重复事件: 设置rrule，milestones必须留空列表[]，start_time设为第一次发生的时间
- 重复: 每天→FREQ=DAILY, 每周→FREQ=WEEKLY, 每月X号→FREQ=MONTHLY;BYMONTHDAY=X
- 重复N次→添加;COUNT=N
- 提醒支持: 分钟/小时/天/周
- notes提取用户标注为"备注"的内容(URL、说明等)

仅返回JSON。"""
    
    def __init__(self, context=None):
        self.context = context
        self.rule_parser = RuleParser()
    
    def _load_prompt_template(self, template_name: str) -> str:
        """获取prompt模板（内联版本）"""
        if template_name == 'parse_natural_language':
            return self.PARSE_PROMPT_TEMPLATE
        raise ValueError(f"Unknown template: {template_name}")
    
    def _get_llm_secrets(self):
        """获取LLM配置"""
        if self.context and hasattr(self.context, 'secrets'):
            return self.context.secrets.get("plugins", {}).get("pendo", {})
        return {}
    
    async def _call_llm(self, messages: list[dict[str, str]], temperature: float = 0.3) -> Optional[str]:
        """调用LLM API"""
        try:
            from .llm_client import chat_completions_with_fallback_paths
            
            secrets = self._get_llm_secrets()
            api_base = secrets.get("api_base", "")
            api_key = secrets.get("api_key", "")
            model = secrets.get("model", "")
            proxy = secrets.get("proxy", "")
            
            if not api_key:
                return None
            
            raw, _ = await chat_completions_with_fallback_paths(
                session=getattr(self.context, 'http_session', None),
                api_base=api_base,
                api_key=api_key,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=1000,
                timeout_seconds=30,
                max_retry=2,
                retry_interval_seconds=1,
                proxy=proxy,
            )
            return raw
        except Exception as e:
            logger.warning("LLM调用失败: %s", e)
            return None
    
    def parse_natural_language(self, text: str, user_id: str) -> dict[str, Any]:
        """同步解析自然语言（规则解析，用于event）"""
        parsed = self.rule_parser.parse(text, user_id)
        parsed['parse_source'] = 'rule'
        parsed['type'] = 'event'  # 固定为event类型
        return parsed
    
    async def parse_event_with_ai(self, text: str, user_id: str) -> dict[str, Any]:
        """使用AI解析日程（专用于event类型）
        
        Args:
            text: 用户输入文本
            user_id: 用户ID
            
        Returns:
            解析后的event数据字典
        """
        # 检查速率限制
        allowed, wait_seconds = self._rate_limiter.check_rate_limit(user_id)
        if not allowed:
            logger.warning("用户 %s 超过AI解析速率限制，等待 %s 秒", user_id, wait_seconds)
            return self._fallback_parse(text, user_id)
        
        try:
            prompt_template = self._load_prompt_template('parse_natural_language')
            current_date = datetime.now().strftime('%Y-%m-%d %H:%M')
            weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
            current_weekday = weekday_names[datetime.now().weekday()]
            
            prompt = prompt_template.format(
                current_date=current_date,
                current_weekday=current_weekday,
                text=text
            )
            
            messages = [
                {"role": "system", "content": "你是日程解析助手，只返回JSON。"},
                {"role": "user", "content": prompt}
            ]
            
            response = await self._call_llm(messages)
            if not response:
                return self._fallback_parse(text, user_id)
            
            try:
                parsed = json.loads(self._extract_json(response))
                if not isinstance(parsed, dict):
                    return self._fallback_parse(text, user_id)
            except (json.JSONDecodeError, ValueError):
                return self._fallback_parse(text, user_id)

            logger.info("AI解析结果: %s", json.dumps(parsed, ensure_ascii=False))

            # 构建event结果（类型固定为event）
            result = {
                'type': 'event',
                'title': parsed.get('title', text[:50]) or text[:50],
                'content': text,
                'category': parsed.get('category', '未分类'),
                'owner_id': user_id,
                'parse_source': 'ai'
            }

            # 时间字段
            for field in ['start_time', 'end_time']:
                if parsed.get(field):
                    try:
                        dt = parser.parse(str(parsed[field]))
                        result[field] = dt.isoformat()
                    except (ValueError, TypeError):
                        # 时间格式无效，跳过该字段
                        pass
            
            # 地点和重复规则
            if parsed.get('location'):
                result['location'] = parsed['location']
            if parsed.get('rrule'):
                result['rrule'] = parsed['rrule']

            # milestones（多时间节点事件）
            raw_milestones = parsed.get('milestones')
            if raw_milestones and isinstance(raw_milestones, list) and len(raw_milestones) >= 2:
                valid_milestones = []
                for m in raw_milestones:
                    if isinstance(m, dict) and m.get('name') and m.get('time'):
                        try:
                            dt = parser.parse(str(m['time']))
                            valid_milestones.append({'name': m['name'], 'time': dt.isoformat()})
                        except (ValueError, TypeError):
                            pass
                if len(valid_milestones) >= 2:
                    result['milestones'] = valid_milestones
                    result['start_time'] = valid_milestones[0]['time']
                    result['end_time'] = valid_milestones[-1]['time']
                    if parsed.get('remind_offsets'):
                        result['remind_times'] = self.build_remind_times_for_milestones(
                            valid_milestones, parsed['remind_offsets']
                        )

            # notes
            if parsed.get('notes'):
                result['notes'] = str(parsed['notes'])

            # 提醒时间（仅单次事件；多节点事件在 milestones 块中处理）
            if not result.get('remind_times') and parsed.get('remind_offsets') and result.get('start_time'):
                result['remind_times'] = self.build_remind_times_from_offsets(
                    result['start_time'], parsed['remind_offsets']
                )

            return result

        except Exception as e:
            logger.exception("AI解析失败: %s", e)
            return self._fallback_parse(text, user_id)
    
    # 保持向后兼容的别名
    async def parse_natural_language_with_ai(self, text: str, user_id: str) -> dict[str, Any]:
        """向后兼容：调用parse_event_with_ai"""
        return await self.parse_event_with_ai(text, user_id)
    
    def _fallback_parse(self, text: str, user_id: str) -> dict[str, Any]:
        """降级到规则解析（固定返回event类型）"""
        parsed = self.rule_parser.parse(text, user_id)
        parsed['parse_source'] = 'rule'
        parsed['type'] = 'event'  # 固定为event类型
        return parsed
    
    def _extract_json(self, response: str) -> str:
        """从响应中提取JSON"""
        if not response:
            return ""
        
        text = response.strip()
        
        # 尝试提取代码块内的内容（非贪婪匹配）
        code_block = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
        if code_block:
            text = code_block.group(1).strip()
        
        # 尝试提取JSON对象
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            return match.group(0).strip()
        
        return text
    
    def build_remind_times_from_offsets(self, start_time: str, offsets: list[str]) -> list[str]:
        """根据偏移量构建提醒时间"""
        if not start_time or not offsets:
            return []

        start_dt = datetime.fromisoformat(start_time)
        now = datetime.now()
        remind_times = []

        for offset in offsets:
            delta = self._parse_offset(offset)
            if delta:
                remind_time = start_dt - delta
                if remind_time > now:
                    remind_times.append(remind_time.isoformat())

        return remind_times

    def build_remind_times_for_milestones(self, milestones: list[dict], offsets: list[str]) -> list[str]:
        """为多个里程碑的每个时间节点应用提醒偏移，返回去重后的平铺列表"""
        all_times = set()
        for milestone in milestones:
            t = milestone.get('time')
            if not t:
                continue
            times = self.build_remind_times_from_offsets(t, offsets)
            all_times.update(times)
        return sorted(all_times)

    def build_remind_times_from_description(self, description: str, base_time: str) -> list[str]:
        """从自然语言描述中提取所有偏移量并生成提醒时间

        例如: "提前1天和2小时提醒" → offsets=["1天", "2小时"]
        """
        pattern = r'(?:提前\s*)?(\d+|[一二三四五六七八九十半两]+)\s*(?:个)?\s*(分钟|min|小时|hour|天|day|周|week)'
        offsets = [''.join(m) for m in re.findall(pattern, description)]
        if not offsets:
            return []
        return self.build_remind_times_from_offsets(base_time, offsets)

    def _parse_offset(self, offset: str) -> Optional[timedelta]:
        """解析偏移量字符串"""
        match = re.search(r'(\d+|[一二三四五六七八九十半两]+)\s*(分钟|min|m|小时|hour|h|天|day|d|周|week|w)', str(offset))
        if not match:
            return None
        
        num = self._parse_chinese_number(match.group(1))
        if num is None:
            try:
                num = int(match.group(1))
            except (ValueError, TypeError):
                # 无法解析为整数
                return None
        
        unit = match.group(2)
        if unit in ['分钟', 'min', 'm']:
            return timedelta(minutes=float(num))
        elif unit in ['小时', 'hour', 'h']:
            return timedelta(hours=float(num))
        elif unit in ['天', 'day', 'd']:
            return timedelta(days=float(num))
        elif unit in ['周', 'week', 'w']:
            return timedelta(weeks=float(num))
        return None
    
    def _parse_chinese_number(self, text: str) -> Optional[float]:
        """解析中文数字

        支持格式：
        - 基本数字：一、二、三...九、十
        - 组合数字：十一、二十、一百、一百二十三等
        - 特殊数字：半(0.5)、两(2)
        """
        if not text:
            return None
        if text == '半':
            return 0.5
        if text == '两':
            return 2.0

        digits = {'零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}

        if text.isdigit():
            return float(text)

        # 处理包含"十"的情况
        if '十' in text:
            parts = text.split('十')
            # "十" -> 10
            # "二十" -> 20
            # "二十一" -> 21
            left = digits.get(parts[0], 1) if parts[0] else 1
            right = digits.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
            return float(left * 10 + right)

        # 处理包含"百"的情况
        if '百' in text:
            parts = text.split('百')
            # "一百" -> 100
            # "一百二十" -> 120
            # "一百二十三" -> 123
            hundred_part = digits.get(parts[0], 1) if parts[0] else 1
            remaining = parts[1] if len(parts) > 1 else ''
            # 处理剩余部分（可能是"二十"、"三"等）
            if '十' in remaining:
                ten_parts = remaining.split('十')
                ten_left = digits.get(ten_parts[0], 1) if ten_parts[0] else 1
                ten_right = digits.get(ten_parts[1], 0) if len(ten_parts) > 1 and ten_parts[1] else 0
                return float(hundred_part * 100 + ten_left * 10 + ten_right)
            elif remaining in digits:
                return float(hundred_part * 100 + digits[remaining])
            return float(hundred_part * 100)

        if text in digits:
            return float(digits[text])

        return None

    async def generate_daily_briefing(self, user_id: str, items: list) -> str:
        """生成每日简报
        
        Args:
            user_id: 用户ID
            items: 今日日程和待办列表 (Item dataclass实例)
            
        Returns:
            简报文本
        """
        from ..models.item import ItemType
        
        current_date = datetime.now().strftime('%Y年%m月%d日')
        weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
        weekday = weekday_names[datetime.now().weekday()]
        
        lines = [f"☀️ 早上好！今天是{current_date} {weekday}", ""]
        
        # 分离日程和待办
        events = [i for i in items if i.type == ItemType.EVENT]
        tasks = [i for i in items if i.type == ItemType.TASK]
        
        # 今日日程
        if events:
            lines.append("🗓️ **今日日程**")
            for evt in events[:5]:
                start_time = evt.start_time or ''
                time_str = start_time[11:16] if len(start_time) > 11 else ''
                title = evt.title or '无标题'
                location = f" @{evt.location}" if evt.location else ''
                lines.append(f"  • {time_str} {title}{location}")
            if len(events) > 5:
                lines.append(f"  ...还有 {len(events) - 5} 项")
            lines.append("")
        else:
            lines.append("🗓️ 今日暂无日程安排")
            lines.append("")
        
        # 今日待办
        if tasks:
            lines.append("✅ **今日待办**")
            for task in tasks[:5]:
                title = task.title or '无标题'
                priority = task.priority if hasattr(task, 'priority') and task.priority is not None else 3
                if hasattr(priority, 'value'):
                    priority = priority.value
                # 优先级: 1=紧急 2=高 3=中 4=低
                priority_mark = '🔴' if priority <= 2 else '🟡' if priority == 3 else '⚪'
                lines.append(f"  {priority_mark} {title}")
            if len(tasks) > 5:
                lines.append(f"  ...还有 {len(tasks) - 5} 项")
            lines.append("")
        else:
            lines.append("✅ 今日暂无待办事项")
            lines.append("")

        lines.append("🌟 祝你今天工作顺利！")

        return "\n".join(lines)
