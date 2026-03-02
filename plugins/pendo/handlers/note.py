"""
笔记(Note)处理器
处理笔记相关的所有操作，不需要AI解析
"""
from typing import Any
from datetime import datetime
import re
import logging
from ..models.item import ItemType
from ..models.constants import ItemFields
from ..core.exceptions import OwnershipException
from core.plugin_base import run_sync
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import handle_command_errors
from ..config import PendoConfig
from ..utils.formatters import ItemFormatter, format_success_message

logger = logging.getLogger(__name__)

class NoteHandler(DbOpsMixin):
    """笔记处理器
    
    负责处理笔记（Note）相关的所有操作：
    - 快速记录笔记（支持 cat:xxx #tag 语法）
    - 查看、列表、删除
    
    不需要AI解析，直接规则解析
    """
    
    def __init__(self, db, ai_parser=None):
        self.db = db
        # ai_parser保留接口兼容性，但不使用

    @handle_command_errors
    async def handle(self, user_id: str, args: str, context: dict, group_id: int = None) -> dict[str, Any]:
        """处理笔记相关命令
        
        命令格式：
        - /pendo note add <内容> [cat:xxx] [#tag]
        - /pendo note list [cat:xxx] [#tag] [all|page:n]
        - /pendo note view <id>
        - /pendo note edit <id> <新内容> [cat:xxx] [#tag]
        - /pendo note delete <id|cat:xxx>
        """
        parts = args.split(maxsplit=1)
        if not parts or not parts[0]:
            # L-6修复：无参数时显示笔记概览，而非报错
            return await self.list_notes(user_id, '', context)

        command = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        
        handlers = {
            'add': lambda: self.create_note(user_id, rest, context, group_id),
            'list': lambda: self.list_notes(user_id, rest, context),
            'view': lambda: self.view_note(user_id, rest, context),
            'edit': lambda: self.edit_note(user_id, rest, context),
            'delete': lambda: self.delete_note(user_id, rest, context),
        }
        
        handler = handlers.get(command)
        if handler:
            return await handler()
        else:
            # 未知命令，给出提示
            return {
                'status': 'error',
                'message': (
                    f'❌ 未知笔记命令: {command}\n\n'
                    '可用命令:\n'
                    '• /pendo note add <内容> [cat:xxx] [#tag]\n'
                    '• /pendo note list [cat:xxx] [#tag]\n'
                    '• /pendo note view <id>\n'
                    '• /pendo note edit <id> <新内容>\n'
                    '• /pendo note delete <id|cat:xxx>'
                )
            }

    async def create_note(self, user_id: str, content: str, context: dict, group_id: int = None) -> dict[str, Any]:
        """创建笔记（支持cat:xxx和#tag语法）"""
        if not content:
            return {'status': 'error', 'message': '❌ 请提供内容'}
        
        # 解析内容
        parsed = self._parse_note_text(content)
        
        clean_content = parsed['content']
        title = self._generate_note_title(clean_content)

        # 创建数据
        from ..models.item import NoteItem
        
        note_item = NoteItem(
            owner_id=user_id,
            title=title,
            content=clean_content,
            tags=parsed['tags'],
            category=parsed['category'],
            context={'group_id': group_id} if group_id else {},
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
        
        # 保存到数据库
        item_id = await self._db_create_with_log(
            note_item,
            owner_id=user_id,
            action='create_note'
        )
        
        note_item.id = item_id

        # 格式化返回消息
        tags_str = ItemFormatter.format_tags(parsed['tags'])
        message = "✅ 已记录笔记\n\n"
        message += f"📝 {title}\n"
        message += f"📂 分类: {parsed['category']}\n"
        if tags_str:
            message += f"🏷️ 标签: {tags_str}\n"
        message += f"`{item_id}`\n\n"
        message += f"💡 用 /pendo note view {item_id} 查看详情"

        return {
            'status': 'success',
            'message': message,
            'item_id': item_id
        }

    @staticmethod
    def _generate_note_title(content: str) -> str:
        """取第一行或前50字符作标题，末尾加 '...' 表示截断"""
        title = content.split('\n')[0][:50] if '\n' in content else content[:50]
        return title + '...' if len(title) == 50 else title

    def _parse_note_text(self, text: str) -> dict[str, Any]:
        """解析笔记文本（纯规则解析）
        
        支持格式：
        - 内容 cat:xxx #tag1 #tag2
        """
        result = {
            'content': '',
            'category': '未分类',
            'tags': []
        }
        
        # 提取 cat:xxx
        cat_match = re.search(r'cat:(\S+)', text)
        if cat_match:
            result['category'] = cat_match.group(1)
            text = text.replace(cat_match.group(0), '').strip()
        
        # 提取 #tag
        tags = re.findall(r'#(\w+)', text)
        if tags:
            result['tags'] = tags
            text = re.sub(r'#\w+', '', text).strip()
        
        # 剩余内容
        result['content'] = text.strip()
        
        return result

    async def list_notes(self, user_id: str, filter_str: str, context: dict) -> dict[str, Any]:
        """列出笔记
        
        格式：
        - /pendo note list -> 列出所有笔记（每分类显示前10项）
        - /pendo note list cat:xxx -> 按分类筛选
        - /pendo note list #tag -> 按标签筛选
        - /pendo note list cat:xxx #tag -> 同时筛选
        - /pendo note list cat:xxx all -> 显示该分类全部笔记
        - /pendo note list cat:xxx page:2 -> 显示该分类第2页
        """
        filters = {'type': ItemType.NOTE.value}
        filter_display = []
        show_all = False
        page_num = 1
        
        if filter_str:
            filter_str = filter_str.strip()
            
            # 提取 cat:xxx
            cat_match = re.search(r'cat:(\S+)', filter_str)
            if cat_match:
                filters['category'] = cat_match.group(1)
                filter_display.append(f"分类: {cat_match.group(1)}")
                # 移除 cat:xxx 以便后续处理其他参数
                filter_str = filter_str.replace(cat_match.group(0), '').strip()
            else:
                # 检查是否直接使用分类名（如：/pendo note list 想法）
                parts = filter_str.split()
                if parts and not parts[0].startswith('#') and not parts[0].startswith('page:') and parts[0].lower() != 'all':
                    # 假设第一个参数是分类名
                    category_candidate = parts[0]
                    # 检查是否是保留关键字
                    if category_candidate not in ['all', 'page'] and not category_candidate.startswith('page:'):
                        filters['category'] = category_candidate
                        filter_display.append(f"分类: {category_candidate}")
                        # L-4修复：通过丢弃第一个 token 来移除分类名，避免
                        # str.replace 将同名字符串从 #tag 内部错误截除
                        filter_str = ' '.join(parts[1:])
            
            # 提取 #tag
            tag_match = re.search(r'#(\w+)', filter_str)
            if tag_match:
                filters['tags'] = tag_match.group(1)
                filter_display.append(f"标签: #{tag_match.group(1)}")
            
            # 检查是否显示全部
            if re.search(r'\ball\b', filter_str, re.IGNORECASE):
                show_all = True
            
            # 检查是否指定页码
            page_match = re.search(r'page:(\d+)', filter_str)
            if page_match:
                page_num = int(page_match.group(1))
        
        # 查询（如果显示全部或分页，增加limit）
        query_limit = 1000 if show_all or page_num > 1 else PendoConfig.DEFAULT_SEARCH_LIMIT
        items = await run_sync(self.db.items.get_items, user_id, filters, query_limit)
        
        if not items:
            filter_text = " (" + ", ".join(filter_display) + ")" if filter_display else ""
            return {
                'status': 'success',
                'message': f'📝 没有找到笔记{filter_text}'
            }
        
        # 按分类分组
        items_by_category = {}
        for item in items:
            cat = item.category or '未分类'
            if cat not in items_by_category:
                items_by_category[cat] = []
            items_by_category[cat].append(item)
        
        # 如果没有指定分类筛选，只显示分类概览
        has_category_filter = 'category' in filters
        
        # 格式化输出
        filter_text = " - " + ", ".join(filter_display) if filter_display else ""
        if show_all:
            filter_text += " (全部显示)"
        elif page_num > 1:
            filter_text += f" (第{page_num}页)"
        
        message = f"📝 **笔记列表**{filter_text} (共{len(items)}项)\n\n"
        
        # 没有指定分类时，显示分类概览
        if not has_category_filter:
            message += "**分类概览**\n\n"
            for category, cat_items in items_by_category.items():
                # 统计该分类的标签
                category_tags = set()
                for item in cat_items:
                    if item.tags:
                        category_tags.update(item.tags)
                
                tags_str = f" {ItemFormatter.format_tags(list(category_tags))}" if category_tags else ""
                message += f"📂 **{category}** ({len(cat_items)}项){tags_str}\n"
            
            message += "\n💡 用 /pendo note list <分类名> 查看该分类的笔记"
            return {'status': 'success', 'message': message}
        
        # 指定了分类，显示该分类的笔记详情
        for category, cat_items in items_by_category.items():
            # 统计该分类下的标签分布
            category_tags = set()
            items_with_tags = 0
            for item in cat_items:
                if item.tags:
                    category_tags.update(item.tags)
                    items_with_tags += 1
            
            # 判断是否统一显示标签：所有笔记都有相同标签
            show_tags_inline = True
            if items_with_tags == len(cat_items) and len(category_tags) == 1:
                show_tags_inline = False
            
            message += f"**📂 {category}**"
            if category_tags and not show_tags_inline:
                message += f" {ItemFormatter.format_tags(list(category_tags))}"
            message += f" ({len(cat_items)}项)\n\n"
            
            # 分页处理
            page_size = PendoConfig.LIST_PAGE_SIZE
            if show_all:
                # 显示全部
                display_items = cat_items
            else:
                # 分页显示
                start_idx = (page_num - 1) * page_size
                end_idx = start_idx + page_size
                display_items = cat_items[start_idx:end_idx]
            
            for idx, item in enumerate(display_items, 1):
                title = item.title or '无标题'
                item_id = item.id or ''
                tags = item.tags or []
                
                # 计算全局序号
                global_idx = (page_num - 1) * page_size + idx
                
                # 标题过长时截断
                if len(title) > 40:
                    title = title[:38] + ".."
                
                message += f"{global_idx}. {title}\n"
                
                # 显示标签（如果不统一显示）
                if show_tags_inline and tags:
                    tags_str = ItemFormatter.format_tags(tags)
                    message += f"   🏷️ {tags_str}\n"
                
                message += f"   `{item_id}`\n\n"

            # 显示提示信息
            if len(cat_items) > page_size and not show_all:
                remaining = len(cat_items) - page_size
                if page_num == 1:
                    message += f"   ... 还有{remaining}项 (使用 'all' 显示全部或 'page:2' 查看第2页)\n"
                elif (page_num - 1) * page_size + page_size < len(cat_items):
                    message += f"   ... (使用 'page:{page_num + 1}' 查看下一页)\n"
            
            message += "\n"
        
        message += "💡 用 /pendo note view <id> 查看详情"
        
        return {
            'status': 'success',
            'message': message
        }

    async def view_note(self, user_id: str, note_id: str, context: dict) -> dict[str, Any]:
        """查看笔记详情"""
        if not note_id:
            return {'status': 'error', 'message': '❌ 请指定ID'}

        note_id = note_id.strip()

        # 获取笔记（_db_get_and_check 已包含所有权验证）
        note = await self._db_get_and_check(note_id, user_id)
        
        # 更新查看时间
        await self._db_update_item(note_id, {
            'last_viewed': datetime.now().isoformat()
        }, owner_id=user_id)
        
        # 格式化输出
        message = f"📝 **{note.title or '无标题'}**\n\n"

        # 元信息
        created_str = ItemFormatter.format_datetime(note.created_at or '')
        message += f"🗓️ 创建: {created_str}\n"
        message += f"📂 分类: {note.category or '未分类'}\n"

        if note.tags:
            tags_str = ItemFormatter.format_tags(note.tags)
            message += f"🏷️ 标签: {tags_str}\n"

        message += "\n---\n\n"

        # 内容
        message += note.content or ''

        return {
            'status': 'success',
            'message': message
        }

    async def edit_note(self, user_id: str, args: str, context: dict) -> dict[str, Any]:
        """编辑笔记
        
        格式：/pendo note edit <id> <新内容> [cat:xxx] [#tag]
        """
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {'status': 'error', 'message': '❌ 用法: /pendo note edit <id> <新内容> [cat:xxx] [#tag]'}
        
        note_id = parts[0].strip()
        new_content = parts[1]
        
        # 获取笔记
        note = await self._db_get_and_check(note_id, user_id)
        
        # 解析新内容
        parsed = self._parse_note_text(new_content)
        
        clean_content = parsed['content']
        title = self._generate_note_title(clean_content)

        # 构建更新
        updates = {
            'title': title,
            'content': clean_content,
            'type': ItemType.NOTE.value
        }
        
        # 只有明确指定才更新
        if 'cat:' in new_content:
            updates['category'] = parsed['category']
        if parsed.get('tags'):
            updates['tags'] = parsed['tags']
        
        await self._db_update_with_log(note_id, updates, user_id, action='edit_note')
        
        return {
            'status': 'success',
            'message': f'✅ 已更新笔记\n\n📝 {title}\n\n💡 /pendo note view {note_id} 查看详情 | /pendo undo 撤销编辑'
        }

    async def delete_note(self, user_id: str, args: str, context: dict) -> dict[str, Any]:
        """删除笔记
        
        格式：
        - /pendo note delete <id> -> 删除单个笔记
        - /pendo note delete cat:xxx -> 删除整个分类下的笔记
        - 不允许按标签删除
        """
        if not args:
            return {'status': 'error', 'message': '❌ 请指定ID或cat:xxx'}
        
        args = args.strip()
        
        # 检查是否是按标签删除（不允许）
        if args.startswith('#'):
            return {'status': 'error', 'message': '❌ 不支持按标签删除，请使用 /pendo note delete <id> 或 /pendo note delete cat:xxx'}
        
        # 检查是否是cat:xxx格式
        cat_match = re.match(r'cat:(\S+)', args)
        if cat_match:
            category = cat_match.group(1)
            return await self._delete_category_notes(user_id, category, context)
        
        # 单个ID删除
        note_id = args
        note = await self._db_get_and_check(note_id, user_id)
        
        # 软删除
        await self._db_soft_delete_with_log(note_id, user_id, item_type=ItemType.NOTE.value)
        
        return {
            'status': 'success',
            'message': f'🗑️ 已删除: {note.title or "无标题"}\n\n💡 5分钟内可用 /pendo undo 撤销'
        }

    async def _delete_category_notes(self, user_id: str, category: str, context: dict) -> dict[str, Any]:
        """删除整个分类下的笔记"""
        filters = {
            'type': ItemType.NOTE.value,
            'category': category
        }
        notes = await run_sync(self.db.items.get_items, user_id, filters, 1000)

        if not notes:
            return {
                'status': 'success',
                'message': f'📂 分类 {category} 下没有笔记'
            }

        note_ids = [note.id for note in notes]

        def _batch_delete():
            # M-4修复：单事务批量软删除，替代 N 次独立调用（N+1 问题）
            conn = self.db.get_connection()
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            placeholders = ','.join(['?' for _ in note_ids])
            log_records = [
                (user_id, 'delete', ItemType.NOTE.value, note_id, '{"soft_delete": true}', now)
                for note_id in note_ids
            ]
            with conn:
                cursor.execute(f"""
                    UPDATE items SET deleted = 1, deleted_at = ?, updated_at = ?
                    WHERE id IN ({placeholders}) AND owner_id = ? AND type = ?
                """, [now, now] + note_ids + [user_id, ItemType.NOTE.value])
                updated_count = cursor.rowcount
                cursor.executemany("""
                    INSERT INTO operation_logs (user_id, action, item_type, item_id, details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, log_records)
                for note_id in note_ids:
                    cursor.execute("DELETE FROM items_fts WHERE id = ?", (note_id,))
            self.db.items.cache_clear()
            return updated_count

        deleted_count = await run_sync(_batch_delete)

        return {
            'status': 'success',
            'message': f'🗑️ 已删除分类 {category} 下的 {deleted_count} 个笔记\n\n💡 5分钟内可用 /pendo undo 撤销'
        }
