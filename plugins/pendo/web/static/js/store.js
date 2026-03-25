const state = {
    user: null,
    currentPage: 'dashboard',
    categories: null,
    diaryTemplates: null,
};

const listeners = new Set();

export function getState() { return state; }

export function setState(updates) {
    Object.assign(state, updates);
    listeners.forEach(fn => fn(state));
}

export function subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
}
