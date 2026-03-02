"""
待办(Task)处理器
按照分类(cat)管理待办事项，不需要AI解析
"""
from typing import Any, Optional
from datetime import datetime, timedelta
import re
import logging
from ..models.item import ItemType, TaskStatus, Priority
from ..models.constants import ItemFields
from ..core.exceptions import OwnershipException, MissingRequiredFieldException
from ..utils.time_utils import now_in_timezone, TimezoneHelper
from core.plugin_base import run_sync
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import handle_command_errors
from ..config import PendoConfig
from ..utils.formatters import ItemFormatter, format_success_message

logger = logging.getLogger(__name__)

def _enum_val(x):
    """Return the .value of an Enum, or x itself if it's already a plain value."""
    return x.value if hasattr(x, 'value') else x


def _sort_category_keys(keys) -> list:
    """Sort category keys: date-format categories (newest first), then others (alphabetical)."""
    date_cats = sorted([k for k in keys if re.match(r'\d{4}-\d{2}-\d{2}', k)], reverse=True)
    other_cats = sorted([k for k in keys if not re.match(r'\d{4}-\d{2}-\d{2}', k)])
    return date_cats + other_cats


class TaskHandler(DbOpsMixin):
    """待办处理器
    
    按分类(cat)管理待办事项：
    - 当天待办：cat为日期格式(如 2026-02-02)
    - 非当天待办：cat为自定义分类名(如 工作、学习)
    
    不需要AI解析，直接规则解析
    """
    
    def __init__(self, db, ai_parser=None, reminder_service=None):
        self.db = db
        # ai_parser和reminder_service保留接口兼容性，但不使用

    @handle_command_errors
    async def handle(self, user_id: str, args: str, context: dict, group_id: int | None = None) -> dict[str, Any]:
        """处理待办相关命令
        
        命令格式：
        - /pendo todo add <内容> [cat:xxx] [p:1-4]
        - /pendo todo list [cat] [done/undone] [all|page:n]
        - /pendo todo done <id>
        - /pendo todo undone <id>
        - /pendo todo delete <id|cat:xxx>
        - /pendo todo edit <id> <内容>
        """
        parts = args.split(maxsplit=1)
        if not parts:
            return await self.list_all_categories(user_id, context)
        
        command = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        
        handlers = {
            'add': lambda: self.add_task(user_id, rest, context, group_id),
            'list': lambda: self.list_tasks(user_id, rest, context),
            'done': lambda: self.mark_done(user_id, rest, context),
            'undone': lambda: self.mark_undone(user_id, rest, context),
            'delete': lambda: self.delete_task(user_id, rest, context),
            'edit': lambda: self.edit_task(user_id, rest, context),
        }
        
        handler = handlers.get(command)
        if handler:
            return await handler()
        else:
            # 未知命令，当作list处理
            return await self.list_tasks(user_id, args, context)

    async def add_task(self, user_id: str, text: str, context: dict, group_id: int | None = None) -> dict[str, Any]:
        """添加待办
        
        格式：
        - /pendo todo add 事件  -> 添加到当天cat（晚上8点后自动归为第二天）
        - /pendo todo add 事件 cat:xxx p:1 -> 添加到指定cat，优先级1
        """
        if not text:
            return {'status': 'error', 'message': '❌ 请提供待办内容\n\n用法: /pendo todo add <内容> [cat:xxx] [p:1-4]'}
        
        # 解析参数
        parsed = self._parse_task_text(text, user_id)
        
        # 创建待办数据
        from ..models.item import TaskItem
        
        task_item = TaskItem(
            owner_id=user_id,
            title=parsed['title'],
            content=parsed.get('content', ''),
            category=parsed['category'],  # cat
            priority=parsed['priority'],
            status=TaskStatus.TODO,
            tags=parsed.get('tags', []),
            context={'group_id': group_id} if group_id else {},
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
        
        # 保存到数据库
        item_id = await self._db_create_with_log(
            task_item,
            owner_id=user_id,
            action='create_task'
        )
        
        task_item.id = item_id

        # 格式化返回消息
        priority_str = ItemFormatter.format_priority(parsed['priority'])
        message = f"✅ 已添加待办\n\n"
        message += f"📝 {parsed['title']}\n"
        message += f"📂 分类: {parsed['category']}"
        
        # 如果分类是明天，添加提示
        now = datetime.now()
        if now.hour >= 20 and parsed['category'] == (now + timedelta(days=1)).strftime('%Y-%m-%d'):
            message += " (明天)"
        
        message += f"\n⚡ 优先级: {priority_str}\n"
        message += f"`{item_id}`\n\n"
        message += f"💡 用 /pendo todo done {item_id} 完成"

        return {
            'status': 'success',
            'message': message,
            'item_id': item_id
        }

    def _parse_task_text(self, text: str, user_id: str) -> dict[str, Any]:
        """解析待办文本（纯规则解析，不用AI）
        
        支持格式：
        - 事件内容 cat:xxx p:1
        - cat:xxx 事件内容 p:1
        """
        # 确定默认分类：晚上8点后自动归为第二天
        now = datetime.now()
        default_category = now.strftime('%Y-%m-%d')
        if now.hour >= 20:
            # 晚上8点后，默认分类为明天
            default_category = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        
        result = {
            'title': '',
            'content': '',
            'category': default_category,  # 默认当天或明天（晚上8点后）
            'priority': 3,  # 默认中优先级
            'tags': []
        }
        
        # 提取 cat:xxx
        cat_match = re.search(r'cat:(\S+)', text)
        if cat_match:
            result['category'] = cat_match.group(1)
            text = text.replace(cat_match.group(0), '').strip()
        
        # 提取 p:1-4 优先级
        priority_match = re.search(r'p:([1-4])', text)
        if priority_match:
            result['priority'] = int(priority_match.group(1))
            text = text.replace(priority_match.group(0), '').strip()
        
        # 提取 #tag
        tags = re.findall(r'#(\w+)', text)
        if tags:
            result['tags'] = tags
            text = re.sub(r'#\w+', '', text).strip()
        
        # 剩余内容作为标题
        result['title'] = text.strip() or '无标题待办'
        
        return result

    async def list_all_categories(self, user_id: str, context: dict) -> dict[str, Any]:
        """列出所有分类（/pendo todo list 不带参数时）"""
        # 查询所有未删除的待办，按分类分组
        tasks = await run_sync(self.db.items.get_items, user_id, {'type': 'task'}, 1000)
        
        if not tasks:
            return {
                'status': 'success',
                'message': '📝 **待办列表**\n\n暂无待办事项\n\n💡 用 /pendo todo add <内容> 添加待办'
            }
        
        # 按分类分组统计
        categories = {}
        for task in tasks:
            cat = task.category or '未分类'
            if cat not in categories:
                categories[cat] = {'done': 0, 'undone': 0}
            
            status_val = _enum_val(task.status)
            
            if status_val == TaskStatus.DONE.value:
                categories[cat]['done'] += 1
            else:
                categories[cat]['undone'] += 1
        
        # 格式化输出
        message = "📝 **待办分类列表**\n\n"
        
        for cat in _sort_category_keys(categories):
            stats = categories[cat]
            total = stats['done'] + stats['undone']
            message += f"📂 **{cat}** ({stats['undone']}未完成/{total}总)\n"
        
        message += f"\n💡 用 /pendo todo list <分类名> 查看详情"
        message += f"\n💡 用 /pendo todo list today 查看今日待办"
        
        return {
            'status': 'success',
            'message': message
        }

    async def list_tasks(self, user_id: str, filter_str: str, context: dict) -> dict[str, Any]:
        """列出待办
        
        格式：
        - /pendo todo list -> 列出所有分类
        - /pendo todo list today -> 列出今天的待办
        - /pendo todo list cat [done/undone] -> 列出指定分类
        - /pendo todo list done -> 列出所有分类下已完成的待办
        - /pendo todo list undone -> 列出所有分类下未完成的待办
        - /pendo todo list cat all -> 显示该分类全部待办
        - /pendo todo list cat page:2 -> 显示该分类第2页
        """
        filter_str = (filter_str or '').strip()
        
        if not filter_str:
            return await self.list_all_categories(user_id, context)
        
        parts = filter_str.split()
        category = parts[0]
        
        # 检查是否是全局 done/undone 筛选
        global_status = None
        if category.lower() in ['done', '已完成']:
            global_status = TaskStatus.DONE.value
            return await self.list_all_tasks_by_status(user_id, global_status, context, filter_str)
        elif category.lower() in ['undone', '未完成', 'todo']:
            global_status = TaskStatus.TODO.value
            return await self.list_all_tasks_by_status(user_id, global_status, context, filter_str)
        
        # today 快捷方式
        if category.lower() == 'today':
            category = datetime.now().strftime('%Y-%m-%d')
        
        # 解析参数
        status_filter = None
        show_all = False
        page_num = 1
        
        for i, part in enumerate(parts[1:], 1):
            part_lower = part.lower()
            if part_lower in ['done', '已完成']:
                status_filter = TaskStatus.DONE.value
            elif part_lower in ['undone', '未完成', 'todo']:
                status_filter = TaskStatus.TODO.value
            elif part_lower == 'all':
                show_all = True
            elif part.startswith('page:'):
                try:
                    page_num = int(part.split(':')[1])
                except (IndexError, ValueError):
                    pass
        
        # 构建查询条件
        filters = {
            'type': 'task',
            'category': category
        }
        
        if status_filter:
            filters['status'] = status_filter
        
        # 查询（如果显示全部或分页，增加limit）
        query_limit = 1000 if show_all or page_num > 1 else PendoConfig.DEFAULT_SEARCH_LIMIT
        tasks = await run_sync(self.db.items.get_items, user_id, filters, query_limit)
        
        if not tasks:
            return {
                'status': 'success',
                'message': f'📝 **{category}** 的待办\n\n暂无待办事项'
            }
        
        # 按优先级排序，再按创建时间排序
        tasks.sort(key=lambda t: (_enum_val(t.priority) or 3, t.created_at or ''))
        
        # 分页处理
        page_size = PendoConfig.LIST_PAGE_SIZE
        if show_all:
            # 显示全部
            display_tasks = tasks
            page_info = " (全部显示)"
        elif page_num > 1:
            # 分页显示
            start_idx = (page_num - 1) * page_size
            end_idx = start_idx + page_size
            display_tasks = tasks[start_idx:end_idx]
            page_info = f" (第{page_num}页)"
        else:
            # 默认显示第一页
            display_tasks = tasks[:page_size]
            page_info = ""
        
        # 格式化输出
        message = f"📝 **{category}** 的待办 (共{len(tasks)}项){page_info}\n\n"

        for idx, task in enumerate(display_tasks, 1):
            status_icon = ItemFormatter.format_status_icon(_enum_val(task.status))
            priority_icon = ItemFormatter.format_priority_icon(_enum_val(task.priority))
            title = task.title or '无标题'
            task_id = task.id or ''
            
            # 计算全局序号
            global_idx = (page_num - 1) * page_size + idx
            
            message += f"{global_idx}. {status_icon} {priority_icon} {title}\n"
            message += f"   `{task_id}`\n\n"

        # 显示分页提示
        if len(tasks) > page_size and not show_all:
            remaining = len(tasks) - page_size
            if page_num == 1:
                message += f"   ... 还有{remaining}项 (使用 'all' 显示全部或 'page:2' 查看第2页)\n"
            elif (page_num - 1) * page_size + page_size < len(tasks):
                message += f"   ... (使用 'page:{page_num + 1}' 查看下一页)\n"

        message += f"💡 /pendo todo done <id> 完成 | /pendo todo undone <id> 重开"
        
        return {
            'status': 'success',
            'message': message
        }

    async def list_all_tasks_by_status(self, user_id: str, status: str, context: dict, filter_str: str = '') -> dict[str, Any]:
        """列出所有分类下指定状态的待办
        
        Args:
            user_id: 用户ID
            status: 任务状态 (todo/done)
            context: 上下文
            filter_str: 过滤字符串（支持 all/page:n）
            
        Returns:
            待办列表消息
        """
        # 解析参数
        show_all = False
        page_num = 1
        
        if filter_str:
            parts = filter_str.split()
            for part in parts:
                part_lower = part.lower()
                if part_lower == 'all':
                    show_all = True
                elif part.startswith('page:'):
                    try:
                        page_num = int(part.split(':')[1])
                    except (IndexError, ValueError):
                        pass
        
        # 查询所有指定状态的待办
        query_limit = 1000 if show_all or page_num > 1 else PendoConfig.DEFAULT_SEARCH_LIMIT
        filters = {
            'type': 'task',
            'status': status
        }
        tasks = await run_sync(self.db.items.get_items, user_id, filters, query_limit)
        
        if not tasks:
            status_text = '已完成' if status == TaskStatus.DONE.value else '未完成'
            return {
                'status': 'success',
                'message': f'📝 所有分类的{status_text}待办\n\n暂无{status_text}待办事项'
            }
        
        # 按分类分组
        categories = {}
        for task in tasks:
            cat = task.category or '未分类'
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(task)
        
        # 分页信息
        page_size = PendoConfig.LIST_PAGE_SIZE
        if show_all:
            page_info = " (全部显示)"
        elif page_num > 1:
            page_info = f" (第{page_num}页)"
        else:
            page_info = ""
        
        # 格式化输出
        status_text = '已完成' if status == TaskStatus.DONE.value else '未完成'
        total_count = sum(len(cats) for cats in categories.values())
        
        # 初始消息头
        message = f"📝 所有分类的{status_text}待办 (共{total_count}项){page_info}\n\n"
        
        # 确定显示范围（计算起始和结束位置）
        if show_all:
            start_idx = 0
            end_idx = total_count
        else:
            start_idx = (page_num - 1) * page_size
            end_idx = min(start_idx + page_size, total_count)
        
        processed_count = 0
        for cat in _sort_category_keys(categories):
            sorted_tasks = categories[cat]
            sorted_tasks.sort(key=lambda t: (_enum_val(t.priority) or 3, t.created_at or ''))

            cat_start_idx = processed_count
            cat_end_idx = cat_start_idx + len(sorted_tasks)
            processed_count = cat_end_idx

            if cat_end_idx <= start_idx:
                continue
            if cat_start_idx >= end_idx:
                break

            display_start = max(0, start_idx - cat_start_idx)
            display_end = min(len(sorted_tasks), end_idx - cat_start_idx)
            cat_tasks = sorted_tasks[display_start:display_end]
            cat_display_count = len(cat_tasks)
            
            if cat_display_count == 0:
                continue
            
            message += f"📂 **{cat}** ({len(categories[cat])}项)\n"
            
            for idx, task in enumerate(cat_tasks, 1):
                status_icon = ItemFormatter.format_status_icon(task.status.value if hasattr(task.status, 'value') else task.status)
                priority_icon = ItemFormatter.format_priority_icon(task.priority.value if hasattr(task.priority, 'value') else task.priority)
                title = task.title or '无标题'
                task_id = task.id or ''
                
                message += f"  {idx}. {status_icon} {priority_icon} {title}\n"
                message += f"     `{task_id}`\n"
            
            message += "\n"
        
        # 分页提示
        if not show_all and end_idx < total_count:
            remaining = total_count - end_idx
            if page_num == 1:
                message += f"... 还有{remaining}项 (使用 'all' 显示全部或 'page:2' 查看第2页)\n"
            else:
                message += f"... (使用 'page:{page_num + 1}' 查看下一页)\n"
        
        message += f"💡 /pendo todo done <id> 完成 | /pendo todo undone <id> 重开"
        
        return {
            'status': 'success',
            'message': message
        }

    async def mark_done(self, user_id: str, task_id: str, context: dict) -> dict[str, Any]:
        """标记为完成"""
        if not task_id:
            raise MissingRequiredFieldException('task_id')

        task_id = task_id.strip()

        # 获取任务（_db_get_and_check 已包含所有权验证）
        task = await self._db_get_and_check(task_id, user_id)
        
        # 更新状态
        now = now_in_timezone(user_id, self.db)
        updates = {
            ItemFields.STATUS: TaskStatus.DONE.value,
            'completed_at': TimezoneHelper.format_for_storage(now),
            'type': ItemType.TASK.value
        }
        await self._db_update_with_log(task_id, updates, user_id, action='complete_task')
        
        return {
            'status': 'success',
            'message': f'✅ 已完成: {task.title or "无标题"}\n\n🎉 干得好！\n💡 用 /pendo todo list 查看待办'
        }

    async def mark_undone(self, user_id: str, task_id: str, context: dict) -> dict[str, Any]:
        """标记为未完成"""
        if not task_id:
            raise MissingRequiredFieldException('task_id')
        
        task_id = task_id.strip()
        
        # 获取任务并检查权限
        task = await self._db_get_and_check(task_id, user_id)
        
        updates = {
            ItemFields.STATUS: TaskStatus.TODO.value,
            'completed_at': None,
            'type': ItemType.TASK.value
        }
        await self._db_update_with_log(task_id, updates, user_id, action='reopen_task')
        
        return {
            'status': 'success',
            'message': f'↩️ 已重新打开: {task.title or "无标题"}\n\n💡 用 /pendo todo done {task_id} 完成'
        }

    async def delete_task(self, user_id: str, args: str, context: dict) -> dict[str, Any]:
        """删除待办
        
        格式：
        - /pendo todo delete <id> -> 删除单个待办
        - /pendo todo delete cat:xxx -> 删除整个分类下的待办
        """
        if not args:
            raise MissingRequiredFieldException('id或cat:xxx')
        
        args = args.strip()
        
        # 检查是否是cat:xxx格式
        cat_match = re.match(r'cat:(\S+)', args)
        if cat_match:
            category = cat_match.group(1)
            return await self._delete_category_tasks(user_id, category, context)
        
        # 单个ID删除
        task_id = args
        task = await self._db_get_and_check(task_id, user_id)
        
        # 软删除
        await self._db_soft_delete_with_log(task_id, user_id, item_type=ItemType.TASK.value)
        
        return {
            'status': 'success',
            'message': f'🗑️ 已删除: {task.title or "无标题"}\n\n💡 5分钟内可用 /pendo undo 撤销'
        }

    async def _delete_category_tasks(self, user_id: str, category: str, context: dict) -> dict[str, Any]:
        """删除整个分类下的待办（批量操作）"""
        # 查询该分类下所有待办
        filters = {
            'type': 'task',
            'category': category
        }
        tasks = await run_sync(self.db.items.get_items, user_id, filters, 1000)
        
        if not tasks:
            return {
                'status': 'success',
                'message': f'📂 分类 {category} 下没有待办'
            }
        
        # 批量软删除（使用单个事务）
        deleted_count = await run_sync(self._batch_soft_delete_tasks, user_id, tasks)
        
        return {
            'status': 'success',
            'message': f'🗑️ 已删除分类 {category} 下的 {deleted_count} 个待办\n\n💡 5分钟内可用 /pendo undo 撤销'
        }
    
    def _batch_soft_delete_tasks(self, user_id: str, tasks: list) -> int:
        """批量软删除待办（内部同步方法）
        
        Args:
            user_id: 用户ID
            tasks: 待办列表（dict或Item dataclass实例）
            
        Returns:
            成功删除的数量
        """
        conn = self.db.get_connection()
        cursor = conn.cursor()

        try:
            task_ids = [task.id for task in tasks]
            placeholders = ','.join(['?' for _ in task_ids])
            now = datetime.now().isoformat()
            log_records = [(user_id, 'delete_task', ItemType.TASK.value, task_id, 'Deleted task in batch', now) for task_id in task_ids]

            # S-1修复：使用 with conn: 代替手动 BEGIN/COMMIT/ROLLBACK
            with conn:
                cursor.execute(f"""
                    UPDATE items
                    SET deleted = 1, deleted_at = ?, updated_at = ?
                    WHERE id IN ({placeholders}) AND owner_id = ? AND type = ?
                """, [now, now] + task_ids + [user_id, ItemType.TASK.value])

                updated_count = cursor.rowcount

                cursor.executemany("""
                    INSERT INTO operation_logs (user_id, action, item_type, item_id, details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, log_records)

                for task_id in task_ids:
                    cursor.execute("DELETE FROM items_fts WHERE id = ?", (task_id,))

            return updated_count
        except Exception as e:
            logger.exception("批量删除待办失败: %s", e)
            return 0

    async def edit_task(self, user_id: str, args: str, context: dict) -> dict[str, Any]:
        """编辑待办
        
        格式：/pendo todo edit <id> <新内容> [cat:xxx] [p:1-4]
        """
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {'status': 'error', 'message': '❌ 用法: /pendo todo edit <id> <新内容>'}
        
        task_id = parts[0].strip()
        new_content = parts[1]
        
        # 获取任务
        task = await self._db_get_and_check(task_id, user_id)
        
        # 解析新内容
        parsed = self._parse_task_text(new_content, user_id)
        
        # 构建更新
        updates = {
            'title': parsed['title'],
            'type': ItemType.TASK.value
        }
        
        # 只有明确指定才更新
        if 'cat:' in new_content:
            updates['category'] = parsed['category']
        if 'p:' in new_content:
            updates['priority'] = parsed['priority']
        if parsed.get('tags'):
            updates['tags'] = parsed['tags']
        
        await self._db_update_with_log(task_id, updates, user_id, action='edit_task')
        
        return {
            'status': 'success',
            'message': f'✅ 已更新待办: {parsed["title"]}\n\n💡 /pendo todo done {task_id} 完成 | /pendo undo 撤销编辑'
        }
