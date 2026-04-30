/**
 * Chart.js loader — reuses an existing Chart global or loads Chart.js from CDN.
 */
let _promise = null;

const CHART_CDN = 'https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js';

function appendScript(src) {
    return new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = src;
        script.onload = () => resolve();
        script.onerror = () => reject(new Error(`无法加载 ${src}`));
        document.head.appendChild(script);
    });
}

export function loadChart() {
    if (_promise) return _promise;
    _promise = (async () => {
        if (window.Chart) {
            return window.Chart;
        }

        await appendScript(CHART_CDN);
        if (window.Chart) return window.Chart;

        throw new Error('Chart.js 加载失败');
    })();
    return _promise;
}
