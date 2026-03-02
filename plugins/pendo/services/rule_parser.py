"""
规则解析服务（精简版）
基于正则表达式和关键词匹配的自然语言解析
作为AI解析的备用方案
"""
import re
from typing import Any, Optional
from datetime import datetime, timedelta

class RuleParser:
    """规则解析器"""
    
    def __init__(self):
        # 时间关键词
        self.time_keywords = {
            '今天': 0, '明天': 1, '后天': 2,
            '今晚': 0, '明晚': 1,
            '这周': 0, '下周': 7, '下下周': 14,
        }
        
        # 优先级关键词
        self.priority_keywords = {
            '紧急': 1, '重要': 2, '普通': 3, '低': 4,
            '高优先级': 2, '低优先级': 4,
        }
        
        # 重复规则关键词
        self.repeat_keywords = {
            '每天': 'DAILY', '每周': 'WEEKLY',
            '每月': 'MONTHLY', '每个月': 'MONTHLY',
        }
    
    def parse(self, text: str, user_id: str) -> dict[str, Any]:
        """解析自然语言，返回结构化数据"""
        from ..models.item import ItemType
        
        result = {
            'type': self._detect_type(text),
            'title': text[:50],
            'content': text,
            'tags': self._extract_tags(text),
            'category': self._extract_category(text) or '未分类',
            'owner_id': user_id,
            'needs_confirmation': [],
        }
        
        # 提取时间信息
        time_info = self._extract_time(text)
        if time_info:
            if result['type'] == ItemType.EVENT:
                result['start_time'] = time_info.get('start_time')
                result['end_time'] = time_info.get('end_time')
            elif result['type'] == ItemType.TASK:
                result['due_time'] = time_info.get('due_time') or time_info.get('start_time')
        
        # 提取地点
        location = self._extract_location(text)
        if location and result['type'] == ItemType.EVENT:
            result['location'] = location
        
        # 提取优先级
        priority = self._extract_priority(text)
        if priority and result['type'] == ItemType.TASK:
            result['priority'] = priority
        
        # 提取提醒设置
        remind_times = self._extract_reminders(text, time_info)
        if remind_times:
            result['remind_times'] = remind_times
        
        # 提取重复规则
        if result['type'] == ItemType.EVENT:
            rrule = self._extract_rrule(text)
            if rrule:
                result['rrule'] = rrule
        
        # 检查缺失字段
        if result['type'] == ItemType.EVENT and not result.get('start_time'):
            result['needs_confirmation'].append('start_time')
        if result['type'] == ItemType.TASK and not result.get('due_time'):
            result['needs_confirmation'].append('due_time')
        
        return result
    
    def _detect_type(self, text: str) -> 'ItemType':
        """检测条目类型"""
        from ..models.item import ItemType
        
        event_kw = ['会议', '开会', '约', '见面', '活动', '聚会', '上课']
        task_kw = ['待办', '任务', '完成', '提交', '截止', 'deadline', 'todo']
        note_kw = ['想法', '灵感', '点子', '记录', '笔记', 'idea']
        
        text_lower = text.lower()
        
        for kw in note_kw:
            if kw in text_lower:
                return ItemType.NOTE
        
        for kw in self.repeat_keywords:
            if kw in text:
                return ItemType.EVENT
        
        for kw in event_kw:
            if kw in text:
                return ItemType.EVENT
        
        for kw in task_kw:
            if kw in text:
                return ItemType.TASK
        
        # 有时间表达式倾向于判断为日程或任务
        if self._has_time_expression(text):
            if any(k in text for k in ['到', '截止', '之前', '前']):
                return ItemType.TASK
            return ItemType.EVENT
        
        return ItemType.NOTE
    
    def _extract_time(self, text: str) -> Optional[dict[str, str]]:
        """提取时间信息"""
        now = datetime.now()
        result = {}
        
        # 相对时间
        for keyword, days_offset in self.time_keywords.items():
            if keyword in text:
                target_date = now + timedelta(days=days_offset)
                
                # 提取具体时间点
                time_match = re.search(r'(\d{1,2})[点:：](\d{1,2})?', text)
                if time_match:
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2)) if time_match.group(2) else 0
                    target_time = target_date.replace(hour=hour, minute=minute, second=0)
                    result['start_time'] = target_time.isoformat()
                    
                    # 检查结束时间
                    end_match = re.search(r'到\s*(\d{1,2})[点:：](\d{1,2})?', text)
                    if end_match:
                        end_hour = int(end_match.group(1))
                        end_minute = int(end_match.group(2)) if end_match.group(2) else 0
                        result['end_time'] = target_date.replace(
                            hour=end_hour, minute=end_minute, second=0
                        ).isoformat()
                else:
                    result['start_time'] = target_date.replace(hour=9, minute=0, second=0).isoformat()
                break
        
        # 绝对日期 YYYY-MM-DD
        date_match = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', text)
        if date_match:
            year, month, day = map(int, date_match.groups())
            target_date = datetime(year, month, day)
            result['start_time'] = target_date.isoformat()
        
        # 截止时间
        if '截止' in text or 'deadline' in text.lower():
            if 'start_time' in result:
                result['due_time'] = result.pop('start_time')
                result.pop('end_time', None)
        
        return result if result else None
    
    def _has_time_expression(self, text: str) -> bool:
        """检查是否包含时间表达"""
        patterns = [r'\d{1,2}[点:：]', r'今天|明天|后天', r'\d{4}-\d{1,2}-\d{1,2}', r'下周|这周']
        return any(re.search(p, text) for p in patterns)
    
    def _extract_location(self, text: str) -> Optional[str]:
        """提取地点"""
        patterns = [
            r'在([^，,。.！!？?]+?)(开会|见面|会议)',
            r'地点[：:]\s*([^，,。.！!？?]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()
        return None
    
    def _extract_rrule(self, text: str) -> Optional[str]:
        """提取重复规则"""
        for keyword, freq in self.repeat_keywords.items():
            if keyword in text:
                rrule_parts = [f'FREQ={freq}']
                
                # 提取重复次数
                count_match = re.search(r'重复(\d+)(个月|周|天|次)', text)
                if count_match:
                    rrule_parts.append(f'COUNT={count_match.group(1)}')
                
                # 提取每月日期
                if freq == 'MONTHLY':
                    day_match = re.search(r'(\d{1,2})[号日]', text)
                    if day_match:
                        rrule_parts.append(f'BYMONTHDAY={day_match.group(1)}')
                
                # 提取星期几
                if freq == 'WEEKLY':
                    week_map = {'周一': 'MO', '周二': 'TU', '周三': 'WE',
                                '周四': 'TH', '周五': 'FR', '周六': 'SA', '周日': 'SU'}
                    for cn_day, en_day in week_map.items():
                        if cn_day in text:
                            rrule_parts.append(f'BYDAY={en_day}')
                            break
                
                return ';'.join(rrule_parts)
        return None
    
    def _extract_priority(self, text: str) -> Optional[int]:
        """提取优先级"""
        for keyword, priority in self.priority_keywords.items():
            if keyword in text.lower():
                return priority
        return None
    
    def _extract_reminders(self, text: str, time_info: Optional[dict]) -> list[str]:
        """提取提醒时间"""
        if not time_info or 'start_time' not in time_info:
            return []
        
        reminders = []
        start_time = datetime.fromisoformat(time_info['start_time'])
        
        patterns = [
            r'提前(\d+)(分钟|小时|天)',
            r'(\d+)(分钟|小时|天)前提醒',
        ]
        
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                num = int(match.group(1))
                unit = match.group(2)
                
                if unit == '分钟':
                    remind_time = start_time - timedelta(minutes=num)
                elif unit == '小时':
                    remind_time = start_time - timedelta(hours=num)
                elif unit == '天':
                    remind_time = start_time - timedelta(days=num)
                else:
                    continue
                
                if remind_time > datetime.now():
                    reminders.append(remind_time.isoformat())
        
        return reminders
    
    def _extract_tags(self, text: str) -> list[str]:
        """提取标签 (#tag格式)"""
        return re.findall(r'#(\w+)', text)
    
    def _extract_category(self, text: str) -> Optional[str]:
        """提取分类"""
        categories = {
            '健康': ['体检', '锻炼', '跑步', '健身', '运动', '医院'],
            '工作': ['工作', '会议', '项目', '报告', '开会', '邮件', '周报'],
            '学习': ['学习', '课程', '作业', '论文', '考试', '阅读'],
            '生活': ['购物', '家务', '做饭', '买菜'],
            '财务': ['理财', '投资', '报销', '账单', '还款'],
        }
        
        for category, keywords in categories.items():
            if any(kw in text for kw in keywords):
                return category
        return None
