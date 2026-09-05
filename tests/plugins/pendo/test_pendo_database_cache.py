"""Pendo 缓存行为回归测试。"""

import pytest


class TestLRUCache:
    """LRU 缓存应淘汰最久未访问的条目，而非最早插入的条目。"""

    def _make_db(self, cache_size: int = 3):
        from plugins.pendo.services.db import Database

        db                = Database(":memory:")
        db.CACHE_MAX_SIZE = cache_size
        return db

    @pytest.fixture
    def db(self):
        database = self._make_db(cache_size=3)
        try:
            yield database
        finally:
            database.cleanup()

    def test_lru_evicts_least_recently_used_not_least_recently_inserted(self, db):
        """读取最旧条目后，新写入应淘汰第二旧且未被访问的条目。"""
        db._cache_set("key1", "val1")
        db._cache_set("key2", "val2")
        db._cache_set("key3", "val3")

        assert db._cache_get_or_miss("key1") == "val1"
        db._cache_set("key4", "val4")

        assert db._cache_get_or_miss("key1") == "val1"
        assert "key2" not in db._cache
        assert db._cache_get_or_miss("key3") == "val3"
        assert db._cache_get_or_miss("key4") == "val4"


class TestUpdateItemCacheInvalidation:
    """更新条目时即使省略 owner_id，也必须失效所属用户的列表缓存。"""

    def _make_db(self):
        from plugins.pendo.services.db import Database

        db = Database(":memory:")
        db._init_database()
        return db

    @pytest.fixture
    def db(self):
        database = self._make_db()
        try:
            yield database
        finally:
            database.cleanup()

    def test_update_without_owner_id_invalidates_user_list_cache(self, db):
        item_id = db.insert_item(
            {
                "title": "原始标题",
                "owner_id": "user1",
                "type": "note",
            }
        )
        assert db.get_items("user1", {"type": "note"}, 100)[0].title == "原始标题"

        db.update_item(item_id, {"title": "新标题"})

        items = db.get_items("user1", {"type": "note"}, 100)
        assert len(items) == 1
        assert items[0].title == "新标题"
