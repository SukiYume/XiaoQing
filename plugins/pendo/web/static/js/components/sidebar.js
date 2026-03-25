import { onRouteChange } from '../router.js';

const NAV_ITEMS = [
    { path: 'dashboard', label: '总览', icon: '📊', color: 'var(--color-dashboard)' },
    { path: 'events',    label: '日程', icon: '🗓️', color: 'var(--color-events)' },
    { path: 'tasks',     label: '待办', icon: '✅', color: 'var(--color-tasks)' },
    { path: 'ledger',    label: '记账', icon: '💰', color: 'var(--color-ledger)' },
    { path: 'notes',     label: '笔记', icon: '📝', color: 'var(--color-notes)' },
    { path: 'diary',     label: '日记', icon: '📔', color: 'var(--color-diary)' },
    { path: 'search',    label: '搜索', icon: '🔍', color: 'var(--color-search)' },
    { path: 'stats',     label: '统计', icon: '📈', color: 'var(--color-stats)' },
];
const BOTTOM_ITEMS = [
    { path: 'settings', label: '设置', icon: '⚙️', color: 'var(--color-text-secondary)' },
];

export function renderSidebar(container) {
    const sidebar = document.createElement('aside');
    sidebar.className = 'sidebar';

    // Logo
    sidebar.innerHTML = `<div class="sidebar-logo"><span class="sidebar-logo-icon">📋</span><span class="sidebar-logo-text">Pendo</span></div>`;

    // Nav
    const nav = document.createElement('nav');
    nav.className = 'sidebar-nav';

    NAV_ITEMS.forEach(item => {
        nav.appendChild(createNavItem(item));
    });

    // Separator + bottom items
    const sep = document.createElement('div');
    sep.className = 'sidebar-separator';
    nav.appendChild(sep);

    BOTTOM_ITEMS.forEach(item => {
        nav.appendChild(createNavItem(item));
    });

    sidebar.appendChild(nav);
    container.appendChild(sidebar);

    // Track active route
    onRouteChange(path => {
        sidebar.querySelectorAll('.nav-item').forEach(el => {
            const isActive = el.dataset.path === path;
            el.classList.toggle('active', isActive);
            if (isActive) {
                el.style.borderLeftColor = el.dataset.color;
            } else {
                el.style.borderLeftColor = 'transparent';
            }
        });
    });
}

function createNavItem(item) {
    const a = document.createElement('a');
    a.href = `#/${item.path}`;
    a.className = 'nav-item';
    a.dataset.path = item.path;
    a.dataset.color = item.color;
    a.innerHTML = `<span class="nav-icon">${item.icon}</span><span class="nav-label">${item.label}</span>`;
    return a;
}
