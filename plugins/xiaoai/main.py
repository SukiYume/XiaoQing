"""管理员私聊小爱云端桥接；持久记录提交，避免重复设备动作。"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import Any

from core.delivery import DeliveryReceipt, ScheduledDelivery
from core.plugin_base import has_control_characters, segments

from . import home, login
from .auth import token_identity
from .client import AuthRequired, BridgeError, read_json, reserve_request, services, write_json
from .feeding import reject_unchecked_forwarding

HELP = """🏠 小爱 · 家庭助手
━━━━━━━━━━━━
💬 直接说需求
  小爱 打开台灯
  小爱 家里温湿度多少
  小爱 出一份粮
  小爱 天气

📋 查看设备与功能
  小爱 设备
  小爱 能力 台灯（也可以直接发“小爱 台灯”）

⚙ 登录与偏好：小爱 设置"""

SETTINGS_HELP = """⚙ 小爱 · 设置
━━━━━━━━━━━━
🔑 账号
  小爱 登录 · 扫码绑定
  小爱 状态 · 登录状态
  小爱 检查 · 连接检查
  小爱 续期 · 更新授权

🏠 偏好
  小爱 天气城市 北京
  小爱 份量 10 · 每份 10 克
  小爱 刷新设备

🔧 高级
  小爱 接口 <设备名>
  小爱 执行 <原话> · 音箱转发"""


def normalize(text: str) -> str:
    return re.sub(r"[\s，。！？,.!?]", "", text).casefold()


def matching_answer(
    messages: list[dict[str, Any]],
    text: str,
    since_ms: int,
    seen: set[str],
) -> str:
    for message in reversed(messages):
        request_id = message.get("request_id")
        stamp      = message.get("timestamp_ms")
        if (
            not request_id
            or request_id in seen
            or type(stamp) is not int
            or stamp < since_ms
            or stamp > int(time.time() * 1000) + 5000
        ):
            continue
        for answer in message.get("response", {}).get("answer", []):
            question, content = answer.get("question"), answer.get("content")
            if (
                isinstance(question, str)
                and normalize(question) == normalize(text)
                and isinstance(content, str)
                and content.strip()
            ):
                return content.strip()[:1500]
    return ""


async def execute(context: Any, text: str, event: dict[str, Any], *, check: bool = False) -> str:
    if not check:
        reject_unchecked_forwarding(text)
    profile = read_json(context.data_dir / "speaker.json")
    manager = login.runtime(context).manager
    token   = manager.load()
    if profile.get("account_id") and profile["account_id"] != str(token.get("userId")):
        raise BridgeError("账号与音箱绑定信息需要更新，请发送 /小爱 登录。")
    # 旧版终端配置在管理员首次使用时登记提醒接收人。
    if (
        not profile.get("notify_user_id")
        and type(event.get("user_id")) is int
        and event["user_id"] > 0
    ):
        profile["notify_user_id"] = event["user_id"]
        profile["account_id"]     = str(token["userId"])
        write_json(context.data_dir / "speaker.json", profile)
    token = await manager.ensure_fresh()
    io, mina = services(context.http_session, token, profile["region"], manager.refresh)
    # 操作前只读核验绑定的设备，名称变更保持 DID 绑定。
    devices = await io.device_list("full")
    device  = next((d for d in devices if str(d.get("did")) == profile["did"]), None)
    if device is None or device.get("model") != profile["model"]:
        raise BridgeError("绑定音箱的设备身份发生变化，请重新运行初始化。")
    if device.get("isOnline") is False:
        raise BridgeError("绑定的小爱音箱当前离线。")
    if check:
        return f"云端连接正常，已找到音箱：{profile['name']}（{profile['model']}）。"

    digest = hashlib.sha256(normalize(text).encode()).hexdigest()
    if not reserve_request(context, event, "requests.json", [digest]):
        return "本次请求已被重复保护拦截，请核对上次执行结果；相同文本间隔需满 20 秒。"
    seen: set[str] = set()
    baseline_ok    = False
    try:
        previous    = await mina.get_latest_ask(profile["mina_id"])
        seen        = {m["request_id"] for m in previous if m.get("request_id")}
        baseline_ok = True
    except AuthRequired:
        raise
    except Exception:
        pass
    since_ms = int(time.time() * 1000)
    spec     = profile["directive"]
    values   = [text if kind == "text" else profile.get("silent", True) for kind in spec["inputs"]]
    try:
        code = await io.miot_action(profile["did"], (spec["siid"], spec["aiid"]), values)
    except BridgeError as exc:
        return f"本次指令提交结果待确认。{exc}"
    except Exception:
        return "指令提交结果待确认。请核对设备实际状态；本次请求已保留重复保护记录。"
    if code != 0:
        return "小米云返回动作失败。请检查音箱状态和文字执行能力；本次请求已保留记录。"
    if baseline_ok:
        try:
            async with asyncio.timeout(6):
                for _ in range(4):
                    await asyncio.sleep(1)
                    messages = await mina.get_latest_ask(profile["mina_id"])
                    answer   = matching_answer(messages, text, since_ms, seen)
                    if answer:
                        return f"小爱回复：{answer}\n（音箱应答；设备实际状态以设备回报为准。）"
        except AuthRequired as exc:
            return f"指令已提交，小爱回答暂未取得。{exc}"
        except Exception:
            pass
    return "指令已提交给小爱。本次暂未取得可匹配的文字回答，设备执行结果待确认。"


async def handle(
    command: str, args: str, event: dict[str, Any], context: Any
) -> list[dict[str, Any]]:
    text = args.strip()
    if not text or text in {"help", "帮助"}:
        return segments(HELP)
    if text == "设置":
        summary = home.preference_summary(context)
        return segments(SETTINGS_HELP + ("\n\n已保存\n" + summary if summary else ""))
    limit = 6000 if text.startswith("调用 ") else 300
    if len(text) > limit or has_control_characters(text, include_c1=True):
        return segments(f"指令需为 1–{limit} 字的可显示文本。")
    if text == "状态":
        pending = login.pending_status(context, event)
        if pending:
            return segments(pending)
        ready = all((context.data_dir / name).exists() for name in ("token.json", "speaker.json"))
        if ready:
            manager = login.runtime(context).manager
            try:
                status = manager.status()
                if status.get("identity") == token_identity(manager.load()) and status.get(
                    "needs_login"
                ):
                    return segments(str(AuthRequired()))
            except Exception:
                return segments("本地配置读取失败，请发送 /小爱 登录 重新配置。")
        return segments(
            "本地配置已就绪，自动续期已启用；发送 /小爱 检查 核验云端。" if ready else HELP
        )
    if text == "执行":
        return segments(HELP)
    explicit = text.startswith("执行 ")
    if explicit:
        text = text[3:].strip()
    lock = context.state.setdefault("xiaoai_lock", asyncio.Lock())
    if lock.locked():
        return segments("上一条小爱请求正在处理，请稍后再试。")
    try:
        async with lock, asyncio.timeout(120):
            if not explicit:
                result = await login.command(context, text, event)
                if result is not None:
                    return segments(result)
            if explicit or text == "检查":
                return segments(
                    await execute(context, text, event, check=text == "检查" and not explicit)
                )
            return segments(await home.handle(context, text, event))
    except FileNotFoundError:
        return segments("请私聊发送 /小爱 登录，用米家 App 扫码完成授权。")
    except BridgeError as exc:
        return segments(str(exc))
    except Exception:
        return segments(
            "小爱请求未能完成。请核对设备状态，并运行 /小爱 检查；可发送 /小爱 登录 重新授权。"
        )


async def shutdown(context: Any) -> None:
    instance = context.state.get("xiaoai_runtime")
    if isinstance(instance, login.LoginRuntime):
        await instance.close()


async def scheduled_auth(context: Any) -> list[ScheduledDelivery]:
    """半小时只读检查并续期；明确需要重新授权时向绑定人发送一次提醒。"""
    instance = login.runtime(context)
    manager  = instance.manager
    lock     = context.state.setdefault("xiaoai_lock", asyncio.Lock())
    if lock.locked() or instance.pending:
        return []
    try:
        async with lock, asyncio.timeout(55):
            profile = read_json(context.data_dir / "speaker.json")
            token   = manager.load()
            if profile.get("account_id") and profile["account_id"] != str(token.get("userId")):
                return []
            try:
                token = await manager.ensure_fresh()
                io, mina = services(context.http_session, token, profile["region"], manager.refresh)
                await io.device_list("full")
                await mina.device_list()
            except AuthRequired:
                pass  # TokenManager 已持久记录需要重新授权的账号。
            status = manager.status()
            if (
                not status.get("needs_login")
                or status.get("notified_at")
                or status.get("identity") != token_identity(manager.load())
            ):
                return []
            recipient = profile.get("notify_user_id")
            if type(recipient) is not int or recipient <= 0:
                return []
            identity = status["identity"]

            def commit() -> None:
                latest = manager.status()
                if latest == status and token_identity(manager.load()) == identity:
                    latest["notified_at"] = time.time()
                    write_json(context.data_dir / "auth-state.json", latest)

            receipt = DeliveryReceipt(
                expected_actions=1, commit=commit, rollback=lambda: None, unknown=lambda: None
            )
            return [
                ScheduledDelivery.private(
                    recipient,
                    segments(
                        "小爱插件的登录授权已失效，自动续期需要重新授权。请私聊发送 /小爱 登录 扫码恢复。",
                    ),
                    receipt=receipt,
                )
            ]
    except Exception:
        # 网络波动保持现有授权，等待下一轮检查；异常正文可能包含上游凭据。
        return []
