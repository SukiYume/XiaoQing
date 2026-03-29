import { getToken, setToken, clearToken, verifyToken } from './api.js';
import { init as initRouter, registerRoute } from './router.js';

// Register all page routes (lazy loaded)
registerRoute('dashboard', () => import('./pages/dashboard.js'));
registerRoute('events', () => import('./pages/events.js'));
registerRoute('tasks', () => import('./pages/tasks.js'));
registerRoute('ledger', () => import('./pages/ledger.js'));
registerRoute('notes', () => import('./pages/notes.js'));
registerRoute('diary', () => import('./pages/diary.js'));
registerRoute('search', () => import('./pages/search.js'));
registerRoute('stats', () => import('./pages/stats.js'));
registerRoute('settings', () => import('./pages/settings.js'));
registerRoute('transfer', () => import('./pages/transfer.js'));

async function bootstrap() {
    const token = getToken();
    if (token) {
        const result = await verifyToken(token);
        if (result.ok) {
            await showApp();
            return;
        }
        clearToken();
        showLogin(result.message || '登录已失效，请重新粘贴令牌。');
        return;
    }
    showLogin();
}

function showLogin(initialError = '') {
    document.getElementById('login-screen').style.display = 'flex';
    document.getElementById('app').style.display = 'none';

    const btn = document.getElementById('login-btn');
    const clearBtn = document.getElementById('login-clear-btn');
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
        const token = input.value.trim();
        if (!token) {
            error.textContent = '请先粘贴登录令牌';
            error.style.display = 'block';
            input.focus();
            return;
        }

        btn.disabled = true;
        clearBtn.disabled = true;
        btn.textContent = '验证中...';
        error.style.display = 'none';
        helper.textContent = '正在校验令牌…';

        const result = await verifyToken(token);
        if (result.ok) {
            setToken(token);
            await showApp();
        } else {
            error.textContent = result.message || '令牌无效或已过期';
            error.style.display = 'block';
            helper.textContent = '请回到聊天中重新生成令牌后再试。';
        }
        btn.disabled = false;
        clearBtn.disabled = false;
        btn.textContent = '进入 Pendo';
    };

    btn.onclick = submit;
    clearBtn.onclick = () => {
        input.value = '';
        error.style.display = 'none';
        helper.textContent = '令牌只保存在当前浏览器，可在设置页随时退出。';
        input.focus();
    };

    input.onkeydown = (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') submit();
    };
    input.focus();
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
}

bootstrap();
