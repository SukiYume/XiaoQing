const TOAST_TYPES         = new Set(['success', 'error', 'info', 'warning']);
const DISPLAY_DURATION_MS = 3000;
const EXIT_DURATION_MS    = 250;

/**
 * 显示一条全局提示。
 *
 * 消息使用原生文本节点渲染，避免把接口错误或用户输入解释为 HTML；提示类型也只允许
 * 使用已有样式，未知值统一回退为普通信息提示。
 */
export function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) {
        throw new Error('缺少 #toast-container 提示容器');
    }

    const normalizedType = typeof type === 'string' ? type.trim().toLowerCase() : '';
    const toastType      = TOAST_TYPES.has(normalizedType) ? normalizedType : 'info';

    const toast = document.createElement('div');
    toast.className = `toast toast-${toastType}`;

    const messageElement = document.createElement('span');
    messageElement.className = 'toast-message';
    messageElement.textContent = String(message ?? '');

    const dismissButton = document.createElement('button');
    dismissButton.type = 'button';
    dismissButton.className = 'toast-dismiss';
    dismissButton.textContent = '×';
    dismissButton.setAttribute('aria-label', '关闭提示');
    toast.append(messageElement, dismissButton);

    // 关闭动作必须幂等：手动关闭后清除自动计时器，只保留一次退场和节点移除。
    let autoDismissTimer;
    let isDismissing = false;
    const dismiss    = () => {
        if (isDismissing) return;
        isDismissing = true;
        clearTimeout(autoDismissTimer);
        toast.classList.remove('toast-show');
        setTimeout(() => toast.remove(), EXIT_DURATION_MS);
    };

    dismissButton.addEventListener('click', dismiss);
    container.appendChild(toast);

    // 节点挂载后再切换状态，确保浏览器能够播放进入动画。
    requestAnimationFrame(() => {
        if (!isDismissing) toast.classList.add('toast-show');
    });
    autoDismissTimer = setTimeout(dismiss, DISPLAY_DURATION_MS);
}
