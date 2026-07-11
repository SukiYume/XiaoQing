import importlib

import pytest

from plugins.pendo.services.db import Database


def test_redesign_migration_failure_keeps_original_database_unchanged(tmp_path, monkeypatch):
    db_path = tmp_path / "pendo.db"
    db = Database(str(db_path))
    db.insert_item({"type": "note", "owner_id": "u1", "title": "original"})
    db.cleanup()
    original = db_path.read_bytes()
    module = importlib.import_module("plugins.pendo.scripts.migrate_pendo_redesign")

    def fail_migration(*_args, **_kwargs):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(module, "migrate_event_graph", fail_migration)
    with pytest.raises(RuntimeError, match="injected"):
        module.migrate_pendo_redesign(db_path, apply=True)

    assert db_path.read_bytes() == original
    assert not (tmp_path / "pendo.db.pendo-redesign.lock").exists()
    assert '"state": "failed"' in (tmp_path / "pendo.db.pendo-redesign-journal.json").read_text(encoding="utf-8")
