from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_vbs_launcher_delegates_to_the_monitor() -> None:
    source = (ROOT / "run-bot.vbs").read_text(encoding="utf-8")

    assert "scripts\\run-bot-monitor.ps1" in source
    assert "-WindowStyle Hidden" in source
    assert "tasklist" not in source.lower()
    assert "wmic" not in source.lower()


def test_monitor_uses_repository_scoped_identity_backoff_and_rotation() -> None:
    source = (ROOT / "scripts" / "run-bot-monitor.ps1").read_text(encoding="utf-8")

    assert "XiaoQing.BotMonitor" in source
    assert "xiaoqing-bot.pid.json" in source
    assert "Get-CimInstance" in source
    assert "Test-CommandLineContains" in source
    assert "MaximumRestartDelaySeconds" in source
    assert "Rotate-Log" in source
    assert "Get-TrackedBotProcess" in source
    assert "tasklist" not in source.lower()
    assert "wmic" not in source.lower()
