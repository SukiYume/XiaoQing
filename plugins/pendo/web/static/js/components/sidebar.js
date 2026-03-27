import { onRouteChange } from '../router.js';

const NAV_ITEMS = [
    { path: 'dashboard', label: '总览', icon: '📊', color: 'var(--color-dashboard)', hint: '今日状态' },
    { path: 'events',    label: '日程', icon: '🗓️', color: 'var(--color-events)', hint: '安排与提醒' },
    { path: 'tasks',     label: '待办', icon: '✅', color: 'var(--color-tasks)', hint: '执行清单' },
    { path: 'ledger',    label: '记账', icon: '💰', color: 'var(--color-ledger)', hint: '收支脉搏' },
    { path: 'notes',     label: '笔记', icon: '📝', color: 'var(--color-notes)', hint: '知识沉淀' },
    { path: 'diary',     label: '日记', icon: '📔', color: 'var(--color-diary)', hint: '月度书写' },
    { path: 'search',    label: '搜索', icon: '🔍', color: 'var(--color-search)', hint: '跨模块检索' },
    { path: 'stats',     label: '统计', icon: '📈', color: 'var(--color-stats)', hint: '整体分析' },
];
const BOTTOM_ITEMS = [
    { path: 'settings', label: '设置', icon: '⚙️', color: 'var(--color-text-secondary)', hint: '偏好与安全' },
];

export function renderSidebar(container) {
    const backdrop = document.createElement('button');
    backdrop.type = 'button';
    backdrop.className = 'sidebar-backdrop';
    backdrop.setAttribute('aria-label', '关闭导航菜单');

    const sidebar = document.createElement('aside');
    sidebar.className = 'sidebar';

    // Logo
    sidebar.innerHTML = `
        <div class="sidebar-logo">
            <span class="sidebar-logo-icon">📋</span>
            <div class="sidebar-logo-copy">
                <span class="sidebar-logo-text">Pendo</span>
                <span class="sidebar-logo-subtitle">个人管理台</span>
            </div>
        </div>`;

    // Nav
    const nav = document.createElement('nav');
    nav.className = 'sidebar-nav';

    const topLabel = document.createElement('div');
    topLabel.className = 'nav-section-label';
    topLabel.textContent = '工作区';
    nav.appendChild(topLabel);

    NAV_ITEMS.forEach(item => {
        nav.appendChild(createNavItem(item));
    });

    // Separator + bottom items
    const sep = document.createElement('div');
    sep.className = 'sidebar-separator';
    nav.appendChild(sep);

    const bottomLabel = document.createElement('div');
    bottomLabel.className = 'nav-section-label';
    bottomLabel.textContent = '偏好';
    nav.appendChild(bottomLabel);

    BOTTOM_ITEMS.forEach(item => {
        nav.appendChild(createNavItem(item));
    });

    sidebar.appendChild(nav);
    container.appendChild(backdrop);
    container.appendChild(sidebar);

    const setMobileOpen = (open) => {
        const mobile = window.matchMedia('(max-width: 768px)').matches;
        const shouldOpen = mobile && open;
        sidebar.classList.toggle('mobile-open', shouldOpen);
        backdrop.classList.toggle('visible', shouldOpen);
        document.body.classList.toggle('sidebar-open', shouldOpen);
    };

    backdrop.addEventListener('click', () => setMobileOpen(false));
    document.addEventListener('pendo:toggle-sidebar', () => {
        const isOpen = sidebar.classList.contains('mobile-open');
        setMobileOpen(!isOpen);
    });
    window.addEventListener('resize', () => {
        if (window.innerWidth > 768) {
            setMobileOpen(false);
        }
    });

    // Track active route
    onRouteChange(path => {
        sidebar.querySelectorAll('.nav-item').forEach(el => {
            const isActive = el.dataset.path === path;
            el.classList.toggle('active', isActive);
        });
        if (window.innerWidth <= 768) {
            setMobileOpen(false);
        }
    });
}

function createNavItem(item) {
    const a = document.createElement('a');
    a.href = `#/${item.path}`;
    a.className = 'nav-item';
    a.dataset.path = item.path;
    a.dataset.module = item.path;
    a.style.setProperty('--nav-accent', item.color);
    a.innerHTML = `
        <span class="nav-icon-shell"><span class="nav-icon">${item.icon}</span></span>
        <span class="nav-copy">
            <span class="nav-label">${item.label}</span>
            <span class="nav-hint">${item.hint}</span>
        </span>`;
    return a;
}
