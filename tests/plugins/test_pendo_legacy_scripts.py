from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_unsafe_legacy_pendo_mutation_scripts_are_not_shipped():
    scripts_dir = ROOT / "plugins" / "pendo" / "scripts"
    retired = {
        "import_old_bill.py.old",
        "backfill_start_time_reminders.py.old",
        "convert_text_export_to_pendo_bundle.py.old",
    }
    assert not any((scripts_dir / filename).exists() for filename in retired)
