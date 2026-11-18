import {showStats} from "./stats-view.js";
import {createStagePlayer, scenes} from "./stage.js";

const main = document.querySelector("#main");
const [page, entry] = location.pathname.slice(new URL(document.baseURI).pathname.length).split("/");
const titles = {
  "": "首页", live: "生活直播间", diary: "小青的日记本", timeline: "校园足迹",
  travel: "旅行手帐", research: "研究手帐", gallery: "场景图鉴", stats: "生活统计", guide: "观测指南",
};
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[c]);
const empty    = text => '<p class="empty">' + esc(text) + '</p>';
const dayLabel = value => esc(value).replaceAll("-", ".");
const diaryUrl = row => "diary/" + encodeURIComponent(row.date);
const kindName = row => row.kind === "weekly" ? "周记" : "日记";
const active   = document.querySelector(`nav a[href="${page || "./"}"]`);
active?.setAttribute("aria-current", "page");
document.title = page ? `${titles[page]} · 小青观测站` : "小青观测站 · 校园日常";
function heading(english, intro) {
  return `<div class="pagehead"><span class="eyebrow">${english}</span><h1>${titles[page]}</h1><p>${intro}</p></div>`;
}
function diaryList(journal) {
  return journal.length ? journal.map(row => `<a class="sheet" href="${diaryUrl(row)}">
    <span class="paper-meta">${dayLabel(row.date)} <b>${kindName(row)}</b></span>
    <p>${esc(row.text)}</p><span class="read">翻开这页 →</span></a>`).join("")
    : empty("日记本里还没有这一类记录。");
}
function trail(episodes) {
  return episodes.length ? episodes.map(row => `<article class="trail"><time>${dayLabel(row.at.slice(0,10))}
    <small>${esc(row.at.slice(11,16))}</small></time><div><h3>${esc(row.title)}</h3>
    <p>${esc(row.reaction)}</p></div></article>`).join("") : empty("小青还没有标记想分享的经历。");
}
// 作息卡每行是「星期 起-止 活动（睡/忙）」。按开始时间排成时刻表，读不出时间的行原样列在后面。
function routineRows(lines) {
  const marks = {睡: "睡着了", 忙: "在忙"};
  const rows = lines.map(line => line.match(/^(.+?)\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s+(.+?)(?:（(.+?)）)?$/) || line);
  const timed = rows.filter(Array.isArray).sort((a, b) => a[2].padStart(5, "0").localeCompare(b[2].padStart(5, "0")));
  return timed.map(([, days, from, to, what, mark]) => `<div class="routine-row"><time>${esc(from)}–${esc(to)}</time>
    <span>${esc(what)}<small>${esc(days)}</small></span>${mark ? `<b>${esc(marks[mark] || mark)}</b>` : ""}</div>`).join("")
    + rows.filter(row => !Array.isArray(row)).map(line => `<div class="ruled">${esc(line)}</div>`).join("");
}
function home() {
  main.innerHTML = `<section class="hero"><img class="hero-art" src="assets/club.webp" alt="${scenes.club.alt}">
    <div class="hero-content wrap"><span class="eyebrow"><i></i> XIAOQING OBSERVATORY</span>
    <h1>小青的<br><em>校园日常</em></h1>
    <p>我叫小青，天文系大二。<br>带上观测本，来天台吹会儿风？</p>
    <div class="actions"><a class="action primary" href="live">▶ 进入直播间</a>
    <a class="action secondary" href="diary">翻开日记本 ↗</a></div></div></section>
    <div class="transmission"><div class="wrap"><b><i class="signal"></i> 此刻</b>
    <span id="now">正在读取作息…</span><a href="live">去看看 →</a></div></div>
    <section class="wrap home-log"><div class="section-title"><div><span class="eyebrow">FIELD NOTES</span>
    <h2>最近的记录</h2></div><a href="timeline">全部足迹 →</a></div>
    <div id="latest" class="home-columns"><p>正在翻笔记本…</p></div></section>`;
}
function showGallery() {
  const entries = Object.entries(scenes);
  main.innerHTML = `<section class="wrap inner gallery">${heading("SCENE ARCHIVE", "选个地方找小青。轻点她、翻看道具，也可以听她接着讲。")}
    <label class="scene-select">换个场景 <select>${entries.map(([key, art]) => `<option value="${key}">${art.name}</option>`).join("")}</select></label>
    <div class="gallery-layout"><div id="gallery-stage"><div class="opening-screen">正在打开场景…</div></div><aside class="gallery-index"><h2>场景目录</h2>
      <div class="gallery-list">${entries.map(([key, art]) => `<a href="gallery?scene=${key}" data-key="${key}"><img src="assets/${key}.webp" alt=""><span>${art.name}</span></a>`).join("")}</div></aside></div></section>`;
  const select    = main.querySelector(".scene-select select");
  const links     = Array.from(main.querySelectorAll(".gallery-list a"));
  const showScene = createStagePlayer(main.querySelector("#gallery-stage"));
  let activeKey;
  function render() {
    const requested = new URLSearchParams(location.search).get("scene");
    const key       = Object.hasOwn(scenes, requested) ? requested : "club";
    if (key === activeKey) return;
    activeKey = key;
    select.value = key;
    for (const link of links) {
      if (link.dataset.key === key) link.setAttribute("aria-current", "true");
      else link.removeAttribute("aria-current");
    }
    showScene(key, "图鉴 · " + scenes[key].name);
  }
  function choose(key) {
    if (key !== activeKey) history.pushState(null, "", "gallery?scene=" + key);
    render();
  }
  for (const link of links) link.onclick = event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    choose(link.dataset.key);
  };
  select.onchange = () => choose(select.value);
  window.addEventListener("popstate", render);
  render();
}
function showProfile() {
  const topics = [
    ["campus","认路","我认得猎户座，教学楼得靠那排银杏。","校园图折四折，刚好塞进书包侧袋。早八赶得急，她靠车架和行道树认门，走对了还会悄悄把路线描深一笔。"],
    ["club","观星","第一次看见土星环，我叫得整栋楼都听见了吧。","大一那次，她在道布森望远镜旁找了四十分钟。如今笔记里连目镜都分栏记，最舍得把观测时间留给土星和猎户座大星云。"],
    ["tea","口味","少糖去冰。里脊挑边上那块，脆。","她认准食堂二楼的糖醋里脊，微辣封顶，香菜挑开。拿到奶茶先核杯贴，再把吸管对准封口正中央。"],
    ["game","游戏","就再跳一次。这句里的“一次”可以续。","喜欢《空洞骑士》《星露谷物语》和《旷野之息》。苦痛之路卡了两个月，她把起跳、贴墙、落地拆开练。"],
    ["library","读史","借书票先给我，夹在这座桥这里。","她翻历史书总会先找卷末的地图。沿河道数桥比背地名记得牢，看完一段再翻回正文对照。"],
    ["desk","小黑","我妈发来八张连拍，我当然每一张都要存。","小黑是家里的奶牛猫，白爪子像两只短袜。住四人间时，桌边贴张照片，写完一行报告就抬头看一眼。"],
  ];
  const host = main.querySelector("#profile-book");
  host.innerHTML = `<div class="profile-topics" role="group" aria-label="小青的兴趣和习惯">${topics.map(([,name],i)=>`<button type="button" data-i="${i}">${name}</button>`).join("")}</div><div class="profile-note"></div>`;
  const buttons = Array.from(host.querySelectorAll("button"));
  function select(i) {
    const [scene, name, quote, note] = topics[i];
    buttons.forEach((button,n) => button.setAttribute("aria-pressed", String(i === n)));
    host.querySelector(".profile-note").innerHTML = `<img src="assets/${scene}.webp" alt="${scenes[scene].alt}"><div aria-live="polite"><h3>${name}</h3>
      <blockquote>“${quote}”</blockquote><p>${note}</p><a href="gallery?scene=${scene}">进入场景 · ${scenes[scene].name} →</a></div>`;
  }
  buttons.forEach((button,i) => button.onclick = () => select(i));
  select(0);
}
function showTravel(stops) {
  main.innerHTML = `<section class="wrap inner travel-book">
    ${heading("TRAVEL JOURNAL", "走到哪里，就从哪里寄一张明信片回来。")}
    <div class="travel-stops"></div>
    <dialog class="travel-viewer" aria-label="旅行明信片大图"><form method="dialog"><button autofocus>关闭 ×</button></form>
      <img alt=""><p></p></dialog></section>`;
  const list = main.querySelector(".travel-stops");
  if (!stops.length) {
    list.innerHTML = empty("第一张旅行明信片还在路上。");
    return;
  }
  let next = 0;
  function addStops() {
    list.querySelector(".travel-more")?.remove();
    const batch = stops.slice(next, next + 7);
    list.insertAdjacentHTML("beforeend", batch.map(stop => `<article class="travel-stop">
      <time datetime="${esc(stop.date)}">${dayLabel(stop.date)}</time>
      <div class="travel-place"><header><span>${esc(stop.country)}</span><h2>${esc(stop.city)}</h2></header>
        <div class="postcards">${stop.photos.map((photo, i) => {
          const label = esc(`小青在${stop.city}的旅行明信片 · 第 ${i + 1} 张`);
          return `<figure class="postcard"><button type="button" class="postcard-open" aria-haspopup="dialog" aria-label="放大${label}">
            <img src="${esc(photo.src.replace(/^\//, ""))}" width="${esc(photo.width)}" height="${esc(photo.height)}" alt="${label}" loading="lazy">
            <span aria-hidden="true">看大图 ↗</span></button><figcaption>${esc(photo.caption)}</figcaption></figure>`;
        }).join("")}</div></div></article>`).join(""));
    next += batch.length;
    if (next < stops.length) {
      list.insertAdjacentHTML("beforeend", '<button class="action secondary travel-more">翻看更早的旅行 ↓</button>');
      list.querySelector(".travel-more").onclick = addStops;
    }
  }
  addStops();
  const viewer = main.querySelector(".travel-viewer");
  list.addEventListener("click", event => {
    const open = event.target.closest(".postcard-open");
    if (!open) return;
    const thumb = open.querySelector("img");
    const full  = viewer.querySelector("img");
    full.src = thumb.src;
    full.alt = thumb.alt;
    viewer.querySelector("p").textContent = open.nextElementSibling.textContent;
    viewer.showModal();
  });
}
function showArchive(data) {
  const {journal, episodes, threads, learnings} = data;
  if (page === "") {
    document.querySelector("#latest").innerHTML = `<section><h3>睡前手记</h3>${diaryList(journal.slice(0,1))}</section>
      <section><h3>最近想分享的事</h3>${trail(episodes.slice(0,2))}</section>`;
  } else if (page === "diary" && entry) {
    const index = journal.findIndex(row => row.date === entry);
    if (index < 0) {
      main.innerHTML = '<section class="wrap inner"><h1>这一页还空着</h1><a class="action secondary" href="diary">返回日记本</a></section>';
      return;
    }
    const row = journal[index];
    document.title = `${row.date} ${kindName(row)} · 小青观测站`;
    main.innerHTML = `<section class="wrap inner reader"><a class="back" href="diary">← 日记目录</a>
      <article class="paper"><span class="paper-meta">小青的${kindName(row)}</span><h1>${dayLabel(row.date)}</h1>
      <p>${esc(row.text)}</p><div class="signature">小青 ✦</div></article><div class="leaf-nav">
      ${journal[index+1] ? `<a href="${diaryUrl(journal[index+1])}">← 早一篇</a>` : "<span></span>"}
      ${index ? `<a href="${diaryUrl(journal[index-1])}">晚一篇 →</a>` : ""}</div></section>`;
  } else if (page === "diary") {
    const filter = new URLSearchParams(location.search).get("kind");
    const kind   = ["weekly", "diary"].includes(filter) ? filter : "";
    main.innerHTML = `<section class="wrap inner">${heading("SLEEPY JOURNAL", "睡前记下的一两件事，攒起来就成了一本。")}
      <div class="filters">${[["","全部"],["diary","日记"],["weekly","周记"]].map(([key,name]) =>
        `<a href="diary${key ? "?kind="+key : ""}" ${kind === key ? 'aria-current="true"' : ''}>${name}</a>`).join("")}</div>
      <div class="sheets">${diaryList(journal.filter(row => !kind || row.kind === kind))}</div></section>`;
  } else if (page === "timeline") {
    main.innerHTML = `<section class="wrap inner narrow">${heading("FIELD LOG", "小青上网时标记为想分享的经历，新的放在前面。")}
      <p class="more"><a href="travel">翻看世界旅行手帐 →</a></p>
      ${trail(episodes)}</section>`;
  } else if (page === "research") {
    const status = {open:"在查",reviewing:"整理中",parked:"暂时搁置",done:"已收尾",dropped:"已放下"};
    main.innerHTML = `<section class="wrap inner narrow">${heading("RESEARCH NOTES", "从一个好奇的小问题，到笔记里写下的收获。")}
      <h2>查到的事</h2>${learnings.map(row => `<article class="note"><small${row.verified ? "" : ' class="pending"'}>${row.verified ? "已核实" : "待核实"}</small>
      <h3>${esc(row.topic)}</h3><p>${esc(row.text)}</p></article>`).join("") || empty("下一条收获在路上。")}
      <h2>课题进度</h2>${threads.map(row => `<div class="ruled"><span>${esc(row.title)}</span>
      <small data-status="${esc(row.status)}">${esc(status[row.status] || "已记录")}</small></div>`).join("") || empty("课题本还是空的。")}</section>`;
  } else if (page === "stats") {
    showStats(main, data);
  }
}
async function fetchData(name) {
  const res = await fetch("api/" + name, {signal: AbortSignal.timeout(12000)});
  if (!res.ok) throw Error("read failed");
  return res.json();
}
let lastScene = "";
let showLiveScene;
function updateLive(data) {
  const art   = scenes[data.scene];
  const clock = data.at.slice(11,16);
  if (page === "") {
    document.querySelector("#now").textContent = `${clock} · ${data.activity}（${data.source}）`;
  } else {
    if (lastScene !== data.scene) {
      showLiveScene ??= createStagePlayer(document.querySelector("#live-stage"));
      showLiveScene(data.scene, "LIVE · " + art.name);
      lastScene = data.scene;
    }
    document.querySelector("#status").textContent = `${data.activity} · ${data.source}`;
    document.querySelector("#sync").textContent = `最近同步 ${data.at.slice(0,10)} ${clock} · ${data.zone}`;
  }
}
async function refreshLive() {
  try {
    updateLive(await fetchData("live"));
  } catch {
    const target = document.querySelector(page === "" ? "#now" : "#sync");
    target.textContent = "状态暂时读不到，稍后自动重试。";
    if (page === "live") {
      document.querySelector("#status").textContent = lastScene ? "上次同步的场景" : "等待同步";
      if (!lastScene) document.querySelector("#live-stage").textContent = "直播间正在等待生活记录。";
    }
  }
}
async function start() {
  if (page === "gallery") return showGallery();
  if (page === "" || page === "live") {
    if (!page) home();
    else main.innerHTML = `<section class="wrap inner live"><div class="section-title"><div>
      <span class="eyebrow"><i class="signal"></i> LIVE CHANNEL</span><h1>生活直播间</h1></div>
      <a href="gallery">翻场景图鉴 →</a></div><div id="live-stage" class="loading">正在接收场景…</div>
      <div class="live-meta"><b id="status">读取作息中…</b><span id="sync"></span></div></section>`;
    let pending = false;
    const poll  = async () => {
      if (document.hidden || pending) return;
      pending = true;
      try { await refreshLive(); } finally { pending = false; }
    };
    // A newly opened background tab still gets an initial scene.
    pending = true;
    refreshLive().finally(() => { pending = false; });
    setInterval(poll, 30000);
    document.addEventListener("visibilitychange", poll);
    if (page === "live") return;
  }
  if (page === "guide") {
    main.innerHTML = `<section class="wrap inner narrow">${heading("FIELD GUIDE", "第一次来，从这里认识小青。")}
      <div class="profile"><span class="portrait"></span><div><h2>小青</h2><p>二十岁，快逃科学技术研究院天文专业大二，住四人间。喜欢土星环、奶牛猫小黑和食堂二楼的糖醋里脊。</p></div></div>
      <h2>小青的随身手册</h2><div id="profile-book"></div>
      <h2>怎么玩</h2><p>在<a href="live">直播间</a>轻点小青，看她回头。连点四下，她会抗议。睡觉时戳醒她，还能看见她抱着枕头坐起来。画面左下角可以看她手边的东西，对话框的三角可以听她接着讲。<a href="gallery">图鉴</a>可以自由选场景。</p>
      <h2>她的一天</h2><p>直播间优先显示正在进行的上网活动，其他时段按人设卡的作息配图。台词和像素插画用于角色演出。</p><div id="routine"></div>
      <h2>记录从哪来</h2><p>日记、足迹、小课题和统计来自小青生活插件。睡前整理产生日记，有周记时也会收进日记本。上网和研究由小青的 AI 生活系统驱动。</p></section>`;
    showProfile();
    const data = await fetchData("live");
    document.querySelector("#routine").innerHTML = routineRows(data.routine) || empty("当前按安静时段安排休息。");
    return;
  }
  if (page) main.innerHTML = '<div class="wrap inner">正在翻笔记本…</div>';
  if (page === "travel") return showTravel(await fetchData("travel"));
  showArchive(await fetchData("archive"));
}
start().catch(() => {
  const block = document.querySelector("#latest, #routine") || main;
  block.innerHTML = '<div class="wrap error" role="alert"><p>这次没读到生活记录。</p><button class="action secondary" id="retry">重新读取</button></div>';
  document.querySelector("#retry").onclick = () => location.reload();
});
