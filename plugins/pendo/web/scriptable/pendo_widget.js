// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: deep-purple; icon-glyph: magic;

// Replace these placeholders with your own deployed Pendo Web address and
// `/pendo web widget-token` value before using the Scriptable widget.
const BASE_URL = "https://example.com/pendo".replace(/\/+$/, "");
const TOKEN = "PASTE_WIDGET_TOKEN_HERE";
const DEFAULT_MEDIUM_SECTION = "auto";
// 日历同步：在 Scriptable 内直接运行脚本时，将 Pendo 日程同步到 iOS 日历。
// 设为空字符串（""）可禁用同步功能。
const SYNC_CALENDAR_NAME = "Pendo";

// ---------- 主题（日夜自动切换） ----------
const LIGHT = {
  bg: "#F5F7FA",
  panel: "#FFFFFF",
  text: "#17171B",
  subtext: "#55606E",
  line: "#D1D6E0", // 加深分割线颜色以修复不可见问题
};
const DARK = {
  bg: "#17171B",
  panel: "#1F2026",
  text: "#F5F7FA",
  subtext: "#A7AFBF",
  line: "#343844",
};
const dyn = (light, dark) => Color.dynamic(new Color(light), new Color(dark));
const COLORS = {
  bg: dyn(LIGHT.bg, DARK.bg),
  panel: dyn(LIGHT.panel, DARK.panel),
  text: dyn(LIGHT.text, DARK.text),
  subtext: dyn(LIGHT.subtext, DARK.subtext),
  line: dyn(LIGHT.line, DARK.line),
  accent: new Color("#FF6A5C"),
  good: new Color("#44D17A"),
  warn: new Color("#F4B740"),
  accentBg: Color.dynamic(
    new Color("#FF6A5C", 0.15),
    new Color("#FF6A5C", 0.2),
  ),
};

// ---------- 动态背景（原生 LinearGradient 支持自动日夜切换） ----------
// Scriptable 中 DrawContext 绘制的是静态图，无法跟随系统随时无缝切换。
// 改为使用系统原生支持的 LinearGradient + Color.dynamic 来实现完美过渡。
function getDynamicGradient() {
  const gradient = new LinearGradient();
  gradient.locations = [0, 0.35, 0.65, 1];
  gradient.colors = [
    // 对应之前 DrawContext 中的 4 个关键渐变色卡
    Color.dynamic(new Color("#FDF6F0"), new Color("#0F0F14")),
    Color.dynamic(new Color("#F7EEF5"), new Color("#161228")),
    Color.dynamic(new Color("#EDE8F5"), new Color("#13111D")),
    Color.dynamic(new Color("#F5F7FA"), new Color("#17171B"))
  ];
  return gradient;
}

// ---------- 装饰背景层（透明静态层，叠加在原生渐变之上） ----------
function bgCanvasSize(fam) {
  if (fam === "small") return { w: 340, h: 340 };
  if (fam === "large") return { w: 720, h: 760 };
  return { w: 720, h: 340 }; // medium
}

function drawTransparentDecorations(fam) {
  const { w, h } = bgCanvasSize(fam);
  const ctx = new DrawContext();
  ctx.size = new Size(w, h);
  ctx.opaque = false; // 关键：透明镂空背景
  ctx.respectScreenScale = false;

  // 使用在深浅色渐变底色下都具美感且不显突兀的折中极高透明度颜色
  const orbs = [
    // 右上角光晕
    { x: w * 0.85, y: h * 0.05, r: w * 0.38, color: "#FF6A5C", alpha: 0.06 },
    // 左下角光晕
    { x: w * 0.05, y: h * 0.85, r: w * 0.32, color: "#7B68EE", alpha: 0.06 },
    // 中偏右光晕
    { x: w * 0.55, y: h * 0.5, r: w * 0.2, color: "#44D17A", alpha: 0.045 },
  ];
  for (const o of orbs) {
    ctx.setFillColor(new Color(o.color, o.alpha));
    ctx.fillEllipse(new Rect(o.x - o.r, o.y - o.r, o.r * 2, o.r * 2));
  }

  // 右下角装饰弧线
  ctx.setStrokeColor(new Color("#FF6A5C", 0.08));
  ctx.setLineWidth(1.5);
  const acx = w * 0.92, acy = h * 0.92;
  for (let a = 0; a < 3; a++) {
    const ar = w * (0.18 + a * 0.07);
    ctx.strokeEllipse(new Rect(acx - ar, acy - ar, ar * 2, ar * 2));
  }

  // 左上角淡淡的粗细节装饰横线
  ctx.setFillColor(new Color("#FFFFFF", 0.1));
  ctx.fillRect(new Rect(w * 0.04, h * 0.18, w * 0.22, 1));

  return ctx.getImage();
}

const family = config.widgetFamily || "medium";
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
  light: (size) => Font.lightSystemFont(size),
};
// 布局参数：像素值基于 iPhone 14 系列尺寸估算。
// leadWidth = 日程时间列宽度；markerWidth = 面板标记列宽度。
const LAYOUTS = {
  small: {
    padding: [11, 12, 11, 12],
    dividerGap: 4,
    header: {
      dateSize: 18,
      weekdaySize: 12,
      countSize: 8,
      gap: 4,
      titleSize: TYPE_SCALE.sectionTitle,
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
    dividerGap: 4,
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
    topSpacing: 8,
    dividerGap: 6,
    rowSpacing: 8,
    quadWidth: 160,
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
function appUrl(path) {
  return `${BASE_URL}/${String(path || "#/dashboard").replace(/^\/+/, "")}`;
}

function truncate(text, limit) {
  const value = String(text || "").trim();
  const chars = Array.from(value);
  if (chars.length <= limit) return value;
  return `${chars.slice(0, Math.max(0, limit - 1)).join("")}…`;
}

function addText(stack, text, opts = {}) {
  const size = opts.size ?? 12;
  const node = stack.addText(String(text || ""));
  node.font = opts.font || Font.systemFont(size);
  node.textColor = opts.color || COLORS.text;
  node.lineLimit = opts.lineLimit ?? 1;
  if (opts.opacity != null) node.textOpacity = opts.opacity;
  if (opts.minimumScaleFactor != null)
    node.minimumScaleFactor = opts.minimumScaleFactor;
  return node;
}

function addDivider(parent, { vertical = false, thickness = 1 } = {}) {
  const line = parent.addStack();
  line.size = vertical ? new Size(thickness, 0) : new Size(0, thickness);
  line.backgroundColor = COLORS.line;
  return line;
}

function addSectionDivider(parent, gap) {
  parent.addSpacer(gap);
  addDivider(parent);
  parent.addSpacer(gap);
}

function getSectionIcon(section) {
  if (section === "ledger") return "creditcard";
  if (section === "tasks") return "checkmark.circle";
  if (section === "notes") return "doc.plaintext";
  if (section === "events" || section === "agenda") return "calendar";
  return "square.grid.2x2.fill";
}

function renderSectionTitle(
  stack,
  title,
  { size = TYPE_SCALE.sectionTitle, icon } = {},
) {
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
  const node = addText(column, text, opts);
  column.addSpacer();
  return { column, node };
}

function getMetaParts(value) {
  return String(value || "")
    .split("·")
    .map((part) => part.trim())
    .filter(Boolean);
}

function firstMetaPart(value) {
  return getMetaParts(value)[0] || "";
}

function lastMetaPart(value) {
  const parts = getMetaParts(value);
  return parts[parts.length - 1] || "";
}

function generatedDateKey(data) {
  const raw = String(data?.generated_at || "");
  return raw.length >= 10 ? raw.slice(0, 10) : "";
}

function shiftDateKey(dateKey, days) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(dateKey || ""))) return "";
  const value = new Date(`${dateKey}T00:00:00`);
  value.setDate(value.getDate() + days);
  const year = value.getFullYear();
  const month = `${value.getMonth() + 1}`.padStart(2, "0");
  const day = `${value.getDate()}`.padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function monthDayLabel(dateKey) {
  const parts = String(dateKey || "").split("-");
  if (parts.length !== 3) return "--/--";
  return `${Number(parts[1])}/${Number(parts[2])}`;
}

function agendaDayLabel(item, data) {
  const day = String(item?.day || "");
  const todayKey = generatedDateKey(data);
  const tomorrowKey = shiftDateKey(todayKey, 1);
  if (day && day === todayKey) return "今天";
  if (day && day === tomorrowKey) return "明天";
  return monthDayLabel(day);
}

function agendaLeadLabel(item, data) {
  const dayText = agendaDayLabel(item, data);
  const timeText = firstMetaPart(item?.meta || item?.subtitle || "");
  return timeText ? `${dayText} ${timeText}` : dayText;
}

function taskStatusLabel(item) {
  return firstMetaPart(item?.meta || "");
}

function amountIsExpense(value) {
  return String(value || "")
    .trim()
    .startsWith("-");
}

// ---------- 数据请求 ----------
function widgetSectionParam() {
  const raw = String(args.widgetParameter || DEFAULT_MEDIUM_SECTION)
    .trim()
    .toLowerCase();
  return raw || DEFAULT_MEDIUM_SECTION;
}

async function fetchData(section) {
  const request = new Request(
    `${BASE_URL}/api/widget/summary?section=${encodeURIComponent(section)}`,
  );
  request.method = "GET";
  request.headers = { Authorization: `Bearer ${TOKEN}` };
  request.timeoutInterval = 20;
  const result = await request.loadJSON();
  if (!result.ok) {
    throw new Error(result.message || "Widget request failed");
  }
  return result.data || {};
}

// ---------- 渲染组件 ----------
function renderDateHeader(
  stack,
  data,
  { dateSize = 19, weekdaySize = 13 } = {},
) {
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

  addText(pill, data.agenda?.date?.weekday || "--", {
    size: weekdaySize,
    color: COLORS.accent,
    font: FONTS.section(weekdaySize),
    lineLimit: 1,
    minimumScaleFactor: 1,
  });
  addText(pill, String(data.agenda?.date?.day || "--"), {
    size: dateSize,
    color: COLORS.accent,
    font: FONTS.section(dateSize),
    lineLimit: 1,
    minimumScaleFactor: 1,
  });
  return line;
}

function renderCountsInline(stack, data, { size = 10 } = {}) {
  addText(stack, "今天", { size, color: COLORS.subtext });
  stack.addSpacer(2);
  addText(stack, `${data.agenda?.today_count ?? 0}`, {
    size: size + 2,
    color: COLORS.accent,
    font: FONTS.section(size + 2),
  });
  stack.addSpacer(size === 8 ? 4 : 8);
  addText(stack, "明天", { size, color: COLORS.subtext });
  stack.addSpacer(2);
  addText(stack, `${data.agenda?.tomorrow_count ?? 0}`, {
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
  const items = data.agenda?.items || [];
  if (!items.length) {
    addText(stack, data.agenda?.empty_text || "最近没有安排", {
      size,
      color: COLORS.subtext,
      lineLimit: 2,
    });
    return;
  }
  const slice = items.slice(0, limit);
  const url = appUrl(data.links?.events);
  const titleWidth = columnWidth
    ? Math.max(0, columnWidth - leadWidth - gap)
    : 0;
  for (let i = 0; i < slice.length; i++) {
    const item = slice[i];
    const row = stack.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();
    row.spacing = gap;
    if (columnWidth) row.size = new Size(columnWidth, 0);
    row.url = url;

    addSizedTextColumn(row, agendaLeadLabel(item, data), leadWidth, {
      size: actualLeadSize,
      color: COLORS.subtext,
      font: FONTS.bodyMono(actualLeadSize),
      lineLimit: 1,
    });

    if (titleWidth) {
      addSizedTextColumn(row, item.title, titleWidth, {
        size,
        font: FONTS.body(size),
        lineLimit: 1,
      });
    } else {
      addText(row, item.title, {
        size,
        font: FONTS.body(size),
        lineLimit: 1,
      });
    }

    if (i < slice.length - 1) stack.addSpacer(rowGap);
  }
}

function panelSectionKey(panel) {
  return String(panel?.section || "").toLowerCase();
}

function panelItemMarker(section, item) {
  if (section === "ledger") {
    return amountIsExpense(item.amount_text)
      ? { icon: "arrow.down.right", color: COLORS.accent, sizeOffset: 0 }
      : { icon: "arrow.up.right", color: COLORS.good, sizeOffset: 0 };
  }
  if (section === "notes")
    return { icon: "doc.text.fill", color: COLORS.subtext, sizeOffset: 0 };
  const status = taskStatusLabel(item);
  if (status === "已完成")
    return { icon: "checkmark.circle.fill", color: COLORS.good, sizeOffset: 0 };
  if (status === "已取消")
    return { icon: "xmark.circle.fill", color: COLORS.subtext, sizeOffset: 0 };
  return { icon: "circle", color: COLORS.subtext, sizeOffset: 0 };
}

function panelItemDetail(section, item) {
  return item.preview || item.meta || "";
}

// 统一面板列表渲染器，覆盖 tasks / ledger / notes；差异仅在标记样式、标题截断和尾部文字。
function renderPanelList(stack, panel, data, opts = {}) {
  const {
    limit = 5,
    size = TYPE_SCALE.item,
    titleLimit,
    showDetail = false,
    columnWidth = 0,
    markerWidth = 14,
    markerGap = 4,
    rowGap = 4,
    showTaskStatus = true,
  } = opts;
  if (!panel) {
    addText(stack, "暂无内容", { size, color: COLORS.subtext });
    return;
  }
  const section = panelSectionKey(panel);
  const items = panel.items || [];
  const url = appUrl(panel.path || data.links?.dashboard);
  if (!items.length) {
    addText(stack, panel.empty_text || "暂无内容", {
      size,
      color: COLORS.subtext,
      lineLimit: 2,
    });
    return;
  }
  const slice = items.slice(0, limit);
  const detailIndent = markerWidth + markerGap;
  for (let i = 0; i < slice.length; i++) {
    const item = slice[i];
    const row = stack.addStack();
    row.layoutVertically();
    if (columnWidth) row.size = new Size(columnWidth, 0);
    row.url = url;

    const main = row.addStack();
    main.layoutHorizontally();
    main.centerAlignContent();
    if (columnWidth) main.size = new Size(columnWidth, 0);

    const marker = panelItemMarker(section, item);
    const markerBox = main.addStack();
    markerBox.layoutHorizontally();
    markerBox.size = new Size(markerWidth, 0);

    if (marker.icon) {
      const sym = SFSymbol.named(marker.icon);
      if (sym) {
        const markerSize = Math.max(10, size + (marker.sizeOffset ?? 0));
        const img = markerBox.addImage(sym.image);
        img.imageSize = new Size(markerSize, markerSize);
        img.tintColor = marker.color;
      }
    } else {
      const markerSize = Math.max(8, size + (marker.sizeOffset ?? -1));
      addText(markerBox, marker.text, {
        size: markerSize,
        color: marker.color,
        font: FONTS.body(markerSize),
      });
    }
    main.addSpacer(markerGap);

    addText(main, item.title, {
      size,
      font: FONTS.body(size),
    });
    main.addSpacer();

    if (item.amount_text) {
      addText(main, item.amount_text, {
        size,
        color: amountIsExpense(item.amount_text) ? COLORS.accent : COLORS.good,
        font: FONTS.body(size),
      });
    } else if (section === "tasks" && showTaskStatus) {
      const status = truncate(lastMetaPart(item.meta || ""), 6);
      if (status) {
        addText(main, status, {
          size: size - 1,
          color: COLORS.subtext,
        });
      }
    }

    if (showDetail) {
      const detail = panelItemDetail(section, item);
      if (detail) {
        row.addSpacer(1);
        const detailRow = row.addStack();
        detailRow.layoutHorizontally();
        if (columnWidth) detailRow.size = new Size(columnWidth, 0);
        detailRow.addSpacer(detailIndent);
        addText(detailRow, truncate(detail, section === "notes" ? 30 : 26), {
          size: size - 1,
          color: COLORS.subtext,
        });
        detailRow.addSpacer();
      }
    }
    if (i < slice.length - 1) stack.addSpacer(showDetail ? rowGap + 1 : rowGap);
  }
}

// ---------- 布局 ----------
function renderSmall(widget, data) {
  const layout = LAYOUTS.small;
  widget.setPadding(...layout.padding);

  renderAgendaSummaryBar(widget, data, {
    url: appUrl(data.links?.events),
    header: layout.header,
  });

  addSectionDivider(widget, layout.dividerGap);

  renderAgendaList(widget, data, { ...layout.agenda });
}

function renderMedium(widget, data) {
  const layout = LAYOUTS.medium;
  widget.setPadding(...layout.padding);
  widget.addSpacer();

  const leftColWidth = layout.leftColWidth;
  const rightColWidth = layout.rightColWidth;

  const top = widget.addStack();
  top.layoutHorizontally();
  top.centerAlignContent();
  top.spacing = layout.topSpacing;

  renderAgendaSummaryBar(top, data, {
    width: leftColWidth,
    url: appUrl(data.links?.events),
    header: layout.header,
  });

  const rightHead = top.addStack();
  rightHead.layoutHorizontally();
  rightHead.centerAlignContent();
  if (layout.header.rightTitleOffset) {
    rightHead.setPadding(layout.header.rightTitleOffset, 0, 0, 0);
  }
  rightHead.size = new Size(rightColWidth, 0);
  rightHead.url = appUrl(data.panel?.path || data.links?.dashboard);
  const panelSection = panelSectionKey(data.panel);

  renderSectionTitle(rightHead, data.panel?.title || "总览", {
    size: layout.header.titleSize,
    icon: getSectionIcon(panelSection),
  });
  rightHead.addSpacer(6);
  addText(rightHead, data.panel?.summary?.primary || "", {
    size: layout.header.summarySize,
    color: COLORS.subtext,
  });
  rightHead.addSpacer();

  addSectionDivider(widget, layout.dividerGap);

  const body = widget.addStack();
  body.layoutHorizontally();
  body.spacing = layout.topSpacing;

  const left = body.addStack();
  left.layoutVertically();
  left.size = new Size(leftColWidth, 0);
  left.url = appUrl(data.links?.events);
  renderAgendaList(left, data, { ...layout.agenda, columnWidth: leftColWidth });

  const right = body.addStack();
  right.layoutVertically();
  right.size = new Size(rightColWidth, 0);
  renderPanelList(right, data.panel, data, {
    ...layout.panel,
    columnWidth: rightColWidth,
  });

  widget.addSpacer();
}

function renderQuadrant(
  parent,
  title,
  url,
  bodyFn,
  {
    width = 150,
    titleSize = TYPE_SCALE.sectionTitle,
    icon,
    titleBottomGap = 5,
  } = {},
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
  widget.addSpacer();
  const panels = data.panels || {};
  const tasks = panels.tasks;
  const ledger = panels.ledger;
  const notes = panels.notes;

  renderAgendaSummaryBar(widget, data, {
    url: appUrl(data.links?.dashboard),
    header: layout.header,
  });

  widget.addSpacer(layout.topSpacing);
  addDivider(widget);
  widget.addSpacer(layout.dividerGap);

  const quadWidth = layout.quadWidth;

  const row1 = widget.addStack();
  row1.layoutHorizontally();
  row1.spacing = layout.rowSpacing;
  renderQuadrant(
    row1,
    "日程",
    appUrl(data.links?.events),
    (s) => {
      renderAgendaList(s, data, { ...layout.agenda, columnWidth: quadWidth });
    },
    {
      width: quadWidth,
      titleSize: layout.header.titleSize,
      icon: getSectionIcon("events"),
      titleBottomGap: layout.header.titleBottomGap,
    },
  );
  renderQuadrant(
    row1,
    tasks?.title || "待办",
    appUrl(tasks?.path || data.links?.tasks),
    (s) => {
      renderPanelList(s, tasks, data, {
        ...layout.panel,
        columnWidth: layout.rightQuadWidth,
      });
    },
    {
      width: layout.rightQuadWidth,
      titleSize: layout.header.titleSize,
      icon: getSectionIcon("tasks"),
      titleBottomGap: layout.header.titleBottomGap,
    },
  );

  addSectionDivider(widget, layout.dividerGap);

  const row2 = widget.addStack();
  row2.layoutHorizontally();
  row2.spacing = layout.rowSpacing;
  renderQuadrant(
    row2,
    ledger?.title || "财务",
    appUrl(ledger?.path || data.links?.ledger),
    (s) => {
      renderPanelList(s, ledger, data, {
        ...layout.panel,
        titleLimit: layout.ledger.titleLimit,
        columnWidth: quadWidth,
      });
    },
    {
      width: quadWidth,
      titleSize: layout.header.titleSize,
      icon: getSectionIcon("ledger"),
      titleBottomGap: layout.header.titleBottomGap,
    },
  );
  renderQuadrant(
    row2,
    notes?.title || "笔记",
    appUrl(notes?.path || data.links?.notes),
    (s) => {
      renderPanelList(s, notes, data, {
        ...layout.panel,
        titleLimit: layout.notes.titleLimit,
        columnWidth: layout.rightQuadWidth,
      });
    },
    {
      width: layout.rightQuadWidth,
      titleSize: layout.header.titleSize,
      icon: getSectionIcon("notes"),
      titleBottomGap: layout.header.titleBottomGap,
    },
  );

  widget.addSpacer();
}

// ---------- 刷新调度 ----------
function parseAgendaStart(item) {
  // 优先使用后端传来的结构化 start_time，避免从 meta 字符串解析
  const raw = String(item?.start_time || "");
  if (raw.length >= 16) {
    const parsed = new Date(raw);
    if (!isNaN(parsed.getTime())) return parsed;
  }
  // 兜底：从 day + meta 推断
  const day = String(item?.day || "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const time = firstMetaPart(item?.meta || item?.subtitle || "");
  const match = /^(\d{2}):(\d{2})/.exec(String(time || ""));
  if (!match) return null;
  const fallback = new Date(`${day}T${match[1]}:${match[2]}:00`);
  return isNaN(fallback.getTime()) ? null : fallback;
}

// iOS 对 widget 刷新有节流，`refreshAfterDate` 只是建议。
// 策略（按最早者取）：
//   1. 下一个未开始的日程前 5 分钟 —— 临近事件时主动刷新
//      若事件已在 5 分钟内，改为 1 分钟后尽快刷新
//   2. 次日 00:05 —— 跨天时更新"今天/明天"计数
//   3. 兜底 30 分钟
function computeNextRefresh(data) {
  const now = new Date();
  const candidates = [];

  const rollover = new Date(now);
  rollover.setDate(rollover.getDate() + 1);
  rollover.setHours(0, 5, 0, 0);
  candidates.push(rollover);

  const earliest = new Date(now.getTime() + 60 * 1000);
  for (const item of data?.agenda?.items || []) {
    const start = parseAgendaStart(item);
    if (start && start > now) {
      // 提前 5 分钟刷新；若事件已在 5 分钟内，则尽快（1 分钟后）刷新
      const preAlert = new Date(start.getTime() - 5 * 60 * 1000);
      candidates.push(preAlert > earliest ? preAlert : earliest);
    }
  }

  candidates.push(new Date(now.getTime() + 30 * 60 * 1000));

  const future = candidates
    .filter((d) => d.getTime() > now.getTime() + 60 * 1000)
    .sort((a, b) => a - b);
  return future[0] || new Date(now.getTime() + 30 * 60 * 1000);
}

// ---------- 日历同步 ----------
// 仅在 app 内直接运行脚本时触发；widget 渲染时不执行同步。
// 同步逻辑：
//  1. 获取完整 agenda（section=auto）
//  2. 在 iOS 日历中查找或创建名为 SYNC_CALENDAR_NAME 的日历
//  3. 读取该日历今天起 30 天内的已有事件
//  4. 对比 title + startTime 去重，仅写入新事件
//  5. 弹出 Notification 展示同步结果
async function syncAgendaToCalendar() {
  if (!SYNC_CALENDAR_NAME) return "同步已禁用（SYNC_CALENDAR_NAME 为空）";

  // 拉取 agenda 数据（复用 widget 的 fetchData）
  const data = await fetchData("auto");
  const items = data?.agenda?.items || [];
  if (!items.length) return "没有需要同步的日程";

  // 查找或创建目标日历
  let targetCal;
  try {
    targetCal = await Calendar.forEventsByTitle(SYNC_CALENDAR_NAME);
  } catch (_) {
    targetCal = null;
  }
  if (!targetCal) {
    // Calendar API 没有 createForEvents，需手动创建+保存
    const allCals = await Calendar.forEvents();
    targetCal = allCals.find(
      (c) => c.title === SYNC_CALENDAR_NAME && c.allowsContentModifications,
    );
  }
  if (!targetCal) {
    // 仍然找不到 → 提示用户先在 iOS 日历 app 中手动创建
    return `未找到名为「${SYNC_CALENDAR_NAME}」的日历，请先在系统日历 App 中创建`;
  }

  const SYNC_MARKER = "[由 Pendo Widget 同步]";

  // 预解析所有远程日程的 title + startTime，用于新增去重和清理旧事件
  const remoteKeys = new Set();
  const parsedItems = [];
  for (const item of items) {
    const startRaw = String(item.start_time || "");
    let startDate = null;
    if (startRaw.length >= 16) {
      startDate = new Date(startRaw);
      if (isNaN(startDate.getTime())) startDate = null;
    }
    if (!startDate && item.day) {
      const timePart = firstMetaPart(item.meta || "");
      const tm = /^(\d{2}):(\d{2})/.exec(timePart);
      if (tm) {
        startDate = new Date(`${item.day}T${tm[1]}:${tm[2]}:00`);
      } else {
        startDate = new Date(`${item.day}T00:00:00`);
      }
    }
    if (!startDate || isNaN(startDate.getTime())) continue;

    let endDate = null;
    const endRaw = String(item.end_time || "");
    if (endRaw.length >= 16) {
      endDate = new Date(endRaw);
      if (isNaN(endDate.getTime())) endDate = null;
    }
    if (!endDate) {
      endDate = new Date(startDate.getTime() + 60 * 60 * 1000);
    }

    const title = String(item.title || "无标题").replace(/…$/, "");
    const key = `${title}|${startDate.getTime()}`;
    remoteKeys.add(key);
    parsedItems.push({ title, startDate, endDate, location: item.location });
  }

  // 读取目标日历中今天起 30 天的已有事件
  const now = new Date();
  const rangeStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const rangeEnd = new Date(rangeStart.getTime() + 30 * 24 * 60 * 60 * 1000);
  const existing = await CalendarEvent.between(rangeStart, rangeEnd, [
    targetCal,
  ]);
  const existingKeys = new Set(
    existing.map((e) => `${e.title}|${e.startDate.getTime()}`),
  );

  // ── 新增：写入远程有、本地无的事件 ──
  let created = 0;
  let skipped = 0;
  for (const pi of parsedItems) {
    const key = `${pi.title}|${pi.startDate.getTime()}`;
    if (existingKeys.has(key)) {
      skipped++;
      continue;
    }
    const event = new CalendarEvent();
    event.title = pi.title;
    event.startDate = pi.startDate;
    event.endDate = pi.endDate;
    event.calendar = targetCal;
    if (pi.location) event.location = pi.location;
    event.notes = SYNC_MARKER;
    event.save();
    existingKeys.add(key);
    created++;
  }

  // ── 清理：删除本地有、远程已不存在的旧同步事件 ──
  // 仅删除由本脚本创建的事件（notes 包含 SYNC_MARKER），不触碰用户手动添加的事件
  let removed = 0;
  for (const e of existing) {
    if (!String(e.notes || "").includes(SYNC_MARKER)) continue;
    const key = `${e.title}|${e.startDate.getTime()}`;
    if (!remoteKeys.has(key)) {
      e.remove();
      removed++;
    }
  }

  const parts = [`新增 ${created}`];
  if (skipped) parts.push(`跳过 ${skipped}`);
  if (removed) parts.push(`清理 ${removed}`);
  return `同步完成：${parts.join("，")}`;
}

// ---------- 主流程 ----------
async function createWidget() {
  let section;
  if (family === "large") section = "all";
  else if (family === "small") section = "auto";
  else section = widgetSectionParam();

  const data = await fetchData(section);

  const widget = new ListWidget();
  widget.setPadding(0, 0, 0, 0); // 移除组件默认内边距
  // 使用原生动态渐变背景
  widget.backgroundGradient = getDynamicGradient();
  widget.refreshAfterDate = computeNextRefresh(data);
  widget.url = appUrl(data.links?.dashboard);

  // 增加统一层级用于显示透明叠加层图
  const mainStack = widget.addStack();
  mainStack.layoutVertically();
  mainStack.backgroundImage = drawTransparentDecorations(family);

  if (family === "small") renderSmall(mainStack, data);
  else if (family === "large") renderLarge(mainStack, data);
  else renderMedium(mainStack, data);

  return widget;
}

function createErrorWidget(error) {
  const widget = new ListWidget();
  widget.setPadding(0, 0, 0, 0);
  widget.backgroundGradient = getDynamicGradient();

  const mainStack = widget.addStack();
  mainStack.layoutVertically();
  mainStack.backgroundImage = drawTransparentDecorations(family);
  mainStack.setPadding(14, 14, 14, 14);

  addText(mainStack, "Pendo", { size: 18, font: FONTS.section(18) });
  mainStack.addSpacer(8);
  addText(mainStack, "组件加载失败", {
    size: 14,
    color: COLORS.accent,
    font: FONTS.section(14),
  });
  mainStack.addSpacer(6);
  addText(mainStack, truncate(error.message || String(error), 60), {
    size: 12,
    color: COLORS.subtext,
    lineLimit: 3,
  });
  widget.url = appUrl("#/dashboard");
  return widget;
}

let widget;
try {
  if (BASE_URL.includes("example.com")) {
    throw new Error("请先把脚本里的 BASE_URL 改成你自己的 Pendo Web 地址");
  }
  if (TOKEN === "PASTE_WIDGET_TOKEN_HERE") {
    throw new Error(
      "请先把脚本里的 TOKEN 改成 /pendo web widget-token 生成的值",
    );
  }
  widget = await createWidget();
} catch (error) {
  widget = createErrorWidget(error);
}

// 在 Scriptable App 内直接运行时（而非 Widget 渲染），自动执行日历同步
if (!config.runsInWidget) {
  try {
    const result = await syncAgendaToCalendar();
    const note = new Notification();
    note.title = "Pendo 日历同步";
    note.body = result;
    note.schedule();
    console.log(result);
  } catch (syncErr) {
    console.error("日历同步失败: " + syncErr.message);
  }
}

Script.setWidget(widget);
Script.complete();
