const TOKEN_KEY = 'pendo_token';

export function getToken() { return localStorage.getItem(TOKEN_KEY); }
export function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }
export function clearToken() { localStorage.removeItem(TOKEN_KEY); }

async function request(path, options = {}) {
    const token = getToken();
    const headers = {
        'Content-Type': 'application/json',
        ...options.headers,
    };
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
        throw new Error(data.message || data.detail || 'Request failed');
    }
    return data;
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
