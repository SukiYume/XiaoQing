"""扫码、凭据续期和授权失效的协议回归。"""

import asyncio
import json
import re
import time
from copy import deepcopy
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aioresponses import aioresponses

from plugins.xiaoai import auth, client

LOGIN    = re.compile(r"https://account\.xiaomi\.com/pass/serviceLogin\?.*")
QR       = re.compile(r"https://account\.xiaomi\.com/longPolling/loginUrl\?.*")
STS      = re.compile(r"https://sts\.api\.io\.mi\.com/sts\?.*")
MINA_STS = re.compile(r"https://api2\.mina\.mi\.com/sts\?.*")
POLL     = "https://ak.lp.account.xiaomi.com/lp/s?ticket=private"


def credentials():
    return {
        "userId": "42",
        "passToken": "private-pass",
        "deviceId": "device",
        "xiaomiio": ["a2V5", "old-io"],
        "micoapi": ["a2V5", "old-mina"],
        "_refreshed_at": {"xiaomiio": 0, "micoapi": 0},
    }


def login_response(sid="xiaomiio"):
    host = "sts.api.io.mi.com" if sid == "xiaomiio" else "api2.mina.mi.com"
    return {
        "code": 0,
        "userId": "42",
        "passToken": "rotated-pass",
        "ssecurity": "a2V5",
        "nonce": "1",
        "location": f"https://{host}/sts?ticket=private",
    }


@pytest.mark.asyncio
async def test_qr_protocol_isolated_cookies_and_both_services():
    flow = auth.QRFlow()
    with aioresponses() as mocked:
        mocked.get(
            LOGIN,
            payload={"code": 70016, "qs": "encoded", "callback": "https://sts.api.io.mi.com/sts"},
        )
        mocked.get(
            QR,
            body="&&&START&&&"
            + json.dumps(
                {
                    "code": 0,
                    "loginUrl": "https://ak.account.xiaomi.com/longPolling/login?ticket=qr",
                    "lp": POLL,
                    "timeout": 300,
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
        with aioresponses() as mocked:
            mocked.get(STS, status=302, headers={"Location": "https://evil.example/steal"})
            with pytest.raises(client.BridgeError):
                await auth.exchange(session, login_response(), "xiaomiio")
            assert sum(map(len, mocked.requests.values())) == 1


@pytest.mark.asyncio
async def test_qr_start_error_closes_owned_session(monkeypatch):
    session = aiohttp.ClientSession()
    monkeypatch.setattr(auth, "session_factory", lambda: session)
    monkeypatch.setattr(auth, "account_json", AsyncMock(side_effect=TimeoutError()))
    with pytest.raises(TimeoutError):
        await auth.QRFlow().start()
    assert session.closed


@pytest.mark.asyncio
async def test_qr_poll_timeout_expires_and_cancellation_propagates(monkeypatch):
    flow            = auth.QRFlow()
    flow.poll_url   = POLL
    flow.expires_at = time.monotonic() - 1
    with pytest.raises(client.BridgeError, match="过期"):
        await flow.wait()
    flow.expires_at = time.monotonic() + 300
    monkeypatch.setattr(auth, "account_json", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await flow.wait()


@pytest.mark.asyncio
async def test_refresh_persists_rotated_credentials_and_serializes(tmp_path, monkeypatch):
    token = credentials()
    client.write_json(tmp_path / "token.json", token)
    manager = auth.TokenManager(tmp_path)

    async def renew(session, observed, sid):
        result                       = deepcopy(observed)
        result[sid]                  = ["new-key", "new-service"]
        result["passToken"]          = "new-pass"
        result["_refreshed_at"][sid] = time.time()
        await asyncio.sleep(0)
        return result

    refresh = AsyncMock(side_effect=renew)
    monkeypatch.setattr(auth, "refresh_service", refresh)
    results = await asyncio.gather(
        manager.refresh("xiaomiio", token), manager.refresh("xiaomiio", token)
    )
    assert results[0] == results[1]
    assert refresh.await_count == 1
    assert manager.load()["passToken"] == "new-pass"
    assert token["passToken"] == "private-pass"


@pytest.mark.asyncio
async def test_explicit_auth_failure_is_persisted_and_network_failure_is_transient(
    tmp_path, monkeypatch
):
    token = credentials()
    client.write_json(tmp_path / "token.json", token)
    manager = auth.TokenManager(tmp_path)
    failure = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(auth, "refresh_service", failure)
    with pytest.raises(TimeoutError):
        await manager.ensure_fresh()
    assert not manager.status()
    assert manager.load() == token
    failure.side_effect = client.AuthRequired()
    with pytest.raises(client.AuthRequired):
        await manager.ensure_fresh()
    assert manager.status()["needs_login"] is True
    assert manager.load() == token
    count = failure.await_count
    with pytest.raises(client.AuthRequired):
        await manager.ensure_fresh()
    assert failure.await_count == count


@pytest.mark.asyncio
async def test_proactive_refresh_uses_six_hour_age(tmp_path, monkeypatch):
    token                  = credentials()
    token["_refreshed_at"] = {sid: time.time() for sid in auth.SERVICES}
    client.write_json(tmp_path / "token.json", token)
    manager = auth.TokenManager(tmp_path)
    refresh = AsyncMock(return_value=token)
    monkeypatch.setattr(manager, "refresh", refresh)
    assert await manager.ensure_fresh() == token
    refresh.assert_not_awaited()
    token["_refreshed_at"]["xiaomiio"] = time.time() - auth.RENEW_AFTER - 1
    client.write_json(tmp_path / "token.json", token)
    await manager.ensure_fresh()
    assert refresh.await_args.args[0] == "xiaomiio"
    assert refresh.await_count == 1


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
async def test_service_login_requires_user_validation_and_preserves_secrets():
    async with auth.session_factory() as session:
        with aioresponses() as mocked:
            mocked.get(LOGIN, payload={"code": 70016, "description": "private-pass"})
            with pytest.raises(client.AuthRequired) as exc:
                await auth.refresh_service(session, credentials(), "xiaomiio")
            assert "private-pass" not in str(exc.value)
        with aioresponses() as mocked:
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
async def test_qr_body_timeout_continues_polling(monkeypatch):
    from core.bounded_http import ResponseTransportError

    flow            = auth.QRFlow()
    flow.poll_url   = POLL
    flow.expires_at = time.monotonic() + 300
    error           = ResponseTransportError("response body transport failed")
    error.__cause__ = TimeoutError()
    request = AsyncMock(side_effect=[error, {"code": 1}])
    monkeypatch.setattr(auth, "account_json", request)
    with pytest.raises(client.BridgeError, match="二维码"):
        await flow.wait()
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_callback_accepts_case_insensitive_location_header():
    async with auth.session_factory() as session:
        with aioresponses() as mocked:
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
