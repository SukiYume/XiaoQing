"""私聊扫码会话、音箱选择与登录任务生命周期。"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from dataclasses import dataclass, field
from typing import Any

from core.delivery import SENSITIVE_ACTION_KEY
from core.plugin_base import bounded_external_text, segments

from .auth import QRFlow, TokenManager
from .client import (
    BridgeError,
    directive_spec,
    fetch_spec,
    protect_directory,
    read_json,
    save_binding,
    services,
    speaker_candidates,
)

REGIONS = {"cn", "de", "i2", "ru", "sg", "us"}


def label(value: Any) -> str:
    return bounded_external_text(value, max_chars=80, max_bytes=320)


def previous_settings(context: Any) -> dict[str, Any]:
    try:
        return read_json(context.data_dir / "speaker.json")
    except (OSError, ValueError, BridgeError):
        return {}


def user_id(context: Any, event: dict[str, Any]) -> int:
    value = getattr(context, "current_user_id", None) or event.get("user_id")
    if type(value) is not int or value <= 0:
        raise BridgeError("请通过管理员私聊使用小爱登录功能。")
    return value


async def send_private(context: Any, recipient: int, message: list[dict[str, Any]]) -> bool | None:
    try:
        result = await context.send_action(
            {
                "action": "send_private_msg",
                "params": {"user_id": recipient, "message": message},
                SENSITIVE_ACTION_KEY: True,
            }
        )
        return result if result is True or result is False else None
    except Exception:
        return False


def qr_image(url: str) -> dict[str, Any]:
    try:
        import qrcode
    except ImportError as exc:
        raise BridgeError('扫码登录需要依赖，请运行 python -m pip install -e ".[xiaoai]"') from exc
    output = io.BytesIO()
    qrcode.make(url, box_size=6, border=4).save(output, format="PNG")
    return {
        "type": "image",
        "data": {"file": "base64://" + base64.b64encode(output.getvalue()).decode()},
    }


@dataclass
class PendingLogin:
    owner: int
    region: str
    flow: QRFlow
    expires_at: float
    task: asyncio.Task[Any] | None = None
    token: dict[str, Any] | None   = None
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    silent: bool = True

    def choices(self) -> str:
        lines = ["扫码授权已完成，请在 5 分钟内选择音箱："]
        lines += [
            f"{index}. {label(device.get('name'))}（{label(device.get('model'))}）"
            for index, (_, device) in enumerate(self.candidates, 1)
        ]
        lines.append("发送 /小爱 选择 1 完成绑定。")
        return "\n".join(lines)


class LoginRuntime:
    def __init__(self, directory: Any) -> None:
        self.manager                       = TokenManager(directory)
        self.pending: PendingLogin | None  = None
        self.tasks: set[asyncio.Task[Any]] = set()

    async def close(self) -> None:
        await self.cancel()
        remaining = list(self.tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        self.tasks.clear()

    async def cancel(self) -> None:
        pending, self.pending = self.pending, None
        if pending is not None:
            pending.done.set()
            if pending.task is not None:
                pending.task.cancel()
                await asyncio.gather(pending.task, return_exceptions=True)
            await pending.flow.close()
            pending.token = None
            pending.candidates.clear()

    async def start(self, context: Any, owner: int, region: str) -> str:
        if self.pending is not None and self.pending.owner != owner:
            return "另一位管理员正在登录，请等待该流程完成。"
        await self.cancel()
        old_silent = previous_settings(context).get("silent", True)
        silent     = old_silent if type(old_silent) is bool else True
        flow       = QRFlow()
        try:
            async with asyncio.timeout(35):
                await flow.start()
                picture = qr_image(flow.login_url)
                result  = await send_private(
                    context,
                    owner,
                    [
                        *segments(
                            "请用米家 App 扫描二维码并确认登录。二维码最多有效 5 分钟。\n"
                            "扫码后小青会私聊提示选择音箱；可发送 /小爱 取消登录 结束本次流程。",
                        ),
                        picture,
                    ],
                )
            if result is False:
                raise BridgeError("二维码发送失败，请稍后重新发送 /小爱 登录。")
        except BaseException:
            await flow.close()
            raise
        pending = PendingLogin(owner, region, flow, flow.expires_at, silent=silent)
        self.pending = pending
        pending.task = asyncio.create_task(self._watch(context, pending), name="xiaoai-qr-login")
        self.tasks.add(pending.task)
        pending.task.add_done_callback(self.tasks.discard)
        return (
            "二维码已发到你的私聊，请扫码确认。"
            if result is True
            else "二维码投递待确认，请查看私聊；本次登录流程已启动。"
        )

    async def _watch(self, context: Any, pending: PendingLogin) -> None:
        try:
            token = await pending.flow.wait()
            async with asyncio.timeout(40):
                miio, mina = services(context.http_session, token, pending.region)
                speakers = await mina.device_list()
                devices  = await miio.device_list("full")
            candidates = speaker_candidates(speakers or [], devices)
            if not candidates:
                raise BridgeError(
                    "扫码成功，当前地区暂未找到可关联音箱。可用 /小爱 登录 cn 指定账号地区。"
                )
            if len(candidates) > 30:
                raise BridgeError("音箱数量超过当前选择列表限制，请使用终端初始化。")
            pending.token      = token
            pending.candidates = candidates
            pending.expires_at = time.monotonic() + 300
            await pending.flow.close()
            await send_private(context, pending.owner, segments(pending.choices()))
            try:
                await asyncio.wait_for(pending.done.wait(), timeout=300)
            except TimeoutError:
                await send_private(
                    context, pending.owner, segments("音箱选择已过期，请重新发送 /小爱 登录。")
                )
        except BridgeError as exc:
            await send_private(context, pending.owner, segments(str(exc)))
        except Exception:
            await send_private(
                context, pending.owner, segments("本次扫码登录未完成，请稍后重新发送 /小爱 登录。")
            )
        finally:
            await pending.flow.close()
            if self.pending is pending:
                self.pending = None
            pending.token = None
            pending.candidates.clear()

    async def select(self, context: Any, owner: int, selection: str) -> str:
        pending = self.pending
        if pending is None or pending.expires_at <= time.monotonic():
            return "当前登录流程已结束，请发送 /小爱 登录。"
        if pending.owner != owner:
            return "该登录流程属于另一位管理员，请由发起人选择音箱。"
        if not pending.token:
            return "正在等待扫码确认，请先在米家 App 完成授权。"
        if (
            not selection.isascii()
            or not selection.isdecimal()
            or not 1 <= int(selection) <= len(pending.candidates)
        ):
            return pending.choices()
        speaker, device = pending.candidates[int(selection) - 1]
        # 复制令牌后再 await，超时清理会话时仍能完成过期判定。
        token     = dict(pending.token)
        directive = directive_spec(await fetch_spec(context.http_session, device["model"]))
        async with self.manager.lock:
            await asyncio.to_thread(protect_directory, context.data_dir)
            if self.pending is not pending or pending.expires_at <= time.monotonic():
                return "本次登录流程已过期，请重新发送 /小爱 登录。"
            profile = {
                "name": device["name"],
                "model": device["model"],
                "did": str(device["did"]),
                "mina_id": speaker["deviceID"],
                "region": pending.region,
                "silent": pending.silent,
                "directive": directive,
                "notify_user_id": owner,
            }
            save_binding(context.data_dir, token, profile)
            pending.done.set()
            self.pending = None
        return (
            f"已绑定 {label(device['name'])}，自动续期和登录失效私聊提醒已启用。可发送 /小爱 检查。"
        )


def runtime(context: Any) -> LoginRuntime:
    instance = context.state.get("xiaoai_runtime")
    if not isinstance(instance, LoginRuntime):
        instance                        = LoginRuntime(context.data_dir)
        context.state["xiaoai_runtime"] = instance
    return instance


async def command(context: Any, text: str, event: dict[str, Any]) -> str | None:
    verb, _, argument = text.partition(" ")
    if verb not in {"登录", "选择", "取消登录", "续期"}:
        return None
    owner    = user_id(context, event)
    instance = runtime(context)
    if verb == "登录":
        region = argument.strip()
        if not region:
            region = previous_settings(context).get("region", "cn")
        if region not in REGIONS:
            return "账号地区可选 cn、de、i2、ru、sg、us；例如 /小爱 登录 cn。"
        return await instance.start(context, owner, region)
    if verb == "选择":
        return await instance.select(context, owner, argument.strip())
    if verb == "取消登录":
        if instance.pending and instance.pending.owner != owner:
            return "该登录流程属于另一位管理员，请由发起人取消。"
        await instance.cancel()
        return "当前扫码登录流程已取消，已保存的音箱绑定继续保留。"
    for sid in ("xiaomiio", "micoapi"):
        await instance.manager.refresh(sid, instance.manager.load())
    return "米家与小爱服务令牌续期完成。"


def pending_status(context: Any, event: dict[str, Any]) -> str:
    pending = runtime(context).pending
    if pending is None:
        return ""
    if pending.owner != user_id(context, event):
        return "另一位管理员正在进行扫码登录。"
    if pending.token:
        return pending.choices()
    return f"正在等待扫码确认，剩余约 {max(0, int(pending.expires_at - time.monotonic()))} 秒。"
