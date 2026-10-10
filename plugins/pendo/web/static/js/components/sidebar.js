/** Pendo Web 主导航的分区渲染、路由状态和移动端抽屉交互。 */

import { onRouteChange } from '../router.js';

const MOBILE_QUERY = '(max-width: 768px)';
const NAV_SECTIONS = [
    {
        label: '工作区',
        items: [
            {
                path: 'dashboard',
                label: '总览',
                icon: '📊',
                color: 'var(--color-dashboard)',
                hint: '今日状态',
            },
            {
                path: 'events',
                label: '日程',
                icon: '🗓️',
                color: 'var(--color-events)',
                hint: '安排与提醒',
            },
            {
                path: 'tasks',
                label: '待办',
                icon: '✅',
                color: 'var(--color-tasks)',
                hint: '执行清单',
            },
            {
                path: 'ledger',
                label: '记账',
                icon: '💰',
                color: 'var(--color-ledger)',
                hint: '收支脉搏',
            },
            {
                path: 'notes',
                label: '笔记',
                icon: '📝',
                color: 'var(--color-notes)',
                hint: '知识沉淀',
            },
            {
                path: 'diary',
                label: '日记',
                icon: '📔',
                color: 'var(--color-diary)',
                hint: '月度书写',
            },
            {
                path: 'search',
                label: '搜索',
                icon: '🔍',
                color: 'var(--color-search)',
                hint: '跨模块检索',
            },
            {
                path: 'stats',
                label: '统计',
                icon: '📈',
                color: 'var(--color-stats)',
                hint: '整体分析',
            },
        ],
    },
    {
        label: '偏好',
        items: [
            {
                path: 'settings',
                label: '设置',
                icon: '⚙️',
                color: 'var(--color-text-secondary)',
                hint: '偏好与安全',
            },
        ],
    },
];

function createNavItem(item) {
    const link = document.createElement('a');
    link.href = `#/${item.path}`;
    link.tabIndex = 0;
    link.className = 'nav-item';
    link.dataset.path = item.path;
    link.dataset.module = item.path;
    link.style.setProperty('--nav-accent', item.color);

    const iconShell = document.createElement('span');
    iconShell.className = 'nav-icon-shell';
    const icon = document.createElement('span');
    icon.className = 'nav-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = item.icon;
    iconShell.appendChild(icon);

    const copy = document.createElement('span');
    copy.className = 'nav-copy';
    const label = document.createElement('span');
    label.className = 'nav-label';
    label.textContent = item.label;
    const hint = document.createElement('span');
    hint.className = 'nav-hint';
    hint.textContent = item.hint;
    copy.append(label, hint);
    link.append(iconShell, copy);
    return link;
}

export function renderSidebar(container) {
    if (!container?.replaceChildren) throw new TypeError('侧栏容器无效');

    const backdrop = document.createElement('button');
    backdrop.type = 'button';
    backdrop.className = 'sidebar-backdrop';
    backdrop.tabIndex = -1;
    backdrop.setAttribute('aria-label', '关闭导航菜单');
    backdrop.setAttribute('aria-hidden', 'true');

    const sidebar = document.createElement('aside');
    sidebar.id = 'pendo-sidebar';
    sidebar.className = 'sidebar';
    sidebar.setAttribute('aria-label', '主要导航');
    sidebar.innerHTML = `
        <div class="sidebar-logo">
            <span class="sidebar-logo-icon" aria-hidden="true">📋</span>
            <div class="sidebar-logo-copy">
                <span class="sidebar-logo-text">Pendo</span>
                <span class="sidebar-logo-subtitle">个人管理台</span>
            </div>
        </div>`;

    const navigation = document.createElement('nav');
    navigation.className = 'sidebar-nav';
    navigation.setAttribute('aria-label', '功能导航');
    NAV_SECTIONS.forEach((section, sectionIndex) => {
        if (sectionIndex > 0) {
            const separator = document.createElement('div');
            separator.className = 'sidebar-separator';
            separator.setAttribute('role', 'separator');
            navigation.appendChild(separator);
        }

        const sectionLabel = document.createElement('div');
        sectionLabel.className = 'nav-section-label';
        sectionLabel.textContent = section.label;
        navigation.appendChild(sectionLabel);
        section.items.forEach((item) => {
            navigation.appendChild(createNavItem(item));
        });
    });
    sidebar.appendChild(navigation);
    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.className = 'sidebar-close btn btn-secondary';
    closeButton.textContent = '关闭导航';
    sidebar.appendChild(closeButton);
    container.replaceChildren(backdrop, sidebar);

    const mobileMedia   = window.matchMedia(MOBILE_QUERY);
    let previousFocus   = null;
    let backgroundInert = false;
    const background    = document.querySelector('.main-wrapper');
    const setMobileOpen = (open) => {
        const shouldOpen = mobileMedia.matches && Boolean(open);
        const wasOpen    = sidebar.classList.contains('mobile-open');
        if (shouldOpen && !wasOpen) {
            previousFocus = document.activeElement;
            backgroundInert = Boolean(background?.inert);
        }
        sidebar.classList.toggle('mobile-open', shouldOpen);
        sidebar.setAttribute('aria-hidden', mobileMedia.matches && !shouldOpen ? 'true' : 'false');
        backdrop.classList.toggle('visible', shouldOpen);
        backdrop.tabIndex = -1;
        backdrop.setAttribute('aria-hidden', shouldOpen ? 'false' : 'true');
        document.body.classList.toggle('sidebar-open', shouldOpen);
        sidebar.inert = mobileMedia.matches && !shouldOpen;
        closeButton.hidden = !mobileMedia.matches;
        if (background) background.inert = shouldOpen ? true : backgroundInert;

        const toggleButton = document.querySelector('.header-toggle');
        if (toggleButton) {
            toggleButton.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
            toggleButton.setAttribute('aria-label', shouldOpen ? '关闭导航菜单' : '打开导航菜单');
        }
        if (shouldOpen && !wasOpen) {
            const focusNavigation = () => {
                if (sidebar.classList.contains('mobile-open') && !sidebar.contains(document.activeElement)) {
                    closeButton.focus();
                }
            };
            focusNavigation();
            window.setTimeout?.(focusNavigation, 0);
        }
        if (!shouldOpen && wasOpen) previousFocus?.focus?.();
    };
    const closeFromUserAction = () => {
        setMobileOpen(false);
    };

    backdrop.addEventListener('click', closeFromUserAction);
    closeButton.addEventListener('click', closeFromUserAction);
    document.addEventListener('pendo:toggle-sidebar', () => {
        setMobileOpen(!sidebar.classList.contains('mobile-open'));
    });
    document.addEventListener('keydown', (event) => {
        if (event.isComposing) return;
        if (!sidebar.classList.contains('mobile-open')) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            closeFromUserAction();
        } else if (event.key === 'Tab') {
            const links   = [...sidebar.querySelectorAll('.nav-item'), closeButton];
            const current = links.indexOf(document.activeElement);
            if (current < 0 || (event.shiftKey && current === 0) || (!event.shiftKey && current === links.length - 1)) {
                event.preventDefault();
                (event.shiftKey ? links.at(-1) : links[0]).focus();
            }
        }
    });
    mobileMedia.addEventListener('change', () => setMobileOpen(false));

    onRouteChange((path) => {
        sidebar.querySelectorAll('.nav-item').forEach((link) => {
            const active = link.dataset.path === path;
            link.classList.toggle('active', active);
            if (active) link.setAttribute('aria-current', 'page');
            else link.removeAttribute('aria-current');
        });
        setMobileOpen(false);
    });
}
