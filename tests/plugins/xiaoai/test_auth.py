"""扫码登录、凭据续期和授权失效的协议行为。"""

import asyncio
import json
import time
from copy import deepcopy
from unittest.mock import AsyncMock

import aiohttp
import pytest

from plugins.xiaoai import auth, client
from tests.helpers.http import (
    AiohttpResponse,
    QueuedAiohttpSession,
    aiohttp_json_response,
    mocked_aiohttp,
)
from tests.helpers.xiaoai_test_support import LOGIN, MINA_STS, QR, STS

POLL = "https://ak.lp.account.xiaomi.com/lp/s?ticket=private"


def credentials():
    return {
        "userId"       : "42",
        "passToken"    : "private-pass",
        "deviceId"     : "device",
        "xiaomiio"     : ["a2V5", "old-io"],
        "micoapi"      : ["a2V5", "old-mina"],
        "_refreshed_at": {"xiaomiio": 0, "micoapi": 0},
    }


class _PollSession:
    """按顺序给出预置响应（或抛出预置异常）的 aiohttp 会话替身，只实现 ``get``。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)

    def get(self, _url, **_kwargs):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _TimeoutBody:
    async def iter_chunked(self, _size):
        raise TimeoutError()
        yield b""


def _timeout_while_reading_body():
    response = AiohttpResponse(b"{}", media_type="application/json")
    response.content = _TimeoutBody()
    response.cookies = {}
    return response


def _json_poll_response(payload):
    response         = aiohttp_json_response(payload)
    response.cookies = {}
    return response


def _login_requests(mocked):
    return [
        call
        for (_method, url), calls in mocked.requests.items()
        if url.path == "/pass/serviceLogin"
        for call in calls
    ]


def login_response(sid="xiaomiio"):
    host = "sts.api.io.mi.com" if sid == "xiaomiio" else "api2.mina.mi.com"
    return {
        "code"     : 0,
        "userId"   : "42",
        "passToken": "rotated-pass",
        "ssecurity": "a2V5",
        "nonce"    : "1",
        "location" : f"https://{host}/sts?ticket=private",
    }


@pytest.mark.asyncio
async def test_qr_protocol_isolated_cookies_and_both_services():
    flow = auth.QRFlow()
    with mocked_aiohttp() as mocked:
        mocked.get(
            LOGIN,
            payload={"code": 70016, "qs": "encoded", "callback": "https://sts.api.io.mi.com/sts"},
        )
        mocked.get(
            QR,
            body="&&&START&&&"
            + json.dumps(
                {
                    "code"    : 0,
                    "loginUrl": "https://ak.account.xiaomi.com/longPolling/login?ticket=qr",
                    "lp"      : POLL,
                    "timeout" : 300,
                }
            ),
        )
        mocked.get(POLL, payload=login_response())
        mocked.get(STS, headers={"Set-Cookie": "serviceToken=io-new; Path=/"}, body="ok")
        mocked.get(LOGIN, payload=login_response("micoapi"))
        mocked.get(MINA_STS, headers={"Set-Cookie": "serviceToken=mina-new; Path=/"}, body="ok")
        await flow.start()
        session = flow.session
        assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
        assert 295 < flow.expires_at - time.monotonic() <= 300
        token = await flow.wait()
        assert token["xiaomiio"] == ["a2V5", "io-new"]
        assert token["micoapi"] == ["a2V5", "mina-new"]
        assert token["passToken"] == "rotated-pass"
        for (_method, url), calls in mocked.requests.items():
            for call in calls:
                assert call.kwargs["allow_redirects"] is False
                if url.host in {"sts.api.io.mi.com", "api2.mina.mi.com"}:
                    assert not call.kwargs["cookies"]
        await flow.close()
        assert session.closed and not flow.poll_url and not flow.login_url


@pytest.mark.parametrize(
    "url",
    [
        "http://account.xiaomi.com/a",
        "https://account.xiaomi.com.evil.example/a",
        "https://user:pass@account.xiaomi.com/a",
        "https://account.xiaomi.com:8443/a",
    ],
)
def test_qr_hosts_fail_closed(url):
    with pytest.raises(client.BridgeError):
        auth.account_url(url)


@pytest.mark.asyncio
async def test_callback_redirect_blocks_external_host():
    async with auth.session_factory() as session:
        with mocked_aiohttp() as mocked:
            mocked.get(STS, status=302, headers={"Location": "https://evil.example/steal"})
            with pytest.raises(client.BridgeError):
                await auth.exchange(session, login_response(), "xiaomiio")
            assert sum(map(len, mocked.requests.values())) == 1


@pytest.mark.asyncio
async def test_qr_start_error_closes_owned_session(monkeypatch):
    opened     = []
    real_close = aiohttp.ClientSession.close

    async def tracked_close(session):
        opened.append(session)
        await real_close(session)

    monkeypatch.setattr(aiohttp.ClientSession, "close", tracked_close)
    flow = auth.QRFlow()
    with mocked_aiohttp() as mocked:
        mocked.get(LOGIN, exception=TimeoutError())
        with pytest.raises(TimeoutError):
            await flow.start()
    assert len(opened) == 1 and opened[0].closed
    assert flow.session is None and not flow.poll_url


@pytest.mark.asyncio
async def test_qr_poll_timeout_expires_and_cancellation_propagates():
    flow            = auth.QRFlow()
    flow.poll_url   = POLL
    flow.expires_at = time.monotonic() - 1
    with pytest.raises(client.BridgeError, match="过期"):
        await flow.wait()
    flow.expires_at = time.monotonic() + 300
    flow.session    = _PollSession(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await flow.wait()


@pytest.mark.asyncio
async def test_refresh_persists_rotated_credentials_and_serializes(tmp_path):
    token = credentials()
    client.write_json(tmp_path / "token.json", token)
    manager = auth.TokenManager(tmp_path)
    with mocked_aiohttp() as mocked:
        mocked.get(LOGIN, payload=login_response())
        mocked.get(STS, headers={"Set-Cookie": "serviceToken=io-new; Path=/"}, body="ok")
        results = await asyncio.gather(
            manager.refresh("xiaomiio", token), manager.refresh("xiaomiio", token)
        )
        assert len(_login_requests(mocked)) == 1
    assert results[0] == results[1]
    assert manager.load()["passToken"] == "rotated-pass"
    assert manager.load()["xiaomiio"] == ["a2V5", "io-new"]
    assert token["passToken"] == "private-pass"


@pytest.mark.asyncio
async def test_explicit_auth_failure_is_persisted_and_network_failure_is_transient(tmp_path):
    token = credentials()
    client.write_json(tmp_path / "token.json", token)
    manager = auth.TokenManager(tmp_path)
    with mocked_aiohttp() as mocked:
        mocked.get(LOGIN, exception=TimeoutError())
        mocked.get(LOGIN, payload={"code": 70016})
        with pytest.raises(TimeoutError):
            await manager.ensure_fresh()
        assert not manager.status()
        assert manager.load() == token
        with pytest.raises(client.AuthRequired):
            await manager.ensure_fresh()
        assert manager.status()["needs_login"] is True
        assert manager.load() == token
        count = len(_login_requests(mocked))
        assert count == 2
        with pytest.raises(client.AuthRequired):
            await manager.ensure_fresh()
        assert len(_login_requests(mocked)) == count


@pytest.mark.asyncio
async def test_proactive_refresh_uses_six_hour_age(tmp_path):
    token                  = credentials()
    token["_refreshed_at"] = {sid: time.time() for sid in auth.SERVICES}
    client.write_json(tmp_path / "token.json", token)
    manager = auth.TokenManager(tmp_path)
    with mocked_aiohttp() as mocked:
        assert await manager.ensure_fresh() == token
        assert not mocked.requests
        token["_refreshed_at"]["xiaomiio"] = time.time() - auth.RENEW_AFTER - 1
        client.write_json(tmp_path / "token.json", token)
        mocked.get(LOGIN, payload=login_response())
        mocked.get(STS, headers={"Set-Cookie": "serviceToken=io-new; Path=/"}, body="ok")
        renewed = await manager.ensure_fresh()
        logins  = _login_requests(mocked)
        assert len(logins) == 1
        assert logins[0].kwargs["params"]["sid"] == "xiaomiio"
    assert renewed["xiaomiio"] == ["a2V5", "io-new"]
    assert renewed["micoapi"] == token["micoapi"]


@pytest.mark.asyncio
async def test_read_only_retries_after_refresh_and_actions_never_replay(monkeypatch):
    token                  = credentials()
    renewed                = deepcopy(token)
    renewed["xiaomiio"][1] = "new"
    refresh = AsyncMock(return_value=renewed)
    request = AsyncMock(side_effect=[{"code": 401}, {"code": 0, "result": {}}])
    monkeypatch.setattr(client, "request_json", request)
    account = client.TokenAccount(object(), token, refresh)
    await account.mi_request("xiaomiio", "https://api.io.mi.com/app/home/device_list", {}, {})
    assert request.await_count == 2 and refresh.await_count == 1
    assert request.await_args.kwargs["cookies"]["serviceToken"] == "new"
    request.reset_mock(side_effect=True)
    request.side_effect = [{"code": 401}]
    with pytest.raises(client.BridgeError, match="单次提交"):
        await account.mi_request("xiaomiio", "https://api.io.mi.com/app/miotspec/action", {}, {})
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_cloud_rate_limit_is_reported_as_such_not_as_a_login_problem(monkeypatch):
    from core.bounded_http import HttpStatusError

    monkeypatch.setattr(client, "request_json", AsyncMock(side_effect=HttpStatusError(429)))
    account = client.TokenAccount(object(), credentials(), AsyncMock())
    with pytest.raises(client.BridgeError, match="限流") as exc:
        await account.mi_request("xiaomiio", "https://api.io.mi.com/app/home/device_list", {}, {})
    assert not isinstance(exc.value, client.AuthRequired)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
async def test_client_withdraw_preserves_cause_without_refresh_or_secret_logging(status, caplog):
    session = QueuedAiohttpSession(
        aiohttp_json_response(
            {"code": -127, "message": "client withdraw", "description": "private-pass"},
            status     = status,
            media_type = "text/plain",
        )
    )
    refresh = AsyncMock()
    account = client.TokenAccount(session, credentials(), refresh)
    with pytest.raises(client.ClientRejected, match="-127") as error:
        await account.mi_request("xiaomiio", "https://api.io.mi.com/app/home/device_list", {}, {})
    assert len(session.requests) == 1
    refresh.assert_not_awaited()
    assert "过几分钟" not in str(error.value)
    assert "upstream_code=-127" in caplog.text
    assert "reason_code=client_rejected" in caplog.text
    assert f"http_status={status}" in caplog.text
    assert "private-pass" not in caplog.text
    assert "old-io" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", ["xiaomiio", "micoapi"])
async def test_cloud_client_identity_matches_login_and_preserves_speaker_headers(sid):
    session = QueuedAiohttpSession(aiohttp_json_response({"code": 0, "result": {}}))
    headers = {"User-Agent": "old-client", "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2"}
    account = client.TokenAccount(session, credentials())
    url     = (
        "https://api.io.mi.com/app/home/device_list"
        if sid == "xiaomiio"
        else "https://api2.mina.mi.com/admin/v2/device_list"
    )
    await account.mi_request(sid, url, None, headers)
    sent = session.requests[0][2]["headers"]
    assert sent["User-Agent"] == (auth.USER_AGENT if sid == "xiaomiio" else "old-client")
    assert sent["x-xiaomi-protocal-flag-cli"] == "PROTOCAL-HTTP2"
    assert headers["User-Agent"] == "old-client"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [b'{"code":429,"message":"private-pass"}', b"<html>private-pass</html>"]
)
async def test_actual_rate_limit_is_logged_and_never_replayed(body, caplog):
    session = QueuedAiohttpSession(AiohttpResponse(body, status=429, media_type="text/plain"))
    refresh = AsyncMock()
    account = client.TokenAccount(session, credentials(), refresh)
    with pytest.raises(client.BridgeError, match="限流"):
        await account.mi_request("xiaomiio", "https://api.io.mi.com/app/miotspec/action", {}, {})
    assert len(session.requests) == 1
    refresh.assert_not_awaited()
    assert "reason_code=rate_limited" in caplog.text
    assert "private-pass" not in caplog.text


@pytest.mark.asyncio
async def test_http_auth_failure_with_non_json_body_still_refreshes_once(caplog):
    session = QueuedAiohttpSession(
        AiohttpResponse(b"private-pass", status=401, media_type="text/plain"),
        aiohttp_json_response({"code": 0, "result": {}}),
    )
    refresh = AsyncMock(return_value=credentials())
    await client.TokenAccount(session, credentials(), refresh).mi_request(
        "xiaomiio",
        "https://api.io.mi.com/app/home/device_list",
        {},
        {},
    )
    refresh.assert_awaited_once()
    assert len(session.requests) == 2
    assert "http_status=401" in caplog.text
    assert "private-pass" not in caplog.text


@pytest.mark.asyncio
async def test_service_login_requires_user_validation_and_preserves_secrets():
    async with auth.session_factory() as session:
        with mocked_aiohttp() as mocked:
            mocked.get(LOGIN, payload={"code": 70016, "description": "private-pass"})
            with pytest.raises(client.AuthRequired) as exc:
                await auth.refresh_service(session, credentials(), "xiaomiio")
            assert "private-pass" not in str(exc.value)
        with mocked_aiohttp() as mocked:
            mocked.get(LOGIN, payload={**login_response(), "userId": "different-user"})
            with pytest.raises(client.AuthRequired):
                await auth.refresh_service(session, credentials(), "xiaomiio")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"code": 0, "message": "auth err"},
        {"code": 2},
        {"code": 3},
        {"code": 0, "message": "SERVICETOKEN_EXPIRED"},
    ],
)
async def test_cloud_auth_error_variants(payload, monkeypatch):
    monkeypatch.setattr(client, "request_json", AsyncMock(return_value=payload))
    account = client.TokenAccount(object(), credentials())
    with pytest.raises(client.AuthRequired):
        await account.mi_request("xiaomiio", "https://api.io.mi.com/app/home/device_list", {}, {})


@pytest.mark.asyncio
async def test_rejected_service_after_successful_renewal_keeps_login_recoverable(monkeypatch):
    request = AsyncMock(return_value={"code": 401})
    monkeypatch.setattr(client, "request_json", request)
    account = client.TokenAccount(object(), credentials(), AsyncMock(return_value=credentials()))
    with pytest.raises(client.BridgeError) as error:
        await account.mi_request("xiaomiio", "https://api.io.mi.com/app/home/device_list", {}, {})
    assert not isinstance(error.value, client.AuthRequired)
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_qr_body_timeout_continues_polling():
    flow            = auth.QRFlow()
    flow.poll_url   = POLL
    flow.expires_at = time.monotonic() + 300
    flow.session    = _PollSession(
        _timeout_while_reading_body(),
        _json_poll_response({"code": 1}),
    )
    with pytest.raises(client.BridgeError, match="二维码"):
        await flow.wait()
    assert flow.session.outcomes == []


@pytest.mark.asyncio
async def test_callback_accepts_case_insensitive_location_header():
    async with auth.session_factory() as session:
        with mocked_aiohttp() as mocked:
            mocked.get(STS, status=302, headers={"location": "/sts?ticket=second"})
            mocked.get(STS, headers={"Set-Cookie": "serviceToken=new; Path=/"}, body="ok")
            assert await auth.exchange(session, login_response(), "xiaomiio") == ["a2V5", "new"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@api.io.mi.com/app/home/device_list",
        "https://api.io.mi.com:8443/app/home/device_list",
    ],
)
async def test_cloud_origin_validation_precedes_credentials(url, monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(client, "request_json", request)
    account = client.TokenAccount(object(), credentials())
    with pytest.raises(client.BridgeError):
        await account.mi_request("xiaomiio", url, {}, {})
    request.assert_not_awaited()
