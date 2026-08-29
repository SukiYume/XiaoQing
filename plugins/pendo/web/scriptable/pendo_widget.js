// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: deep-purple; icon-glyph: magic;

// 使用前填写 Pendo Web 地址和 `/pendo web widget-token` 返回的只读令牌。
const BASE_URL = normalizeBaseUrl('https://example.com/pendo');
const TOKEN = 'PASTE_WIDGET_TOKEN_HERE';
const DEFAULT_MEDIUM_SECTION = 'auto';
// 日历同步：在 Scriptable 内直接运行脚本时，将 Pendo 日程同步到 iOS 日历。
// 设为空字符串（""）可禁用同步功能。
const SYNC_CALENDAR_NAME = 'Pendo';
const SYNC_MARKER = '[由 Pendo Widget 同步]';
const CALENDAR_SYNC_INITIAL_LOOKBACK_DAYS = 30;
const CALENDAR_SYNC_LOOKAHEAD_DAYS = 30;
const CALENDAR_SYNC_MAX_RANGE_DAYS = 3660;
const CALENDAR_SYNC_CURSOR_KEY = `pendo.calendar-last-success:${BASE_URL}:${SYNC_CALENDAR_NAME}`;
const CALENDAR_EVENT_ID_PREFIX = 'Pendo-ID: ';
const PANEL_SECTIONS = new Set(['tasks', 'ledger', 'notes']);

// ---------- 主题（日夜自动切换） ----------
const LIGHT = {
    text: '#17171B',
    subtext: '#55606E',
    line: '#D1D6E0',
};
const DARK = {
    text: '#F5F7FA',
    subtext: '#A7AFBF',
    line: '#343844',
};
const dyn = (light, dark) => Color.dynamic(new Color(light), new Color(dark));
const COLORS = {
    text: dyn(LIGHT.text, DARK.text),
    subtext: dyn(LIGHT.subtext, DARK.subtext),
    line: dyn(LIGHT.line, DARK.line),
    accent: new Color('#FF6A5C'),
    good: new Color('#44D17A'),
    accentBg: Color.dynamic(new Color('#FF6A5C', 0.15), new Color('#FF6A5C', 0.2)),
};

// ---------- 动态背景（原生 LinearGradient 支持自动日夜切换） ----------
// 原生渐变支持动态颜色，避免静态画布无法跟随系统主题切换。
function getDynamicGradient() {
    const gradient = new LinearGradient();
    gradient.locations = [0, 0.35, 0.65, 1];
    gradient.colors = [
        // 四个关键色共同维持浅色与深色主题的层次。
        Color.dynamic(new Color('#FDF6F0'), new Color('#0F0F14')),
        Color.dynamic(new Color('#F7EEF5'), new Color('#161228')),
        Color.dynamic(new Color('#EDE8F5'), new Color('#13111D')),
        Color.dynamic(new Color('#F5F7FA'), new Color('#17171B')),
    ];
    return gradient;
}

// ---------- 装饰背景层（透明静态层，叠加在原生渐变之上） ----------
function drawTransparentDecorations(fam) {
    const { w, h } = fam === 'small' ? { w: 340, h: 340 } : fam === 'large' ? { w: 720, h: 760 } : { w: 720, h: 340 };
    const base = Math.min(w, h);
    const ctx = new DrawContext();
    ctx.size = new Size(w, h);
    ctx.opaque = false; // 关键：透明镂空背景
    ctx.respectScreenScale = false;

    // 低透明度装饰在两种主题下都不会盖住正文。
    const orbs = [
        // 右上角光晕
        {
            x: w * 0.85,
            y: h * 0.05,
            r: base * 0.38,
            color: '#FF6A5C',
            alpha: 0.06,
        },
        // 左下角光晕
        {
            x: w * 0.05,
            y: h * 0.85,
            r: base * 0.32,
            color: '#7B68EE',
            alpha: 0.06,
        },
        // 中偏右光晕
        {
            x: w * 0.55,
            y: h * 0.5,
            r: base * 0.2,
            color: '#44D17A',
            alpha: 0.045,
        },
    ];
    for (const o of orbs) {
        ctx.setFillColor(new Color(o.color, o.alpha));
        ctx.fillEllipse(new Rect(o.x - o.r, o.y - o.r, o.r * 2, o.r * 2));
    }

    // 右下角装饰弧线
    ctx.setStrokeColor(new Color('#FF6A5C', 0.08));
    ctx.setLineWidth(1.5);
    const acx = w * 0.92,
        acy = h * 0.92;
    for (let a = 0; a < 3; a++) {
        const ar = base * (0.18 + a * 0.07);
        ctx.strokeEllipse(new Rect(acx - ar, acy - ar, ar * 2, ar * 2));
    }

    return ctx.getImage();
}

function createWidgetShell({ centerHorizontally = false, padding } = {}) {
    const widget = new ListWidget();
    widget.setPadding(0, 0, 0, 0);
    widget.backgroundGradient = getDynamicGradient();

    const mainStack = widget.addStack();
    mainStack.layoutVertically();
    mainStack.backgroundImage = drawTransparentDecorations(family);
    mainStack.addSpacer();

    let content;
    if (centerHorizontally) {
        const row = mainStack.addStack();
        row.layoutHorizontally();
        row.addSpacer();

        content = row.addStack();
        content.layoutVertically();

        row.addSpacer();
    } else {
        content = mainStack.addStack();
        content.layoutVertically();
    }

    mainStack.addSpacer();
    if (padding) content.setPadding(...padding);
    return { widget, content };
}

const family = config.widgetFamily || 'medium';
// 字体与间距集中管理，保持 small / medium / large 三种尺寸同步。
const TYPE_SCALE = {
    item: 12,
    agendaLead: 10,
    sectionTitle: 16,
    panelSummary: 10,
};
const FONTS = {
    body: (size) => Font.mediumSystemFont(size),
    bodyMono: (size) => Font.mediumMonospacedSystemFont(size),
    section: (size) => Font.semiboldSystemFont(size),
};
// 布局参数：像素值基于 iPhone 14 系列尺寸估算。
// leadWidth = 日程时间列宽度；markerWidth = 面板标记列宽度。
const LAYOUTS = {
    small: {
        padding: [11, 12, 11, 12],
        headerBodyGap: 10,
        header: {
            dateSize: 18,
            weekdaySize: 12,
            countSize: 8,
            gap: 4,
        },
        agenda: {
            limit: 5,
            size: TYPE_SCALE.item,
            leadSize: TYPE_SCALE.agendaLead,
            leadWidth: 66,
            gap: 0,
            rowGap: 3,
        },
    },
    medium: {
        padding: [14, 14, 14, 14],
        topSpacing: 8,
        headerBodyGap: 10,
        leftColWidth: 164,
        rightColWidth: 128,
        header: {
            dateSize: 19,
            weekdaySize: 13,
            countSize: 9,
            gap: 10,
            titleSize: TYPE_SCALE.sectionTitle,
            summarySize: TYPE_SCALE.panelSummary,
            rightTitleOffset: 4,
        },
        agenda: {
            limit: 5,
            size: TYPE_SCALE.item,
            leadSize: TYPE_SCALE.agendaLead,
            leadWidth: 64,
            gap: 2,
            titleLimit: 12,
            rowGap: 6,
        },
        panel: {
            limit: 5,
            size: TYPE_SCALE.item,
            titleLimit: 8,
            rowGap: 6,
            markerWidth: 14,
            markerGap: 4,
        },
    },
    large: {
        padding: [16, 20, 16, 12],
        dividerGap: 6,
        rowSpacing: 8,
        quadWidth: 160,
        headerBodyGap: 12,
        header: {
            dateSize: 22,
            weekdaySize: 14,
            countSize: 10,
            gap: 14,
            titleSize: TYPE_SCALE.sectionTitle,
            titleBottomGap: 7,
        },
        agenda: {
            limit: 5,
            size: TYPE_SCALE.item,
            leadSize: TYPE_SCALE.agendaLead,
            leadWidth: 64,
            gap: 2,
            titleLimit: 12,
            rowGap: 6,
        },
        panel: {
            limit: 5,
            size: TYPE_SCALE.item,
            titleLimit: 7,
            rowGap: 6,
            markerWidth: 14,
            markerGap: 4,
        },
        rightQuadWidth: 144,
        ledger: { titleLimit: 8 },
        notes: { titleLimit: 10 },
    },
};

// ---------- 通用辅助 ----------
function isRecord(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function textValue(value, fallback = '', limit = 200) {
    const normalized = typeof value === 'string' ? value.trim() : '';
    const text = normalized || fallback;
    return Array.from(text).slice(0, limit).join('');
}

function nonNegativeInteger(value) {
    try {
        const number = Number(value);
        return Number.isFinite(number) && number >= 0 ? Math.floor(number) : 0;
    } catch {
        return 0;
    }
}

function errorMessage(error, fallback = '未知错误') {
    return textValue(error?.message) || textValue(error) || fallback;
}

function normalizeBaseUrl(value) {
    return textValue(value, '', 500)
        .trim()
        .split('#')[0]
        .split('?')[0]
        .replace(/\/api\/widget\/summary\/?$/i, '')
        .replace(/\/api\/?$/i, '')
        .replace(/\/+$/, '');
}

function appUrl(path) {
    return `${BASE_URL}/${textValue(path, '#/dashboard').replace(/^\/+/, '')}`;
}

function truncate(text, limit) {
    const value = textValue(text);
    const chars = Array.from(value);
    if (chars.length <= limit) return value;
    return `${chars.slice(0, Math.max(0, limit - 1)).join('')}…`;
}

function normalizePanel(value) {
    if (!isRecord(value)) return null;
    const section = textValue(value.section).toLowerCase();
    if (!PANEL_SECTIONS.has(section)) return null;
    const summary = isRecord(value.summary) ? value.summary : {};
    const items = Array.isArray(value.items)
        ? value.items
              .filter(isRecord)
              .slice(0, 5)
              .map((item) => {
                  const transactionType = textValue(item.transaction_type).toLowerCase();
                  return {
                      title: textValue(item.title, '无标题', 100),
                      meta: textValue(item.meta, '', 120),
                      transaction_type: ['expense', 'income', 'transfer'].includes(transactionType)
                          ? transactionType
                          : '',
                      amount_text: textValue(item.amount_text, '', 60),
                  };
              })
        : [];
    return {
        section,
        title: textValue(value.title, '总览', 60),
        path: textValue(value.path, '#/dashboard', 200),
        summary: {
            primary: textValue(summary.primary, '', 100),
        },
        items,
        empty_text: textValue(value.empty_text, '暂无内容', 120),
    };
}

function normalizeAgendaItems(value, limit = null) {
    if (!Array.isArray(value)) return [];
    const records = value.filter(isRecord);
    const selected = Number.isInteger(limit) && limit >= 0 ? records.slice(0, limit) : records;
    return selected.map((item) => ({
        id: textValue(item.id, '', 160).replace(/[\r\n]/g, '').trim(),
        title: textValue(item.title, '无标题', 160),
        subtitle: textValue(item.subtitle, '', 160),
        meta: textValue(item.meta, '', 160),
        day: parseDateKey(item.day)?.key || '',
        start_time: textValue(item.start_time, '', 64),
        end_time: textValue(item.end_time, '', 64),
        location: textValue(item.location, '', 160),
    }));
}

function normalizeWidgetData(value) {
    if (!isRecord(value) || !isRecord(value.agenda)) {
        throw new Error('小组件摘要结构无效');
    }
    const rawAgenda = value.agenda;
    const rawDate = isRecord(rawAgenda.date) ? rawAgenda.date : {};
    const rawLinks = isRecord(value.links) ? value.links : {};
    const rawPanels = isRecord(value.panels) ? value.panels : {};
    const calendarDay = nonNegativeInteger(rawDate.day);
    const agendaItems = normalizeAgendaItems(rawAgenda.items, 5);
    return {
        generated_at: textValue(value.generated_at, '', 64),
        agenda: {
            date: {
                weekday: textValue(rawDate.weekday, '--', 12),
                day: calendarDay >= 1 && calendarDay <= 31 ? calendarDay : 0,
            },
            today_count: nonNegativeInteger(rawAgenda.today_count),
            tomorrow_count: nonNegativeInteger(rawAgenda.tomorrow_count),
            items: agendaItems,
            empty_text: textValue(rawAgenda.empty_text, '最近没有安排', 120),
        },
        links: {
            dashboard: textValue(rawLinks.dashboard, '#/dashboard', 200),
            events: textValue(rawLinks.events, '#/events', 200),
            tasks: textValue(rawLinks.tasks, '#/tasks', 200),
            ledger: textValue(rawLinks.ledger, '#/ledger', 200),
            notes: textValue(rawLinks.notes, '#/notes', 200),
        },
        panel: normalizePanel(value.panel),
        panels: {
            tasks: normalizePanel(rawPanels.tasks),
            ledger: normalizePanel(rawPanels.ledger),
            notes: normalizePanel(rawPanels.notes),
        },
    };
}

function addText(stack, text, opts = {}) {
    const size = opts.size ?? 12;
    const node = stack.addText(String(text ?? ''));
    node.font = opts.font || Font.systemFont(size);
    node.textColor = opts.color || COLORS.text;
    node.lineLimit = opts.lineLimit ?? 1;
    if (opts.opacity != null) node.textOpacity = opts.opacity;
    if (opts.minimumScaleFactor != null) node.minimumScaleFactor = opts.minimumScaleFactor;
    return node;
}

function getSectionIcon(section) {
    if (section === 'ledger') return 'creditcard';
    if (section === 'tasks') return 'checkmark.circle';
    if (section === 'notes') return 'doc.plaintext';
    if (section === 'events' || section === 'agenda') return 'calendar';
    return 'square.grid.2x2.fill';
}

function renderSectionTitle(stack, title, { size = TYPE_SCALE.sectionTitle, icon } = {}) {
    const row = stack.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();
    if (icon) {
        const sym = SFSymbol.named(icon);
        if (sym) {
            const img = row.addImage(sym.image);
            img.imageSize = new Size(size, size);
            img.tintColor = COLORS.subtext;
            row.addSpacer(5);
        }
    }
    addText(row, title, {
        size,
        font: FONTS.section(size),
    });
    return row;
}

function addSizedTextColumn(parent, text, width, opts = {}) {
    const column = parent.addStack();
    column.layoutHorizontally();
    if (width) column.size = new Size(width, 0);
    addText(column, text, opts);
    column.addSpacer();
}

function getMetaParts(value) {
    return textValue(value)
        .split('·')
        .map((part) => part.trim())
        .filter(Boolean);
}

function firstMetaPart(value) {
    return getMetaParts(value)[0] || '';
}

function parseDateKey(value) {
    const key = textValue(value, '', 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(key)) return null;
    const [year, month, day] = key.split('-').map(Number);
    const date = new Date(year, month - 1, day);
    if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) {
        return null;
    }
    return { key, date };
}

function agendaLeadLabel(item, data) {
    const dayInfo = parseDateKey(item?.day);
    const todayInfo = parseDateKey(textValue(data?.generated_at, '', 10));
    let dayText = '--/--';
    if (dayInfo && todayInfo && dayInfo.key === todayInfo.key) {
        dayText = '今天';
    } else if (dayInfo && todayInfo) {
        const tomorrow = new Date(todayInfo.date);
        tomorrow.setDate(tomorrow.getDate() + 1);
        if (
            dayInfo.date.getFullYear() === tomorrow.getFullYear() &&
            dayInfo.date.getMonth() === tomorrow.getMonth() &&
            dayInfo.date.getDate() === tomorrow.getDate()
        ) {
            dayText = '明天';
        }
    }
    if (dayInfo && dayText === '--/--') {
        dayText = `${dayInfo.date.getMonth() + 1}/${dayInfo.date.getDate()}`;
    }
    const timeText = firstMetaPart(item?.meta || item?.subtitle || '');
    return timeText ? `${dayText} ${timeText}` : dayText;
}

function ledgerAmountKind(item) {
    const explicit = textValue(item?.transaction_type).toLowerCase();
    if (['expense', 'income', 'transfer'].includes(explicit)) return explicit;

    const text = textValue(item?.amount_text);
    if (text.startsWith('↔')) return 'transfer';
    if (text.startsWith('-')) return 'expense';
    return 'income';
}

// ---------- 数据请求 ----------
function resolveWidgetSection(familyValue = family, parameter = args.widgetParameter) {
    if (familyValue === 'large') return 'all';
    if (familyValue === 'small') return 'auto';
    const section = textValue(parameter, DEFAULT_MEDIUM_SECTION, 16).toLowerCase();
    return section === 'auto' || PANEL_SECTIONS.has(section) ? section : DEFAULT_MEDIUM_SECTION;
}

async function fetchData(section, token) {
    const url = `${BASE_URL}/api/widget/summary?section=${encodeURIComponent(section)}`;
    return normalizeWidgetData(await fetchWidgetApi(url, token));
}

async function fetchWidgetApi(url, token) {
    const request = new Request(url);
    request.method = 'GET';
    request.headers = { Authorization: `Bearer ${textValue(token, '', 4096)}` };
    request.timeoutInterval = 20;
    const raw = await request.loadString();
    const status = Number(request.response?.statusCode);
    const hasStatus = Number.isInteger(status) && status > 0;
    let result;
    try {
        result = JSON.parse(String(raw ?? '').replace(/^\uFEFF/, ''));
    } catch {
        const statusText = hasStatus ? `HTTP ${status}: ` : '';
        throw new Error(`${statusText}接口未返回 JSON。请检查 BASE_URL 只填 Pendo Web 根地址，不要带 #/dashboard。`);
    }
    if (hasStatus && status === 401) {
        throw new Error('Widget Token 已失效或被吊销；请更新脚本顶部的 TOKEN');
    }
    if (!isRecord(result) || result.ok !== true || (hasStatus && (status < 200 || status >= 300))) {
        const statusText = hasStatus ? `HTTP ${status}: ` : '';
        throw new Error(textValue(result?.message) || textValue(result?.detail) || `${statusText}Widget 请求失败`);
    }
    return result.data;
}

// ---------- 渲染组件 ----------
function renderDateHeader(stack, data, { dateSize = 19, weekdaySize = 13 } = {}) {
    const line = stack.addStack();
    line.layoutHorizontally();
    line.centerAlignContent();

    const pill = line.addStack();
    pill.backgroundColor = COLORS.accentBg;
    pill.cornerRadius = 6;
    pill.setPadding(3, 5, 3, 5);
    pill.layoutHorizontally();
    pill.centerAlignContent();
    pill.spacing = 4;

    addText(pill, data.agenda.date.weekday || '--', {
        size: weekdaySize,
        color: COLORS.accent,
        font: FONTS.section(weekdaySize),
        lineLimit: 1,
        minimumScaleFactor: 1,
    });
    addText(pill, String(data.agenda.date.day || '--'), {
        size: dateSize,
        color: COLORS.accent,
        font: FONTS.section(dateSize),
        lineLimit: 1,
        minimumScaleFactor: 1,
    });
    return line;
}

function renderCountsInline(stack, data, { size = 10 } = {}) {
    addText(stack, '今天', { size, color: COLORS.subtext });
    stack.addSpacer(2);
    addText(stack, `${data.agenda.today_count}`, {
        size: size + 2,
        color: COLORS.accent,
        font: FONTS.section(size + 2),
    });
    stack.addSpacer(size === 8 ? 4 : 8);
    addText(stack, '明天', { size, color: COLORS.subtext });
    stack.addSpacer(2);
    addText(stack, `${data.agenda.tomorrow_count}`, {
        size: size + 2,
        color: COLORS.good,
        font: FONTS.section(size + 2),
    });
}

// 日程顶栏：日期 + 今天/明天计数。
function renderAgendaSummaryBar(parent, data, { width = 0, url, header } = {}) {
    const bar = parent.addStack();
    bar.layoutHorizontally();
    bar.centerAlignContent();
    if (width) bar.size = new Size(width, 0);
    if (url) bar.url = url;

    renderDateHeader(bar, data, header);
    bar.addSpacer(header.gap);
    renderCountsInline(bar, data, { size: header.countSize });
    bar.addSpacer();
    return bar;
}

// 日程列表：使用固定宽度等宽字体的前导列，保证日期/时间视觉对齐、不随内容跳动。
function renderAgendaList(stack, data, opts = {}) {
    const {
        limit = 5,
        size = TYPE_SCALE.item,
        leadSize,
        leadWidth = 56,
        gap = 4,
        titleLimit = 7,
        columnWidth = 0,
        rowGap = 5,
    } = opts;
    const actualLeadSize = leadSize ?? Math.max(9, size - 2);
    const items = data.agenda.items;
    if (!items.length) {
        addText(stack, data.agenda.empty_text, {
            size,
            color: COLORS.subtext,
            lineLimit: 2,
        });
        return;
    }
    const slice = items.slice(0, limit);
    const url = appUrl(data.links.events);
    const titleWidth = columnWidth ? Math.max(0, columnWidth - leadWidth - gap) : 0;
    for (let i = 0; i < slice.length; i++) {
        const item = slice[i];
        const row = stack.addStack();
        row.layoutHorizontally();
        row.centerAlignContent();
        row.spacing = gap;
        if (columnWidth) row.size = new Size(columnWidth, 0);
        row.url = url;

        const titleText = titleLimit ? truncate(item.title, titleLimit) : item.title;

        addSizedTextColumn(row, agendaLeadLabel(item, data), leadWidth, {
            size: actualLeadSize,
            color: COLORS.subtext,
            font: FONTS.bodyMono(actualLeadSize),
            lineLimit: 1,
        });

        if (titleWidth) {
            addSizedTextColumn(row, titleText, titleWidth, {
                size,
                font: FONTS.body(size),
                lineLimit: 1,
            });
        } else {
            addText(row, titleText, {
                size,
                font: FONTS.body(size),
                lineLimit: 1,
            });
        }

        if (i < slice.length - 1) stack.addSpacer(rowGap);
    }
}

function panelSectionKey(panel) {
    return textValue(panel?.section).toLowerCase();
}

function panelItemMarker(section, item) {
    if (section === 'ledger') {
        const kind = ledgerAmountKind(item);
        if (kind === 'expense') return { icon: 'arrow.down.right', color: COLORS.accent };
        if (kind === 'transfer') return { icon: 'arrow.left.arrow.right', color: COLORS.subtext };
        return { icon: 'arrow.up.right', color: COLORS.good };
    }
    if (section === 'notes') return { icon: 'doc.text.fill', color: COLORS.subtext };
    const status = firstMetaPart(item?.meta || '');
    if (status === '已完成') return { icon: 'checkmark.circle.fill', color: COLORS.good };
    if (status === '已取消') return { icon: 'xmark.circle.fill', color: COLORS.subtext };
    return { icon: 'circle', color: COLORS.subtext };
}

// 统一面板列表渲染器，覆盖 tasks / ledger / notes；差异仅在标记样式、标题截断和尾部文字。
function renderPanelList(stack, panel, data, opts = {}) {
    const {
        limit = 5,
        size = TYPE_SCALE.item,
        titleLimit,
        columnWidth = 0,
        markerWidth = 14,
        markerGap = 4,
        rowGap = 4,
    } = opts;
    if (!panel) {
        addText(stack, '暂无内容', { size, color: COLORS.subtext });
        return;
    }
    const section = panelSectionKey(panel);
    const items = panel.items || [];
    const url = appUrl(panel.path || data.links.dashboard);
    if (!items.length) {
        addText(stack, panel.empty_text || '暂无内容', {
            size,
            color: COLORS.subtext,
            lineLimit: 2,
        });
        return;
    }
    const slice = items.slice(0, limit);
    for (let i = 0; i < slice.length; i++) {
        const item = slice[i];
        const titleText = titleLimit ? truncate(item.title, titleLimit) : item.title;
        const row = stack.addStack();
        row.layoutHorizontally();
        row.centerAlignContent();
        if (columnWidth) row.size = new Size(columnWidth, 0);
        row.url = url;

        const marker = panelItemMarker(section, item);
        const markerBox = row.addStack();
        markerBox.layoutHorizontally();
        markerBox.size = new Size(markerWidth, 0);

        const sym = SFSymbol.named(marker.icon);
        if (sym) {
            const markerSize = Math.max(10, size);
            const img = markerBox.addImage(sym.image);
            img.imageSize = new Size(markerSize, markerSize);
            img.tintColor = marker.color;
        } else {
            addText(markerBox, '•', {
                size: Math.max(8, size - 1),
                color: marker.color,
                font: FONTS.body(Math.max(8, size - 1)),
            });
        }
        row.addSpacer(markerGap);

        addText(row, titleText, {
            size,
            font: FONTS.body(size),
        });

        if (item.amount_text) {
            row.addSpacer();
            const amountKind = ledgerAmountKind(item);
            addText(row, item.amount_text, {
                size,
                color:
                    amountKind === 'expense' ? COLORS.accent : amountKind === 'transfer' ? COLORS.subtext : COLORS.good,
                font: FONTS.body(size),
            });
        } else if (section === 'tasks') {
            const metaParts = getMetaParts(item.meta || '');
            const status = truncate(metaParts[metaParts.length - 1] || '', 6);
            if (status) {
                if (opts.inlineTaskStatus) row.addSpacer(opts.statusGap ?? 8);
                else row.addSpacer();
                addText(row, status, {
                    size: size - 1,
                    color: COLORS.subtext,
                });
                if (opts.inlineTaskStatus) row.addSpacer();
            } else {
                row.addSpacer();
            }
        } else {
            row.addSpacer();
        }
        if (i < slice.length - 1) stack.addSpacer(rowGap);
    }
}

// ---------- 布局 ----------
function renderSmall(widget, data) {
    const layout = LAYOUTS.small;
    widget.setPadding(...layout.padding);

    renderAgendaSummaryBar(widget, data, {
        url: appUrl(data.links.events),
        header: layout.header,
    });

    widget.addSpacer(layout.headerBodyGap);

    renderAgendaList(widget, data, { ...layout.agenda });
}

function renderMedium(widget, data) {
    const layout = LAYOUTS.medium;
    widget.setPadding(...layout.padding);

    const leftColWidth = layout.leftColWidth;
    const rightColWidth = layout.rightColWidth;

    const top = widget.addStack();
    top.layoutHorizontally();
    top.centerAlignContent();
    top.spacing = layout.topSpacing;

    renderAgendaSummaryBar(top, data, {
        width: leftColWidth,
        url: appUrl(data.links.events),
        header: layout.header,
    });

    const rightHead = top.addStack();
    rightHead.layoutHorizontally();
    rightHead.centerAlignContent();
    if (layout.header.rightTitleOffset) {
        rightHead.setPadding(layout.header.rightTitleOffset, 0, 0, 0);
    }
    rightHead.size = new Size(rightColWidth, 0);
    rightHead.url = appUrl(data.panel?.path || data.links.dashboard);
    const panelSection = panelSectionKey(data.panel);

    renderSectionTitle(rightHead, data.panel?.title || '总览', {
        size: layout.header.titleSize,
        icon: getSectionIcon(panelSection),
    });
    rightHead.addSpacer(6);
    addText(rightHead, data.panel?.summary?.primary || '', {
        size: layout.header.summarySize,
        color: COLORS.subtext,
    });
    rightHead.addSpacer();

    widget.addSpacer(layout.headerBodyGap);

    const body = widget.addStack();
    body.layoutHorizontally();
    body.spacing = layout.topSpacing;

    const left = body.addStack();
    left.layoutVertically();
    left.size = new Size(leftColWidth, 0);
    left.url = appUrl(data.links.events);
    renderAgendaList(left, data, { ...layout.agenda, columnWidth: leftColWidth });

    const right = body.addStack();
    right.layoutVertically();
    right.size = new Size(rightColWidth, 0);
    renderPanelList(right, data.panel, data, {
        ...layout.panel,
        columnWidth: rightColWidth,
    });
}

function renderQuadrant(
    parent,
    title,
    url,
    bodyFn,
    { width = 150, titleSize = TYPE_SCALE.sectionTitle, icon, titleBottomGap = 5 } = {},
) {
    const col = parent.addStack();
    col.layoutVertically();
    col.size = new Size(width, 0);
    col.url = url;

    renderSectionTitle(col, title, { size: titleSize, icon });
    col.addSpacer(titleBottomGap);
    bodyFn(col);
    col.addSpacer();
    return col;
}

function renderLarge(widget, data) {
    const layout = LAYOUTS.large;
    widget.setPadding(...layout.padding);
    const panels = data.panels;
    const tasks = panels.tasks;
    const ledger = panels.ledger;
    const notes = panels.notes;

    renderAgendaSummaryBar(widget, data, {
        url: appUrl(data.links.dashboard),
        header: layout.header,
    });

    widget.addSpacer(layout.headerBodyGap);

    const quadWidth = layout.quadWidth;
    const rows = [
        [
            {
                title: '日程',
                url: appUrl(data.links.events),
                width: quadWidth,
                iconSection: 'events',
                render: (stack) => {
                    renderAgendaList(stack, data, {
                        ...layout.agenda,
                        columnWidth: quadWidth,
                    });
                },
            },
            {
                title: tasks?.title || '待办',
                url: appUrl(tasks?.path || data.links.tasks),
                width: layout.rightQuadWidth,
                iconSection: 'tasks',
                render: (stack) => {
                    renderPanelList(stack, tasks, data, {
                        ...layout.panel,
                        columnWidth: layout.rightQuadWidth,
                        inlineTaskStatus: true,
                        statusGap: 8,
                    });
                },
            },
        ],
        [
            {
                title: ledger?.title || '财务',
                url: appUrl(ledger?.path || data.links.ledger),
                width: quadWidth,
                iconSection: 'ledger',
                render: (stack) => {
                    renderPanelList(stack, ledger, data, {
                        ...layout.panel,
                        titleLimit: layout.ledger.titleLimit,
                        columnWidth: quadWidth,
                    });
                },
            },
            {
                title: notes?.title || '笔记',
                url: appUrl(notes?.path || data.links.notes),
                width: layout.rightQuadWidth,
                iconSection: 'notes',
                render: (stack) => {
                    renderPanelList(stack, notes, data, {
                        ...layout.panel,
                        titleLimit: layout.notes.titleLimit,
                        columnWidth: layout.rightQuadWidth,
                    });
                },
            },
        ],
    ];

    rows.forEach((specs, index) => {
        if (index > 0) {
            widget.addSpacer(layout.dividerGap);
            const line = widget.addStack();
            line.size = new Size(0, 1);
            line.backgroundColor = COLORS.line;
            widget.addSpacer(layout.dividerGap);
        }
        const row = widget.addStack();
        row.layoutHorizontally();
        row.spacing = layout.rowSpacing;
        specs.forEach((spec) => {
            renderQuadrant(row, spec.title, spec.url, spec.render, {
                width: spec.width,
                titleSize: layout.header.titleSize,
                icon: getSectionIcon(spec.iconSection),
                titleBottomGap: layout.header.titleBottomGap,
            });
        });
    });
}

// ---------- 刷新调度 ----------
function parseItemStartDate(item, { allowDateOnly = false } = {}) {
    // 优先使用后端传来的结构化 start_time，避免从 meta 字符串解析
    const raw = textValue(item?.start_time, '', 64);
    if (raw.length >= 16) {
        const parsed = new Date(raw);
        if (!Number.isNaN(parsed.getTime())) return parsed;
    }

    // 兜底：从 day + meta/subtitle 推断
    const dayInfo = parseDateKey(item?.day);
    if (!dayInfo) return null;
    const time = firstMetaPart(item?.meta || item?.subtitle || '');
    const match = /^([01]\d|2[0-3]):([0-5]\d)/.exec(time);
    if (match) {
        const parsed = new Date(dayInfo.date);
        parsed.setHours(Number(match[1]), Number(match[2]), 0, 0);
        return parsed;
    }

    if (!allowDateOnly) return null;
    return new Date(dayInfo.date);
}

// iOS 对 widget 刷新有节流，`refreshAfterDate` 只是建议。
// 策略（按最早者取）：
//   1. 下一个未开始的日程前 5 分钟 —— 临近事件时主动刷新
//      若事件已在 5 分钟内，改为 1 分钟后尽快刷新
//   2. 次日 00:05 —— 跨天时更新"今天/明天"计数
//   3. 兜底 30 分钟
function computeNextRefresh(data, currentTime = new Date()) {
    const now =
        currentTime instanceof Date && !Number.isNaN(currentTime.getTime()) ? new Date(currentTime) : new Date();
    const candidates = [];

    const rollover = new Date(now);
    rollover.setDate(rollover.getDate() + 1);
    rollover.setHours(0, 5, 0, 0);
    candidates.push(rollover);

    const earliest = new Date(now.getTime() + 60 * 1000);
    for (const item of data?.agenda?.items || []) {
        const start = parseItemStartDate(item);
        if (start && start > now) {
            // 提前 5 分钟刷新；若事件已在 5 分钟内，则尽快（1 分钟后）刷新
            const preAlert = new Date(start.getTime() - 5 * 60 * 1000);
            candidates.push(preAlert > earliest ? preAlert : earliest);
        }
    }

    candidates.push(new Date(now.getTime() + 30 * 60 * 1000));

    const future = candidates
        .filter((date) => date.getTime() >= earliest.getTime())
        .sort((left, right) => left.getTime() - right.getTime());
    return future[0] || new Date(now.getTime() + 30 * 60 * 1000);
}

// ---------- 日历同步 ----------
// 仅在 app 内直接运行脚本时触发；widget 渲染时不执行同步。
// Keychain 游标记录“上次成功运行日”。每次至少回看 30 天并覆盖未来 30 天；
// 游标更早时从游标续传，既补齐间隔，也重新对账近期修改和删除。
// 每次只查询一次这个完整窗口，不遍历或删除窗口外的日历事件。
function localDateKey(value) {
    const year = value.getFullYear();
    const month = String(value.getMonth() + 1).padStart(2, '0');
    const day = String(value.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
}

function addLocalDays(value, days) {
    const result = new Date(value);
    result.setDate(result.getDate() + days);
    return result;
}

function buildCalendarSyncWindow(currentTime = new Date()) {
    const now =
        currentTime instanceof Date && !Number.isNaN(currentTime.getTime()) ? new Date(currentTime) : new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const plannedEnd = addLocalDays(today, CALENDAR_SYNC_LOOKAHEAD_DAYS);
    let lastSuccess = null;
    if (Keychain.contains(CALENDAR_SYNC_CURSOR_KEY)) {
        lastSuccess = parseDateKey(Keychain.get(CALENDAR_SYNC_CURSOR_KEY));
        if (!lastSuccess) Keychain.remove(CALENDAR_SYNC_CURSOR_KEY);
    }

    // 始终回看最近一段时间，确保已同步日程的修改和删除也能被重新对账；
    // 若上次成功运行更早，则仍从旧游标开始补齐完整缺口。
    const storedIsLater = lastSuccess && lastSuccess.date > today;
    const initialStart = addLocalDays(today, -CALENDAR_SYNC_INITIAL_LOOKBACK_DAYS);
    const requestedStart = new Date(
        lastSuccess && lastSuccess.date < initialStart ? lastSuccess.date : initialStart,
    );
    const earliestAllowedStart = addLocalDays(plannedEnd, -(CALENDAR_SYNC_MAX_RANGE_DAYS - 1));
    const rangeStart = requestedStart < earliestAllowedStart ? earliestAllowedStart : requestedStart;
    const nextCursor = storedIsLater ? new Date(lastSuccess.date) : new Date(today);
    return {
        startDate: rangeStart,
        endDate: new Date(plannedEnd),
        startKey: localDateKey(rangeStart),
        endKey: localDateKey(plannedEnd),
        nextCursorKey: localDateKey(nextCursor),
    };
}

function normalizeCalendarSyncData(value, window) {
    if (!isRecord(value) || !Array.isArray(value.items)) {
        throw new Error('日历同步响应结构无效');
    }
    const startKey = parseDateKey(value.start_date)?.key || '';
    const endKey = parseDateKey(value.end_date)?.key || '';
    if (startKey !== window.startKey || endKey !== window.endKey) {
        throw new Error('日历同步响应窗口与请求不一致');
    }
    return {
        startKey,
        endKey,
        items: normalizeAgendaItems(value.items),
    };
}

async function fetchCalendarSyncData(window, token) {
    const query = `start_date=${encodeURIComponent(window.startKey)}&end_date=${encodeURIComponent(window.endKey)}`;
    const value = await fetchWidgetApi(`${BASE_URL}/api/widget/calendar?${query}`, token);
    return normalizeCalendarSyncData(value, window);
}

function calendarLegacyKey(title, startDate) {
    return `${textValue(title, '无标题', 160)}|${startDate.getTime()}`;
}

function calendarSyncMetadata(notes) {
    const text = textValue(notes, '', 4096);
    const lines = text ? text.split(/\r?\n/) : [];
    const managed = lines.includes(SYNC_MARKER);
    if (!managed) return { managed: false, id: '' };
    const idLine = lines.find((line) => line.startsWith(CALENDAR_EVENT_ID_PREFIX));
    const id = idLine
        ? textValue(idLine.slice(CALENDAR_EVENT_ID_PREFIX.length), '', 160).trim()
        : '';
    return { managed: true, id };
}

function calendarSyncNotes(notes, eventId) {
    const currentNotes = textValue(notes, '', 4096);
    const extraLines = currentNotes
        ? currentNotes
              .split(/\r?\n/)
              .filter((line) => line !== SYNC_MARKER && !line.startsWith(CALENDAR_EVENT_ID_PREFIX))
        : [];
    const markerLines = [SYNC_MARKER];
    if (eventId) markerLines.push(`${CALENDAR_EVENT_ID_PREFIX}${eventId}`);
    return [...markerLines, ...extraLines].join('\n');
}

function calendarDatesEqual(actual, expected) {
    return (
        actual instanceof Date &&
        !Number.isNaN(actual.getTime()) &&
        actual.getTime() === expected.getTime()
    );
}

function updateCalendarEventFields(event, item, replacementNotes = null) {
    let changed = false;
    if (String(event.title || '') !== item.title) {
        event.title = item.title;
        changed = true;
    }
    if (!calendarDatesEqual(event.startDate, item.startDate)) {
        event.startDate = item.startDate;
        changed = true;
    }
    if (!calendarDatesEqual(event.endDate, item.endDate)) {
        event.endDate = item.endDate;
        changed = true;
    }
    if (Boolean(event.isAllDay) !== item.isAllDay) {
        event.isAllDay = item.isAllDay;
        changed = true;
    }
    if (String(event.location || '') !== item.location) {
        event.location = item.location;
        changed = true;
    }
    if (replacementNotes !== null && String(event.notes || '') !== replacementNotes) {
        event.notes = replacementNotes;
        changed = true;
    }
    return changed;
}

function addIndexedCalendarEvent(index, key, event) {
    const matches = index.get(key) || [];
    matches.push(event);
    index.set(key, matches);
}

function takeIndexedCalendarEvent(index, key) {
    const matches = index.get(key);
    if (!matches?.length) return null;
    return matches.shift() || null;
}

function parseCalendarSyncItem(item, rangeStart, rangeEnd) {
    if (!isRecord(item)) throw new Error('日历同步响应包含无效日程');
    const id = textValue(item.id, '', 160).replace(/[\r\n]/g, '').trim();
    const startDate = parseItemStartDate(item, { allowDateOnly: true });
    if (!startDate) throw new Error('日历同步响应包含无效日程');

    const hasTime = Boolean(
        textValue(item.start_time) ||
        /^([01]\d|2[0-3]):[0-5]\d/.test(firstMetaPart(item.meta || item.subtitle || '')),
    );

    let endDate = null;
    const endRaw = textValue(item.end_time, '', 64);
    if (endRaw.length >= 16) {
        const parsedEnd = new Date(endRaw);
        if (!Number.isNaN(parsedEnd.getTime()) && parsedEnd > startDate) endDate = parsedEnd;
    }
    if (!endDate) {
        endDate = new Date(startDate);
        if (hasTime) endDate.setHours(endDate.getHours() + 1);
        else endDate.setDate(endDate.getDate() + 1);
    }
    // 跨天日程可以从窗口之前开始，只要仍与本次完整窗口相交。
    if (startDate >= rangeEnd || endDate <= rangeStart) {
        throw new Error('日历同步响应包含窗口外的日程');
    }

    return {
        id,
        title: textValue(item.title, '无标题', 160),
        startDate,
        endDate,
        isAllDay: !hasTime,
        location: textValue(item.location, '', 160),
    };
}

function prepareCalendarSyncItems(items, rangeStart, rangeEnd) {
    if (!Array.isArray(items)) throw new Error('日历同步响应结构无效');
    const prepared = [];
    const identities = new Set();
    for (const rawItem of items) {
        const item = parseCalendarSyncItem(rawItem, rangeStart, rangeEnd);
        const identity = item.id
            ? `id:${item.id}`
            : `fields:${calendarLegacyKey(item.title, item.startDate)}`;
        if (identities.has(identity)) continue;
        identities.add(identity);
        prepared.push(item);
    }
    return prepared;
}

async function findWritableSyncCalendar() {
    let targetCalendar = null;
    try {
        targetCalendar = await Calendar.forEventsByTitle(SYNC_CALENDAR_NAME);
    } catch {
        // 兼容不支持按标题读取的 Scriptable 版本，回退到完整日历列表。
    }
    if (targetCalendar && targetCalendar.allowsContentModifications !== false) {
        return targetCalendar;
    }

    const calendars = await Calendar.forEvents();
    return (
        (Array.isArray(calendars) ? calendars : []).find((calendar) => {
            return (
                calendar.title === SYNC_CALENDAR_NAME &&
                calendar.allowsContentModifications !== false
            );
        }) || null
    );
}

function indexExistingCalendarEvents(existing) {
    const index = {
        managedById: new Map(),
        managedLegacyByKey: new Map(),
        unmanagedByLegacyKey: new Map(),
    };
    for (const event of Array.isArray(existing) ? existing : []) {
        if (!(event?.startDate instanceof Date) || Number.isNaN(event.startDate.getTime())) continue;
        const metadata = calendarSyncMetadata(event.notes);
        if (metadata.id) {
            addIndexedCalendarEvent(index.managedById, metadata.id, event);
            continue;
        }
        const legacyKey = calendarLegacyKey(event.title, event.startDate);
        const targetIndex = metadata.managed
            ? index.managedLegacyByKey
            : index.unmanagedByLegacyKey;
        addIndexedCalendarEvent(targetIndex, legacyKey, event);
    }
    return index;
}

async function reconcileCalendarItem(item, index, targetCalendar, retainedEvents) {
    const legacyKey = calendarLegacyKey(item.title, item.startDate);
    let event = item.id ? (index.managedById.get(item.id) || [])[0] || null : null;
    let replacementNotes = null;
    if (!event) {
        event = takeIndexedCalendarEvent(index.managedLegacyByKey, legacyKey);
        if (event && item.id) replacementNotes = calendarSyncNotes(event.notes, item.id);
    }
    if (event) {
        retainedEvents.add(event);
        if (updateCalendarEventFields(event, item, replacementNotes)) {
            await event.save();
            return 'updated';
        }
        return 'unchanged';
    }

    // 不接管用户自行创建的事件；同名同刻只消费一个旧式匹配，其他 Pendo ID
    // 仍分别写入，避免把不同条目错误合并。
    if (takeIndexedCalendarEvent(index.unmanagedByLegacyKey, legacyKey)) return 'skipped';

    event = new CalendarEvent();
    event.title = item.title;
    event.startDate = item.startDate;
    event.endDate = item.endDate;
    event.isAllDay = item.isAllDay;
    event.calendar = targetCalendar;
    event.location = item.location;
    event.notes = calendarSyncNotes('', item.id);
    await event.save();
    return 'created';
}

async function removeUnretainedManagedEvents(managedById, retainedEvents) {
    let removed = 0;
    for (const matches of managedById.values()) {
        for (const event of matches) {
            if (retainedEvents.has(event)) continue;
            await event.remove();
            removed++;
        }
    }
    return removed;
}

function formatCalendarSyncCounts(counts) {
    const parts = [`新增 ${counts.created}`];
    for (const [key, label] of [
        ['updated', '更新'],
        ['removed', '删除'],
        ['unchanged', '未变化'],
        ['skipped', '跳过'],
    ]) {
        if (counts[key]) parts.push(`${label} ${counts[key]}`);
    }
    return `同步完成：${parts.join('，')}`;
}

async function applyAgendaItemsToCalendar(items, rangeStart, rangeEnd) {
    const preparedItems = prepareCalendarSyncItems(items, rangeStart, rangeEnd);
    const targetCalendar = await findWritableSyncCalendar();
    if (!targetCalendar) {
        return {
            completed: false,
            message: `未找到名为「${SYNC_CALENDAR_NAME}」的日历，请先在系统日历 App 中创建`,
        };
    }

    const existing = await CalendarEvent.between(rangeStart, rangeEnd, [targetCalendar]);
    const index = indexExistingCalendarEvents(existing);
    const retainedEvents = new Set();
    const counts = { created: 0, updated: 0, removed: 0, unchanged: 0, skipped: 0 };
    for (const item of preparedItems) {
        const action = await reconcileCalendarItem(item, index, targetCalendar, retainedEvents);
        counts[action]++;
    }
    counts.removed = await removeUnretainedManagedEvents(index.managedById, retainedEvents);

    return { completed: true, message: formatCalendarSyncCounts(counts) };
}

async function syncCalendarFromServer(token, currentTime = new Date()) {
    if (!SYNC_CALENDAR_NAME) return '同步已禁用（SYNC_CALENDAR_NAME 为空）';

    const window = buildCalendarSyncWindow(currentTime);
    const data = await fetchCalendarSyncData(window, token);
    // 服务端窗口按自然日闭区间定义，Scriptable 的 between 使用右开区间。
    const rangeEnd = addLocalDays(window.endDate, 1);
    const result = await applyAgendaItemsToCalendar(data.items, window.startDate, rangeEnd);
    if (result.completed) {
        Keychain.set(CALENDAR_SYNC_CURSOR_KEY, window.nextCursorKey);
    }
    return result.message;
}

// ---------- 主流程 ----------
function createWidget(data) {
    const { widget, content } = createWidgetShell();
    widget.refreshAfterDate = computeNextRefresh(data);
    widget.url = appUrl(data.links.dashboard);

    if (family === 'small') renderSmall(content, data);
    else if (family === 'large') renderLarge(content, data);
    else renderMedium(content, data);

    return widget;
}

function createErrorWidget(error) {
    const { widget, content } = createWidgetShell({
        centerHorizontally: true,
        padding: [14, 14, 14, 14],
    });

    addText(content, 'Pendo', { size: 18, font: FONTS.section(18) });
    content.addSpacer(8);
    addText(content, '组件加载失败', {
        size: 14,
        color: COLORS.accent,
        font: FONTS.section(14),
    });
    content.addSpacer(6);
    addText(content, truncate(errorMessage(error), 60), {
        size: 12,
        color: COLORS.subtext,
        lineLimit: 3,
    });
    widget.url = appUrl('#/dashboard');
    return widget;
}

let widget;
let widgetData = null;
const widgetToken = textValue(TOKEN, '', 4096);
try {
    if (!/^https?:\/\/[^\s/]+/i.test(BASE_URL) || BASE_URL.includes('example.com')) {
        throw new Error('请先把脚本里的 BASE_URL 改成你自己的 Pendo Web 地址');
    }
    if (!widgetToken || widgetToken === 'PASTE_WIDGET_TOKEN_HERE') {
        throw new Error('请先把脚本顶部的 TOKEN 改成 /pendo web widget-token 返回的令牌');
    }
    widgetData = await fetchData(resolveWidgetSection(), widgetToken);
    widget = createWidget(widgetData);
} catch (error) {
    widget = createErrorWidget(error);
}

// 在 Scriptable App 内直接运行时（而非 Widget 渲染），自动执行日历同步
if (!config.runsInWidget && widgetData) {
    try {
        const result = await syncCalendarFromServer(widgetToken);
        const note = new Notification();
        note.title = 'Pendo 日历同步';
        note.body = result;
        await note.schedule();
        console.log(result);
    } catch (syncErr) {
        console.error(`日历同步失败：${errorMessage(syncErr)}`);
    }
}

Script.setWidget(widget);
Script.complete();
