"""B站 扫码登录：只登录管理员扫码授权的那一个账号，凭证只用于 ``api.bilibili.com`` 的只读 GET。

边界（见 DESIGN.md 10.11）：

- 凭证值、二维码链接、``qrcode_key`` 不进日志、状态、命令输出、提示词、异常信息；日志只记步骤与错误类型。
- 二维码只发给发起人的私聊（动作带 ``SENSITIVE_ACTION_KEY``）。
- passport 的生成 / 轮询请求不带 Cookie；Cookie 只会附在发往 ``api.bilibili.com`` 的请求上。
- 凭证文件在 ``<data_dir>/secrets/bilibili.json``，不是 ``LifeStore`` 的集合，不进备份与损坏恢复。
- 不调用任何写接口（包括远端退出登录）：``logout`` 只删本地文件。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import aiohttp

from core.bounded_http import (
    BodyLimits,
    BoundedHttpError,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from core.delivery import SENSITIVE_ACTION_KEY
from core.plugin_base import segments

from .safety import clean_text

logger = logging.getLogger(__name__)

SECRET_DIR        = "secrets"
SECRET_FILE       = "bilibili.json"
PASSPORT          = "https://passport.bilibili.com"
API               = "https://api.bilibili.com"
QR_HOSTS          = frozenset({"passport.bilibili.com", "account.bilibili.com"})
REQUEST_HOSTS     = frozenset({"passport.bilibili.com", "api.bilibili.com"})
LOGIN_KEYS        = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")
COOKIE_NAMES      = (*LOGIN_KEYS, "buvid3", "buvid4")
REQUIRED_KEYS     = ("SESSDATA", "bili_jct", "DedeUserID")
MAX_VALUE_CHARS   = 512
POLL_SECONDS      = 2.0
LOGIN_TIMEOUT     = 180.0
UNAME_CHARS       = 40
_COOKIE_URL_HOSTS = frozenset({"passport.biligame.com"})
_FINGERPRINT_RE   = re.compile(r"[A-Za-z0-9_%.\-+/|]+={0,2}")
_KEY_RE           = re.compile(r"[A-Za-z0-9_\-]{8,128}")
USER_AGENT        = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_LIMITS = BodyLimits(max_wire_bytes=256 * 1024, max_decoded_bytes=256 * 1024)
_JSON = JsonLimits(max_bytes=256 * 1024, max_depth=16, max_nodes=20_000, max_string_chars=65_536)
_MIME = MimePolicy(
    exact               = frozenset({"application/json", "text/json", "text/plain", "text/html"}),
    structured_suffixes = frozenset({"+json"}),
)

MSG_NOT_PRIVATE = "请私聊发送 /life login。"
MSG_BUSY        = "另一位管理员正在登录，请等对方完成，或由对方发 /life login cancel。"
MSG_STARTED     = "二维码已发到你的私聊，请用 B站 App 扫码确认，约 3 分钟内有效。"
MSG_RETRY       = "这次扫码登录没有完成，请重新发送 /life login。"
MSG_DONE        = "B站登录好了（账号：{uname}）。之后她点开视频能读字幕。发 /life logout 可以退出。"
MSG_INVALID     = "B站登录失效了，发 /life login 重新扫码就好。"
MSG_NO_QR_LIB   = '扫码登录需要依赖 qrcode[pil]，请运行 python -m pip install "qrcode[pil]"。'
LINE_NONE       = "B站：未登录（字幕读不到，只看简介和评论）"
LINE_INVALID    = "B站：登录失效，需要重新扫码"


class LoginError(Exception):
    """登录流程里可预期的失败；只带固定标识，不带外部内容。"""


# ───────────────────────────── 凭证文件 ─────────────────────────────


def valid_value(value: Any) -> bool:
    """Cookie 值：非空字符串、≤512 字符、不含分号和任何控制字符（含换行）。"""

    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_VALUE_CHARS
        and ";" not in value
        and all(ord(ch) >= 32 and ord(ch) != 127 for ch in value)
    )


def build_cookie_header(cookies: Mapping[str, Any]) -> str:
    """只拼六个允许的名字；值不合规的跳过；缺必需登录键时返回空。"""

    if not all(valid_value(cookies.get(name)) for name in REQUIRED_KEYS):
        return ""
    return "; ".join(
        f"{name}={cookies[name]}" for name in COOKIE_NAMES if valid_value(cookies.get(name))
    )


def protect_directory(directory: Path) -> None:
    """收紧凭证目录权限：POSIX 0700；Windows 去继承、只给当前用户（同 xiaoai 的做法）。"""

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        identity = subprocess.check_output(["whoami"], text=True).strip()  # noqa: S607
        subprocess.run(  # noqa: S603
            ["icacls", str(directory), "/inheritance:r", "/grant:r", f"{identity}:(OI)(CI)F"],  # noqa: S607
            check  = True,
            stdout = subprocess.DEVNULL,
            stderr = subprocess.DEVNULL,
        )
    else:
        directory.chmod(0o700)


def write_secret(path: Path, value: dict[str, Any]) -> None:
    """临时文件（0o600）+ fsync + ``os.replace`` 原子写。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    fd        = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class BiliCredential:
    """懒加载凭证文件，按文件 mtime 失效重读；已标记失效、无文件或读失败时 ``cookie_header()`` 为空。"""

    def __init__(self, data_dir: Path, *, on_invalid: Callable[[], None] | None = None) -> None:
        self.path = Path(data_dir) / SECRET_DIR / SECRET_FILE
        self.invalid = False
        self.on_invalid = on_invalid
        self._cache: tuple[tuple[int, int], dict[str, Any]] | None = None

    def _document(self) -> dict[str, Any] | None:
        try:
            info  = self.path.stat()
            stamp = (info.st_mtime_ns, info.st_size)  # 同一时间刻度内的改写也要认出来
        except OSError:
            self._cache = None
            return None
        if self._cache is not None and self._cache[0] == stamp:
            return self._cache[1]
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._cache = None
            return None
        if not isinstance(document, dict):
            self._cache = None
            return None
        self._cache = (stamp, document)
        return document

    def exists(self) -> bool:
        return self._document() is not None

    def cookie_header(self) -> str:
        if self.invalid:
            return ""
        document = self._document()
        cookies  = document.get("cookies") if document else None
        return build_cookie_header(cookies) if isinstance(cookies, dict) else ""

    def usable(self) -> bool:
        return bool(self.cookie_header())

    def info(self) -> dict[str, Any]:
        """账号名、保存时间、发起人 ID；不含任何 Cookie 值。"""

        document = self._document() or {}
        owner    = document.get("owner")
        saved    = document.get("saved_at")
        return {
            "uname"   : clean_text(document.get("uname"), UNAME_CHARS),
            "saved_at": float(saved) if isinstance(saved, (int, float)) else 0.0,
            "owner"   : owner if type(owner) is int and owner > 0 else 0,
        }

    def mark_invalid(self) -> None:
        if self.invalid:
            return
        self.invalid = True
        if self.on_invalid is not None:
            self.on_invalid()

    def mark_valid(self) -> None:
        self.invalid = False
        self._cache  = None

    def delete(self) -> bool:
        """删除本地凭证文件（含残留临时文件）；返回原来是否存在。"""

        existed = self.path.exists()
        for target in (self.path, self.path.with_suffix(".tmp")):
            try:
                target.unlink()
            except FileNotFoundError:
                continue
        self._cache  = None
        self.invalid = False
        return existed


# ───────────────────────────── 请求与解析 ─────────────────────────────


async def _get(
    session: Any, url: str, params: Mapping[str, str] | None = None, *, cookie: str = ""
) -> tuple[dict[str, Any], Mapping[str, str]]:
    """有界 GET；Cookie 只会发给 ``api.bilibili.com``，passport 永远不带。失败只抛 ``LoginError``。"""

    host = (urlsplit(url).hostname or "").lower()
    if host not in REQUEST_HOSTS:
        raise LoginError("host_not_allowed")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept"    : "application/json, text/plain, */*",
        "Referer"   : "https://www.bilibili.com/",
    }
    if cookie and host == "api.bilibili.com":
        headers["Cookie"] = cookie
    try:
        response = await aiohttp_request_bounded(
            session,
            "GET",
            url,
            limits         = _LIMITS,
            mime_policy    = _MIME,
            headers        = headers,
            request_kwargs = {
                "params" : dict(params or {}),
                "timeout": aiohttp.ClientTimeout(total=10),
            },
        )
        payload = parse_bounded_json(response, limits=_JSON)
    except (BoundedHttpError, aiohttp.ClientError, TimeoutError) as exc:
        raise LoginError(type(exc).__name__) from None
    if not isinstance(payload, dict):
        raise LoginError("bad_payload")
    return payload, response.headers


def check_qr_url(url: Any) -> str:
    """二维码链接必须是 https 且主机在白名单。"""

    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme != "https" or (parts.hostname or "").lower() not in QR_HOSTS:
        raise LoginError("bad_qr_host")
    return str(url)


def _cookies_from_url(url: Any) -> dict[str, str]:
    """跨域登录链接查询串里的登录键，取**原样**（不做百分号解码）的值。

    浏览器里 ``SESSDATA`` 的 Cookie 值本身带 ``%2C``；``parse_qs`` 会把它解成逗号。解码版只在
    原样版过不了 ``nav`` 校验时作为备选（``decoded_variant``）。
    """

    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme != "https":
        return {}
    # 实际返回的跨域登录链接主机是 ``passport.biligame.com``（B站自家域名），只认这一个和 B站 子域。
    host = (parts.hostname or "").lower()
    if not (host.endswith(".bilibili.com") or host in _COOKIE_URL_HOSTS):
        return {}
    found: dict[str, list[str]] = {}
    for piece in parts.query.split("&"):
        key, sep, value = piece.partition("=")
        if sep and key in LOGIN_KEYS:
            found.setdefault(key, []).append(value)
    return {key: values[0] for key, values in found.items() if len(values) == 1}


def decoded_variant(cookies: Mapping[str, str]) -> dict[str, str] | None:
    """登录键百分号解码后的备选；和原样相同或解码后不合规时返回 None。"""

    decoded = {k: (unquote(v) if k in LOGIN_KEYS else v) for k, v in cookies.items()}
    if decoded == dict(cookies) or not all(valid_value(v) for v in decoded.values()):
        return None
    return decoded


def _cookies_from_header(headers: Mapping[str, str]) -> dict[str, str]:
    cookies: dict[str, str] = {}
    # Each Set-Cookie field is independent; Expires attributes can contain commas.
    for key, raw in headers.items():
        if key.lower() != "set-cookie":
            continue
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except CookieError:
            continue
        cookies.update({name: morsel.value for name, morsel in jar.items() if name in LOGIN_KEYS})
    return cookies


def extract_cookies(data: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, str]:
    """只认四个登录键；来源是 ``data.url`` 的查询串与响应 ``Set-Cookie``。缺必需键或值不合规就放弃。"""

    cookies = _cookies_from_url(data.get("url"))
    for name, value in _cookies_from_header(headers).items():
        cookies.setdefault(name, value)
    if not all(name in cookies for name in REQUIRED_KEYS):
        raise LoginError("missing_credentials")
    if not all(valid_value(value) for value in cookies.values()):
        raise LoginError("bad_credential_value")
    return cookies


async def fetch_fingerprint(session: Any) -> dict[str, str]:
    """``b_3 / b_4`` → ``buvid3 / buvid4``；任何问题都返回空（不影响登录）。"""

    try:
        payload, _ = await _get(session, f"{API}/x/frontend/finger/spi")
    except LoginError:
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    result: dict[str, str] = {}
    for cookie, field in (("buvid3", "b_3"), ("buvid4", "b_4")):
        value = data.get(field)
        if isinstance(value, str) and len(value) <= 256 and _FINGERPRINT_RE.fullmatch(value):
            result[cookie] = value
    return result if "buvid3" in result else {}


async def verify_login(session: Any, cookies: Mapping[str, str]) -> dict[str, Any]:
    """带新凭证请求 ``nav``；``code == 0`` 且 ``data.isLogin`` 为真才返回 ``data``。"""

    payload, _ = await _get(
        session, f"{API}/x/web-interface/nav", cookie=build_cookie_header(cookies)
    )
    data = payload.get("data")
    if payload.get("code") != 0 or not isinstance(data, dict) or data.get("isLogin") is not True:
        raise LoginError("nav_not_logged_in")
    return data


def qr_image(url: str) -> dict[str, Any]:
    try:
        import qrcode
    except ImportError as exc:
        raise LoginError("qrcode_missing") from exc
    output = io.BytesIO()
    qrcode.make(url, box_size=6, border=4).save(output, format="PNG")
    return {
        "type": "image",
        "data": {"file": "base64://" + base64.b64encode(output.getvalue()).decode()},
    }


async def send_private(context: Any, recipient: int, message: list[dict[str, Any]]) -> bool | None:
    """私聊发送；动作带 ``SENSITIVE_ACTION_KEY``（不进投递记录与日志）。"""

    try:
        result = await context.send_action(
            {
                "action"            : "send_private_msg",
                "params"            : {"user_id": recipient, "message": message},
                SENSITIVE_ACTION_KEY: True,
            }
        )
        return result if result is True or result is False else None
    except Exception:
        return False


def requester_id(context: Any, event: Mapping[str, Any]) -> int:
    value = getattr(context, "current_user_id", None) or event.get("user_id")
    if type(value) is not int or value <= 0:
        raise LoginError("not_private")
    return value


# ───────────────────────────── 扫码流程 ─────────────────────────────


@dataclass(slots=True)
class PendingLogin:
    owner: int
    task : asyncio.Task[Any] | None = None


Spawn = Callable[[Coroutine[Any, Any, Any]], "asyncio.Task[Any] | None"]


class BiliLogin:
    """单一进行中的扫码登录：发起人、后台轮询任务、取消与关闭。"""

    def __init__(
        self,
        credential: BiliCredential,
        store: Any,
        spawn: Spawn,
        *,
        clock: Callable[[], float]               = time.time,
        monotonic: Callable[[], float]           = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        poll_seconds: float                      = POLL_SECONDS,
        timeout: float                           = LOGIN_TIMEOUT,
    ) -> None:
        self.credential                   = credential
        self.store                        = store
        self.spawn                        = spawn
        self.clock                        = clock
        self.monotonic                    = monotonic
        self.sleep                        = sleep
        self.poll_seconds                 = poll_seconds
        self.timeout                      = timeout
        self.pending: PendingLogin | None = None
        self._lock                        = asyncio.Lock()

    # —— 状态记录（不含任何凭证值） ——

    async def record(self, valid: bool) -> None:
        now = self.clock()
        await self.store.mutate(
            lambda s: s.state.__setitem__("bili_login", {"valid": valid, "since": now})
        )

    async def _clear_record(self) -> None:
        await self.store.mutate(lambda s: s.state.__setitem__("bili_login", {}))

    def mark_invalid_record(self) -> None:
        """凭证被 B站 判定失效时记入状态（同步入口，由 ``BiliCredential.on_invalid`` 触发）。"""

        now = self.clock()

        def apply(s: Any) -> None:
            current = s.state.get("bili_login") or {}
            if current.get("valid") is False:
                return
            s.state["bili_login"] = {"valid": False, "since": now}

        self.spawn(self.store.mutate(apply))

    def restore(self) -> None:
        """启动时按状态里的失效标记恢复内存标记。"""

        record = self.store.state.get("bili_login")
        if isinstance(record, dict) and record.get("valid") is False:
            self.credential.invalid = True

    # —— 命令 ——

    async def start(self, context: Any, event: Mapping[str, Any]) -> str:
        try:
            owner = requester_id(context, event)
        except LoginError:
            return MSG_NOT_PRIVATE
        async with self._lock:
            if self.pending is not None and self.pending.owner != owner:
                return MSG_BUSY
            await self._cancel_pending()
            try:
                return await self._begin(context, owner)
            except LoginError as exc:
                logger.info("xiaoqing_life step=bili_login start_failed reason=%s", exc)
                return MSG_NO_QR_LIB if str(exc) == "qrcode_missing" else MSG_RETRY

    async def _begin(self, context: Any, owner: int) -> str:
        session = context.http_session
        payload, _ = await _get(session, f"{PASSPORT}/x/passport-login/web/qrcode/generate")
        data = payload.get("data")
        if payload.get("code") != 0 or not isinstance(data, dict):
            raise LoginError("generate_rejected")
        url = check_qr_url(data.get("url"))
        key = data.get("qrcode_key")
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise LoginError("bad_qrcode_key")
        picture = qr_image(url)
        sent    = await send_private(
            context,
            owner,
            [*segments("请用 B站 App 扫描二维码并确认登录，约 3 分钟内有效。"), picture],
        )
        if sent is False:
            raise LoginError("send_failed")
        pending      = PendingLogin(owner)
        self.pending = pending
        task         = self.spawn(self._watch(context, pending, key))
        if task is None:
            self.pending = None
            raise LoginError("closing")
        pending.task = task
        return MSG_STARTED

    async def cancel(self, context: Any, event: Mapping[str, Any]) -> str:
        try:
            owner = requester_id(context, event)
        except LoginError:
            return MSG_NOT_PRIVATE
        async with self._lock:
            if self.pending is None:
                return "没有进行中的扫码登录。"
            if self.pending.owner != owner:
                return MSG_BUSY
            await self._cancel_pending()
        return "已取消扫码登录。"

    async def _cancel_pending(self) -> None:
        pending, self.pending = self.pending, None
        if pending is not None and pending.task is not None:
            pending.task.cancel()
            await asyncio.gather(pending.task, return_exceptions=True)

    async def close(self) -> None:
        """关闭时取消轮询任务（``LifeRuntime.shutdown`` 也会取消登记过的任务，这里是双保险）。"""

        await self._cancel_pending()

    async def logout(self) -> str:
        existed = await asyncio.to_thread(self.credential.delete)
        await self._clear_record()
        return "已删除本地 B站 登录凭证。" if existed else "本来就没有登录。"

    async def status(self, fetcher: Any, tz: Any = None) -> str:
        """有凭证时用 ``nav`` 实时确认一次（带 Cookie 的 -101 会让凭证标记失效）。"""

        if not self.credential.exists():
            return LINE_NONE
        info = self.credential.info()
        if not self.credential.invalid:
            try:
                await fetcher.get_json(f"{API}/x/web-interface/nav")
            except Exception as exc:
                logger.info(
                    "xiaoqing_life step=bili_login status_check_failed error_type=%s",
                    type(exc).__name__,
                )
                return f"B站：已保存登录（{info['uname']}），这会儿确认不了是否有效（网络问题）"
        if self.credential.invalid:
            return LINE_INVALID
        saved = (
            datetime.fromtimestamp(info["saved_at"], tz).strftime("%Y-%m-%d %H:%M")
            if info["saved_at"]
            else "未知"
        )
        return f"B站：已登录（{info['uname']}），保存于 {saved}"

    def status_line(self) -> str:
        """``/life status`` 里的一行（不发请求）。"""

        if not self.credential.exists():
            return LINE_NONE
        if self.credential.invalid:
            return LINE_INVALID
        return f"B站：已登录（{self.credential.info()['uname']}）"

    async def remind_if_invalid(self, context: Any) -> None:
        """失效后在一次上网结束时给发起人私聊一次；同一次失效只提醒一次，失败不重试。"""

        def claim(s: Any) -> bool:
            record = s.state.get("bili_login") or {}
            if record.get("valid") is not False or record.get("reminded"):
                return False
            s.state["bili_login"] = {**record, "reminded": True}
            return True

        if not self.credential.invalid or not await self.store.mutate(claim):
            return
        owner = self.credential.info()["owner"]
        if owner:
            await send_private(context, owner, segments(MSG_INVALID))

    # —— 后台轮询 ——

    async def _watch(self, context: Any, pending: PendingLogin, key: str) -> None:
        owner = pending.owner
        try:
            await self._poll(context, owner, key)
        except asyncio.CancelledError:
            raise
        except LoginError as exc:
            logger.info("xiaoqing_life step=bili_login failed reason=%s", exc)
            await send_private(context, owner, segments(MSG_RETRY))
        except Exception as exc:
            logger.info("xiaoqing_life step=bili_login failed error_type=%s", type(exc).__name__)
            await send_private(context, owner, segments(MSG_RETRY))
        finally:
            if self.pending is pending:
                self.pending = None

    async def _poll(self, context: Any, owner: int, key: str) -> None:
        session  = context.http_session
        deadline = self.monotonic() + self.timeout
        while True:
            if self.monotonic() >= deadline:
                raise LoginError("timeout")
            payload, headers = await _get(
                session, f"{PASSPORT}/x/passport-login/web/qrcode/poll", {"qrcode_key": key}
            )
            data = payload.get("data")
            if payload.get("code") != 0 or not isinstance(data, dict):
                raise LoginError("poll_rejected")
            status = data.get("code")
            if status == 0:
                await self._finish(context, owner, data, headers)
                return
            if status not in (86101, 86090):  # 86038 过期，其余未知码
                raise LoginError("qr_expired" if status == 86038 else "poll_unexpected")
            await self.sleep(self.poll_seconds)

    async def _finish(
        self, context: Any, owner: int, data: Mapping[str, Any], headers: Mapping[str, str]
    ) -> None:
        session = context.http_session
        cookies = extract_cookies(data, headers)
        cookies.update(await fetch_fingerprint(session))
        account: dict[str, Any] | None = None
        for candidate in (cookies, decoded_variant(cookies)):
            if candidate is None:
                continue
            try:
                account = await verify_login(session, candidate)
            except LoginError:
                continue
            cookies = candidate
            break
        if account is None:  # 校验失败：旧凭证文件保持不变
            raise LoginError("nav_not_logged_in")
        token    = data.get("refresh_token")
        uname    = clean_text(account.get("uname"), UNAME_CHARS)
        mid      = account.get("mid")
        document = {
            "cookies"      : cookies,
            "refresh_token": token if valid_value(token) else "",
            "uid"          : mid if type(mid) is int else cookies["DedeUserID"],
            "uname"        : uname,
            "owner"        : owner,
            "saved_at"     : self.clock(),
        }
        await asyncio.to_thread(protect_directory, self.credential.path.parent)
        await asyncio.to_thread(write_secret, self.credential.path, document)
        self.credential.mark_valid()
        await self.record(True)
        logger.info("xiaoqing_life step=bili_login saved")
        await send_private(context, owner, segments(MSG_DONE.format(uname=uname or "已确认")))
