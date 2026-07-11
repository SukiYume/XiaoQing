import { createDemoSession, exchangeLoginCode, getSession } from './api.js';
import { init as initRouter, registerRoute, getCurrentPage, onRouteChange } from './router.js';

// Register all page routes (lazy loaded)
registerRoute('dashboard', () => import('./pages/dashboard.js?v=20260430'));
registerRoute('events', () => import('./pages/events.js'));
registerRoute('tasks', () => import('./pages/tasks.js'));
registerRoute('ledger', () => import('./pages/ledger.js'));
registerRoute('notes', () => import('./pages/notes.js'));
registerRoute('diary', () => import('./pages/diary.js'));
registerRoute('search', () => import('./pages/search.js'));
registerRoute('stats', () => import('./pages/stats.js'));
registerRoute('settings', () => import('./pages/settings.js'));
registerRoute('transfer', () => import('./pages/transfer.js'));

const BACK_TO_TOP_THEME = {
    dashboard: 'var(--color-dashboard)',
    events: 'var(--color-events)',
    tasks: 'var(--color-tasks)',
    ledger: 'var(--color-ledger)',
    notes: 'var(--color-notes)',
    diary: 'var(--color-diary)',
    search: 'var(--color-search)',
    stats: 'var(--color-stats)',
    settings: 'var(--color-text-secondary)',
    transfer: 'var(--color-dashboard)',
};

function extractLoginCode(rawValue) {
    const text = String(rawValue || '').trim();
    if (!text) return '';
    try {
        const url = new URL(text);
        return url.searchParams.get('code') || '';
    } catch {
        const match = text.match(/(?:[?&]code=|^)([A-Za-z0-9_-]{20,})(?:$|[&#\s])/);
        return match ? match[1] : text;
    }
}

async function bootstrap() {
    const url = new URL(window.location.href);
    const loginCode = url.searchParams.get('code');
    if (loginCode) {
        url.searchParams.delete('code');
        window.history.replaceState({}, document.title, `${url.pathname}${url.search}${url.hash}`);
        const result = await exchangeLoginCode(loginCode);
        if (result.ok) {
            await showApp();
            return;
        }
        showLogin(result.message || '登录链接已失效，请重新生成。');
        return;
    }
    const result = await getSession();
    if (result.ok) {
        await showApp();
        return;
    }
    showLogin(result.message || '');
}

function showLogin(initialError = '') {
    document.getElementById('login-screen').style.display = 'flex';
    document.getElementById('app').style.display = 'none';

    const btn = document.getElementById('login-btn');
    const clearBtn = document.getElementById('login-clear-btn');
    const demoBtn = document.getElementById('login-demo-btn');
    const input = document.getElementById('token-input');
    const error = document.getElementById('login-error');
    const helper = document.getElementById('login-helper');

    if (initialError) {
        error.textContent = initialError;
        error.style.display = 'block';
    } else {
        error.style.display = 'none';
    }

    const submit = async () => {
        const code = extractLoginCode(input.value);
        if (!code) {
            error.textContent = '请先粘贴一次性登录链接或登录码';
            error.style.display = 'block';
            input.focus();
            return;
        }

        if (code !== input.value.trim()) {
            input.value = code;
        }

        btn.disabled = true;
        clearBtn.disabled = true;
        btn.textContent = '验证中...';
        error.style.display = 'none';
        helper.textContent = '正在交换一次性登录码…';

        const result = await exchangeLoginCode(code);
        if (result.ok) {
            await showApp();
        } else {
            error.textContent = result.message || '登录链接无效或已过期';
            error.style.display = 'block';
            helper.textContent = '请回到聊天中重新生成一次性登录链接后再试。';
        }
        btn.disabled = false;
        clearBtn.disabled = false;
        btn.textContent = '进入 Pendo';
    };

    const enterDemo = async () => {
        btn.disabled = true;
        clearBtn.disabled = true;
        demoBtn.disabled = true;
        error.style.display = 'none';
        helper.textContent = '正在创建临时演示空间…';

        const result = await createDemoSession();
        if (result.ok) {
            await showApp();
            return;
        }

        error.textContent = result.message || '暂时无法进入演示空间';
        error.style.display = 'block';
        helper.textContent = '你也可以回到聊天里生成自己的一次性登录链接。';
        btn.disabled = false;
        clearBtn.disabled = false;
        demoBtn.disabled = false;
    };

    btn.onclick = submit;
    clearBtn.onclick = () => {
        input.value = '';
        error.style.display = 'none';
        helper.textContent = '一次性登录码不会保存在浏览器；会话可在设置页随时退出。';
        input.focus();
    };
    demoBtn.onclick = enterDemo;

    input.onkeydown = (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') submit();
    };
    input.focus();
}

function initBackToTop() {
    const btn = document.createElement('button');
    btn.id = 'back-to-top';
    btn.setAttribute('aria-label', '回到顶部');
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="18 15 12 9 6 15"></polyline></svg>';
    document.body.appendChild(btn);

    const style = document.createElement('style');
    style.id = 'pendo-back-to-top-style';
    style.textContent = `
        #back-to-top {
            --btt-accent: var(--color-dashboard);
            position: fixed;
            bottom: 24px;
            right: 20px;
            width: 38px;
            height: 38px;
            border-radius: 50%;
            background: color-mix(in srgb, var(--btt-accent) 68%, transparent);
            color: #fff;
            border: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 8px 18px color-mix(in srgb, var(--btt-accent) 18%, transparent);
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
            opacity: 0;
            transform: translateY(10px) scale(0.92);
            transition: opacity 0.2s ease, transform 0.2s ease, background 0.15s ease;
            pointer-events: none;
            z-index: 500;
            outline: none;
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }
        #back-to-top.btt-visible {
            opacity: 1;
            transform: translateY(0) scale(1);
            pointer-events: auto;
        }
        #back-to-top:hover {
            background: color-mix(in srgb, var(--btt-accent) 82%, transparent);
            transform: translateY(-2px) scale(1);
        }
        #back-to-top:active { transform: translateY(0) scale(0.94); }
        #back-to-top:focus,
        #back-to-top:focus-visible {
            outline: none;
            box-shadow:
                0 0 0 2px rgba(255,255,255,0.88),
                0 0 0 5px color-mix(in srgb, var(--btt-accent) 16%, transparent);
        }
        #back-to-top svg { width: 16px; height: 16px; }
    `;
    document.head.appendChild(style);

    const applyTheme = (path) => {
        btn.style.setProperty('--btt-accent', BACK_TO_TOP_THEME[path] || 'var(--color-dashboard)');
    };
    applyTheme(getCurrentPage());
    onRouteChange((path) => applyTheme(path));

    window.addEventListener('scroll', () => {
        btn.classList.toggle('btt-visible', window.scrollY > 240);
    }, { passive: true });

    btn.addEventListener('click', () => {
        window.scrollTo({ top: 0, behavior: 'smooth' });
    });
}

async function showApp() {
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('app').style.display = 'flex';

    // Init layout components
    const { renderSidebar } = await import('./components/sidebar.js');
    const { renderHeader } = await import('./components/header.js');

    renderSidebar(document.getElementById('sidebar-container'));
    renderHeader(document.getElementById('header-container'));

    // Init router (loads current page)
    await initRouter(document.getElementById('content'));

    initBackToTop();
}

bootstrap();
