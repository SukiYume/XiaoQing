let csrfToken = '';

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

function formatErrorDetail(detail) {
    if (Array.isArray(detail)) {
        return detail
            .map((item) => item?.msg || item?.message || item?.detail || '')
            .filter(Boolean)
            .join('；');
    }
    if (detail && typeof detail === 'object') {
        return detail.message || detail.detail || '';
    }
    return detail || '';
}

function rememberSession(data) {
    csrfToken = String(data?.csrf_token || '');
    return {
        ok: Boolean(data?.owner_id && csrfToken),
        ownerId: data?.owner_id || '',
        expiresAt: data?.expires_at || null,
        message: '',
    };
}

function sessionHeaders(options = {}) {
    const headers = { ...options.headers };
    const method = String(options.method || 'GET').toUpperCase();
    const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;
    const isBinary = typeof Blob !== 'undefined' && options.body instanceof Blob;
    if (!isFormData && !isBinary && !headers['Content-Type']) {
        headers['Content-Type'] = 'application/json';
    }
    if (!SAFE_METHODS.has(method) && csrfToken) {
        headers['X-CSRF-Token'] = csrfToken;
    }
    return headers;
}

async function request(path, options = {}) {
    const res = await fetch(`api${path}`, {
        ...options,
        headers: sessionHeaders(options),
        credentials: 'same-origin',
    });
    if (res.status === 401) {
        csrfToken = '';
        throw new Error('Unauthorized');
    }

    const data = await res.json();
    if (!data.ok || !res.ok) {
        throw new Error(formatErrorDetail(data.message) || formatErrorDetail(data.detail) || 'Request failed');
    }
    return data;
}

async function parseErrorResponse(res) {
    try {
        const data = await res.json();
        return data.message || formatErrorDetail(data.detail) || 'Request failed';
    } catch {
        return 'Request failed';
    }
}

export const api = {
    get(path, params) {
        const qs = params ? '?' + new URLSearchParams(params).toString() : '';
        return request(path + qs);
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

export async function apiUpload(path, body, headers = {}) {
    return request(path, { method: 'POST', body, headers });
}

export async function apiDownload(path, body) {
    const options = { method: 'POST', body: JSON.stringify(body) };
    const res = await fetch(`api${path}`, {
        ...options,
        headers: sessionHeaders(options),
        credentials: 'same-origin',
    });
    if (res.status === 401) {
        csrfToken = '';
        throw new Error('Unauthorized');
    }
    if (!res.ok) {
        throw new Error(await parseErrorResponse(res));
    }

    const blob = await res.blob();
    const disposition = res.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^"]+)"?/i);
    return { blob, filename: match?.[1] || 'download.bin' };
}

export async function getSession() {
    try {
        const res = await fetch('api/auth/session', { credentials: 'same-origin' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
            csrfToken = '';
            return { ok: false, message: data?.message || '登录已失效' };
        }
        return rememberSession(data.data);
    } catch {
        return { ok: false, message: '无法连接到 Web 服务' };
    }
}

export async function exchangeLoginCode(code) {
    try {
        const res = await fetch('api/auth/exchange', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
            return { ok: false, message: data?.message || '登录链接无效或已失效' };
        }
        return rememberSession(data.data);
    } catch {
        return { ok: false, message: '无法连接到 Web 服务' };
    }
}

export async function createDemoSession() {
    try {
        const res = await fetch('api/auth/demo', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
            return { ok: false, message: data?.message || '无法创建演示空间' };
        }
        return rememberSession(data.data);
    } catch {
        return { ok: false, message: '无法创建演示空间' };
    }
}

export async function logout() {
    try {
        await request('/auth/logout', { method: 'POST' });
    } finally {
        csrfToken = '';
    }
}
