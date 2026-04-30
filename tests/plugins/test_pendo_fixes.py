"""
测试 pendo 插件代码修复（9个问题）

按 TDD 流程写 RED 测试，然后修代码让测试变绿。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# ========================================================
# Issue 1: LRU 缓存退化为 FIFO
# ========================================================

class TestLRUCache:
    """LRU 缓存应淘汰最久未访问的条目，而非最早插入的条目"""

    def _make_db(self, cache_size: int = 3):
        from plugins.pendo.services.db import Database
        db = Database(":memory:")
        db.CACHE_MAX_SIZE = cache_size
        return db

    def test_lru_evicts_least_recently_used_not_least_recently_inserted(self):
        """填满缓存后，读取最旧的条目，再插入新条目时，应淘汰第二旧的（未访问的）而非最旧的"""
        db = self._make_db(cache_size=3)

        # 填满缓存：插入 key1, key2, key3
        db._cache_set("key1", "val1")
        db._cache_set("key2", "val2")
        db._cache_set("key3", "val3")

        # 访问 key1，使它成为最近使用的
        result = db._cache_get("key1")
        assert result == "val1"

        # 插入 key4，应淘汰 key2（最久未访问），而非 key1（刚被访问）
        db._cache_set("key4", "val4")

        assert db._cache_get("key1") == "val1", "key1 刚被访问，不应被淘汰"
        assert db._cache_get("key2") is None, "key2 最久未访问，应被淘汰"
        assert db._cache_get("key3") == "val3"
        assert db._cache_get("key4") == "val4"


# ========================================================
# Issue 2: update_item 不传 owner_id 时列表缓存未失效
# ========================================================

class TestUpdateItemCacheInvalidation:
    """update_item 不传 owner_id 时，也应失效该用户的列表缓存"""

    def _make_db(self):
        from plugins.pendo.services.db import Database
        db = Database(":memory:")
        db._init_database()
        return db

    def test_update_without_owner_id_invalidates_user_list_cache(self):
        """不带 owner_id 的 update_item 后，get_items 应返回更新后的数据"""
        db = self._make_db()

        # 插入一条记录
        item_id = db.items.insert_item({
            "title": "原始标题",
            "owner_id": "user1",
            "type": "note",
        })

        # 触发列表缓存
        items_before = db.items.get_items("user1", {"type": "note"}, 100)
        assert len(items_before) == 1
        assert items_before[0].title == "原始标题"

        # 不传 owner_id 更新
        db.items.update_item(item_id, {"title": "新标题"})

        # 再次查询，应返回新数据而非缓存的旧数据
        items_after = db.items.get_items("user1", {"type": "note"}, 100)
        assert len(items_after) == 1
        assert items_after[0].title == "新标题", (
            "update_item 不传 owner_id 后列表缓存应被失效，应返回更新后的标题"
        )


# ========================================================
# Issue 3: JSON 字段列表重复定义（应提取为类常量 _JSON_FIELDS）
# ========================================================

class TestJsonFieldsConstant:
    """Database._JSON_FIELDS 应作为类常量存在，_prepare_data 和 _row_to_item 不应各自重复定义"""

    def test_json_fields_class_constant_exists(self):
        """Database 应有 _JSON_FIELDS 类属性"""
        from plugins.pendo.services.db import Database
        assert hasattr(Database, "_JSON_FIELDS"), (
            "Database 应有 _JSON_FIELDS 类属性，避免在 _prepare_data 和 _row_to_item 中重复定义"
        )

    def test_prepare_data_uses_class_constant(self):
        """_prepare_data 方法内不应有局部 json_fields 变量"""
        db_src = (ROOT / "plugins" / "pendo" / "services" / "db.py").read_text(encoding="utf-8")
        lines = db_src.splitlines()

        in_prepare_data = False
        for line in lines:
            stripped = line.strip()
            if "def _prepare_data" in stripped:
                in_prepare_data = True
            elif in_prepare_data and stripped.startswith("def "):
                in_prepare_data = False
            elif in_prepare_data and stripped.startswith("json_fields = ["):
                pytest.fail("_prepare_data 内不应再有局部 json_fields = [...] 定义，应使用 self._JSON_FIELDS")

    def test_row_to_item_uses_class_constant(self):
        """_row_to_item 方法内不应有局部 json_fields 变量"""
        db_src = (ROOT / "plugins" / "pendo" / "services" / "db.py").read_text(encoding="utf-8")
        lines = db_src.splitlines()

        in_row_to_item = False
        for line in lines:
            stripped = line.strip()
            if "def _row_to_item" in stripped:
                in_row_to_item = True
            elif in_row_to_item and stripped.startswith("def "):
                in_row_to_item = False
            elif in_row_to_item and stripped.startswith("json_fields = ["):
                pytest.fail("_row_to_item 内不应再有局部 json_fields = [...] 定义，应使用 self._JSON_FIELDS")


# ========================================================
# Issue 4: undo_edit 中重复定义 fts_fields frozenset
# ========================================================

class TestFtsFrozensetNotDuplicated:
    """undo_edit 应使用 self._FTS_FIELDS，不应重复定义局部 frozenset"""

    def test_undo_edit_does_not_redefine_fts_fields(self):
        """undo_edit 方法内不应有局部 fts_fields = frozenset(...) 定义"""
        db_src = (ROOT / "plugins" / "pendo" / "services" / "db.py").read_text(encoding="utf-8")
        lines = db_src.splitlines()

        in_undo_edit = False
        for line in lines:
            stripped = line.strip()
            if "def undo_edit" in stripped:
                in_undo_edit = True
            elif in_undo_edit and stripped.startswith("def "):
                in_undo_edit = False
            elif in_undo_edit and "fts_fields = frozenset" in stripped:
                pytest.fail(
                    "undo_edit 内不应有 fts_fields = frozenset(...) 定义，应使用 self._FTS_FIELDS"
                )


# ========================================================
# Issue 5: 批量删除使用 cache_clear() 而非精确失效
# ========================================================

class TestBatchDeleteCacheInvalidation:
    """_db_batch_soft_delete_with_log 应使用精确的 cache_invalidate，而非 cache_clear()"""

    def test_batch_delete_does_not_call_cache_clear(self):
        """db_ops.py 的 _db_batch_soft_delete_with_log 内不应调用 cache_clear()"""
        src = (ROOT / "plugins" / "pendo" / "utils" / "db_ops.py").read_text(encoding="utf-8")
        lines = src.splitlines()

        in_batch_delete = False
        for line in lines:
            stripped = line.strip()
            if "def _db_batch_soft_delete_with_log" in stripped:
                in_batch_delete = True
            elif in_batch_delete and stripped.startswith("async def ") and "batch_soft_delete" not in stripped:
                in_batch_delete = False
            elif in_batch_delete and "cache_clear()" in stripped:
                pytest.fail(
                    "_db_batch_soft_delete_with_log 内不应调用 cache_clear()，应使用精确 cache_invalidate"
                )


# ========================================================
# Issue 6: task.py 内部未统一使用 _enum_val()
# ========================================================

class TestEnumValUsedConsistently:
    """task.py 中凡是需要取 Enum .value 的地方，都应使用 _enum_val()"""

    def test_list_tasks_by_status_uses_enum_val(self):
        """list_all_tasks_by_status 区块内不应有内联的 hasattr(x, 'value') 模式"""
        src = (ROOT / "plugins" / "pendo" / "handlers" / "task.py").read_text(encoding="utf-8")
        lines = src.splitlines()

        # 找到 list_all_tasks_by_status 方法
        in_method = False
        for line in lines:
            stripped = line.strip()
            if "def list_all_tasks_by_status" in stripped:
                in_method = True
            elif in_method and stripped.startswith("async def ") or (in_method and stripped.startswith("def ") and "list_all_tasks_by_status" not in stripped):
                in_method = False
            elif in_method and 'hasattr(' in stripped and '"value"' in stripped:
                pytest.fail(
                    "list_all_tasks_by_status 内不应有内联 hasattr(x, 'value') 模式，应使用 _enum_val()"
                )


# ========================================================
# Issue 7: task.py 中硬编码 "task" 字符串
# ========================================================

class TestTaskHandlerUsesItemTypeEnum:
    """task.py 中过滤字典应使用 ItemType.TASK.value，不应硬编码字符串 'task'"""

    def test_no_hardcoded_task_string_in_filters(self):
        """get_items 调用的 filter dict 里不应有 'type': 'task' 硬编码"""
        src = (ROOT / "plugins" / "pendo" / "handlers" / "task.py").read_text(encoding="utf-8")
        # 检查是否存在 {"type": "task"} 或 {'type': 'task'} 的过滤字典写法
        assert '{"type": "task"}' not in src and "{'type': 'task'}" not in src, (
            "task.py 中不应硬编码 {\"type\": \"task\"}，应使用 {\"type\": ItemType.TASK.value}"
        )


# ========================================================
# Issue 8: event.py 中 parsed["type"] = ItemType.EVENT 是死代码
# ========================================================

class TestEventAddNoDeadCode:
    """event.py 中 parsed['type'] = ItemType.EVENT 是死代码，应被移除"""

    def test_no_dead_type_assignment_in_handle_add(self):
        """event.py 不应有 parsed['type'] = ItemType.EVENT 这行死代码"""
        src = (ROOT / "plugins" / "pendo" / "handlers" / "event.py").read_text(encoding="utf-8")
        assert 'parsed["type"] = ItemType.EVENT' not in src and \
               "parsed['type'] = ItemType.EVENT" not in src, (
            "event.py 中 parsed['type'] = ItemType.EVENT 是死代码（EventItem 构造时已默认设置），应移除"
        )


# ========================================================
# Issue 9: note.py 和 task.py 批量删除参数不一致
# ========================================================

class TestBatchDeleteConsistency:
    """note.py 和 task.py 的批量删除调用应有一致的 action 命名规范和参数"""

    def test_note_batch_delete_action_name_is_delete_note(self):
        """note.py 的批量删除 action 应为 'delete_note'（与 task.py 的 'delete_task' 对称）"""
        src = (ROOT / "plugins" / "pendo" / "handlers" / "note.py").read_text(encoding="utf-8")
        assert '"delete_note"' in src, (
            "note.py 的批量删除 action 应为 'delete_note'，与 task.py 的 'delete_task' 保持命名对称"
        )

    def test_note_batch_delete_no_redundant_details_factory(self):
        """note.py 的批量删除不应传 details_factory（默认行为已够用）"""
        src = (ROOT / "plugins" / "pendo" / "handlers" / "note.py").read_text(encoding="utf-8")
        lines = src.splitlines()
        in_delete_category = False
        for line in lines:
            stripped = line.strip()
            if "def _delete_category_notes" in stripped:
                in_delete_category = True
            elif in_delete_category and stripped.startswith("async def ") or \
                 (in_delete_category and stripped.startswith("def ") and "_delete_category_notes" not in stripped):
                in_delete_category = False
            elif in_delete_category and "details_factory" in stripped:
                pytest.fail(
                    "note.py _delete_category_notes 不应传 details_factory 参数，"
                    "默认 {'soft_delete': True} 已足够"
                )
