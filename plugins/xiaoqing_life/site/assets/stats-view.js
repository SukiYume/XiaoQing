const activities = {hot_scroll:"刷热榜", interest_dive:"兴趣浏览", thread_continue:"查小课题", work_on:"做小作品", term_lookup:"翻百科"};
const sum = (rows, key) => rows.reduce((total, day) => total + day[key], 0);
const shortDay = day => day.slice(5).replace("-", "/");

export function showStats(main, {week, zone, journal, episodes, learnings, threads}) {
  const tracked      = week.filter(day => day.recorded);
  const max          = Math.max(1, ...tracked.flatMap(day => [day.sessions, day.episodes]));
  const journalCount = journal.filter(row => row.kind === "diary").length;
  const verified     = learnings.filter(row => row.verified).length;
  main.innerHTML = `<section class="wrap inner stats-page">
    <header class="stats-head"><div><span class="eyebrow">ACTIVITY LOG</span><h1>生活统计</h1></div>
      <div class="stats-dates">${week[0].day.replaceAll("-", ".")} — ${week.at(-1).day.replaceAll("-", ".")}<small>最近七个生活日 · ${zone} 05:00 换日</small></div></header>
    <dl class="stat-readout">${[["有记录的天数",tracked.length,"/ 7 天"],["上网活动",sum(tracked,"sessions"),"次"],["新增经历",sum(tracked,"episodes"),"条"],["研究要点",sum(tracked,"findings"),"条"]].map(([label,n,unit],i) =>
      `<div><dt>${label}</dt><dd>${!tracked.length && i ? "—" : n}<small>${unit}</small></dd></div>`).join("")}</dl>
    <div class="stats-chart"><div class="section-title"><h2>每日活动</h2><span class="legend"><i></i>上网活动 <i></i>新增经历</span></div>
      <div class="plot" role="group" aria-label="选择日期查看活动明细">${week.map((day,i) => `<button type="button" class="plot-day" data-i="${i}" aria-pressed="false" aria-label="${day.day}，${day.recorded ? '上网'+day.sessions+'次，新增经历'+day.episodes+'条' : '缺记录'}">
        <span class="bars" aria-hidden="true">${day.recorded ? `<i style="height:${day.sessions/max*100}%"></i><i style="height:${day.episodes/max*100}%"></i><small><b>${day.sessions}</b> · <b>${day.episodes}</b></small>` : '<span class="gap-mark">—</span>'}</span>
        <span>${shortDay(day.day)}</span></button>`).join("")}</div>
      <div class="plot-caption"><span id="selected-day" aria-live="polite"></span><button type="button" class="sum-week" aria-pressed="true">七日合计</button></div></div>
    <section class="stats-detail"><h2 id="period-title"></h2><div id="period-data"></div></section>
    <section class="stats-archive"><h2>手帐里留下的记录</h2><p>按目前保存的全部记录汇总。</p>
      <div class="shelf-links">${[["diary","日记与周记",journalCount+" 篇日记 · "+(journal.length-journalCount)+" 篇周记"],["timeline","公开足迹",episodes.length+" 条想分享的经历"],["research","研究手帐",threads.length+" 个课题 · "+verified+" / "+learnings.length+" 条收获已核实"]].map(([href,title,detail])=>`<a href="${href}"><strong>${title}</strong><span>${detail}</span><i aria-hidden="true">↗</i></a>`).join("")}</div>
    </section></section>`;
  const buttons = Array.from(main.querySelectorAll(".plot-day"));
  const all     = main.querySelector(".sum-week");
  function select(index) {
    const day  = week[index];
    const rows = day ? (day.recorded ? [day] : []) : tracked;
    main.querySelector("#selected-day").textContent = day
      ? shortDay(day.day) + (day.recorded ? `　上网 ${day.sessions} 次 · 新增经历 ${day.episodes} 条` : "　当天暂无运行计数")
      : "点日期看当天明细　·　— 缺记录";
    main.querySelector("#period-title").textContent = (day ? shortDay(day.day) : "七日合计") + " · 活动明细";
    all.setAttribute("aria-pressed", String(!day));
    buttons.forEach((button, i) => button.setAttribute("aria-pressed", String(i === index)));
    const block = main.querySelector("#period-data");
    if (!rows.length) {
      block.innerHTML = '<p class="stats-blank">'+(day ? "这个生活日暂无运行计数。可以点其他日期查看。" : "这七个生活日暂无运行计数。下方仍可翻阅已有馆藏。")+'</p>';
      return;
    }
    const total = sum(rows,"sessions");
    block.innerHTML = `<div class="breakdown"><section><h3>上网去向 <small>${total} 次</small></h3>
      ${Object.entries(activities).map(([key,label]) => {
        const n = rows.reduce((acc, r) => acc + r.activities[key], 0);
        return `<div class="meter-row"><span>${label}</span><span class="meter"><i style="width:${total ? n/total*100 : 0}%"></i></span><b>${n}</b></div>`;
      }).join("")}</section><section><h3>研究与查词</h3>
      ${[["研究轮次","rounds","轮"],["整理出要点","findings","条"],["词条记录","terms","条"],["查到释义","hits","次"],["查无释义","misses","次"]].map(([label,key,unit])=>`<div class="count-row"><span>${label}</span><b>${sum(rows,key)} <small>${unit}</small></b></div>`).join("")}</section></div>`;
  }
  buttons.forEach((button,i) => button.onclick = () => select(i));
  all.onclick = () => select(-1);
  select(-1);
}
