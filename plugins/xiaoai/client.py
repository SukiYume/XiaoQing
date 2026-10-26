"""有界云端请求、型号能力发现和本地凭据存储。"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from core.bounded_http import (
    BodyLimits,
    HttpStatusError,
    JsonLimits,
    aiohttp_request_bounded,
    parse_bounded_json,
)


class BridgeError(Exception):
    """可直接向用户展示的固定错误。"""


class AuthRequired(BridgeError):
    """小米明确要求重新授权。"""

    def __init__(self) -> None:
        super().__init__("小米登录已失效，请私聊发送 /小爱 登录 扫码重新授权。")


Refresh   = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
SPEC_ROOT = "https://miot-spec.org/miot-spec-v2/"


def read_json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 128_000:
        raise BridgeError("本地配置超过大小限制，请重新运行初始化。")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BridgeError("本地配置格式异常，请重新运行初始化。")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    fd        = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def protect_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        identity = subprocess.check_output(["whoami"], text=True).strip()
        subprocess.run(
            ["icacls", str(directory), "/inheritance:r", "/grant:r", f"{identity}:(OI)(CI)F"],
            check  = True,
            stdout = subprocess.DEVNULL,
            stderr = subprocess.DEVNULL,
        )
    else:
        directory.chmod(0o700)


def speaker_candidates(
    speakers: list[dict[str, Any]], devices: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (speaker, device)
        for speaker in speakers
        for device in devices
        if speaker.get("miotDID") is not None and str(speaker["miotDID"]) == str(device.get("did"))
    ]


def save_binding(directory: Path, token: dict[str, Any], profile: dict[str, Any]) -> None:
    """Write the account-bound profile before its token so partial replacement fails closed."""
    profile  = profile | {"account_id": str(token["userId"])}
    old_path = directory / "speaker.json"
    if "notify_user_id" not in profile and old_path.exists():
        old = read_json(old_path)
        if old.get("account_id") == profile["account_id"] and old.get("notify_user_id"):
            profile["notify_user_id"] = old["notify_user_id"]
    write_json(old_path, profile)
    write_json(directory / "token.json", token)
    write_json(directory / "auth-state.json", {})


def reserve_request(
    context: Any, event: dict[str, Any], journal_name: str, digests: list[str]
) -> bool:
    """Persist duplicate guards before a mutation; empty read-only plans need no journal."""
    if not digests:
        return True
    path    = context.data_dir / journal_name
    journal = read_json(path) if path.exists() else {}
    now     = time.time()
    journal = {k: v for k, v in journal.items() if type(v) in {int, float} and 0 <= now - v < 86400}
    message = event.get("message_id") or getattr(context, "request_id", None)
    key     = f"message:{event.get('user_id')}:{message}" if message is not None else ""
    if (key and key in journal) or any(now - journal.get(d, 0) < 20 for d in digests):
        return False
    if len(journal.keys() | set(digests) | ({key} if key else set())) > 1000:
        raise BridgeError("设备动作记录已达上限，请稍后再试。")
    journal.update(dict.fromkeys(digests, now))
    if key:
        journal[key] = now
    write_json(path, journal)
    return True


async def request_json(session: Any, method: str, url: str, **kwargs: Any) -> Any:
    response = await aiohttp_request_bounded(
        session,
        method,
        url,
        limits=BodyLimits(max_wire_bytes=8_000_000, max_decoded_bytes=16_000_000),
        headers=kwargs.pop("headers", None),
        request_kwargs={"timeout": aiohttp.ClientTimeout(total=15), **kwargs},
    )
    return parse_bounded_json(
        response,
        limits=JsonLimits(max_bytes=16_000_000, max_nodes=2_000_000, max_string_chars=12_000_000),
    )


class TokenAccount:
    """授权失败时续期；只读接口可重试一次，设备动作保持单次提交。"""

    def __init__(self, session: Any, token: dict[str, Any], refresh: Refresh | None = None):
        self.session = session
        self.token   = token
        self.refresh = refresh

    async def mi_request(
        self,
        sid: str,
        url: str,
        data: Any,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        read_only = urlsplit(url).path in {
            "/app/home/device_list",
            "/admin/v2/device_list",
            "/app/miotspec/prop/get",
        }
        if urlsplit(url).path == "/remote/ubus" and isinstance(data, dict):
            read_only = data.get("method") == "nlp_result_get" and data.get("path") == "mibrain"
        try:
            return await self._request_once(sid, url, data, headers)
        except AuthRequired:
            if self.refresh is None:
                raise
            self.token = await self.refresh(sid, self.token)
            if not read_only:
                raise BridgeError(
                    "登录凭据已续期；本次设备动作保持单次提交，请先核对设备状态。"
                ) from None
            try:
                return await self._request_once(sid, url, data, headers)
            except AuthRequired:
                raise BridgeError(
                    "服务令牌已续期，云端暂时仍拒绝访问，请稍后运行 /小爱 检查。"
                ) from None

    async def _request_once(
        self,
        sid: str,
        url: str,
        data: Any,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        parsed = urlsplit(url)
        host   = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or host
            not in {
                "api2.mina.mi.com",
                "api.io.mi.com",
                "de.api.io.mi.com",
                "i2.api.io.mi.com",
                "ru.api.io.mi.com",
                "sg.api.io.mi.com",
                "us.api.io.mi.com",
            }
        ):
            raise BridgeError("云端地址校验失败。")
        service = self.token.get(sid)
        if not isinstance(service, (list, tuple)) or len(service) != 2:
            raise AuthRequired()
        cookies = {"userId": str(self.token["userId"]), "serviceToken": service[1]}
        content = data(self.token, cookies) if callable(data) else data
        try:
            result = await request_json(
                self.session,
                "GET" if data is None else "POST",
                url,
                data    = content,
                cookies = cookies,
                headers = headers,
            )
        except HttpStatusError as exc:
            if exc.status == 401:
                raise AuthRequired() from None
            raise
        if isinstance(result, dict):
            message = str(result.get("message", "")).lower()
            if (
                result.get("code") in {401, -3, 2, 3}
                or "auth err" in message
                or message in {"invalid signature", "servicetoken_expired", "未登录"}
            ):
                raise AuthRequired()
        if not isinstance(result, dict) or result.get("code") != 0:
            raise BridgeError("小米云请求失败，请稍后运行 /小爱 检查；可通过 /小爱 登录 重新授权。")
        return result


def services(
    session: Any,
    token: dict[str, Any],
    region: str,
    refresh: Refresh | None = None,
) -> tuple[Any, Any]:
    try:
        from miservice import MiIOService, MiNAService
    except ImportError as exc:
        raise BridgeError('请先在小青环境安装依赖：python -m pip install -e ".[xiaoai]"') from exc
    account = TokenAccount(session, token, refresh)
    return MiIOService(account, region), MiNAService(account)


def directive_spec(spec: dict[str, Any]) -> dict[str, Any]:
    for service in spec.get("services", []):
        properties = {p["iid"]: p for p in service.get("properties", [])}
        for action in service.get("actions", []):
            if ":action:execute-text-directive:" not in action.get("type", ""):
                continue
            inputs = []
            for iid in action.get("in", []):
                prop = properties.get(iid, {})
                kind = prop.get("type", "")
                if ":property:text-content:" in kind and prop.get("format") == "string":
                    inputs.append("text")
                elif ":property:silent-execution:" in kind and prop.get("format") == "bool":
                    inputs.append("silent")
                else:
                    raise BridgeError("该音箱的文字执行参数需要适配，初始化已停止。")
            if inputs.count("text") != 1:
                raise BridgeError("该音箱的文字执行规格缺少唯一文本参数。")
            return {"siid": service["iid"], "aiid": action["iid"], "inputs": inputs}
    raise BridgeError("该型号公开规格中缺少执行文本动作。")


async def fetch_spec(
    session: Any, model: str, instances: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Fetch the latest model specification, reusing a discovery batch's model index."""
    if instances is None:
        index     = await request_json(session, "GET", SPEC_ROOT + "instances?status=all")
        instances = index.get("instances", [])
    matches = [item for item in instances if item.get("model") == model]
    if not matches:
        raise BridgeError("型号暂无公开规格。")
    chosen = max(matches, key=lambda item: item.get("version", 0))
    spec = await request_json(
        session,
        "GET",
        SPEC_ROOT + "instance",
        params={"type": chosen["type"]},
    )
    return {"type": chosen["type"], **spec}
