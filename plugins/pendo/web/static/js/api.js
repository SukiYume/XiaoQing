const TOKEN_KEY = 'pendo_token';

export function getToken() { return localStorage.getItem(TOKEN_KEY); }
export function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }
export function clearToken() { localStorage.removeItem(TOKEN_KEY); }

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

async function request(path, options = {}) {
    const token = getToken();
    const headers = { ...options.headers };
    const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;
    const isBinary = typeof Blob !== 'undefined' && options.body instanceof Blob;
    if (!isFormData && !isBinary && !headers['Content-Type']) {
        headers['Content-Type'] = 'application/json';
    }
    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }

    const res = await fetch(`api${path}`, { ...options, headers });

    if (res.status === 401) {
        clearToken();
        window.location.hash = '';
        window.location.reload();
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
    const token = getToken();
    const headers = { 'Content-Type': 'application/json' };
    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }

    const res = await fetch(`api${path}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
    });

    if (res.status === 401) {
        clearToken();
        window.location.hash = '';
        window.location.reload();
        throw new Error('Unauthorized');
    }
    if (!res.ok) {
        throw new Error(await parseErrorResponse(res));
    }

    const blob = await res.blob();
    const disposition = res.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^"]+)"?/i);
    return {
        blob,
        filename: match?.[1] || 'download.bin',
    };
}

export async function verifyToken(token) {
    try {
        const res = await fetch('api/auth/verify', {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json',
            },
        });
        const data = await res.json().catch(() => ({}));
        return {
            ok: Boolean(res.ok && data.ok),
            ownerId: data?.data?.owner_id || '',
            expiresAt: data?.data?.expires_at || null,
            message: data?.message || data?.detail || '',
        };
    } catch {
        return {
            ok: false,
            ownerId: '',
            expiresAt: null,
            message: '无法连接到 Web 服务',
        };
    }
}

export async function createDemoSession() {
    try {
        const res = await fetch('api/auth/demo', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
        });
        const data = await res.json().catch(() => ({}));
        return {
            ok: Boolean(res.ok && data.ok && data?.data?.token),
            token: data?.data?.token || '',
            ownerId: data?.data?.owner_id || '',
            expiresAt: data?.data?.expires_at || null,
            message: data?.message || data?.detail || '',
        };
    } catch {
        return {
            ok: false,
            token: '',
            ownerId: '',
            expiresAt: null,
            message: '无法创建演示空间',
        };
    }
}
