from plugins.pendo.scripts.convert_text_export_to_pendo_bundle import build_diary_record


def test_build_diary_record_falls_back_to_date_title_for_date_only_body():
    record = build_diary_record(
        {
            "timestamp": "2026/01/21 22:27",
            "fields": {
                "title": "2026-01-21\n今天整理了迁移脚本。",
                "source_id": "legacy-1",
            },
        },
        "Asia/Shanghai",
    )

    assert record["title"] == "2026-01-21"
    assert record["diary_date"] == "2026-01-21"
    assert record["content"] == "今天整理了迁移脚本。"
