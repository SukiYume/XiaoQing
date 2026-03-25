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

async function bootstrap() {
    const token = getToken();
    if (token) {
        const valid = await verifyToken(token);
        if (valid) {
            await showApp();
            return;
        }
        clearToken();
    }
    showLogin();
}

function showLogin() {
    document.getElementById('login-screen').style.display = 'flex';
    document.getElementById('app').style.display = 'none';

    const btn = document.getElementById('login-btn');
    const input = document.getElementById('token-input');
    const error = document.getElementById('login-error');

    btn.onclick = async () => {
        const token = input.value.trim();
        if (!token) return;
        btn.disabled = true;
        btn.textContent = '验证中...';
        error.style.display = 'none';

        const valid = await verifyToken(token);
        if (valid) {
            setToken(token);
            await showApp();
        } else {
            error.textContent = 'Token 无效或已过期';
            error.style.display = 'block';
        }
        btn.disabled = false;
        btn.textContent = '登录';
    };

    input.onkeydown = (e) => { if (e.key === 'Enter') btn.click(); };
    input.focus();
}

async function showApp() {
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('app').style.display = 'flex';

    // Init layout components
    const { renderSidebar } = await import('./components/sidebar.js');
    const { renderHeader } = await import('./components/header.js');
    const { renderFab } = await import('./components/fab.js');

    renderSidebar(document.getElementById('sidebar-container'));
    renderHeader(document.getElementById('header-container'));
    renderFab(document.getElementById('fab-container'));

    // Init router (loads current page)
    await initRouter(document.getElementById('content'));
}

bootstrap();
