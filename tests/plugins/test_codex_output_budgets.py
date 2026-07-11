from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.codex.config import CodexPluginConfig, load_plugin_config
from plugins.codex.runner import (
    CodexRunner,
    OutputBudgetExceeded,
    _capture_final_output,
    _StderrAccumulator,
    _StdoutEventAccumulator,
    _truncate_utf8,
)


@pytest.fixture
def codex_config(tmp_path: Path) -> CodexPluginConfig:
    context = SimpleNamespace(
        config={
            "plugins": {
                "codex": {
                    "default_cwd": str(tmp_path),
                    "allowed_cwd_roots": [str(tmp_path)],
                    "job_timeout_seconds": 30,
                    "max_stdout_bytes": 64 * 1024,
                    "max_stderr_bytes": 64 * 1024,
                    "max_json_line_bytes": 16 * 1024,
                    "max_final_output_bytes": 64 * 1024,
                    "max_image_artifacts": 2,
                }
            }
        },
        secrets={},
    )
    return load_plugin_config(context)


def _json_line(event: dict[str, object]) -> bytes:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _feed_in_chunks(accumulator: _StdoutEventAccumulator, payload: bytes) -> None:
    chunk_sizes = (1, 2, 5, 13, 29, 7)
    offset = 0
    index = 0
    while offset < len(payload):
        size = chunk_sizes[index % len(chunk_sizes)]
        accumulator.feed(payload[offset : offset + size])
        offset += size
        index += 1


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        label="budget-test",
        process=None,
        prompt_started=False,
        cancel_requested=False,
        cancel_event=asyncio.Event(),
    )


def _run_with_subprocess_loop(coroutine):
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def test_chunked_stdout_keeps_events_then_enforces_total_budget(
    codex_config: CodexPluginConfig,
) -> None:
    accumulator = _StdoutEventAccumulator(codex_config)
    events = [
        {"type": "thread.started", "thread_id": "thread-budget"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "first answer"},
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "最后答案",
                "artifact": {"path": "C:/tmp/chart.PNG"},
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 13,
                "cached_input_tokens": 5,
                "output_tokens": 8,
                "ignored": "not retained",
            },
        },
    ]
    payload = b"".join(_json_line(event) for event in events)

    _feed_in_chunks(accumulator, payload)
    accumulator.finish()

    assert accumulator.thread_id == "thread-budget"
    assert accumulator.last_message == "最后答案"
    assert accumulator.usage == {
        "input_tokens": 13,
        "cached_input_tokens": 5,
        "output_tokens": 8,
    }
    assert accumulator.image_paths == ["C:/tmp/chart.PNG"]

    remaining = codex_config.max_stdout_bytes - accumulator.total_bytes
    full_lines, tail_bytes = divmod(remaining, 1024)
    accumulator.feed((b"x" * 1023 + b"\n") * full_lines + b"x" * tail_bytes)
    assert accumulator.total_bytes == codex_config.max_stdout_bytes

    with pytest.raises(OutputBudgetExceeded, match="stdout exceeded"):
        accumulator.feed(b"\n")


def test_stderr_budget_is_a_hard_limit(codex_config: CodexPluginConfig) -> None:
    accumulator = _StderrAccumulator(codex_config.max_stderr_bytes)
    accumulator.feed(b"e" * codex_config.max_stderr_bytes)

    with pytest.raises(OutputBudgetExceeded, match="stderr exceeded"):
        accumulator.feed(b"!")


def test_unterminated_json_line_has_a_hard_limit(codex_config: CodexPluginConfig) -> None:
    accumulator = _StdoutEventAccumulator(codex_config)

    with pytest.raises(OutputBudgetExceeded, match="stdout JSON line exceeded"):
        accumulator.feed(b"{" + b"x" * codex_config.max_json_line_bytes)


def test_utf8_truncation_is_valid_and_bounded() -> None:
    byte_budget = 73
    truncated = _truncate_utf8("青🧪" * 100, byte_budget)

    assert truncated.endswith("...[output truncated]")
    assert "\ufffd" not in truncated
    assert len(truncated.encode("utf-8")) <= byte_budget


def test_oversized_final_file_is_read_and_archived_with_a_hard_bound(
    tmp_path: Path,
    codex_config: CodexPluginConfig,
) -> None:
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    raw_output = output_dir / "raw.txt"
    raw_output.write_bytes(b"a" * (codex_config.max_final_output_bytes + 4096))

    capture = _capture_final_output(
        raw_output,
        output_dir=output_dir,
        job=SimpleNamespace(label="bounded", job_id=7),
        config=codex_config,
    )

    assert capture.limited is True
    assert capture.archive_path is not None
    archive = Path(capture.archive_path)
    assert archive.parent == output_dir.resolve()
    assert archive.stat().st_size <= codex_config.max_final_output_bytes
    assert not raw_output.exists()
    assert "bytes omitted" in archive.read_text(encoding="utf-8")


def test_qq_preview_limit_preserves_the_complete_bounded_file(
    tmp_path: Path,
    codex_config: CodexPluginConfig,
) -> None:
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    raw_output = output_dir / "raw.txt"
    original = "q" * (codex_config.max_qq_text_chars + 100)
    raw_output.write_text(original, encoding="utf-8")

    capture = _capture_final_output(
        raw_output,
        output_dir=output_dir,
        job=SimpleNamespace(label="qq", job_id=8),
        config=codex_config,
    )

    assert capture.limited is True
    assert capture.archive_path is not None
    assert Path(capture.archive_path).read_text(encoding="utf-8") == original
    assert len(capture.text) <= codex_config.max_qq_text_chars
    assert not raw_output.exists()


def test_runner_streams_real_subprocess_events(
    tmp_path: Path,
    codex_config: CodexPluginConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {"type": "thread.started", "thread_id": "thread-real"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "not final"},
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "real final",
                "image": "C:/tmp/real.png",
            },
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 21, "output_tokens": 34},
        },
    ]
    encoded_events = json.dumps(events, separators=(",", ":"))
    script = (
        "import json,sys\n"
        "sys.stdin.buffer.read()\n"
        f"events=json.loads({encoded_events!r})\n"
        "for event in events:\n"
        " print(json.dumps(event,separators=(',',':')),flush=True)\n"
    )
    runner = CodexRunner(codex_config, tmp_path / "outputs")
    monkeypatch.setattr(
        runner,
        "_build_args",
        lambda *_args, **_kwargs: [sys.executable, "-u", "-c", script],
    )

    async def exercise_runner():
        return await asyncio.wait_for(
            runner.run(
                cwd=tmp_path,
                prompt="stream events",
                thread_id=None,
                job=_job(),
            ),
            timeout=10,
        )

    result = _run_with_subprocess_loop(exercise_runner())

    assert result.exit_code == 0
    assert result.output_limited is False
    assert result.thread_id == "thread-real"
    assert result.final_text == "real final"
    assert result.usage == {"input_tokens": 21, "output_tokens": 34}
    assert result.image_paths == ["C:/tmp/real.png"]


def test_runner_terminates_real_subprocess_on_stdout_limit(
    tmp_path: Path,
    codex_config: CodexPluginConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    survived = tmp_path / "survived.txt"
    script = (
        "import pathlib,sys,time\n"
        "sys.stdin.buffer.read()\n"
        "for _ in range(80):\n"
        " sys.stdout.buffer.write(b'x'*1023+b'\\n')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stdout.close()\n"
        "sys.stderr.close()\n"
        "time.sleep(30)\n"
        f"pathlib.Path({str(survived)!r}).write_text('survived',encoding='utf-8')\n"
    )
    runner = CodexRunner(codex_config, tmp_path / "outputs")
    monkeypatch.setattr(
        runner,
        "_build_args",
        lambda *_args, **_kwargs: [sys.executable, "-u", "-c", script],
    )

    async def exercise_runner():
        job = _job()
        result = await asyncio.wait_for(
            runner.run(
                cwd=tmp_path,
                prompt="overflow stdout",
                thread_id=None,
                job=job,
            ),
            timeout=10,
        )
        return result, job

    result, job = _run_with_subprocess_loop(exercise_runner())

    assert result.output_limited is True
    assert "stdout exceeded" in result.final_text
    assert job.process is not None and job.process.returncode is not None
    assert not survived.exists()
