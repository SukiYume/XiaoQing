from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import zipfile
from pathlib import Path
from types import ModuleType

from plugins.pendo.web.services.transfer_bundle import read_bundle

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "plugins" / "pendo" / "scripts"


def _load_script(filename: str) -> ModuleType:
    path = SCRIPTS_DIR / filename
    loader = importlib.machinery.SourceFileLoader(filename.replace(".", "_"), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_convert_text_export_old_script_builds_v2_bundle():
    module = _load_script("convert_text_export_to_pendo_bundle.py.old")
    text = """
2030/1/1 09:00 - note:
:id: note01
:标题: 旧笔记
正文

2030/1/2 10:00 - todo:
:id: todo01
:标题: 最近待办
- [ ] 未完成任务
- [v] 已完成任务

2030/1/3 22:30 - journal:
:id: diary01
:标题: 2030-01-03
今天很好
"""

    entries = module.parse_entries(text)
    records = module.convert_entries(entries, "Asia/Shanghai")
    bundle_bytes = module.build_bundle(records, "Asia/Shanghai")
    parsed = read_bundle(io.BytesIO(bundle_bytes))

    assert parsed.errors == []
    assert parsed.records_by_type["note"][0]["title"] == "旧笔记"
    assert {task["status"] for task in parsed.records_by_type["task"]} == {"open", "done"}
    assert parsed.records_by_type["diary"][0]["entry_time"] == "2030-01-03T22:30:00+08:00"

    with zipfile.ZipFile(io.BytesIO(bundle_bytes), "r") as zf:
        task_rows = [
            json.loads(line)
            for line in zf.read("data/tasks.ndjson").decode("utf-8").splitlines()
            if line
        ]
    assert all(row["_schema"] == 2 for row in task_rows)


def test_import_old_bill_old_script_builds_v2_ledger_bundle(tmp_path: Path):
    module = _load_script("import_old_bill.py.old")
    csv_path = tmp_path / "old_bill.csv"
    csv_path.write_text(
        "事件,收支,标签,时间,记录人,渠道,月份\n"
        "麦当劳,-46.96,吃饭,2025/05/01,,微信,2025/05\n"
        "工资,1000,工资,2025/05/02,,银行卡,2025/05\n",
        encoding="utf-8",
    )

    records, stats, warnings = module.parse_old_bill_rows(csv_path)
    bundle_bytes = module.build_old_bill_bundle(records)
    parsed = read_bundle(io.BytesIO(bundle_bytes))

    assert stats == {"skipped": 0}
    assert warnings == []
    assert parsed.errors == []
    ledgers = parsed.records_by_type["ledger"]
    assert [row["transaction_type"] for row in ledgers] == ["expense", "income"]
    assert [row["amount_cents"] for row in ledgers] == [4696, 100000]
    assert ledgers[0]["ledger_category"] == "餐饮"
    assert ledgers[1]["account_name"] == "银行卡"
