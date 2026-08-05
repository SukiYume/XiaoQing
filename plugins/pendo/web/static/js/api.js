/** Pendo Web 的同源请求、会话与下载边界。 */

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
const DEFAULT_DOWNLOAD_FILENAME = 'download.bin';

// CSRF 令牌只保存在当前页面内存中，任何会话失效都会立即清空。
let csrfToken = '';

function formatErrorDetail(detail) {
    if (Array.isArray(detail)) {
        return detail.map(formatErrorDetail).filter(Boolean).join('；');
    }
    if (detail && typeof detail === 'object') {
        return formatErrorDetail(detail.msg ?? detail.message ?? detail.detail ?? '');
    }
    return detail == null ? '' : String(detail);
}

function rememberSession(data, invalidMessage) {
    const ownerId = String(data?.owner_id ?? '').trim();
    const nextCsrfToken = String(data?.csrf_token ?? '').trim();
    const ok = Boolean(ownerId && nextCsrfToken);
    if (!ok) {
        csrfToken = '';
        return { ok: false, message: invalidMessage };
    }
    csrfToken = nextCsrfToken;
    return {
        ok: true,
        ownerId,
        expiresAt: data?.expires_at ?? null,
        message: '',
    };
}

function sessionHeaders(options = {}) {
    const headers = new Headers(options.headers);
    const method = String(options.method || 'GET').toUpperCase();
    if (typeof options.body === 'string' && !headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json');
    }
    if (!SAFE_METHODS.has(method) && csrfToken) {
        headers.set('X-CSRF-Token', csrfToken);
    }
    return headers;
}

async function fetchApi(path, options = {}) {
    const res = await fetch(`api${path}`, {
        ...options,
        headers: sessionHeaders(options),
        credentials: 'same-origin',
    });
    if (res.status === 401) {
        csrfToken = '';
        throw new Error('Unauthorized');
    }
    return res;
}

async function parseErrorResponse(res) {
    try {
        const data = await res.json();
        return formatErrorDetail(data?.message) || formatErrorDetail(data?.detail) || 'Request failed';
    } catch {
        return 'Request failed';
    }
}

async function request(path, options = {}) {
    const res = await fetchApi(path, options);
    if (!res.ok) {
        throw new Error(await parseErrorResponse(res));
    }

    let data;
    try {
        data = await res.json();
    } catch {
        throw new Error('Request failed');
    }
    if (!data?.ok) {
        throw new Error(formatErrorDetail(data?.message) || formatErrorDetail(data?.detail) || 'Request failed');
    }
    return data;
}

// 优先采用 RFC 5987 的 UTF-8 文件名，再回退到传统 filename 参数。
function downloadFilename(disposition) {
    let filename = '';
    const encodedMatch = disposition.match(/filename\*\s*=\s*([^;]+)/i);
    if (encodedMatch) {
        const encoded = encodedMatch[1]
            .trim()
            .replace(/^"(.*)"$/, '$1')
            .replace(/^UTF-8'[^']*'/i, '');
        try {
            filename = decodeURIComponent(encoded);
        } catch {
            filename = encoded;
        }
    }

    if (!filename) {
        const quotedMatch = disposition.match(/filename\s*=\s*"((?:\\.|[^"])*)"/i);
        const plainMatch = disposition.match(/filename\s*=\s*([^;]+)/i);
        filename = quotedMatch?.[1].replace(/\\([\\"])/g, '$1') || plainMatch?.[1].trim() || '';
    }

    filename = filename.replace(/[\\/\u0000-\u001F\u007F]/g, '_').trim();
    return filename && filename !== '.' && filename !== '..' ? filename : DEFAULT_DOWNLOAD_FILENAME;
}

async function requestSession(path, options, invalidMessage, connectionMessage = invalidMessage) {
    try {
        const res = await fetch(path, {
            ...options,
            credentials: 'same-origin',
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data?.ok) {
            csrfToken = '';
            return {
                ok: false,
                message: formatErrorDetail(data?.message) || formatErrorDetail(data?.detail) || invalidMessage,
                httpStatus: res.status,
            };
        }
        const session = rememberSession(data.data, invalidMessage);
        return session.ok ? session : { ...session, httpStatus: res.status };
    } catch {
        csrfToken = '';
        return { ok: false, message: connectionMessage, httpStatus: null };
    }
}

export const api = {
    get(path, params) {
        const query = new URLSearchParams(params || {}).toString();
        return request(query ? `${path}?${query}` : path);
    },
    post(path, body) {
        return request(path, { method: 'POST', body: JSON.stringify(body) });
    },
    put(path, body) {
        return request(path, { method: 'PUT', body: JSON.stringify(body) });
    },
    delete(path) {
        return request(path, { method: 'DELETE' });
    },
};

export function apiUpload(path, body, headers = {}) {
    return request(path, { method: 'POST', body, headers });
}

export async function apiDownload(path, body) {
    const options = { method: 'POST', body: JSON.stringify(body) };
    const res = await fetchApi(path, options);
    if (!res.ok) {
        throw new Error(await parseErrorResponse(res));
    }

    const blob = await res.blob();
    const disposition = res.headers.get('content-disposition') || '';
    return { blob, filename: downloadFilename(disposition) };
}

export function getSession() {
    return requestSession('api/auth/session', {}, '登录已失效', '无法连接到 Web 服务');
}

export function exchangeLoginCode(code) {
    return requestSession(
        'api/auth/exchange',
        {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code }),
        },
        '登录码无效或已失效',
        '无法连接到 Web 服务',
    );
}

export function createDemoSession() {
    return requestSession(
        'api/auth/demo',
        {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        },
        '无法创建演示空间',
    );
}

export async function logout() {
    try {
        await request('/auth/logout', { method: 'POST' });
    } finally {
        csrfToken = '';
    }
}
