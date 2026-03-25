/**
 * Chart.js loader — loads Chart.js from CDN at runtime.
 * If a local chart.min.js exists, it will have already set window.Chart.
 * Otherwise, we load from jsdelivr CDN.
 */
let _promise = null;

export function loadChart() {
    if (_promise) return _promise;
    _promise = new Promise((resolve, reject) => {
        if (window.Chart) {
            resolve(window.Chart);
            return;
        }
        // Try local first, then CDN
        const sources = [
            '/static/js/lib/chart.min.js',
            'https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js',
        ];
        let idx = 0;
        function tryNext() {
            if (idx >= sources.length) {
                reject(new Error('Chart.js 加载失败'));
                return;
            }
            const script = document.createElement('script');
            script.src = sources[idx++];
            script.onload = () => {
                if (window.Chart) resolve(window.Chart);
                else tryNext();
            };
            script.onerror = tryNext;
            document.head.appendChild(script);
        }
        tryNext();
    });
    return _promise;
}
