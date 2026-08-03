/** Pendo Web 的登录引导、页面路由和全局交互入口。 */

import { createDemoSession, exchangeLoginCode, getSession } from './api.js';
import { init as initRouter, onRouteChange, registerRoute } from './router.js';

// 页面模块保持按需加载，避免登录页预先下载整套管理台代码。
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
    const text = String(rawValue ?? '').trim();
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
    // 首次匿名访问和会话自然过期都是正常登录态，不应暴露后端内部认证错误。
    showLogin(result.httpStatus === 401 ? '' : result.message || '');
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
    let pending = false;

    if (initialError) {
        error.textContent = initialError;
        error.style.display = 'block';
    } else {
        error.textContent = '';
        error.style.display = 'none';
    }

    const setPending = (value) => {
        pending = value;
        btn.disabled = value;
        clearBtn.disabled = value;
        demoBtn.disabled = value;
        input.disabled = value;
    };

    const submit = async () => {
        if (pending) return;
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

        setPending(true);
        btn.textContent = '验证中...';
        error.style.display = 'none';
        helper.textContent = '正在交换一次性登录码…';

        try {
            const result = await exchangeLoginCode(code);
            if (result.ok) {
                await showApp();
                return;
            }
            error.textContent = result.message || '登录链接无效或已过期';
            error.style.display = 'block';
            helper.textContent = '请回到聊天中重新生成一次性登录链接后再试。';
        } catch (cause) {
            console.error('Pendo Web 登录初始化失败:', cause);
            error.textContent = '登录时发生错误，请稍后重试';
            error.style.display = 'block';
            helper.textContent = '页面组件加载失败；你可以刷新页面后重试。';
        } finally {
            setPending(false);
            btn.textContent = '进入 Pendo';
        }
    };

    const enterDemo = async () => {
        if (pending) return;
        setPending(true);
        error.style.display = 'none';
        helper.textContent = '正在创建临时演示空间…';

        try {
            const result = await createDemoSession();
            if (result.ok) {
                await showApp();
                return;
            }

            error.textContent = result.message || '暂时无法进入演示空间';
            error.style.display = 'block';
            helper.textContent = '你也可以回到聊天里生成自己的一次性登录链接。';
        } catch (cause) {
            console.error('Pendo Web 演示空间初始化失败:', cause);
            error.textContent = '暂时无法进入演示空间';
            error.style.display = 'block';
            helper.textContent = '页面组件加载失败；你可以刷新页面后重试。';
        } finally {
            setPending(false);
        }
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
    if (document.getElementById('back-to-top')) return;

    const btn = document.createElement('button');
    btn.id = 'back-to-top';
    btn.type = 'button';
    btn.setAttribute('aria-label', '回到顶部');
    btn.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <polyline points="18 15 12 9 6 15"></polyline>
        </svg>
    `;
    document.body.appendChild(btn);

    const applyTheme = (path) => {
        btn.style.setProperty('--btt-accent', BACK_TO_TOP_THEME[path] || 'var(--color-dashboard)');
    };
    onRouteChange(applyTheme);

    window.addEventListener(
        'scroll',
        () => {
            btn.classList.toggle('btt-visible', window.scrollY > 240);
        },
        { passive: true },
    );

    btn.addEventListener('click', () => {
        const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        window.scrollTo({ top: 0, behavior: reduceMotion ? 'auto' : 'smooth' });
    });
}

async function showApp() {
    // 两个独立布局组件并行加载；加载失败前仍保留可操作的登录界面。
    const [{ renderSidebar }, { renderHeader }] = await Promise.all([
        import('./components/sidebar.js'),
        import('./components/header.js'),
    ]);

    renderSidebar(document.getElementById('sidebar-container'));
    renderHeader(document.getElementById('header-container'));

    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('app').style.display = 'flex';

    // 路由初始化会立即加载当前页面，因此必须等布局可见后再执行。
    await initRouter(document.getElementById('content'));

    initBackToTop();
}

bootstrap().catch((cause) => {
    console.error('Pendo Web 启动失败:', cause);
    showLogin('页面初始化失败，请刷新后重试。');
});
