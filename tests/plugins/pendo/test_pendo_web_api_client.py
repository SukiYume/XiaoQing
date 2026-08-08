"""Pendo Web 浏览器 API 客户端的会话、错误和下载契约回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final = REPOSITORY_ROOT
API_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "api.js"


def _run_api_client(script: str) -> None:
    """在独立 Node 进程中导入真实 ESM 客户端并执行给定契约。"""

    assert_node_esm_contract(API_CLIENT.read_text(encoding="utf-8"), script, cwd=ROOT)


def test_api_client_applies_headers_only_when_the_request_needs_them() -> None:
    """JSON、上传、只读和无请求体写操作应各自获得正确且不重复的请求头。"""

    _run_api_client(
        r"""
        const calls = [];
        globalThis.fetch = async (url, options = {}) => {
            calls.push({
                url,
                method: options.method || 'GET',
                body: options.body,
                credentials: options.credentials,
                headers: Object.fromEntries(new Headers(options.headers).entries()),
            });
            if (url === 'api/auth/session') {
                return new Response(JSON.stringify({
                    ok: true,
                    data: { owner_id: 'owner-1', csrf_token: 'csrf-1', expires_at: '2099-01-01' },
                }), { status: 200, headers: { 'Content-Type': 'application/json' } });
            }
            return new Response(JSON.stringify({ ok: true, data: {} }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            });
        };

        assert.equal((await client.getSession()).ok, true);
        await client.api.get('/items', {});
        await client.api.get('/items', { search: 'a b', limit: 2 });
        await client.api.post('/items', { title: '测试' });
        await client.api.delete('/items/item-1');
        await client.apiUpload('/transfer/import/inspect', 'raw', { 'content-type': 'text/plain' });

        assert.equal(calls.length, 6);
        assert.equal(calls[1].url, 'api/items');
        assert.equal(calls[2].url, 'api/items?search=a+b&limit=2');
        assert.equal(calls[1].headers['content-type'], undefined);
        assert.equal(calls[1].headers['x-csrf-token'], undefined);
        assert.equal(calls[3].headers['content-type'], 'application/json');
        assert.equal(calls[3].headers['x-csrf-token'], 'csrf-1');
        assert.equal(calls[4].headers['content-type'], undefined);
        assert.equal(calls[4].headers['x-csrf-token'], 'csrf-1');
        assert.equal(calls[5].headers['content-type'], 'text/plain');
        assert.equal(calls[5].headers['x-csrf-token'], 'csrf-1');
        assert.ok(calls.every((call) => call.credentials === 'same-origin'));
        """
    )


def test_failed_session_checks_clear_stale_csrf_state() -> None:
    """不完整响应和网络失败都不能让之前的 CSRF 令牌继续污染后续请求。"""

    _run_api_client(
        r"""
        let sessionAttempt = 0;
        const writes = [];
        globalThis.fetch = async (url, options = {}) => {
            if (url === 'api/auth/session') {
                sessionAttempt += 1;
                if (sessionAttempt === 1 || sessionAttempt === 3) {
                    return new Response(JSON.stringify({
                        ok: true,
                        data: { owner_id: 'owner-1', csrf_token: `csrf-${sessionAttempt}` },
                    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
                }
                if (sessionAttempt === 2) {
                    return new Response(JSON.stringify({ ok: true, data: { owner_id: 'owner-1' } }), {
                        status: 200,
                        headers: { 'Content-Type': 'application/json' },
                    });
                }
                throw new Error('offline');
            }
            writes.push(Object.fromEntries(new Headers(options.headers).entries()));
            return new Response(JSON.stringify({ ok: true, data: {} }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            });
        };

        assert.equal((await client.getSession()).ok, true);
        assert.deepEqual(await client.getSession(), {
            ok: false,
            message: '登录已失效',
            httpStatus: 200,
        });
        await client.api.post('/items', {});
        assert.equal(writes[0]['x-csrf-token'], undefined);

        assert.equal((await client.getSession()).ok, true);
        assert.deepEqual(await client.getSession(), {
            ok: false,
            message: '无法连接到 Web 服务',
            httpStatus: null,
        });
        await client.api.post('/items', {});
        assert.equal(writes[1]['x-csrf-token'], undefined);
        """
    )


def test_api_client_normalizes_error_envelopes_and_clears_unauthorized_sessions() -> None:
    """嵌套校验错误、非 JSON 响应、逻辑失败和 401 应产生稳定错误并回收令牌。"""

    _run_api_client(
        r"""
        const responses = [
            new Response(JSON.stringify({ detail: [{ msg: '第一项' }, { message: { detail: '第二项' } }] }), {
                status: 422,
                headers: { 'Content-Type': 'application/json' },
            }),
            new Response('<html>bad gateway</html>', { status: 502 }),
            new Response('not-json', { status: 200 }),
            new Response(JSON.stringify({ ok: false, message: { detail: '逻辑失败' } }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            }),
            new Response(JSON.stringify({
                ok: true,
                data: { owner_id: 'owner-1', csrf_token: 'csrf-live' },
            }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
            new Response(JSON.stringify({ message: 'expired' }), {
                status: 401,
                headers: { 'Content-Type': 'application/json' },
            }),
            new Response(JSON.stringify({ ok: true, data: {} }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            }),
        ];
        const calls = [];
        globalThis.fetch = async (url, options = {}) => {
            calls.push({ url, headers: Object.fromEntries(new Headers(options.headers).entries()) });
            return responses.shift();
        };

        await assert.rejects(client.api.post('/items', {}), { message: '第一项；第二项' });
        await assert.rejects(client.api.get('/items'), { message: 'Request failed' });
        await assert.rejects(client.api.get('/items'), { message: 'Request failed' });
        await assert.rejects(client.api.get('/items'), { message: '逻辑失败' });
        assert.equal((await client.getSession()).ok, true);
        await assert.rejects(client.api.post('/items', {}), { message: 'Unauthorized' });
        await client.api.post('/items', {});
        assert.equal(calls.at(-1).headers['x-csrf-token'], undefined);
        """
    )


def test_login_exchange_demo_and_download_keep_their_public_contracts() -> None:
    """登录入口应返回友好结果，下载应解析 UTF-8 文件名并清洗路径字符。"""

    _run_api_client(
        r"""
        const calls = [];
        const responses = [
            new Response(JSON.stringify({
                ok: true,
                data: { owner_id: 'owner-2', csrf_token: 'csrf-2', expires_at: '2099-02-02' },
            }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
            new Response(JSON.stringify({ detail: [{ msg: '登录码过期' }, { message: '请重试' }] }), {
                status: 400,
                headers: { 'Content-Type': 'application/json' },
            }),
            new Response(JSON.stringify({
                ok: true,
                data: { owner_id: 'owner-3', csrf_token: 'csrf-3' },
            }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
            new Response('payload-1', {
                status: 200,
                headers: {
                    'Content-Disposition': "attachment; filename*=UTF-8''%E5%A4%87%E4%BB%BD.pendo.zip" +
                        '; filename=backup.zip',
                },
            }),
            new Response('payload-2', {
                status: 200,
                headers: { 'Content-Disposition': 'attachment; filename=backup.pendo.zip; size=12' },
            }),
            new Response('payload-3', {
                status: 200,
                headers: { 'Content-Disposition': 'attachment; filename="../unsafe.zip"' },
            }),
            new Response('payload-4', { status: 200 }),
        ];
        globalThis.fetch = async (url, options = {}) => {
            calls.push({
                url,
                method: options.method || 'GET',
                body: options.body,
                credentials: options.credentials,
                headers: Object.fromEntries(new Headers(options.headers).entries()),
            });
            if (url === 'api/auth/demo') throw new Error('offline');
            return responses.shift();
        };

        assert.deepEqual(await client.exchangeLoginCode('code-1'), {
            ok: true,
            ownerId: 'owner-2',
            expiresAt: '2099-02-02',
            message: '',
        });
        assert.equal(calls[0].method, 'POST');
        assert.equal(calls[0].credentials, 'same-origin');
        assert.deepEqual(JSON.parse(calls[0].body), { code: 'code-1' });
        assert.deepEqual(await client.exchangeLoginCode('expired'), {
            ok: false,
            message: '登录码过期；请重试',
            httpStatus: 400,
        });
        assert.deepEqual(await client.createDemoSession(), {
            ok: false,
            message: '无法创建演示空间',
            httpStatus: null,
        });
        assert.equal((await client.getSession()).ok, true);

        const utf8 = await client.apiDownload('/transfer/export/download', {});
        const plain = await client.apiDownload('/transfer/export/download', {});
        const unsafe = await client.apiDownload('/transfer/export/download', {});
        const fallback = await client.apiDownload('/transfer/export/download', {});
        assert.equal(utf8.filename, '备份.pendo.zip');
        assert.equal(plain.filename, 'backup.pendo.zip');
        assert.equal(unsafe.filename, '.._unsafe.zip');
        assert.equal(fallback.filename, 'download.bin');
        assert.ok(utf8.blob instanceof Blob);

        const downloadCalls = calls.filter((call) => call.url === 'api/transfer/export/download');
        assert.equal(downloadCalls.length, 4);
        assert.ok(downloadCalls.every((call) => call.headers['content-type'] === 'application/json'));
        assert.ok(downloadCalls.every((call) => call.headers['x-csrf-token'] === 'csrf-3'));
        """
    )
