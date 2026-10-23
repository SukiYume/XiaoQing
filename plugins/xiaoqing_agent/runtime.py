"""运行中任务的登记、后台执行、当轮交付或主动发送、停止与关闭收尾。"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from core.delivery import DeliveryReceipt, DeliverySegments

from . import receipts
from .config import AgentConfig
from .delivery import (
    SendReport,
    classify_files,
    deliver_outcome,
    outcome_message,
    upload_each,
)
from .loop import run_agent_loop
from .prompts import build_task_messages, capability_summary, configured_bot_name, current_time
from .shells import resolve_shell
from .tasks import (
    MAX_RECORDS_PER_CHAT,
    Actor,
    AgentTask,
    ChatRef,
    Outcome,
    TaskStore,
    describe_record,
    new_task_id,
    record_initiator,
)
from .tools import ToolEnv, build_tools

logger = logging.getLogger(__name__)

# 硬超时在任务预算之外再留一点收尾时间，只兜住卡死的模型或工具调用。
_HARD_TIMEOUT_GRACE         = 120.0
_STOP_WAIT_SECONDS          = 10.0
_UPLOAD_AFTER_REPLY_SECONDS = 1.5
_PLAN_MARKS                 = {"done": "✓", "in_progress": "▶", "pending": "·"}
_MAX_NOTE_CHARS             = 1000
_MAX_PENDING_NOTES          = 10
_MAX_AWAITING_REPORTS       = 200


@dataclass
class _Resend:
    """补发一次结果的交付状态：文字确认发出且附件都没失败才算 ``sent``。"""

    store: TaskStore
    chat_key: str
    task_id: str
    upload_failed: bool = False

    async def sent(self) -> None:
        if not self.upload_failed:
            await self.store.update_delivery(self.chat_key, self.task_id, "sent")

    async def failed(self) -> None:
        self.upload_failed = True
        await self.store.update_delivery(self.chat_key, self.task_id, "failed")

    def receipt(self) -> DeliveryReceipt:
        async def unknown() -> None:
            return None

        return DeliveryReceipt(
            expected_actions = 1,
            commit           = self.sent,
            rollback         = self.failed,
            unknown          = unknown,
        )


def _scene(chat: ChatRef) -> str:
    if chat.is_private:
        return "和发起人的私聊"
    return (
        f"QQ 群 {chat.group_id}，群里其他人都能看到回复。标着（仅私聊）的命令在这里不能用，"
        "需要它们时直接请发起人私聊再问；不要把发起人没要求公开的私人数据发到群里"
    )


class AgentRuntime:
    """一个插件代内共享的任务运行时；所有后台任务都由它创建并在关闭时收回。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir                             = data_dir
        self.store                                = TaskStore(data_dir)
        self.running: dict[str, AgentTask]        = {}
        self.accepting                            = True
        self._deliveries: set[asyncio.Task[None]] = set()
        # 所有还没结束的任务协程，包括已经做完、正在发送结果的；关闭时一起等。
        self._active: set[asyncio.Task[None]] = set()
        # 已经交给聊天侧作为当轮回复、还没收到发送回执的任务。
        self._awaiting_report: dict[str, AgentTask] = {}
        # 最近一轮聊天交来的人设；`/agent run` 这类不经过聊天的任务沿用它。
        self.persona = ""

    def workspace(self, chat: ChatRef) -> Path:
        return self.data_dir / "workspaces" / chat.key

    def running_in(self, chat: ChatRef) -> list[AgentTask]:
        """本会话运行中的任务；群里不分发起人，和工作目录、任务记录的范围一致。"""

        return [task for task in self.running.values() if task.chat.key == chat.key]

    async def recent_records(self, chat: ChatRef, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        records = await self.store.recent(chat.key, limit + len(self.running))
        running = set(self.running)
        return [item for item in records if item.get("task_id") not in running][-limit:]

    # ──────────────────── 启动与执行 ────────────────────

    def _admission(self, config: AgentConfig, chat: ChatRef, *, admin: bool) -> str:
        """名额按身份分开算：Bot 管理员的任务不和其他人的任务抢名额。"""

        pool = [task for task in self.running.values() if task.admin == admin]
        if admin:
            if len(pool) >= config.max_admin_tasks:
                return (
                    f"已经有 {len(pool)} 个任务在跑了，等它们做完，"
                    "或者用 /agent stop 停掉一个再来。"
                )
            return ""
        if config.max_member_tasks <= 0:
            return "现在只有 Bot 管理员能让我动手做事。"
        mine = sum(1 for task in pool if task.chat.user_id == chat.user_id)
        if mine >= config.max_tasks_per_member:
            return f"你已经有 {mine} 个任务在跑了，等它做完或者用 /agent stop 停掉再来。"
        if len(pool) >= config.max_member_tasks:
            return f"大家的任务排满了（{len(pool)} 个在跑），过一会儿再来。"
        return ""

    def start(
        self,
        context: Any,
        config: AgentConfig,
        chat: ChatRef,
        *,
        goal: str,
        request_text: str,
        source: str,
        sender: str                       = "",
        admin: bool                       = False,
        history: Sequence[dict[str, Any]] = (),
        images: Sequence[str]             = (),
        image_files: Sequence[str]        = (),
        quoted_text: str                  = "",
        persona: str                      = "",
    ) -> AgentTask | str:
        """登记并在后台启动任务；不能启动时返回给发起人看的原因。"""

        if not self.accepting:
            return "agent 正在关闭，暂时不能接新任务。"
        refused = self._admission(config, chat, admin=admin)
        if refused:
            return refused
        task_id = new_task_id()
        while task_id in self.running:
            task_id = new_task_id()
        task = AgentTask(
            task_id      = task_id,
            chat         = chat,
            goal         = goal.strip(),
            request_text = request_text.strip(),
            source       = source,
            quoted_text  = quoted_text.strip(),
            image_files  = list(image_files),
            sender       = sender.strip(),
            admin        = admin,
            persona      = persona or self.persona,
        )
        self.running[task_id] = task
        task.runner           = asyncio.create_task(
            self._run(context, config, task, list(history), list(images)),
            name=f"xiaoqing-agent:{task_id}",
        )
        self._active.add(task.runner)
        task.runner.add_done_callback(partial(self._forget, task))
        logger.info(
            "xiaoqing_agent task started task=%s chat=%s source=%s goal_chars=%d",
            task_id,
            chat.key,
            source,
            len(task.goal),
        )
        return task

    async def _prepare(
        self,
        context: Any,
        config: AgentConfig,
        task: AgentTask,
        history: list[dict[str, Any]],
        images: list[str],
    ) -> tuple[ToolEnv, dict[str, Any], list[dict[str, Any]]]:
        workspace = self.workspace(task.chat)
        out_dir   = workspace / "out" / task.task_id
        await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
        shell = resolve_shell(config.shell)
        env   = ToolEnv(
            context       = context,
            config        = config,
            task          = task,
            workspace     = workspace,
            out_dir       = out_dir,
            python        = config.python or sys.executable,
            shell         = shell,
            deadline      = time.monotonic() + config.task_timeout_seconds,
            send_progress = partial(self._send_progress, context, task),
        )
        tools = build_tools(env, loaded_plugins=frozenset(context.list_plugins()))
        recent = await self.recent_records(task.chat, config.history_tasks)
        now, zone = current_time(context)
        messages = build_task_messages(
            bot_name     = configured_bot_name(context),
            sender       = task.sender,
            task         = task,
            now          = now,
            timezone     = zone,
            scene        = _scene(task.chat),
            python       = env.python,
            shell        = shell.label,
            workspace    = str(workspace),
            out_dir      = str(out_dir),
            tool_names   = tuple(tools),
            history      = history,
            recent       = recent,
            images       = images,
            capabilities = capability_summary(
                context.get_command_catalog(),
                getattr(context, "principal", None),
            ),
        )
        return env, tools, messages

    async def _run(
        self,
        context: Any,
        config: AgentConfig,
        task: AgentTask,
        history: list[dict[str, Any]],
        images: list[str],
    ) -> None:
        outcome: Outcome | None          = None
        transcript: list[dict[str, Any]] = []
        try:
            await self.store.save(task)
            env, tools, messages = await self._prepare(context, config, task, history, images)
            # 执行循环会压缩较早的输出；执行记录另存一份完整的模型与工具往返。
            transcript = list(messages)
            async with asyncio.timeout(config.task_timeout_seconds + _HARD_TIMEOUT_GRACE):
                outcome = await run_agent_loop(env, tools, messages, transcript=transcript)
            task.status = "done" if outcome.status == "done" else "failed"
        except asyncio.CancelledError:
            task.status = "cancelled" if self.accepting else "interrupted"
            self._resolve_waiter(task, Outcome("这个任务已经停下了。", status=task.status))
            # 停下的任务不会再发附件：命令为发送做的预留（比如取图）现在就释放。
            await asyncio.shield(receipts.release(task.held_receipts))
            raise
        except TimeoutError:
            task.status = "failed"
            task.error  = "任务超时"
            outcome = Outcome("这件事超时了，没能做完。", status="failed")
        except Exception as exc:
            logger.exception("xiaoqing_agent task crashed task=%s", task.task_id)
            task.status = "failed"
            task.error  = f"{type(exc).__name__}: {str(exc)[:300]}"
            outcome = Outcome("执行时出了点问题，任务没能做完。", status="failed")
        finally:
            task.finished_at = time.time()
            task.outcome     = outcome
            task.activity    = "已结束"
            self.running.pop(task.task_id, None)
            await asyncio.shield(self._persist(task, transcript))
            logger.info(
                "xiaoqing_agent task finished task=%s status=%s steps=%d elapsed=%.1fs",
                task.task_id,
                task.status,
                task.steps,
                task.elapsed,
            )
        if outcome is not None:
            if task.notes:
                # 补充要求赶在最后一步之后才到：如实说没用上，不能当作已经照做。
                missed       = "；".join(task.notes)[:200]
                outcome.text = (
                    f"{outcome.text}\n（补充的要求“{missed}”送到时任务已经做完，没来得及用上，"
                    "需要的话再说一次。）"
                )
                task.notes.clear()
            await self._hand_over(context, task, outcome)

    def _forget(self, task: AgentTask, runner: asyncio.Task[None]) -> None:
        """任务协程结束后总是注销；还没开始就被取消的任务不会进入 ``_run`` 的收尾。"""

        self._active.discard(runner)
        self.running.pop(task.task_id, None)
        if task.status == "running":
            task.status      = "cancelled" if self.accepting else "interrupted"
            task.finished_at = time.time()
            self._resolve_waiter(task, Outcome("这个任务已经停下了。", status=task.status))

    async def _persist(self, task: AgentTask, messages: list[dict[str, Any]]) -> None:
        try:
            await self.store.save(task)
            if messages:
                await self.store.save_transcript(task, messages)
        except Exception as exc:
            logger.warning(
                "xiaoqing_agent task persistence failed task=%s error_type=%s",
                task.task_id,
                type(exc).__name__,
            )

    # ──────────────────── 交付 ────────────────────

    @staticmethod
    def _resolve_waiter(task: AgentTask, outcome: Outcome) -> bool:
        waiter = task.inline_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(outcome)
            return True
        return False

    async def _hand_over(self, context: Any, task: AgentTask, outcome: Outcome) -> None:
        """当轮还有人在等就交给它作为回复，否则主动发回原会话。"""

        if self._resolve_waiter(task, outcome):
            return
        await self._deliver(context, task, outcome)

    async def _deliver(self, context: Any, task: AgentTask, outcome: Outcome) -> None:
        # 群里结果可能隔很久才到、中间还有别人的任务：@ 发起人，说清是给谁的。
        report = await deliver_outcome(context, task.chat, outcome, mention=True)
        await self._settle_report(task, report)
        await receipts.release(task.held_receipts)
        task.delivery = "sent" if report.delivered else "failed"
        if not report.delivered:
            logger.warning("xiaoqing_agent result delivery failed task=%s", task.task_id)
        await self._persist(task, [])

    @staticmethod
    async def _settle_report(task: AgentTask, report: SendReport) -> None:
        """附件按各自那次发送的结果结算命令回执：内嵌图片随文字消息，其余附件各自一份。"""

        await receipts.settle(task.held_receipts, report.images, report.message)
        for path, sent in report.uploads:
            await receipts.settle(task.held_receipts, [path], sent)

    def _track(self, coroutine: Coroutine[Any, Any, None]) -> None:
        delivery = asyncio.create_task(coroutine)
        self._deliveries.add(delivery)
        delivery.add_done_callback(self._deliveries.discard)

    async def _finish_inline(self, context: Any, task: AgentTask, uploads: list[Path]) -> None:
        if uploads:
            # 当轮回复由调用方返回后才发出；稍等一下再上传，文件才会排在说明文字后面。
            await asyncio.sleep(_UPLOAD_AFTER_REPLY_SECONDS)
            for path, sent in await upload_each(context, task.chat, uploads):
                await receipts.settle(task.held_receipts, [path], sent)
                if sent is False:
                    task.delivery = "failed"
        await self._persist(task, [])

    async def wait_inline(self, context: Any, task: AgentTask, seconds: float) -> Outcome | None:
        """最多等 ``seconds`` 秒；做完就把结果交给调用方，否则之后由任务自己发送。"""

        if seconds <= 0 or task.runner is None or task.runner.done():
            return None
        waiter: asyncio.Future[Outcome] = asyncio.get_running_loop().create_future()
        task.inline_waiter              = waiter
        returned                        = False
        try:
            async with asyncio.timeout(seconds):
                outcome = await asyncio.shield(waiter)
            returned = True
            return outcome
        except TimeoutError:
            if waiter.done() and not waiter.cancelled():
                returned = True
                return waiter.result()
            return None
        finally:
            task.inline_waiter = None
            if not returned:
                if waiter.done() and not waiter.cancelled():
                    # 结果已经交过来、但等待方被取消：改为主动发送，不能把结果丢掉。
                    self._track(self._deliver(context, task, waiter.result()))
                else:
                    waiter.cancel()

    async def inline_reply(
        self,
        context: Any,
        task: AgentTask,
        outcome: Outcome,
    ) -> list[dict[str, Any]]:
        """当轮回复：文字和图片作为返回段，其他文件随后另外上传。"""

        message, uploads = outcome_message(outcome)
        images, _, _ = classify_files(outcome.files)
        task.delivery     = "inline"
        task.inline_files = [str(path) for path in images]
        # 不随这次回复发出的命令附件不会再发了，它们的回执现在就释放。
        await receipts.release(task.held_receipts, keep=[*images, *uploads])
        # 当轮回复由聊天侧发出，发没发出去要等它回报（report_delivery）。
        self._awaiting_report[task.task_id] = task
        while len(self._awaiting_report) > _MAX_AWAITING_REPORTS:
            dropped = self._awaiting_report.pop(next(iter(self._awaiting_report)))
            await receipts.settle(dropped.held_receipts, dropped.inline_files, None)
        self._track(self._finish_inline(context, task, uploads))
        return message

    def command_reply(
        self,
        task: AgentTask,
        message: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """``/agent run`` 直接返回的当轮结果没有聊天侧回报，改由 Core 的发送回执回报。"""

        if task.task_id not in self._awaiting_report:
            return message
        report = partial(self.report_delivery, task.task_id)
        return DeliverySegments(
            message,
            DeliveryReceipt(
                expected_actions = 1,
                commit           = partial(report, True),
                rollback         = partial(report, False),
                unknown          = partial(report, None),
            ),
        )

    async def report_delivery(self, task_id: str, delivered: bool | None) -> None:
        """当轮回复的发送结果（聊天侧回报或命令回复的回执）；结果未知时保持 ``inline``。"""

        task = self._awaiting_report.pop(task_id, None)
        if task is None:
            return
        # 回复里内嵌的命令图片和回复一起发出，按同一个结果结算。
        await receipts.settle(task.held_receipts, task.inline_files, delivered)
        if delivered is None:
            return
        if delivered is False:
            logger.warning("xiaoqing_agent inline reply was not delivered task=%s", task_id)
            task.delivery = "failed"
        elif task.delivery != "failed":
            task.delivery = "sent"
        await self._persist(task, [])

    async def _send_progress(
        self,
        context: Any,
        task: AgentTask,
        text: str,
        files: list[str],
    ) -> bool:
        report = await deliver_outcome(context, task.chat, Outcome(text, files))
        await self._settle_report(task, report)
        return report.message is not False

    # ──────────────────── 查询与停止 ────────────────────

    def _find(self, chat: ChatRef, task_id: str, actor: Actor) -> AgentTask | None:
        """按 ID 找运行中的任务；其他会话的任务只有 Bot 管理员能指名查看或操作。"""

        task = self.running.get(task_id)
        if task is None or (task.chat.key != chat.key and not actor.admin):
            return None
        return task

    def select(self, chat: ChatRef, task_id: str, actor: Actor) -> list[AgentTask] | str:
        """选出 ``actor`` 要停止或追加要求的任务；找不到、无权或有歧义时返回说明。

        只有发起人和 Bot 管理员能动一个任务。``all`` 是本会话里 ``actor`` 能动的全部任务；
        不带 ID 时只有恰好一个才选中它，多个时要指明，不会扩大成全部。
        """

        task_id = task_id.strip()
        if task_id and task_id.casefold() != "all":
            task = self._find(chat, task_id, actor)
            if task is None:
                return f"没有找到运行中的任务 {task_id}。"
            if not actor.manages(task):
                return f"任务 {task_id} 不是你发起的，只有发起人或 Bot 管理员能停止或补充它。"
            return [task]
        running = self.running_in(chat)
        tasks   = [task for task in running if actor.manages(task)]
        if not tasks and running:
            return "现在在跑的任务不是你发起的，只有发起人或 Bot 管理员能停止或补充它。"
        if task_id or len(tasks) <= 1:
            return tasks
        listing = "\n".join(f"[{task.task_id}] {task.goal[:60]}" for task in tasks)
        return (
            f"现在有 {len(tasks)} 个任务在跑：\n{listing}\n"
            "请指明是哪一个（任务ID），全部的话说 all。"
        )

    async def stop(self, chat: ChatRef, task_id: str, actor: Actor) -> list[AgentTask] | str:
        targets = self.select(chat, task_id, actor)
        if isinstance(targets, str):
            return targets
        runners = [task.runner for task in targets if task.runner is not None]
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.wait(runners, timeout=_STOP_WAIT_SECONDS)
        return targets

    def add_note(
        self,
        chat: ChatRef,
        task_id: str,
        note: str,
        actor: Actor,
        sender: str = "",
    ) -> str:
        """把追加要求交给运行中的任务，下一步开始前生效；只接受发起人和 Bot 管理员的补充。"""

        targets = self.select(chat, task_id, actor)
        if isinstance(targets, str):
            return targets
        if not targets:
            return "现在没有在跑的任务。"
        if len(targets) != 1:
            return "追加要求只能交给一个任务，请指明任务ID。"
        task = targets[0]
        text = note.strip()[:_MAX_NOTE_CHARS]
        if not text:
            return "要补充什么？"
        if len(task.notes) >= _MAX_PENDING_NOTES:
            return f"任务 {task.task_id} 还有好几条补充没处理，等它跟上再说。"
        task.notes.append(f"{sender}：{text}" if sender and not chat.is_private else text)
        return f"好，已经告诉任务 {task.task_id} 了，它下一步就会按这个调整。"

    def status_text(self, chat: ChatRef, task_id: str, actor: Actor) -> str:
        """本会话运行中的任务谁都能看；指定 ID 查其他会话的任务只给 Bot 管理员。"""

        wanted = task_id.strip()
        if wanted and wanted.casefold() != "all":
            found = self._find(chat, wanted, actor)
            if found is None:
                return f"没有找到运行中的任务 {wanted}。"
            tasks = [found]
        else:
            tasks = self.running_in(chat)
        if not tasks:
            return "没有正在运行的任务。"
        blocks: list[str] = []
        for task in tasks:
            initiator = record_initiator(task.record())
            lines     = [
                f"[{task.task_id}] {task.goal[:100]}"
                + (f"（{initiator} 发起）" if initiator else ""),
                f"已运行 {task.elapsed:.0f} 秒，第 {task.steps} 步，{task.activity}",
            ]
            lines += [
                f"  {_PLAN_MARKS.get(item['status'], '·')} {item['step']}" for item in task.plan
            ]
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    async def history_text(self, chat: ChatRef, limit: int = 10) -> str:
        records = await self.store.recent(chat.key, limit)
        if not records:
            return "这个会话还没有 agent 任务记录。"
        return "\n".join(
            describe_record(item, result_chars=80, full_paths=False) for item in reversed(records)
        )

    async def result_message(
        self,
        context: Any,
        chat: ChatRef,
        task_id: str,
    ) -> list[dict[str, Any]] | str:
        """重新取出本会话一个已结束任务的结果，作为回复发出；附件随后单独发送。"""

        if task_id in self.running:
            return f"任务 {task_id} 还在运行，用 /agent status {task_id} 看进度。"
        records = await self.store.recent(chat.key, MAX_RECORDS_PER_CHAT)
        record  = next((item for item in records if item.get("task_id") == task_id), None)
        if record is None:
            return f"这个会话里没有任务 {task_id} 的记录。"
        text = str(record.get("result") or "").strip()
        if not text:
            error  = str(record.get("error") or "").strip()
            detail = f"：{error}" if error else ""
            return f"任务 {task_id} 没有结果（{record.get('status')}{detail}）。"
        files = [item for item in record.get("files") or [] if isinstance(item, str)]
        message, uploads = outcome_message(Outcome(text, files))
        resend = _Resend(self.store, chat.key, task_id)
        if uploads:
            self._track(self._upload_resent(context, chat, uploads, resend))
        return DeliverySegments(message, resend.receipt())

    async def _upload_resent(
        self,
        context: Any,
        chat: ChatRef,
        uploads: list[Path],
        resend: _Resend,
    ) -> None:
        await asyncio.sleep(_UPLOAD_AFTER_REPLY_SECONDS)
        if any(sent is False for _, sent in await upload_each(context, chat, uploads)):
            await resend.failed()

    # ──────────────────── 生命周期 ────────────────────

    async def shutdown(self) -> None:
        """停下还在执行的任务；已经做完、正在发送结果的任务给一点时间发完。"""

        self.accepting = False
        runners        = [task.runner for task in self.running.values() if task.runner is not None]
        for runner in runners:
            runner.cancel()
        pending = [*self._active, *self._deliveries]
        if not pending:
            return
        _done, stuck = await asyncio.wait(pending, timeout=_STOP_WAIT_SECONDS)
        for leftover in stuck:
            leftover.cancel()
        if stuck:
            logger.warning("xiaoqing_agent shutdown left %d task(s) still unwinding", len(stuck))
