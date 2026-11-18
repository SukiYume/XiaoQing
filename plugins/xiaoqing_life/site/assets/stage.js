export const scenes = {
  club: {
    name: "天文社天台", alt: "晚霞里，小青抱着观测本，站在天台的望远镜旁。",
    item: "看目镜", itemName: "望远镜目镜", tile: "0% 0%", ambient: "✦",
    idle: "目镜我调好了。你从这边看，栏杆那侧风大。",
    startled: ["哎，吓我一跳！我还以为镜盖掉了。", "等下，我的笔差点在星图上划出一颗流星。", "你什么时候上来的？楼梯声全让风盖住了。"],
    sharing: ["往我指的方向看。那三颗排成一线，就是猎户的腰带。", "等天再暗一点，咱们找腰带下面那团雾。", "我第一次找到土星时叫得太响，楼下的人都抬头了。"],
    object: ["眼睛离镜片留一点距离。看见那个小小的环了吗？", "土星快移到边缘了。我推一下镜筒，你接着看。", "这个目镜借你先看。我上次为了找这道环，蹲了四十分钟。"],
    cross: ["本子给你拿三秒。手忙着就顾不上戳我了吧？", "再戳一下，这颗星的名字我可要写成你的手指了。", "站稳，台沿有风。我把这个位置记完就理你。"],
  },
  desk: {
    name: "宿舍书桌", alt: "宿舍的台灯亮着，小青手里拿着笔记本。",
    item: "看猫片", itemName: "小黑的照片", tile: "50% 0%", ambient: "♪",
    idle: "给你腾了半张椅子。先让我把这一行写完。",
    startled: ["嗯？我刚把耳机音量调大，你怎么就来了。", "手一抖，最后一个有效数字长出尾巴了。", "等一下，我找张书签夹住这一页。"],
    sharing: ["看这张！小黑把两只白爪子揣得可整齐了。", "我妈发了八张连拍。我放大比过，第二张眼睛最圆。", "它每次看见镜头就偏头，这张是拿冻干骗来的。"],
    object: ["照片右下角那一小团白的是爪子。小黑小时候这块就这么白。", "背面写着「小黑值班中」。它值的班就是睡在键盘上。", "我把照片贴在灯旁边了，写报告一抬头就能看见。"],
    cross: ["给我十秒！刚才想到的那一步又被你戳没了。", "你跟小黑踩回车键的节奏还挺像。", "这页写完再闹，我刚把小数点对齐。"],
  },
  classroom: {
    name: "物理教室", alt: "阳光穿过窗户，小青坐在光学实验台前翻笔记。",
    item: "转棱镜", itemName: "三棱镜", tile: "100% 0%", ambient: "·",
    idle: "你来得正好，帮我看看这条光落在哪根刻线上。",
    startled: ["嘶，铅笔芯断了。你从哪边冒出来的？", "到我读数了？我刚低头补了个单位。", "手稳一下，差点把这组抄进隔壁那一列。"],
    sharing: ["你看玻璃这条边。转一点，桌上的颜色就跟着走。", "把白纸挪过来，光谱落在书封上都看不清。", "这块玻璃比讲义上的图好玩多了。下课我还想试一次。"],
    object: ["盯着白纸，慢慢转棱镜。红光和紫光的位置分开了。", "拿两端就好，中间那面刚擦过。给你转一下。", "这条彩带收到实验本上，倒是很像我夹了张彩色书签。"],
    cross: ["嘘，老师已经往这排看了。", "你来帮我记读数吧，手也有正经事做了。", "下课再戳。抄错一列的数据，我可重做过整整一下午。"],
  },
  meal: {
    name: "二楼食堂", alt: "小青在食堂端着糖醋里脊饭，手里拿着筷子。",
    item: "看饭碗", itemName: "糖醋里脊饭", tile: "0% 100%", ambient: "♨",
    idle: "对面的位子我占着呢。快坐，今天的里脊还冒着热气。",
    startled: ["哎！这块差点从筷子尖滑下去。", "等我咽一下。你刚才是不是盯上我碗里这块了？", "吓我一跳，我还以为阿姨来收盘子了。"],
    sharing: ["给你挑块边上脆的。张嘴，酱汁要滴啦。", "尝一块再投票。今天这锅我站满分。", "这个角还带点焦糖色，夹给你。"],
    object: ["白饭上拌一小勺汁刚好。拌多了脆皮会软。", "你看这块边缘的薄壳，排二楼这队就为这一口。", "今天阿姨的手特别稳，最底下还藏了一块。"],
    cross: ["碗我先抱走。最后这块已经有主人了。", "停！筷子悬在半空真的会手酸。", "再伸手，可就轮到你排奶茶那队了。"],
  },
  rest: {
    name: "熄灯后的宿舍", alt: "月光照进宿舍，小青枕着枕头，裹在蓝色被子里。",
    item: "看闹钟", itemName: "床头闹钟", tile: "50% 100%", ambient: "z",
    idle: "呼……",
    startled: ["嗯……闹钟响到第几个了……", "眼睛开一条缝……天还黑着呀。", "被角给我留一点……"],
    sharing: ["再五分钟……这边枕头凉凉的……", "唔，土星慢点跑……我还没画完……", "小黑……键盘让给你……"],
    object: ["五个闹钟……都定过了……", "床头这个铃好响，发条先别碰……", "听到了，滴答、滴答……"],
    cross: ["好好好，我坐起来了。你最好是看见流星了。", "我连枕头都抱起来了。现在可以告诉我几点了吗？", "谁在值这个早八叫醒班……我要给你发一面锦旗。"],
    settle: "说好的五分钟。枕头还热着呢……呼……",
  },
  anime: {
    name: "上铺小剧场", alt: "熄灯后，小青戴着耳机靠在床上看动画。",
    item: "看手机", itemName: "追番的手机", tile: "100% 100%", ambient: "♫",
    idle: "给你留一只耳机。室友睡了，咱俩轻一点。",
    startled: ["哎，耳机线挂住了！给我按一下暂停。", "吓得我点回片头了。刚才播到几分来着？", "你也醒着啊？我还以为宿管来查寝。"],
    sharing: ["这里！第一集窗边那颗星，现在终于又出现了。", "你看这个停顿。她还没说话，我已经想倒回去看一遍了。", "耳机给你，这句配乐要跟画面一起听。"],
    object: ["亮度放最低了。你靠过来一点看，字幕还挺小。", "暂停在这帧正好。背景那座天文台你也看到了吧？", "看完这段让我先猜结局。弹幕我攒到二刷再开。"],
    cross: ["手机我先收一下。再笑出声，全宿舍都要知道结局了。", "嘘！下铺刚翻了个身。", "先别碰进度条，我好不容易躲过了三集剧透。"],
  },
  campus: {
    name: "教学楼岔路", alt: "清晨的银杏树下，小青背着书包展开校园地图。",
    item: "摊开地图", itemName: "口袋地图", tile: "0% 0%", fieldRow: 0, ambient: "❧",
    idle: "先找那排银杏。再往前一个路口，就轮到我认路了。",
    startled: ["哎，你从自行车棚后面冒出来的？", "刚才看到第几个路口了？好，重新数。", "等会儿，我把地图举反了。"],
    sharing: ["看，车架歪掉的角就在这里。教学楼在它后面。", "纸上的北对上了。现在咱俩和这张图朝同一个方向。", "天上三颗星我一眼就认出来。地上这三栋楼是真像。"],
    object: ["我在楼梯口画了颗星。找到这颗星，早八就有救。", "这张纸已经能单手折回书包侧袋了。熟练工。", "沿着银杏那排小圆点走。圆点是我自己补的。"],
    cross: ["给你掌图。领错一栋，咱俩一起赶早八。", "我刚记住左转，你一戳，脑子里又右转了。", "手指留着指路。前面那个岔口交给你了。"],
  },
  tea: {
    name: "奶茶窗口", alt: "薄荷色遮阳篷下，小青端着贴有星星杯贴的奶茶。",
    item: "看杯贴", itemName: "少糖去冰奶茶", tile: "100% 0%", fieldRow: 1, ambient: "○",
    idle: "拿到了。先看一眼杯贴，少糖、去冰，对上了。",
    startled: ["嚯，差点给我的袖口也分一口。", "手稳住了。珍珠刚才在杯子里集体转弯。", "你来这么快？我连吸管都还捏在手上。"],
    sharing: ["你看这颗歪歪的星星，贴纸正好盖在我的名字旁边。", "少糖这一杯能喝出茶味。我就喜欢这一口。", "先把杯子拿稳，我给你演示吸管一次扎正中间。"],
    object: ["杯贴上这颗星我想留着，等杯子干了夹进笔记本。", "去冰那一栏打了勾。上回我把那个勾认成了半糖。", "我先把杯贴转到正面。这样举起来才看得清。"],
    cross: ["再晃，下一颗珍珠该从吸管里起飞了。", "我先护住封口。外套昨天才洗。", "这杯由我押送回寝，路上严禁颠簸。"],
  },
  game: {
    name: "雨天掌机局", alt: "雨打在宿舍窗上，小青穿着青绿色帽衫捧着掌机。",
    item: "看掌机", itemName: "掌机存档", tile: "0% 100%", fieldRow: 2, ambient: "◆",
    idle: "从这个平台起跳。手熟了，嘴上可以提前庆祝一下。",
    startled: ["啊。庆祝早了，回存档点。", "我按出二段跳了吗？刚才拇指抖了一下。", "你来了啊。等我落个地，这会儿脚底下全是空的。"],
    sharing: ["你看屏幕边上那个小缺口，拿它当起跳线就行。", "这一跳我拆成两段练，先够到墙，再想怎么落地。", "给你看存档点。设计它的人显然比这段地形温柔。"],
    object: ["这颗跳跃键都被我按亮了。侧过来才能看见。", "音量留一格刚好能听见落地声，室友还能午睡。", "这段过了就存。上一回我手比脑子快，又点了重开。"],
    cross: ["等我落地再戳！刚那一跳我快够着了。", "重开得这么熟练，有你一份功劳。", "手柄交给你。来，给我看看什么叫稳。"],
  },
  library: {
    name: "图书馆旧地图", alt: "午后的历史书架前，小青翻开绿色布面书，书页里夹着旧城图。",
    item: "翻旧城图", itemName: "折页旧城图", tile: "100% 100%", fieldRow: 3, ambient: "·",
    idle: "借书小票还夹在这儿。上次刚沿着河找到第一座桥。",
    startled: ["嗯？我刚才还在书里那座城门底下。", "等我按住这页，折图自己弹回去过两次了。", "你拉椅子好轻。我都没听见。"],
    sharing: ["顺着我的手指走。旧河道一转弯，这条街也跟着弯。", "两座桥隔得这么近，我想翻回去看看当年的集市在哪。", "你看这个折页，摊开比书宽一倍。桌上给我腾一小块。"],
    object: ["这条细蓝线是河。顺着它找，城门比在正文里好认。", "缺了一角的地方正好在折痕上。把两边对起来就能接上。", "我喜欢这种书，翻到最后还藏着一整座城。"],
    cross: ["嘘，隔壁桌已经抬头了。", "先让我夹上小票。第三回找同一页就有点丢人了。", "旧地图的折痕可经不起咱俩一起折腾。"],
  },
};


function loadImage(src) {
  const image = new Image();
  image.alt = "";
  image.src = src;
  return image.decode().then(() => image, () => null);
}

export function createStagePlayer(host) {
  let request = 0;
  let displayedKey, dispose;
  return async (key, label) => {
    const current = ++request;
    if (key === displayedKey) {
      host.removeAttribute("aria-busy");
      return;
    }
    host.setAttribute("aria-busy", "true");
    const base   = "assets/live/";
    const prefix = scenes[key].fieldRow === undefined ? "" : "field-";
    const [room, poses] = await Promise.all([
      loadImage(base + key + "-room.webp"), loadImage(base + key + "-poses.webp"),
      loadImage(base + prefix + "faces.webp"), loadImage(base + prefix + "items.webp"),
    ]);
    if (current !== request) return;
    const layered  = Boolean(room && poses);
    const backdrop = layered ? room : await loadImage("assets/" + key + ".webp");
    if (current !== request) return;
    dispose?.();
    dispose = mountStage(host, key, label, backdrop, layered ? poses : null);
    displayedKey = key;
    host.removeAttribute("aria-busy");
  };
}

// Commit the decoded images and the matching dialogue together.
function mountStage(host, key, label, backdrop, poses) {
  const scene = scenes[key];
  const field = scene.fieldRow !== undefined;
  host.innerHTML = `<div class="stage${field ? " field-stage" : ""}${poses ? " layered" : ""}" data-scene="${key}" data-pose="idle" style="--item-tile:${scene.tile};${field ? "--face-y:" + scene.fieldRow / 3 * 100 + "%" : ""}">
    <div class="viewport"><button class="screen" id="poke" type="button" aria-label="轻点小青。${scene.alt}">
      ${backdrop ? "" : '<span class="art-status">场景插画暂时读不到</span>'}
      <span class="actor"><span class="pose"></span></span>
      <span class="ambience" aria-hidden="true"><i>${scene.ambient}</i><i>${scene.ambient}</i><i>${scene.ambient}</i></span>
      <span class="tap-fx" aria-hidden="true"></span><span class="screen-label">${label}</span>
      <span class="speech" aria-hidden="true" hidden></span></button>
      <button class="prop" type="button"><span class="item-icon" aria-hidden="true"></span>${scene.item}</button>
      <div class="item-view" aria-hidden="true" hidden><span class="item-icon"></span><span>${scene.itemName}</span></div></div>
    <div class="dialogue"><div class="portrait" aria-hidden="true"></div>
      <div class="dialogue-copy"><span class="speaker">小青</span><p id="line" aria-live="polite">${scene.idle}</p></div>
      <button class="advance" type="button" aria-label="听下一句" title="听下一句">▼</button></div></div>`;

  const stage    = host.firstElementChild;
  const screen   = stage.querySelector(".screen");
  const actor    = stage.querySelector(".actor");
  const pose     = stage.querySelector(".pose");
  const speech   = stage.querySelector(".speech");
  const line     = stage.querySelector("#line");
  const itemView = stage.querySelector(".item-view");
  const effects  = stage.querySelector(".tap-fx");
  if (backdrop) {
    backdrop.className = poses ? "room" : "scene-art";
    screen.prepend(backdrop);
  }
  if (poses) pose.append(poses);

  const decks      = new Map();
  const reduced    = matchMedia("(prefers-reduced-motion: reduce)");
  let taps         = [];
  let protestUntil = 0;
  let tapCount     = 0;
  let restTimer;
  let speechTimer;
  let movement;
  function speak(pool) {
    let deck = decks.get(pool);
    if (!deck?.length) {
      deck = [...scene[pool]];
      for (let i = deck.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [deck[i], deck[j]] = [deck[j], deck[i]];
      }
      if (deck.at(-1) === line.textContent) {
        [deck[0], deck[deck.length - 1]] = [deck.at(-1), deck[0]];
      }
      decks.set(pool, deck);
    }
    line.textContent = deck.pop();
    speech.textContent = line.textContent;
    speech.hidden = false;
    clearTimeout(speechTimer);
    speechTimer = setTimeout(() => { speech.hidden = true; itemView.hidden = true; }, 3600);
  }
  function interact(action, event) {
    const now = performance.now();
    let pool  = action === "item" ? "object" : "sharing";
    if (action === "tap" && now >= protestUntil) {
      taps = taps.filter(time => now - time < 3500);
      taps.push(now);
      pool = tapCount++ % 2 === 0 ? "startled" : "sharing";
      if (taps.length >= 4) {
        protestUntil = now + (key === "rest" ? 12000 : 3800);
        taps = [];
      }
    }
    const protesting = now < protestUntil;
    const state      = protesting ? "cross" : pool === "startled" ? "startled" : "sharing";
    if (protesting && action !== "item") pool = "cross";
    clearTimeout(restTimer);
    stage.dataset.pose = state;
    itemView.hidden = action !== "item";
    speak(pool);

    movement?.cancel();
    effects.replaceChildren();
    if (!reduced.matches) {
      const frames = {
        startled: ["none", "translateY(-3%) scale(.97,1.03)", "scale(1.025,.98)", "none"],
        sharing: ["none", "rotate(-2deg) translateY(-1.3%)", "rotate(1deg)", "none"],
        cross: ["none", "translateX(-2.2%) rotate(-2deg)", "translateX(1.8%)", "translateX(-1%)", "none"],
      };
      movement = actor.animate(frames[state].map(transform => ({transform})), {
        duration: state === "cross" ? 480 : 390, easing: "ease-out",
      });
      const bounds  = screen.getBoundingClientRect();
      const pointer = action === "tap" && event.detail > 0;
      effects.style.left = pointer ? `${event.clientX - bounds.left}px` : "69%";
      effects.style.top = pointer ? `${event.clientY - bounds.top}px` : "34%";
      const mark = state === "cross" ? "!" : action === "item" ? "✦" : scene.ambient;
      effects.innerHTML = `<i class="ripple"></i><b>${mark}</b><b>${mark}</b><b>${mark}</b>`;
    }

    restTimer = setTimeout(() => {
      stage.dataset.pose = "idle";
      if (protesting && key === "rest") {
        line.textContent = scene.settle;
        speech.textContent = scene.settle;
        speech.hidden = false;
        clearTimeout(speechTimer);
        speechTimer = setTimeout(() => { speech.hidden = true; }, 3000);
      }
      protestUntil = 0;
      itemView.hidden = true;
    }, protesting ? protestUntil - now : 3400);
  }
  screen.onclick = event => interact("tap", event);
  stage.querySelector(".prop").onclick = event => interact("item", event);
  stage.querySelector(".advance").onclick = event => interact("talk", event);
  return () => {
    clearTimeout(restTimer);
    clearTimeout(speechTimer);
    movement?.cancel();
  };
}
