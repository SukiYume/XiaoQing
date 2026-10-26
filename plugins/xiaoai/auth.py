"""小米扫码与 passToken 续期；认证网络使用独立 Cookie 边界。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp

from core.bounded_http import (
    BodyLimits,
    JsonLimits,
    ResponseTransportError,
    parse_bounded_json,
    read_aiohttp_response,
)

from .client import AuthRequired, BridgeError, read_json, write_json

ACCOUNT     = "https://account.xiaomi.com"
USER_AGENT  = "APP/com.xiaomi.mihome APPV/11.3.203"
SERVICES    = ("xiaomiio", "micoapi")
RENEW_AFTER = 6 * 60 * 60


def account_url(url: str) -> str:
    parsed = urlsplit(url)
    host   = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or not (host == "account.xiaomi.com" or host.endswith(".account.xiaomi.com"))
    ):
        raise BridgeError("小米登录地址校验失败，请重新发起登录。")
    return url


def callback_url(url: str, sid: str) -> str:
    parsed   = urlsplit(url)
    expected = "sts.api.io.mi.com" if sid == "xiaomiio" else "api2.mina.mi.com"
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.path != "/sts"
    ):
        raise BridgeError("小米授权回调地址校验失败。")
    return url


def _json(body: bytes) -> dict[str, Any]:
    value = parse_bounded_json(
        body.removeprefix(b"&&&START&&&"), limits=JsonLimits(max_bytes=256_000)
    )
    if not isinstance(value, dict):
        raise BridgeError("小米登录响应格式异常，请稍后重试。")
    return value


async def auth_request(
    session: Any,
    url: str,
    *,
    cookies: dict[str, str] | None = None,
    params: dict[str, Any] | None  = None,
    timeout: float                 = 15,
) -> tuple[int, dict[str, str], dict[str, str], bytes]:
    # 调用方逐跳验证地址。禁用重定向和自动解压，限制认证响应为 256 KB。
    async with session.get(
        url,
        params  = params,
        cookies = cookies or {},
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
        timeout=aiohttp.ClientTimeout(total=timeout),
        allow_redirects = False,
        auto_decompress = False,
    ) as response:
        bounded = await read_aiohttp_response(
            response,
            limits=BodyLimits(max_wire_bytes=256_000, max_decoded_bytes=256_000),
        )
        return (
            response.status,
            {key.lower(): value for key, value in response.headers.items()},
            {key: value.value for key, value in response.cookies.items()},
            bounded.body,
        )


async def account_json(session: Any, url: str, **kwargs: Any) -> dict[str, Any]:
    status, _, _, body = await auth_request(session, account_url(url), **kwargs)
    if status == 401:
        raise AuthRequired()
    if status != 200:
        raise BridgeError("小米登录服务暂时不可用，请稍后重试。")
    return _json(body)


async def exchange(session: Any, response: dict[str, Any], sid: str) -> list[str]:
    location = callback_url(str(response.get("location", "")), sid)
    security = response.get("ssecurity")
    if not isinstance(security, str) or not security:
        raise BridgeError("小米登录响应缺少服务凭据。")
    if "nonce" in response:
        sign = base64.b64encode(
            hashlib.sha1(
                f"nonce={response['nonce']}&{security}".encode(),
            ).digest()
        ).decode()
        parsed              = urlsplit(location)
        query               = dict(parse_qsl(parsed.query))
        query["clientSign"] = sign
        location = urlunsplit(parsed._replace(query=urlencode(query)))
    for _ in range(4):
        status, headers, cookies, _ = await auth_request(session, callback_url(location, sid))
        if status in {200, 302, 303} and cookies.get("serviceToken"):
            return [security, cookies["serviceToken"]]
        if status in {301, 302, 303, 307, 308}:
            location = callback_url(urljoin(location, headers.get("location", "")), sid)
            continue
        if status == 401:
            raise AuthRequired()
        raise BridgeError("小米服务令牌交换未完成，请稍后重试。")
    raise BridgeError("小米授权回调跳转超过限制。")


async def refresh_service(session: Any, token: dict[str, Any], sid: str) -> dict[str, Any]:
    if sid not in SERVICES:
        raise BridgeError("小米服务类型校验失败。")
    if not all(token.get(key) for key in ("passToken", "userId", "deviceId")):
        raise AuthRequired()
    response = await account_json(
        session,
        ACCOUNT + "/pass/serviceLogin",
        params  = {"sid": sid, "_json": "true"},
        cookies = {
            "userId": str(token["userId"]),
            "passToken": token["passToken"],
            "deviceId": token["deviceId"],
            "sdkVersion": "3.9",
        },
    )
    if response.get("notificationUrl") or response.get("code") in {70016, 70001, 70002}:
        raise AuthRequired()
    if response.get("code") != 0:
        raise BridgeError("小米暂时无法续期登录，请稍后重试。")
    if str(response.get("userId", "")) != str(token["userId"]):
        raise AuthRequired()
    renewed      = deepcopy(token)
    renewed[sid] = await exchange(session, response, sid)
    if response.get("passToken"):
        renewed["passToken"] = response["passToken"]
    renewed.setdefault("_refreshed_at", {})[sid] = time.time()
    return renewed


def session_factory() -> aiohttp.ClientSession:
    # 只通过显式 cookies 传递账号凭据，避免其他插件或账号污染会话。
    return aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())


class QRFlow:
    def __init__(self) -> None:
        self.session: Any = None
        self.device_id    = secrets.token_hex(8).upper()
        self.poll_url     = ""
        self.login_url    = ""
        self.expires_at   = 0.0

    async def start(self) -> None:
        self.session = session_factory()
        try:
            initial = await account_json(
                self.session,
                ACCOUNT + "/pass/serviceLogin",
                params  = {"sid": "xiaomiio", "_json": "true"},
                cookies = {"deviceId": self.device_id, "sdkVersion": "3.9"},
            )
            callback_url(str(initial.get("callback", "")), "xiaomiio")
            params = {key: initial.get(key, "") for key in ("qs", "callback", "serviceParam")}
            params.update(sid="xiaomiio", _qrsize="400", _hasLogo="false", _locale="zh_CN")
            result = await account_json(
                self.session,
                ACCOUNT + "/longPolling/loginUrl",
                params  = params,
                cookies = {"deviceId": self.device_id},
            )
            if result.get("code") != 0:
                raise BridgeError("二维码生成失败，请稍后重新发送 /小爱 登录。")
            self.login_url = account_url(str(result.get("loginUrl", "")))
            self.poll_url  = account_url(str(result.get("lp", "")))
            lifetime       = result.get("timeout", 300)
            if type(lifetime) not in (int, float) or lifetime <= 0:
                raise BridgeError("二维码有效期响应异常。")
            self.expires_at = time.monotonic() + min(lifetime, 300)
        except BaseException:
            await self.close()
            raise

    async def wait(self) -> dict[str, Any]:
        while time.monotonic() < self.expires_at:
            try:
                result = await account_json(
                    self.session,
                    self.poll_url,
                    timeout = min(25, self.expires_at - time.monotonic()),
                    cookies = {"deviceId": self.device_id},
                )
            except TimeoutError:
                continue
            except ResponseTransportError as exc:
                if isinstance(exc.__cause__, TimeoutError):
                    continue
                raise
            if result.get("code") != 0:
                raise BridgeError("二维码已失效或授权已取消，请重新发送 /小爱 登录。")
            if not result.get("passToken") or not result.get("userId"):
                await asyncio.sleep(1)
                continue
            token = {
                "deviceId": self.device_id,
                "userId": str(result["userId"]),
                "passToken": result["passToken"],
            }
            token["xiaomiio"]      = await exchange(self.session, result, "xiaomiio")
            token["_refreshed_at"] = {"xiaomiio": time.time()}
            return await refresh_service(self.session, token, "micoapi")
        raise BridgeError("二维码已过期，请重新发送 /小爱 登录。")

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None
        self.poll_url  = ""
        self.login_url = ""


def token_identity(token: dict[str, Any]) -> str:
    value = str(token.get("userId", "")) + ":" + str(token.get("passToken", ""))
    return hashlib.sha256(value.encode()).hexdigest()


class TokenManager:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lock      = asyncio.Lock()

    def load(self) -> dict[str, Any]:
        return read_json(self.directory / "token.json")

    def status(self) -> dict[str, Any]:
        path = self.directory / "auth-state.json"
        return read_json(path) if path.exists() else {}

    def mark_required(self, token: dict[str, Any]) -> None:
        current  = self.status()
        identity = token_identity(token)
        if current.get("identity") == identity and current.get("needs_login"):
            return
        write_json(
            self.directory / "auth-state.json",
            {
                "identity": identity,
                "event_id": secrets.token_hex(16),
                "needs_login": True,
                "notified_at": 0,
            },
        )

    async def refresh(self, sid: str, observed: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            current = self.load()
            if str(current.get("userId")) != str(observed.get("userId")):
                raise BridgeError("小米账号绑定已更新，请重新发起请求。")
            # 其他请求已完成该服务的续期时直接复用，避免并发覆盖旋转凭据。
            if current.get(sid) != observed.get(sid):
                return current
            status = self.status()
            if status.get("identity") == token_identity(current) and status.get("needs_login"):
                raise AuthRequired()
            try:
                async with session_factory() as session, asyncio.timeout(40):
                    renewed = await refresh_service(session, current, sid)
            except AuthRequired:
                self.mark_required(current)
                raise
            write_json(self.directory / "token.json", renewed)
            return renewed

    async def ensure_fresh(self) -> dict[str, Any]:
        token  = self.load()
        status = self.status()
        if status.get("identity") == token_identity(token) and status.get("needs_login"):
            raise AuthRequired()
        for sid in SERVICES:
            stamp = token.get("_refreshed_at", {}).get(sid, 0)
            if not isinstance(stamp, (int, float)) or time.time() - stamp >= RENEW_AFTER:
                token = await self.refresh(sid, token)
        return token
