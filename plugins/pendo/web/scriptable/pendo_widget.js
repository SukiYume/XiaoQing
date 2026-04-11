// Pendo Scriptable Widget
// 1. 把 BASE_URL 改成你的公网地址（保留 /pendo）
// 2. 把 TOKEN 改成 /pendo web widget-token 生成的 token
// 3. 小组件参数可填：tasks / ledger / notes / auto

const BASE_URL = "https://your-host/pendo";
const TOKEN = "PASTE_WIDGET_TOKEN_HERE";
const DEFAULT_SECTION = "auto";

const COLORS = {
  bg: new Color("#17171B"),
  panel: new Color("#1F2026"),
  panelSoft: new Color("#24262D"),
  text: new Color("#F5F7FA"),
  subtext: new Color("#A7AFBF"),
  line: new Color("#343844"),
  accent: new Color("#FF6A5C"),
  good: new Color("#44D17A"),
  warn: new Color("#F4B740"),
};

function appUrl(path) {
  return `${BASE_URL}/${String(path || "#/dashboard").replace(/^\/+/, "")}`;
}

function truncate(text, limit) {
  const value = String(text || "").trim();
  if (value.length <= limit) return value;
  return `${value.slice(0, Math.max(0, limit - 1))}…`;
}

function widgetSection() {
  const raw = String(args.widgetParameter || DEFAULT_SECTION).trim().toLowerCase();
  return raw || DEFAULT_SECTION;
}

async function fetchWidgetData(section) {
  const request = new Request(`${BASE_URL}/api/widget/summary?section=${encodeURIComponent(section)}`);
  request.method = "GET";
  request.headers = {
    Authorization: `Bearer ${TOKEN}`,
  };
  request.timeoutInterval = 20;
  const result = await request.loadJSON();
  if (!result.ok) {
    throw new Error(result.message || "Widget request failed");
  }
  return result.data || {};
}

function addText(stack, text, size, color, opts = {}) {
  const node = stack.addText(String(text || ""));
  node.font = opts.font || Font.systemFont(size);
  node.textColor = color;
  node.lineLimit = opts.lineLimit ?? 1;
  node.minimumScaleFactor = opts.minimumScaleFactor ?? 0.75;
  if (opts.opacity != null) node.textOpacity = opts.opacity;
  return node;
}

function addDivider(stack) {
  const line = stack.addStack();
  line.size = new Size(0, 1);
  line.backgroundColor = COLORS.line;
  return line;
}

function makeCard(parent, opts = {}) {
  const card = parent.addStack();
  card.layoutVertically();
  card.backgroundColor = opts.backgroundColor || COLORS.panel;
  card.cornerRadius = opts.cornerRadius || 18;
  card.setPadding(
    opts.top ?? 14,
    opts.left ?? 14,
    opts.bottom ?? 14,
    opts.right ?? 14
  );
  if (opts.url) card.url = opts.url;
  if (opts.size) card.size = opts.size;
  return card;
}

function renderDateBlock(stack, agenda) {
  const dateStack = stack.addStack();
  dateStack.layoutVertically();
  dateStack.spacing = -2;

  addText(dateStack, agenda?.date?.weekday || "--", 18, COLORS.accent, {
    font: Font.semiboldSystemFont(18),
  });
  addText(dateStack, String(agenda?.date?.day || "--"), 38, COLORS.text, {
    font: Font.lightSystemFont(38),
  });
}

function renderCounts(stack, agenda) {
  const counts = stack.addStack();
  counts.layoutVertically();
  counts.spacing = 2;

  const today = counts.addStack();
  today.addSpacer();
  addText(today, "今天", 14, COLORS.text, { opacity: 0.92 });
  today.addSpacer(8);
  addText(today, `${agenda?.today_count ?? 0}`, 18, COLORS.accent, { font: Font.semiboldSystemFont(18) });

  const tomorrow = counts.addStack();
  tomorrow.addSpacer();
  addText(tomorrow, "明天", 14, COLORS.text, { opacity: 0.92 });
  tomorrow.addSpacer(8);
  addText(tomorrow, `${agenda?.tomorrow_count ?? 0}`, 18, COLORS.good, { font: Font.semiboldSystemFont(18) });
}

function renderAgenda(left, data) {
  const agenda = data.agenda || {};

  const top = left.addStack();
  top.layoutHorizontally();
  top.centerAlignContent();

  renderDateBlock(top, agenda);
  top.addSpacer(16);
  renderCounts(top, agenda);
  top.addSpacer();

  const mark = top.addStack();
  mark.size = new Size(34, 34);
  mark.cornerRadius = 17;
  mark.backgroundColor = new Color("#2A2B32");
  mark.centerAlignContent();
  mark.addSpacer();
  addText(mark, "🐱", 18, COLORS.text);
  mark.addSpacer();

  left.addSpacer(10);
  addDivider(left);
  left.addSpacer(10);

  const list = left.addStack();
  list.layoutVertically();
  list.spacing = 6;

  const items = agenda.items || [];
  if (!items.length) {
    addText(list, agenda.empty_text || "最近没有安排", 13, COLORS.subtext, {
      lineLimit: 2,
    });
    return;
  }

  for (const item of items.slice(0, 3)) {
    const row = list.addStack();
    row.layoutVertically();
    row.url = appUrl(data.links?.events);
    addText(row, truncate(item.title, 16), 14, COLORS.text, {
      font: Font.semiboldSystemFont(14),
    });
    addText(row, truncate(item.meta || item.subtitle || "", 20), 11, COLORS.subtext, {
      lineLimit: 1,
    });
    list.addSpacer(7);
  }
}

function renderAgendaItems(stack, data, limit = 3) {
  const agenda = data.agenda || {};
  const items = agenda.items || [];
  if (!items.length) {
    addText(stack, agenda.empty_text || "最近没有安排", 13, COLORS.subtext, {
      lineLimit: 2,
    });
    return;
  }

  for (const item of items.slice(0, limit)) {
    const row = stack.addStack();
    row.layoutVertically();
    row.url = appUrl(data.links?.events);
    addText(row, truncate(item.title, 24), 14, COLORS.text, {
      font: Font.semiboldSystemFont(14),
      lineLimit: 1,
    });
    addText(row, truncate(item.meta || item.subtitle || "", 28), 11, COLORS.subtext, {
      lineLimit: 1,
    });
    stack.addSpacer(6);
  }
}

function renderPanelHeader(right, data) {
  const head = right.addStack();
  head.layoutHorizontally();
  head.centerAlignContent();
  head.url = appUrl(data.panel?.path || data.links?.dashboard);

  addText(head, data.panel?.title || "总览", 16, COLORS.text, {
    font: Font.semiboldSystemFont(16),
  });
  head.addSpacer();
  addText(head, "◷", 16, COLORS.warn);

  right.addSpacer(4);
  addText(right, data.panel?.summary?.primary || "", 11, COLORS.subtext, { lineLimit: 1 });
  addText(right, data.panel?.summary?.secondary || "", 11, COLORS.subtext, { lineLimit: 1 });
  right.addSpacer(8);
}

function renderPanelItems(right, data) {
  const items = data.panel?.items || [];
  if (!items.length) {
    addText(right, data.panel?.empty_text || "暂无内容", 13, COLORS.subtext, { lineLimit: 2 });
    return;
  }

  for (const item of items.slice(0, 4)) {
    const row = right.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();
    row.url = appUrl(data.panel?.path || data.links?.dashboard);
    row.spacing = 8;

    addText(row, "☐", 15, COLORS.subtext);

    const copy = row.addStack();
    copy.layoutVertically();
    copy.spacing = 1;
    addText(copy, truncate(item.title, 18), 14, COLORS.text, {
      font: Font.mediumSystemFont(14),
    });

    if (item.amount_text) {
      addText(copy, `${item.meta || ""}  ${item.amount_text}`.trim(), 11, COLORS.subtext, { lineLimit: 1 });
    } else if (item.preview) {
      addText(copy, truncate(item.preview, 20), 11, COLORS.subtext, { lineLimit: 1 });
    } else {
      addText(copy, truncate(item.meta || "", 20), 11, COLORS.subtext, { lineLimit: 1 });
    }

    right.addSpacer(6);
  }
}

function renderPanelItemsLarge(right, data, limit = 5) {
  const items = data.panel?.items || [];
  if (!items.length) {
    addText(right, data.panel?.empty_text || "暂无内容", 14, COLORS.subtext, { lineLimit: 2 });
    return;
  }

  for (const item of items.slice(0, limit)) {
    const row = right.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();
    row.url = appUrl(data.panel?.path || data.links?.dashboard);
    row.spacing = 8;

    addText(row, "☐", 15, COLORS.subtext);

    const copy = row.addStack();
    copy.layoutVertically();
    copy.spacing = 1;
    addText(copy, truncate(item.title, 26), 14, COLORS.text, {
      font: Font.mediumSystemFont(14),
    });

    if (item.amount_text) {
      addText(copy, `${item.meta || ""}  ${item.amount_text}`.trim(), 11, COLORS.subtext, { lineLimit: 1 });
    } else if (item.preview) {
      addText(copy, truncate(item.preview, 30), 11, COLORS.subtext, { lineLimit: 1 });
    } else {
      addText(copy, truncate(item.meta || "", 30), 11, COLORS.subtext, { lineLimit: 1 });
    }
    right.addSpacer(7);
  }
}

function firstMetaPart(value) {
  return String(value || "")
    .split("·")
    .map((part) => part.trim())
    .filter(Boolean)[0] || "";
}

function lastMetaPart(value) {
  const parts = String(value || "")
    .split("·")
    .map((part) => part.trim())
    .filter(Boolean);
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
  const timeText = firstMetaPart(item.meta || item.subtitle || "");
  return timeText ? `${dayText} ${timeText}` : dayText;
}

function mediumPanelPrimaryText(data) {
  const primary = String(data?.panel?.summary?.primary || "");
  if (data?.section === "ledger") {
    return primary.replace(/^支出\s*/, "支 ");
  }
  return primary;
}

function renderAgendaDense(stack, data, limit = 3) {
  const items = data.agenda?.items || [];
  if (!items.length) {
    return false;
  }

  for (const item of items.slice(0, limit)) {
    const row = stack.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();
    row.url = appUrl(data.links?.events);

    const lead = row.addStack();
    lead.size = new Size(62, 0);
    addText(lead, agendaLeadLabel(item, data), 11, COLORS.subtext, {
      font: Font.mediumSystemFont(11),
      lineLimit: 1,
      minimumScaleFactor: 1,
    });

    row.addSpacer(4);
    addText(row, truncate(item.title, 18), 11, COLORS.text, {
      font: Font.mediumSystemFont(11),
      lineLimit: 1,
      minimumScaleFactor: 1,
    });

    stack.addSpacer(3);
  }
  return true;
}

function renderPanelItemsDense(stack, data, limit = 5) {
  const items = data.panel?.items || [];
  const itemTextSize = 11;
  const titleWidth = data.section === "ledger" ? 56 : data.section === "notes" ? 72 : 96;
  const tailWidth = data.section === "ledger" ? 52 : data.section === "notes" ? 28 : 30;
  if (!items.length) {
    addText(stack, data.panel?.empty_text || "暂无内容", itemTextSize, COLORS.subtext, { lineLimit: 2 });
    return;
  }

  for (const item of items.slice(0, limit)) {
    const row = stack.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();
    row.url = appUrl(data.panel?.path || data.links?.dashboard);
    row.spacing = 4;

    const marker = data.section === "ledger"
      ? (String(item.amount_text || "").startsWith("-") ? "↘" : "↗")
      : (data.section === "notes" ? "•" : "☐");
    const markerColor = data.section === "ledger"
      ? (String(item.amount_text || "").startsWith("-") ? COLORS.accent : COLORS.good)
      : COLORS.subtext;

    const markerBox = row.addStack();
    markerBox.size = new Size(10, 0);
    markerBox.centerAlignContent();
    addText(markerBox, marker, 10, markerColor, {
      font: Font.mediumSystemFont(10),
    });

    const copy = row.addStack();
    copy.size = new Size(titleWidth, 0);
    copy.layoutHorizontally();
    addText(copy, truncate(item.title, data.section === "ledger" ? 8 : 16), itemTextSize, COLORS.text, {
      font: Font.mediumSystemFont(itemTextSize),
      lineLimit: 1,
      minimumScaleFactor: 1,
    });
    copy.addSpacer();

    const tail = row.addStack();
    tail.size = new Size(tailWidth, 0);
    tail.layoutHorizontally();
    if (item.amount_text) {
      addText(tail, item.amount_text, 10, String(item.amount_text).startsWith("-") ? COLORS.accent : COLORS.good, {
        font: Font.mediumSystemFont(itemTextSize),
        lineLimit: 1,
        minimumScaleFactor: 1,
      });
    } else if (data.section === "tasks") {
      addText(tail, truncate(lastMetaPart(item.meta || ""), 6), itemTextSize, COLORS.subtext, {
        lineLimit: 1,
        minimumScaleFactor: 1,
      });
    } else if (item.preview) {
      addText(tail, truncate(item.preview, 8), itemTextSize, COLORS.subtext, {
        lineLimit: 1,
        minimumScaleFactor: 1,
      });
    }
    tail.addSpacer();
    stack.addSpacer(4);
  }
}

function renderAgendaExpanded(stack, data, limit = 5) {
  const items = data.agenda?.items || [];
  if (!items.length) {
    return;
  }

  for (const item of items.slice(0, limit)) {
    const row = stack.addStack();
    row.layoutVertically();
    row.url = appUrl(data.links?.events);

    const main = row.addStack();
    main.layoutHorizontally();
    main.centerAlignContent();

    const lead = main.addStack();
    lead.size = new Size(68, 0);
    addText(lead, agendaLeadLabel(item, data), 11, COLORS.subtext, {
      font: Font.mediumSystemFont(11),
      lineLimit: 1,
      minimumScaleFactor: 1,
    });

    main.addSpacer(5);
    addText(main, truncate(item.title, 18), 12, COLORS.text, {
      font: Font.mediumSystemFont(12),
      lineLimit: 1,
      minimumScaleFactor: 1,
    });

    const detail = lastMetaPart(item.meta || item.subtitle || "");
    const timeText = firstMetaPart(item.meta || item.subtitle || "");
    if (detail && detail !== timeText) {
      row.addSpacer(1);
      addText(row, truncate(detail, 24), 10, COLORS.subtext, {
        lineLimit: 1,
      });
    }
    stack.addSpacer(6);
  }
}

function panelItemMarker(data, item) {
  if (data.section === "ledger") {
    return String(item.amount_text || "").startsWith("-")
      ? { text: "↘", color: COLORS.accent }
      : { text: "↗", color: COLORS.good };
  }
  if (data.section === "notes") {
    return { text: "•", color: COLORS.subtext };
  }
  return { text: "☐", color: COLORS.subtext };
}

function panelItemTailText(data, item) {
  if (item.amount_text) return item.amount_text;
  if (data.section === "tasks") return truncate(lastMetaPart(item.meta || ""), 6);
  return "";
}

function panelItemDetailText(data, item) {
  if (data.section === "ledger") return item.meta || "";
  if (data.section === "tasks") return item.meta || "";
  if (item.preview) return item.preview;
  return item.meta || "";
}

function renderPanelItemsExpanded(stack, data, limit = 5) {
  const items = data.panel?.items || [];
  if (!items.length) {
    addText(stack, data.panel?.empty_text || "暂无内容", 12, COLORS.subtext, { lineLimit: 2 });
    return;
  }

  for (const item of items.slice(0, limit)) {
    const row = stack.addStack();
    row.layoutVertically();
    row.url = appUrl(data.panel?.path || data.links?.dashboard);

    const main = row.addStack();
    main.layoutHorizontally();
    main.centerAlignContent();
    main.spacing = 5;

    const marker = panelItemMarker(data, item);
    const markerBox = main.addStack();
    markerBox.size = new Size(10, 0);
    markerBox.centerAlignContent();
    addText(markerBox, marker.text, 10, marker.color, {
      font: Font.mediumSystemFont(10),
    });

    const titleBox = main.addStack();
    titleBox.size = new Size(data.section === "ledger" ? 94 : 112, 0);
    titleBox.layoutHorizontally();
    addText(titleBox, truncate(item.title, data.section === "ledger" ? 14 : 18), 12, COLORS.text, {
      font: Font.mediumSystemFont(12),
      lineLimit: 1,
      minimumScaleFactor: 1,
    });
    titleBox.addSpacer();

    const tailText = panelItemTailText(data, item);
    if (tailText) {
      const tailBox = main.addStack();
      tailBox.size = new Size(data.section === "ledger" ? 70 : 34, 0);
      tailBox.layoutHorizontally();
      addText(
        tailBox,
        tailText,
        11,
        String(tailText).startsWith("-") ? COLORS.accent : (String(tailText).startsWith("+") ? COLORS.good : COLORS.subtext),
        {
          font: Font.mediumSystemFont(11),
          lineLimit: 1,
          minimumScaleFactor: 1,
        }
      );
      tailBox.addSpacer();
    }

    const detail = panelItemDetailText(data, item);
    if (detail) {
      row.addSpacer(1);
      addText(row, truncate(detail, data.section === "notes" ? 30 : 28), 10, COLORS.subtext, {
        lineLimit: 1,
      });
    }
    stack.addSpacer(6);
  }
}

function renderMedium(widget, data) {
  widget.backgroundColor = COLORS.panel;
  widget.setPadding(9, 12, 9, 12);

  const top = widget.addStack();
  top.layoutHorizontally();
  top.centerAlignContent();
  top.spacing = 8;

  const leftHead = top.addStack();
  leftHead.layoutHorizontally();
  leftHead.centerAlignContent();
  leftHead.size = new Size(150, 0);
  leftHead.url = appUrl(data.links?.events);

  const dateLine = leftHead.addStack();
  dateLine.layoutHorizontally();
  dateLine.centerAlignContent();
  addText(dateLine, data.agenda?.date?.weekday || "--", 13, COLORS.accent, {
    font: Font.semiboldSystemFont(13),
  });
  dateLine.addSpacer(5);
  addText(dateLine, String(data.agenda?.date?.day || "--"), 19, COLORS.text, {
    font: Font.lightSystemFont(19),
  });

  leftHead.addSpacer(10);

  const counts = leftHead.addStack();
  counts.layoutHorizontally();
  counts.centerAlignContent();

  addText(counts, "今天", 9, COLORS.subtext);
  counts.addSpacer(3);
  addText(counts, `${data.agenda?.today_count ?? 0}`, 11, COLORS.accent, {
    font: Font.semiboldSystemFont(12),
  });
  counts.addSpacer(8);
  addText(counts, "明天", 9, COLORS.subtext);
  counts.addSpacer(3);
  addText(counts, `${data.agenda?.tomorrow_count ?? 0}`, 11, COLORS.good, {
    font: Font.semiboldSystemFont(12),
  });

  top.addSpacer();

  const rightHead = top.addStack();
  rightHead.layoutHorizontally();
  rightHead.centerAlignContent();
  rightHead.url = appUrl(data.panel?.path || data.links?.dashboard);
  addText(rightHead, data.panel?.title || "总览", 14, COLORS.text, {
    font: Font.semiboldSystemFont(14),
  });
  rightHead.addSpacer(5);
  addText(rightHead, mediumPanelPrimaryText(data), 9, COLORS.subtext, {
    lineLimit: 1,
  });

  widget.addSpacer(5);
  addDivider(widget);
  widget.addSpacer(5);

  const body = widget.addStack();
  body.layoutHorizontally();
  body.spacing = 8;
  const left = body.addStack();
  left.layoutVertically();
  left.size = new Size(150, 0);
  left.url = appUrl(data.links?.events);
  renderAgendaDense(left, data, 5);

  const midLine = body.addStack();
  midLine.size = new Size(1, 0);
  midLine.backgroundColor = COLORS.line;

  const right = body.addStack();
  right.layoutVertically();
  right.url = appUrl(data.panel?.path || data.links?.dashboard);
  renderPanelItemsDense(right, data, 5);
}

function renderLarge(widget, data) {
  widget.backgroundColor = COLORS.panel;
  widget.setPadding(12, 12, 12, 12);

  const top = widget.addStack();
  top.layoutHorizontally();
  top.centerAlignContent();
  top.spacing = 8;

  const leftHead = top.addStack();
  leftHead.layoutHorizontally();
  leftHead.centerAlignContent();
  leftHead.size = new Size(150, 0);
  leftHead.url = appUrl(data.links?.events);

  const dateLine = leftHead.addStack();
  dateLine.layoutHorizontally();
  dateLine.centerAlignContent();
  addText(dateLine, data.agenda?.date?.weekday || "--", 15, COLORS.accent, {
    font: Font.semiboldSystemFont(15),
  });
  dateLine.addSpacer(5);
  addText(dateLine, String(data.agenda?.date?.day || "--"), 24, COLORS.text, {
    font: Font.lightSystemFont(24),
  });

  leftHead.addSpacer(10);

  const counts = leftHead.addStack();
  counts.layoutHorizontally();
  counts.centerAlignContent();
  addText(counts, "今天", 10, COLORS.subtext);
  counts.addSpacer(4);
  addText(counts, `${data.agenda?.today_count ?? 0}`, 12, COLORS.accent, {
    font: Font.semiboldSystemFont(12),
  });
  counts.addSpacer(8);
  addText(counts, "明天", 10, COLORS.subtext);
  counts.addSpacer(4);
  addText(counts, `${data.agenda?.tomorrow_count ?? 0}`, 12, COLORS.good, {
    font: Font.semiboldSystemFont(12),
  });

  top.addSpacer();

  const rightHead = top.addStack();
  rightHead.layoutVertically();
  rightHead.url = appUrl(data.panel?.path || data.links?.dashboard);
  addText(rightHead, data.panel?.title || "总览", 18, COLORS.text, {
    font: Font.semiboldSystemFont(18),
  });
  rightHead.addSpacer(1);
  addText(rightHead, mediumPanelPrimaryText(data), 10, COLORS.subtext, {
    lineLimit: 1,
  });

  widget.addSpacer(7);
  addDivider(widget);
  widget.addSpacer(7);

  const body = widget.addStack();
  body.layoutHorizontally();
  body.spacing = 10;

  const left = body.addStack();
  left.layoutVertically();
  left.size = new Size(150, 0);
  left.url = appUrl(data.links?.events);
  renderAgendaExpanded(left, data, 5);

  const midLine = body.addStack();
  midLine.size = new Size(1, 0);
  midLine.backgroundColor = COLORS.line;

  const right = body.addStack();
  right.layoutVertically();
  right.url = appUrl(data.panel?.path || data.links?.dashboard);
  if (data.panel?.summary?.secondary) {
    addText(right, data.panel.summary.secondary, 10, COLORS.subtext, { lineLimit: 1 });
    right.addSpacer(6);
  }
  renderPanelItemsExpanded(right, data, 5);
}

function renderSmall(widget, data) {
  const top = widget.addStack();
  top.layoutVertically();
  top.url = appUrl(data.links?.dashboard);

  addText(top, `${data.agenda?.date?.weekday || "--"} ${data.agenda?.date?.day || "--"}`, 16, COLORS.accent, {
    font: Font.semiboldSystemFont(16),
  });
  top.addSpacer(6);
  addText(top, data.panel?.title || "总览", 15, COLORS.text, { font: Font.semiboldSystemFont(15) });
  addText(top, data.panel?.summary?.primary || "", 12, COLORS.subtext, { lineLimit: 2 });
  top.addSpacer(8);

  const first = data.agenda?.items?.[0] || data.panel?.items?.[0];
  addText(top, first?.title || "暂无内容", 13, COLORS.text, { lineLimit: 2 });
  addText(top, first?.meta || first?.preview || "", 11, COLORS.subtext, { lineLimit: 2 });
}

async function createWidget() {
  const section = widgetSection();
  const data = await fetchWidgetData(section);

  const widget = new ListWidget();
  widget.backgroundColor = COLORS.bg;
  widget.setPadding(12, 12, 12, 12);
  widget.refreshAfterDate = new Date(Date.now() + 20 * 60 * 1000);
  widget.url = appUrl(data.links?.dashboard);

  if (config.widgetFamily === "small") {
    renderSmall(widget, data);
  } else if (config.widgetFamily === "large") {
    renderLarge(widget, data);
  } else {
    renderMedium(widget, data);
  }

  return widget;
}

async function createErrorWidget(error) {
  const widget = new ListWidget();
  widget.backgroundColor = COLORS.bg;
  widget.setPadding(14, 14, 14, 14);
  addText(widget, "Pendo", 18, COLORS.text, { font: Font.semiboldSystemFont(18) });
  widget.addSpacer(8);
  addText(widget, "组件加载失败", 14, COLORS.accent, { font: Font.semiboldSystemFont(14) });
  widget.addSpacer(6);
  addText(widget, truncate(error.message || String(error), 60), 12, COLORS.subtext, {
    lineLimit: 3,
  });
  widget.url = appUrl("#/dashboard");
  return widget;
}

let widget;
try {
  if (TOKEN === "PASTE_WIDGET_TOKEN_HERE") {
    throw new Error("请先把脚本里的 TOKEN 改成 /pendo web widget-token 生成的值");
  }
  widget = await createWidget();
} catch (error) {
  widget = await createErrorWidget(error);
}

Script.setWidget(widget);
Script.complete();
