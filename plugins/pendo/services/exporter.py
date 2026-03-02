"""
导入导出服务
支持Markdown格式的导入导出
"""
import os
import re
from typing import Any, Optional
from datetime import datetime, timedelta
from pathlib import Path
import yaml

from ..utils.time_utils import parse_search_date_range, parse_date_optional

def _sanitize_user_id(user_id: str) -> str:
    """清洗用户ID，防止路径遍历
    
    Args:
        user_id: 原始用户ID
        
    Returns:
        清洗后的安全用户ID
    """
    # 只保留字母数字、下划线和短横线
    return re.sub(r'[^a-zA-Z0-9_-]', '_', user_id)

def _validate_file_path(file_path: str, user_id: str) -> bool:
    """验证文件路径是否安全
    
    Args:
        file_path: 文件路径
        user_id: 用户ID
        
    Returns:
        是否安全
    """
    # 获取导出目录的绝对路径
    safe_user_id = _sanitize_user_id(user_id)
    export_dir = Path(__file__).parent.parent / 'data' / 'exports' / safe_user_id
    export_dir_abs = export_dir.resolve()
    
    # 获取目标文件的绝对路径
    target_path = Path(file_path).resolve()
    
    # 检查目标路径是否在导出目录内
    try:
        target_path.relative_to(export_dir_abs)
        return True
    except ValueError:
        return False

class ExporterService:
    """导入导出服务"""
    
    def __init__(self, db):
        self.db = db
    
    def export_markdown(self, user_id: str, args: str, context: dict) -> dict[str, Any]:
        """
        导出为Markdown文件
        
        Args:
            user_id: 用户ID
            args: 参数，如 "range=2026-01-01..2026-01-31"
            context: 上下文
        """
        # 解析参数
        params = self._parse_export_params(args)
        
        # 获取数据
        filters = {}
        date_field = params.get('date_field', 'created_at')
        if params.get('start_date'):
            filters['start_date'] = params['start_date']
            filters['date_field'] = date_field
        if params.get('end_date'):
            filters['end_date'] = params['end_date']
            filters['date_field'] = date_field
        
        items = self.db.items.get_items(user_id, filters=filters, limit=10000)
        
        if not items:
            return {
                'status': 'success',
                'message': '没有找到要导出的数据'
            }
        
        # 按类型分组
        items_by_type = {
            'event': [],
            'task': [],
            'note': [],
            'diary': []
        }
        
        for item in items:
            item_type = item.type.value if hasattr(item.type, 'value') else item.type
            if item_type in items_by_type:
                items_by_type[item_type].append(item)
        
        # 生成Markdown文件
        export_format = params.get('format', 'by_type')  # by_type 或 by_date
        
        if export_format == 'by_type':
            markdown_files = self._export_by_type(items_by_type, user_id, params)
        else:
            markdown_files = self._export_by_date(items, user_id, params)
        
        # 返回文件信息
        return {
            'status': 'success',
            'message': f'已生成 {len(markdown_files)} 个Markdown文件',
            'files': markdown_files
        }
    
    def _parse_export_params(self, args: str) -> dict[str, Any]:
        """解析导出参数"""
        params = {
            'format': 'by_type',  # by_type 或 by_date
        }
        
        # 解析 range=...
        range_match = re.search(r'range=([^\s]+)', args)
        if range_match:
            range_str = range_match.group(1)
            start_date, end_date = parse_search_date_range(range_str)
            if start_date and end_date:
                params['start_date'] = start_date
                params['end_date'] = end_date
            else:
                single_date = parse_date_optional(range_str)
                if single_date:
                    params['start_date'] = single_date + 'T00:00:00'
                    params['end_date'] = single_date + 'T23:59:59'
        
        # 解析 format=...
        format_match = re.search(r'format=(by_type|by_date)', args)
        if format_match:
            params['format'] = format_match.group(1)
        
        return params
    
    def _export_by_type(self, items_by_type: dict[str, list[dict]], 
                       user_id: str, params: dict) -> list[str]:
        """按类型导出"""
        files = []
        safe_user_id = _sanitize_user_id(user_id)
        export_dir = Path(__file__).parent.parent / 'data' / 'exports' / safe_user_id
        export_dir.mkdir(parents=True, exist_ok=True)
        
        type_names = {
            'event': '日程',
            'task': '待办',
            'note': '笔记',
            'diary': '日记'
        }
        
        for item_type, items in items_by_type.items():
            if not items:
                continue
            
            filename = f"{type_names.get(item_type, item_type)}_{datetime.now().strftime('%Y%m%d')}.md"
            filepath = export_dir / filename
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(f"# {type_names.get(item_type, item_type)}\n\n")
                f.write(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"条目数量: {len(items)}\n\n")
                f.write("---\n\n")
                
                for item in items:
                    f.write(self._item_to_markdown(item))
                    f.write("\n---\n\n")
            
            files.append(str(filepath))
        
        return files
    
    def _export_by_date(self, items: list[dict], user_id: str, params: dict) -> list[str]:
        """按日期导出"""
        files = []
        safe_user_id = _sanitize_user_id(user_id)
        export_dir = Path(__file__).parent.parent / 'data' / 'exports' / safe_user_id
        export_dir.mkdir(parents=True, exist_ok=True)
        
        # 按日期分组
        items_by_date = {}
        for item in items:
            # 使用created_at的日期部分作为key
            date_str = item.created_at[:10]
            if date_str not in items_by_date:
                items_by_date[date_str] = []
            items_by_date[date_str].append(item)
        
        # 为每个日期生成文件
        for date_str, date_items in sorted(items_by_date.items()):
            filename = f"{date_str}.md"
            filepath = export_dir / filename
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(f"# {date_str}\n\n")
                f.write(f"条目数量: {len(date_items)}\n\n")
                f.write("---\n\n")
                
                for item in date_items:
                    f.write(self._item_to_markdown(item))
                    f.write("\n---\n\n")
            
            files.append(str(filepath))
        
        return files
    
    def _item_to_markdown(self, item) -> str:
        """将条目转换为Markdown格式"""
        lines = []
        
        # 标题
        title = item.title or '无标题'
        lines.append(f"## {title}\n")
        
        # Front Matter (YAML)
        item_type = item.type.value if hasattr(item.type, 'value') else item.type
        front_matter = {
            'id': item.id,
            'type': item_type,
            'created_at': item.created_at,
            'updated_at': item.updated_at,
        }
        
        if item.tags:
            front_matter['tags'] = item.tags
        if item.category:
            front_matter['category'] = item.category
        
        # 类型特定字段
        if item_type == 'event':
            if getattr(item, 'start_time', None):
                front_matter['start_time'] = item.start_time
            if getattr(item, 'end_time', None):
                front_matter['end_time'] = item.end_time
            if getattr(item, 'location', None):
                front_matter['location'] = item.location
        
        elif item_type == 'task':
            if getattr(item, 'due_time', None):
                front_matter['due_time'] = item.due_time
            if getattr(item, 'priority', None):
                priority_val = item.priority.value if hasattr(item.priority, 'value') else item.priority
                front_matter['priority'] = priority_val
            if getattr(item, 'status', None):
                status_val = item.status.value if hasattr(item.status, 'value') else item.status
                front_matter['status'] = status_val
        
        elif item_type == 'diary':
            if getattr(item, 'diary_date', None):
                front_matter['diary_date'] = item.diary_date
            if getattr(item, 'mood', None):
                front_matter['mood'] = item.mood
        
        # 写入Front Matter
        lines.append("```yaml")
        lines.append(yaml.dump(front_matter, allow_unicode=True, default_flow_style=False))
        lines.append("```\n")
        
        # 正文内容
        if item.content:
            lines.append(item.content)
        
        lines.append("")
        
        return '\n'.join(lines)
    
    def import_markdown(self, user_id: str, args: str, context: dict) -> dict[str, Any]:
        """
        导入Markdown文件
        
        用户需要先发送此命令，然后发送Markdown文件
        """
        # 检查是否是预览模式
        if 'preview' in args.lower():
            return {
                'status': 'success',
                'message': '请发送要导入的Markdown文件进行预览。\n\n支持的格式:\n1. 包含YAML Front Matter的Markdown文件\n2. 自动检测条目类型\n3. 支持批量导入\n\n预览模式将显示预计新增和更新的条目数量。'
            }
        
        # TODO: 实现文件接收逻辑
        # 这需要与XiaoQing的文件接收机制集成
        
        return {
            'status': 'success',
            'message': '请发送要导入的Markdown文件。\n\n支持的格式:\n1. 包含YAML Front Matter的Markdown文件\n2. 自动检测条目类型\n3. 支持批量导入\n\n提示: 使用 /pendo import md preview 进入预览模式'
        }
    
    def import_from_file(self, user_id: str, file_path: str, preview: bool = False) -> dict[str, Any]:
        """
        从文件导入
        
        Args:
            user_id: 用户ID
            file_path: 文件路径
            preview: 是否为预览模式
        """
        # 验证文件路径安全性
        if not _validate_file_path(file_path, user_id):
            return {
                'status': 'error',
                'message': f'无效的文件路径: {file_path}'
            }
        
        if not os.path.exists(file_path):
            return {
                'status': 'error',
                'message': f'文件不存在: {file_path}'
            }
        
        # 读取文件
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 解析Markdown
        items = self._parse_markdown_content(content, user_id)
        
        if preview:
            # 预览模式：只统计，不实际导入
            return self._import_preview(items, user_id)
        
        # 导入到数据库
        imported = 0
        updated = 0
        errors = []
        
        for item_data in items:
            try:
                # 确保owner_id正确（导入时必须是当前用户）
                item_data['owner_id'] = user_id
                
                # 检查是否已存在（只能查询和更新自己的数据）
                existing = None
                if 'id' in item_data:
                    existing = self.db.items.get_item(item_data['id'], owner_id=user_id)
                
                if existing:
                    # 更新（只能更新自己的数据）
                    self.db.items.update_item(item_data['id'], item_data, owner_id=user_id)
                    updated += 1
                else:
                    # 新增
                    self.db.items.insert_item(item_data)
                    imported += 1
            except Exception as e:
                errors.append(f"导入失败: {item_data.get('title', 'unknown')} - {str(e)}")
        
        return {
            'status': 'success',
            'message': f'导入完成!\n新增: {imported}\n更新: {updated}\n错误: {len(errors)}',
            'errors': errors if errors else None
        }
    
    def import_preview(self, user_id: str, file_path: str) -> dict[str, Any]:
        """
        导入预览
        
        Args:
            user_id: 用户ID
            file_path: 文件路径
        """
        # 验证文件路径安全性
        if not _validate_file_path(file_path, user_id):
            return {
                'status': 'error',
                'message': f'无效的文件路径: {file_path}'
            }
        
        if not os.path.exists(file_path):
            return {
                'status': 'error',
                'message': f'文件不存在: {file_path}'
            }
        
        # 读取文件
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 解析Markdown
        items = self._parse_markdown_content(content, user_id)
        
        return self._import_preview(items, user_id)
    
    def _import_preview(self, items: list[dict], user_id: str) -> dict[str, Any]:
        """
        生成导入预览
        
        Args:
            items: 解析出的条目列表
            user_id: 用户ID
            
        Returns:
            预览结果
        """
        new_count = 0
        update_count = 0
        items_preview = []
        
        for item_data in items:
            # 检查是否已存在（仅限当前用户的数据）
            existing = None
            if 'id' in item_data:
                existing = self.db.items.get_item(item_data['id'], owner_id=user_id)
            
            if existing:
                update_count += 1
                preview_type = '更新'
            else:
                new_count += 1
                preview_type = '新增'
            
            # 添加到预览列表（最多显示5个）
            if len(items_preview) < 5:
                items_preview.append({
                    'type': preview_type,
                    'title': item_data.get('title', '无标题'),
                    'item_type': item_data.get('type', 'unknown'),
                    'id': item_data.get('id', 'N/A')
                })
        
        # 构建预览消息
        lines = ["📋 导入预览"]
        lines.append(f"\n预计新增: {new_count} 条")
        lines.append(f"预计更新: {update_count} 条")
        lines.append(f"总计: {len(items)} 条")
        
        if items_preview:
            lines.append("\n前5个条目预览:")
            for i, item in enumerate(items_preview, 1):
                type_icon = '➕' if item['type'] == '新增' else '✏️'
                lines.append(f"{i}. {type_icon} [{item['item_type']}] {item['title']} ({item['id']})")
        
        if len(items) > 5:
            lines.append(f"\n... 还有 {len(items) - 5} 个条目")
        
        lines.append("\n💡 确认导入请发送文件，取消请忽略")
        
        return {
            'status': 'preview',
            'new_count': new_count,
            'update_count': update_count,
            'total_count': len(items),
            'items_preview': items_preview,
            'message': '\n'.join(lines)
        }
    
    def _parse_markdown_content(self, content: str, user_id: str) -> list[dict[str, Any]]:
        """解析Markdown内容"""
        items = []
        
        # 分割条目 (用 --- 分隔)
        sections = re.split(r'\n---+\n', content)
        
        for section in sections:
            if not section.strip():
                continue
            
            item = self._parse_markdown_item(section, user_id)
            if item:
                items.append(item)
        
        return items
    
    def _parse_markdown_item(self, section: str, user_id: str) -> Optional[dict[str, Any]]:
        """解析单个Markdown条目"""
        # 提取Front Matter
        yaml_match = re.search(r'```yaml\n(.*?)\n```', section, re.DOTALL)
        
        item_data = {'owner_id': user_id}
        
        if yaml_match:
            yaml_content = yaml_match.group(1)
            try:
                front_matter = yaml.safe_load(yaml_content)
                if front_matter:
                    item_data.update(front_matter)
            except (yaml.YAMLError, ValueError, TypeError):
                # YAML解析失败，使用默认值
                pass
        
        # 提取标题
        title_match = re.search(r'^##\s+(.+)$', section, re.MULTILINE)
        if title_match:
            item_data['title'] = title_match.group(1).strip()
        
        # 提取内容 (去除Front Matter和标题)
        content = section
        if yaml_match:
            content = content.replace(yaml_match.group(0), '')
        if title_match:
            content = content.replace(title_match.group(0), '')
        
        item_data['content'] = content.strip()
        
        # 确保有类型
        if 'type' not in item_data:
            item_data['type'] = 'note'
        
        # 确保有时间戳
        if 'created_at' not in item_data:
            item_data['created_at'] = datetime.now().isoformat()
        if 'updated_at' not in item_data:
            item_data['updated_at'] = datetime.now().isoformat()
        
        return item_data if item_data.get('title') or item_data.get('content') else None
