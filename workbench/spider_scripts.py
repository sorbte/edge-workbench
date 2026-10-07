"""注入页面的宠物（蜘蛛）联动脚本：顶层特效引擎与 iframe 词采器。

纯 JavaScript 字符串，由 worker 通过 Playwright evaluate 注入。"""
from __future__ import annotations


SPIDER_OVERLAY_SCRIPT = r"""
(pet) => {
  "use strict";
  /* 操作可视化 · 宠物联动漫游特效：纯视觉，无任何 UI 挂件。
     宠物平时在可视区内随机游走、可与用户互动（点击时冲刺过去）；**路过绝不抓取**。
     抓取只由插件/工作台在「要获取信息」的时刻显式触发，且带指向性：
     宠物径直冲刺到目标位置（背对目标时原地减速掉头，不做大弧线绕行），
     冲刺途中**路过目标矩形内的词条就逐个点亮**（接触式采集），
     抵达后矩形内剩余词条从宠物所在位置向外以波纹节奏快速补齐——
     既不是绕几圈后整块闪亮，也不会漏标。每次接触标记时在词条旁边弹出
     「已抓取」小提示窗，约 1 秒后渐隐。
     顶层文档之外的内容由各 iframe 里的「词采器」(window.__wbs_collector) 负责：
     worker 侧把矩形换算成帧内坐标后逐帧采集，本脚本只管顶层词条与全部视觉特效。
     公开 API（坐标均为视口坐标，同 Playwright bounding_box）：
       __wbs.fetchRect(x, y, w, h)  指向性抓取：冲刺途中逐词标记 + 抵达后波纹补齐
       __wbs.fetchAt(x, y, r)       指向性抓取：半径 r 的圆形区域
       __wbs.dashTo(x, y)           纯冲刺移动（不抓取）；冲刺速度与剩余距离成正比
       __wbs.harvestRect(x,y,w,h)   原地立即采集矩形内词条（不移动），返回数量
       __wbs.harvestAt(x, y, r)     原地立即采集半径 r 内词条，返回数量
       __wbs.burst(x, y, n) / __wbs.ring(x, y)   粒子迸发 / 涟漪（缺省用宠物当前位置）
       __wbs.celebrate()            转圈庆祝（任务完成等时刻）
       __wbs.wander()               立刻随机换游走目标
       __wbs.addCollected(n)        记账：把帧词采器采集的词条数并入 collected 统计
       __wbs.state()                {x, y, px, py, speed, words, collected, vw, vh}
       __wbs.destroy()              完整移除
     抓取开始时会按 DOM 签名增量补扫新出现的词条（如点击后才展开的抽屉内容），
     已标记的高亮与统计全部保留。
     页内互动控件：页面右下角的宠物徽标（点开菜单：召唤到页面中央 / 开心庆祝 / 撒一把粒子），
     互动入口就在浏览器页面里、每个标签页各自一套，无需切回工作台窗口；
     控件随 destroy() 一并移除，pointer-events 只在徽标与菜单上开启，不拦截页面点击。
     重复注入：同宠物只增量重扫词条（保持状态与待执行的指向性抓取），不重置特效；
     宠物换了（pet.id 不同）则销毁旧实例、按新配色/造型全新初始化。 */
  const PET = pet && typeof pet === "object" ? pet : {};
  const PET_ID = String(PET.id || "spider");
  if (window.__wbs) {
    if (window.__wbs.pet === PET_ID && window.__wbs.version === 8) {  // 同版本同宠物：增量重扫，保留状态
      let reok = false;
      try { window.__wbs.rewordify(); reok = true; } catch (e) {}
      if (reok) return { ...window.__wbs.state() };
    }
    try { window.__wbs.destroy(); } catch (e) {}
  }

  const doc = document;
  const WORD_LIMIT = 5000;
  const TAU = Math.PI * 2;
  const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
  const easeOut = t => 1 - (1 - t) * (1 - t);
  const angNorm = a => { while (a > Math.PI) a -= TAU; while (a < -Math.PI) a += TAU; return a; };
  /* 宠物配色：主色用于词条高亮 / 粒子 / 涟漪 / 眼睛，body 决定本体画法 */
  const hexRGB = h => {
    h = String(h || "").replace("#", "");
    if (h.length === 3) h = h.split("").map(c => c + c).join("");
    const n = parseInt(h, 16) || 0x54d7e8;
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  };
  const ACCENT = String(PET.accent || "#54d7e8");
  const ACCENT_DIM = String(PET.accent_dim || "#155e6b");
  const BODY_KIND = String(PET.body || "spider");
  const BODY_FILL = String(PET.body_fill || "#0a0a12");
  const BODY_EDGE = String(PET.body_edge || "#f2f2fa");
  const PET_GLYPH = String(PET.glyph || "\u{1F577}");
  const SPD = Math.max(0.5, Math.min(2.5, Number(PET.speed) || 1));
  const [AR, AG, AB] = hexRGB(ACCENT);
  const accentA = a => `rgba(${AR},${AG},${AB},${a})`;
  const accentDimA = a => {
    const [r, g, b] = hexRGB(ACCENT_DIM);
    return `rgba(${r},${g},${b},${a})`;
  };

  /* ---------- 词条高亮：优先 CSS Custom Highlight API（零 DOM 改动），旧引擎回退 span 包裹 ---------- */
  const useHighlight = typeof Highlight !== "undefined" && !!(window.CSS && CSS.highlights);
  const highlightObj = useHighlight ? new Highlight() : null;
  let pageSheet = null;
  let fallbackMode = !useHighlight;
  try {
    pageSheet = new CSSStyleSheet();
    pageSheet.replaceSync(fallbackMode
      ? `.wbs-word{transition:background .1s ease,color .1s ease;border-radius:1px}.wbs-word.wbs-sel{background:${ACCENT};color:#04161c;box-shadow:0 0 10px ${accentA(.55)}}`
      : `::highlight(wbs-crawl){background-color:${ACCENT};color:#04161c}`);
    doc.adoptedStyleSheets = doc.adoptedStyleSheets.concat([pageSheet]);
    if (useHighlight) CSS.highlights.set("wbs-crawl", highlightObj);
  } catch (e) { pageSheet = null; }

  const SKIP_SELECTOR = "script,style,noscript,template,iframe,svg,canvas,textarea,select,option,input,#wbs-root,[contenteditable='true']";
  const words = [];
  /* 已扫过的文本节点：抓取时按 DOM 签名增量补扫，只处理新增节点，
     这样点击后才展开的抽屉/弹层内容也能被标记，且不清掉已抓取的高亮 */
  const seenNodes = new WeakSet();
  let TOTAL = 0;

  /* 处理单个文本节点：拆词入表（fallback 模式顺带包 span），返回新增词条数 */
  function harvestTextNode(node) {
    const p = node.parentElement;
    if (!p || p.closest(SKIP_SELECTOR)) return 0;
    const text = node.nodeValue;
    if (!text || !text.trim()) return 0;
    let added = 0;
    if (fallbackMode) {
      const frag = doc.createDocumentFragment();
      for (const part of text.split(/(\s+)/)) {
        if (!part) continue;
        if (/^\s+$/.test(part) || words.length >= WORD_LIMIT) {
          const t = doc.createTextNode(part);
          frag.appendChild(t);
          seenNodes.add(t);
          continue;
        }
        const span = doc.createElement("span");
        span.className = "wbs-word";
        span.textContent = part;
        words.push({ el: span, sel: false, x: 0, y: 0, w: 0, h: 0, text: part });
        added++;
        frag.appendChild(span);
        if (span.firstChild) seenNodes.add(span.firstChild);
      }
      node.parentNode.replaceChild(frag, node);
    } else {
      const re = /\S+/g;
      let m;
      while ((m = re.exec(text))) {
        if (words.length >= WORD_LIMIT) break;
        const range = doc.createRange();
        range.setStart(node, m.index);
        range.setEnd(node, m.index + m[0].length);
        words.push({ range, sel: false, x: 0, y: 0, w: 0, h: 0, text: m[0] });
        added++;
      }
    }
    return added;
  }

  function walkTextNodes() {
    if (!doc.body) return [];
    const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT, null);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    return nodes;
  }

  function collectWords() {
    words.length = 0;
    for (const node of walkTextNodes()) {
      if (seenNodes.has(node)) continue;
      seenNodes.add(node);
      harvestTextNode(node);
    }
    TOTAL = words.length;
  }

  /* 增量补扫：只收集新出现的文本节点（SPA 重渲染 / 新展开的抽屉等），
     保留已有词条与其高亮状态；返回新增数量 */
  function scanNewWords() {
    let added = 0;
    for (const node of walkTextNodes()) {
      if (seenNodes.has(node)) continue;
      seenNodes.add(node);
      added += harvestTextNode(node);
    }
    if (added) TOTAL = words.length;
    return added;
  }

  /* 抓取入口处的保鲜：DOM 有变化先增量补扫，再重测坐标。
     抽屉展开后内容 rect 从 0 变为有效，靠这一步才进得了采集范围 */
  function ensureFreshWords() {
    const sig = domSignature();
    if (sig !== docSig) { docSig = sig; scanNewWords(); }
    measure();
  }

  const domSignature = () => (doc.body ? doc.body.textContent.length * 31 + doc.body.querySelectorAll("*").length : 0);
  let docSig = 0;
  collectWords();
  docSig = domSignature();

  /* ---------- 悬浮控制台：挂在 Shadow DOM 里，样式不与页面互相污染 ---------- */
  const root = doc.createElement("div");
  root.id = "wbs-root";
  root.style.cssText = "position:fixed;left:0;top:0;width:100vw;height:100vh;margin:0;padding:0;border:0;background:transparent;z-index:2147483000;pointer-events:none";
  const shadow = root.shadowRoot || root.attachShadow({ mode: "open" });
  shadow.innerHTML = `
<style>
*{margin:0;padding:0;box-sizing:border-box}
#wbs-fx{position:fixed;inset:0;width:100vw;height:100vh;pointer-events:none}
#wbs-vignette{position:fixed;inset:0;pointer-events:none;background:radial-gradient(ellipse at 50% 42%,transparent 58%,rgba(0,0,0,.20))}
#wbs-scan{position:fixed;inset:0;pointer-events:none;background:repeating-linear-gradient(0deg,rgba(255,255,255,.022) 0 1px,transparent 1px 3px)}
.wbs-toast{position:fixed;left:0;top:0;background:rgba(10,12,18,.88);border:1px solid ${accentA(.6)};color:${ACCENT};font:11px/1.5 Consolas,Menlo,monospace;padding:2px 8px;border-radius:3px;box-shadow:0 0 10px ${accentA(.45)};pointer-events:none;white-space:nowrap;max-width:220px;overflow:hidden;z-index:6}
#wbs-play{position:fixed;right:14px;bottom:14px;z-index:8;pointer-events:auto}
#wbs-play-btn{width:34px;height:34px;border-radius:50%;border:1px solid ${accentA(.5)};background:rgba(10,12,18,.68);color:${ACCENT};font-size:17px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 10px rgba(0,0,0,.35);transition:transform .15s ease,background .15s ease;padding:0}
#wbs-play-btn:hover{transform:scale(1.1);background:rgba(16,20,30,.82)}
#wbs-play-btn.wbs-open{transform:rotate(90deg)}
#wbs-play-menu{position:fixed;right:14px;bottom:56px;display:flex;flex-direction:column;gap:6px;pointer-events:auto}
#wbs-play-menu[hidden]{display:none}
#wbs-play-menu button{min-width:104px;padding:6px 12px;border-radius:8px;border:1px solid ${accentA(.45)};background:rgba(12,14,22,.85);color:#eef2f8;font:12px/1.4 Consolas,Menlo,monospace;cursor:pointer;text-align:left;transition:background .12s ease}
#wbs-play-menu button:hover{background:${accentA(.3)}}
</style>
<canvas id="wbs-fx"></canvas>
<div id="wbs-vignette"></div>
<div id="wbs-scan"></div>
<div id="wbs-play">
  <button id="wbs-play-btn" title="宠物互动" aria-label="宠物互动">${PET_GLYPH}</button>
  <div id="wbs-play-menu" hidden>
    <button type="button" data-act="call">召唤到页面中央</button>
    <button type="button" data-act="celebrate">开心庆祝</button>
    <button type="button" data-act="burst">撒一把粒子</button>
  </div>
</div>`;
  doc.documentElement.appendChild(root);

  const cv = shadow.getElementById("wbs-fx");
  const ctx = cv.getContext("2d");

  /* ---------- 画布尺寸与词条坐标 ---------- */
  let W = window.innerWidth, H = window.innerHeight;
  const DPR = Math.min(window.devicePixelRatio || 1, 2);
  const wordRect = w => (w.el ? w.el.getBoundingClientRect() : w.range.getBoundingClientRect());
  function measure() {
    const sx = window.scrollX, sy = window.scrollY;
    for (const w of words) {
      let r;
      try { r = wordRect(w); } catch (e) { w.w = 0; continue; }
      w.x = r.left + sx; w.y = r.top + sy; w.w = r.width; w.h = r.height;
    }
  }
  function onResize() {
    W = window.innerWidth; H = window.innerHeight;
    cv.width = W * DPR; cv.height = H * DPR;
    cv.style.width = W + "px"; cv.style.height = H + "px";
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    measure();
  }

  /* ---------- 蜘蛛状态（页面坐标，滚动不丢） ---------- */
  const S = { x: 0, y: 0, heading: -0.6, speed: 0, tx: 0, ty: 0, targetAt: -9, dashUntil: 0, dashBoost: false, celebrateUntil: 0, lingerUntil: 0, effectUntil: 0, fetchRect: null, sweep: null };

  /* ---------- 8 条腿：对角步态 + 分级 IK ---------- */
  const ANG = [26, 60, 115, 150], DIST = [60, 68, 62, 54];
  const legs = [];
  for (let side = 0; side < 2; side++) for (let i = 0; i < 4; i++) {
    legs.push({
      a: (side ? -1 : 1) * ANG[i] * Math.PI / 180, rest: DIST[i] + (i % 2 ? 6 : -3), group: (i + side) % 2,
      sgn: (side ? -1 : 1) * (ANG[i] < 90 ? 1 : -1), jit: 0.96 + Math.random() * 0.08,
      fx: 0, fy: 0, stepping: false, t: 0, fx0: 0, fy0: 0, tx2: 0, ty2: 0, thresh: 0, cool: 0, fidget: false
    });
  }
  for (const l of legs) l.thresh = l.rest * 0.5 + Math.random() * 8 - 4;
  let activeGroup = 0;
  const shoulder = l => { const a = S.heading + l.a; return { x: S.x + Math.cos(a) * 9, y: S.y + Math.sin(a) * 9 }; };
  const idealFoot = l => { const a = S.heading + l.a; return { x: S.x + Math.cos(a) * l.rest, y: S.y + Math.sin(a) * l.rest }; };

  /* ---------- 目标选择 / 词条采集 ---------- */
  let count = 0;
  const particles = [], rings = [], trail = [];

  function pickTarget() {
    const top = window.scrollY + H * 0.10, bot = window.scrollY + H * 0.90;
    const pl = window.scrollX + 24, pr = window.scrollX + W - 24;
    const cand = words.filter(w => w.w > 0 && w.y > top && w.y < bot && w.x > pl && w.x < pr);
    if (cand.length && Math.random() < 0.82) {
      const w = cand[(Math.random() * cand.length) | 0];
      S.tx = w.x + w.w * 0.5;
      S.ty = w.y + w.h * 0.5;
    } else {
      S.tx = window.scrollX + 60 + Math.random() * Math.max(10, W - 120);
      S.ty = window.scrollY + 100 + Math.random() * Math.max(10, H - 170);
    }
    S.targetAt = nowS;
  }
  function initSpider() {
    S.x = window.scrollX + W * 0.5;
    S.y = window.scrollY + H * 0.45;
    for (const l of legs) { const f = idealFoot(l); l.fx = f.x; l.fy = f.y; }
    pickTarget();
  }

  /* ---------- 「已抓取」小提示窗：接触标记词条时在其旁边弹出，约 1 秒渐隐 ---------- */
  let toastBudget = 4, toastAt = -9;
  function spawnToast(w) {
    if (toastBudget <= 0 || nowS - toastAt < 0.22) return;   // 密集标记时节流，避免刷屏
    let r;
    try { r = wordRect(w); } catch (e) { return; }
    if (!r || r.width <= 0) return;
    toastAt = nowS; toastBudget--;
    const c = doc.createElement("div");
    c.className = "wbs-toast";
    c.textContent = "已抓取" + (w.text ? " · " + w.text.slice(0, 12) : "");
    c.style.left = clamp(r.right + 6, 4, Math.max(4, window.innerWidth - 150)) + "px";
    c.style.top = clamp(r.top - 4, 4, Math.max(4, window.innerHeight - 26)) + "px";
    shadow.appendChild(c);
    const anim = c.animate([
      { opacity: 0, transform: "translateY(4px)" },
      { opacity: 1, transform: "translateY(0)", offset: .15 },
      { opacity: 1, transform: "translateY(0)", offset: .65 },
      { opacity: 0, transform: "translateY(-5px)" }
    ], { duration: 1000, easing: "ease-out" });   // 1 秒后完全渐隐并移除
    anim.onfinish = () => { c.remove(); toastBudget++; };
  }
  function selectWord(w, px, py) {
    if (w.sel) return false;
    w.sel = true; count++;
    if (w.el) w.el.classList.add("wbs-sel");
    else if (useHighlight && highlightObj) {
      try { highlightObj.add(w.range); CSS.highlights.set("wbs-crawl", highlightObj); } catch (e) {}
    }
    for (let i = 0; i < 2; i++) particles.push({ x: px, y: py, vx: (Math.random() - .5) * 60, vy: (Math.random() - .5) * 60, life: .32, max: .32 });
    spawnToast(w);
    return true;
  }
  function collectInRect(rx0, ry0, rx1, ry1, max) {
    let n = 0;
    for (const w of words) {
      if (n >= max) break;
      if (w.sel || w.w <= 0) continue;
      if (w.x + w.w < rx0 || w.x > rx1 || w.y + w.h < ry0 || w.y > ry1) continue;
      if (selectWord(w, w.x + w.w * 0.5, w.y + w.h * 0.5)) n++;
    }
    return n;
  }
  /* ---------- 指向性抓取：冲刺直达 + 途中接触采集 + 抵达后波纹补齐 ----------
     不再绕弓字形路线：宠物径直冲向目标矩形中心，途中接触到的矩形内词条逐个
     点亮（能看出是蜘蛛爬过抓的）；抵达（或超时放弃）后，矩形内剩余词条从宠物
     所在位置由近及远快速波纹补齐——不整块闪亮、没有无意义的绕圈、也不漏标。 */
  const FETCH_CONTACT = 60;   // 接触半径：词中心落在蜘蛛周围即视为“爬过”（约等于腿的伸展范围）
  const inFetchRect = (w, fr) => w.x + w.w >= fr.x0 && w.x <= fr.x1 && w.y + w.h >= fr.y0 && w.y <= fr.y1;
  /* 冲刺途中：路过目标矩形内的词条就逐个标记（每帧限量，保持“逐个点亮”的观感） */
  function contactFetch() {
    const fr = S.fetchRect;
    if (!fr) return;
    const r2 = FETCH_CONTACT * FETCH_CONTACT;
    let n = 0;
    for (const w of words) {
      if (n >= 8) break;
      if (w.sel || w.w <= 0 || !inFetchRect(w, fr)) continue;
      const dx = w.x + w.w * 0.5 - S.x, dy = w.y + w.h * 0.5 - S.y;
      if (dx * dx + dy * dy > r2) continue;
      if (selectWord(w, w.x + w.w * 0.5, w.y + w.h * 0.5)) n++;
    }
  }
  /* 波纹补齐：矩形内剩余词条按离宠物的距离由近及远逐帧标记，总量越大每帧越多，
     整体约 0.8 秒量级播完，视觉效果是以宠物为中心一圈圈荡开点亮 */
  function startSweep(rect) {
    const rest = [];
    for (const w of words) {
      if (w.sel || w.w <= 0 || !inFetchRect(w, rect)) continue;
      const dx = w.x + w.w * 0.5 - S.x, dy = w.y + w.h * 0.5 - S.y;
      rest.push({ w, d: dx * dx + dy * dy });
    }
    if (!rest.length) return 0;
    rest.sort((a, b) => a.d - b.d);
    S.sweep = { list: rest.map(item => item.w), i: 0, per: Math.max(1, Math.ceil(rest.length / 46)), until: nowS + 2.6 };
    return S.sweep.list.length;
  }
  function stepSweep() {
    const C = S.sweep;
    if (!C) return;
    let n = 0;
    while (C.i < C.list.length && n < C.per) {
      const w = C.list[C.i++];
      if (!w.sel && w.w > 0) { selectWord(w, w.x + w.w * 0.5, w.y + w.h * 0.5); n++; }
    }
    if (C.i >= C.list.length || nowS > C.until) {
      /* 超时兜底（如页面滚动打乱坐标）：就地补完剩余，保证不漏标 */
      for (; C.i < C.list.length; C.i++) {
        const w = C.list[C.i];
        if (!w.sel && w.w > 0) selectWord(w, w.x + w.w * 0.5, w.y + w.h * 0.5);
      }
      S.sweep = null;
    }
  }
  /* 指向性抓取的落地动作：抵达目标后波纹补齐矩形内剩余词条 + 反馈特效；
     特效播放期间 fetching 保持 True，让工作台的自动等待把"特效播完"也包含进去 */
  function performFetch() {
    const fr = S.fetchRect;
    if (!fr) return 0;
    S.fetchRect = null;
    ensureFreshWords();   // 冲刺期间 DOM 可能又变了（如抽屉刚展开），落地前再补扫一次
    const swept = startSweep(fr);
    burst(S.x, S.y, Math.min(16, 6 + swept));
    ring(S.x, S.y);
    const effect = swept > 0 ? 1.1 : 0.6;   // 粒子/涟漪/提示窗播完所需时间
    S.effectUntil = nowS + effect;
    S.lingerUntil = Math.max(S.lingerUntil, nowS + effect);
    return swept;
  }

  /* ---------- 公开 API：抓取必须显式、带指向性；平时只游走与互动 ---------- */
  function burst(x, y, n) {
    for (let i = 0; i < n; i++) {
      const a = Math.random() * TAU, v = 40 + Math.random() * 130;
      particles.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: .5, max: .5 });
    }
  }
  function ring(x, y) { rings.push({ x, y, t: 0 }); }
  function state() {
    return { x: S.x - window.scrollX, y: S.y - window.scrollY, px: S.x, py: S.y, speed: Math.round(S.speed), words: TOTAL, collected: count, fetching: !!S.fetchRect || !!S.sweep || nowS < S.effectUntil, vw: W, vh: H };
  }
  const clampTarget = (x, y) => ({
    x: clamp(x, window.scrollX + 20, window.scrollX + W - 20),
    y: clamp(y, window.scrollY + 60, Math.max(window.scrollY + 61, window.scrollY + H - 40))
  });
  const api = {
    version: 8,
    pet: PET_ID,
    /* 指向性抓取：径直冲刺到目标矩形，途中路过即逐词标记，
       抵达后从宠物位置向外波纹补齐矩形内剩余词条 */
    fetchRect(x, y, w, h) {
      ensureFreshWords();   // DOM 有新增（如刚展开的抽屉）先增量补扫并重测坐标
      const px = (Number(x) || 0) + window.scrollX, py = (Number(y) || 0) + window.scrollY;
      const w2 = Number(w) || 0, h2 = Number(h) || 0;
      S.fetchRect = { x0: px, y0: py, x1: px + w2, y1: py + h2 };
      const c = clampTarget(px + w2 / 2, py + h2 / 2);
      S.tx = c.x; S.ty = c.y; S.targetAt = nowS;
      S.dashUntil = nowS + 4.5; S.dashBoost = true;
      ring(c.x, c.y);
      return { ...state(), tx: c.x, ty: c.y };
    },
    fetchAt(x, y, r) {
      const rr = Number(r) || 150;
      return api.fetchRect((Number(x) || 0) - rr, (Number(y) || 0) - rr, rr * 2, rr * 2);
    },
    /* 纯冲刺移动：只与用户/自动化互动，不抓取 */
    dashTo(x, y) {
      const c = clampTarget(Number(x) || 0, Number(y) || 0);
      S.tx = c.x; S.ty = c.y; S.targetAt = nowS;
      S.dashUntil = nowS + 4.5; S.dashBoost = true;
      ring(c.x, c.y);
      return { ...state(), tx: c.x, ty: c.y };
    },
    /* 原地立即采集（不移动） */
    harvestRect(x, y, w, h, max) {
      ensureFreshWords();
      const px = (Number(x) || 0) + window.scrollX, py = (Number(y) || 0) + window.scrollY;
      const w2 = Number(w) || 0, h2 = Number(h) || 0;
      const n = collectInRect(px, py, px + w2, py + h2, Number(max) || 60);
      burst(px + w2 / 2, py + h2 / 2, Math.min(12, 3 + n));
      return n;
    },
    harvestAt(x, y, radius, max) {
      ensureFreshWords();
      const r = Number(radius) || 150;
      const px = (Number(x) || 0) + window.scrollX, py = (Number(y) || 0) + window.scrollY;
      const n = collectInRect(px - r, py - r, px + r, py + r, Number(max) || 48);
      burst(px, py, Math.min(14, 4 + n));
      ring(px, py);
      return n;
    },
    burst(x, y, n) {
      const vx = x == null ? S.x - window.scrollX : Number(x);
      const vy = y == null ? S.y - window.scrollY : Number(y);
      burst(vx + window.scrollX, vy + window.scrollY, Number(n) || 14);
      return state();
    },
    ring(x, y) {
      const vx = x == null ? S.x - window.scrollX : Number(x);
      const vy = y == null ? S.y - window.scrollY : Number(y);
      ring(vx + window.scrollX, vy + window.scrollY);
      return state();
    },
    celebrate() {
      S.celebrateUntil = nowS + 1.25;
      S.fetchRect = null;   // 停止冲刺；进行中的波纹补齐继续播完，保证不漏标
      burst(S.x, S.y, 16);
      ring(S.x, S.y);
      return state();
    },
    wander() {
      S.dashUntil = 0; S.fetchRect = null; S.sweep = null; pickTarget();
      return state();
    },
    /* 记账：把 iframe 词采器采集的词条数并入本页统计（仅计数，不参与特效） */
    addCollected(n) { count += Math.max(0, Number(n) || 0); return state(); },
    /* 同一页面重复注入时由脚本头部调用：DOM 未变化时只重新测量（保留采集与待执行抓取），
       内容有变化（SPA 重渲染 / 新展开的抽屉）时增量补扫新词条，已抓取的高亮与统计保留 */
    rewordify() {
      const sig = domSignature();
      if (sig !== docSig) {
        docSig = sig;
        scanNewWords();
      }
      measure();
      return state();
    },
    state,
    destroy
  };

  /* ---------- 用户互动：点击页面时蜘蛛冲刺过去看看（纯互动，不抓取） ---------- */
  function onPointerDown(e) {
    try { if (e && e.composedPath && e.composedPath().indexOf(root) >= 0) return; } catch (err) {}
    S.tx = e.clientX + window.scrollX; S.ty = e.clientY + window.scrollY;
    S.dashUntil = nowS + 4.5; S.dashBoost = true;
    rings.push({ x: S.tx, y: S.ty, t: 0 });
  }

  function updateBody(dt) {
    if (nowS < S.celebrateUntil) {
      /* 庆祝：原地转圈 */
      S.heading = angNorm(S.heading + dt * 9);
      S.speed += (60 - S.speed) * Math.min(1, dt * 4);
    } else {
      const want = Math.atan2(S.ty - S.y, S.tx - S.x);
      const diff = angNorm(want - S.heading);
      const eager = nowS < S.dashUntil;
      const lingering = nowS < S.lingerUntil;
      /* 轨迹控制：冲刺时转向速率加倍，且速度随朝向对齐度缩放——背对/侧对目标时
         先原地减速掉头、再直线冲刺，不再冲出大弧线；游走时保留慢转向与轻微
         摆动，步态自然 */
      const turnRate = eager ? 10 : 4.5;
      S.heading = angNorm(S.heading + clamp(diff, -turnRate * dt, turnRate * dt) + (eager ? 0 : Math.sin(nowS * 1.7) * 0.004));
      /* 冲刺速度与剩余距离成正比（远目标自动加速到 950px/s 封顶），保证跟得上自动化节奏 */
      const dashTarget = (S.dashBoost ? clamp(Math.hypot(S.tx - S.x, S.ty - S.y) * 2.4, 340, 950) : 240) * SPD;
      const align = Math.max(0, Math.cos(diff));
      const alignScale = eager ? 0.2 + 0.8 * align : 1;   // 越没对准越慢（原地转向），杜绝绕大弯
      S.speed += ((lingering ? 0 : (eager ? dashTarget * alignScale : 80 * SPD)) - S.speed) * Math.min(1, dt * 5);
      if (!eager && !lingering && Math.random() < dt * 0.10) { S.dashBoost = false; S.dashUntil = nowS + 0.4 + Math.random() * 0.4; }
      if (S.fetchRect) contactFetch();   // 冲刺途中：爬过谁就点亮谁（限目标矩形内）
      if (Math.hypot(S.tx - S.x, S.ty - S.y) < 26 || nowS - S.targetAt > 6.5) {
        if (eager) { S.lingerUntil = Math.max(S.lingerUntil, nowS + 0.55); S.dashUntil = 0; }
        if (S.fetchRect) performFetch();  // 抵达目标（或超时放弃）时波纹补齐指向性抓取
        pickTarget();
      }
    }
    S.x += Math.cos(S.heading) * S.speed * dt;
    S.y += Math.sin(S.heading) * S.speed * dt;
    /* 只在可视区内活动，绝不代替页面/自动化滚动 */
    S.x = clamp(S.x, window.scrollX + 10, Math.max(window.scrollX + 11, window.scrollX + W - 10));
    S.y = clamp(S.y, window.scrollY + 56, Math.max(window.scrollY + 57, window.scrollY + H - 36));
  }

  /* ---------- 步态：脚落定，拉太远才迈步；空闲时原地小碎步 ---------- */
  function updateLegs(dt) {
    let stepping = 0;
    for (const l of legs) if (l.stepping) stepping++;
    for (const l of legs) {
      l.cool -= dt;
      const ideal = idealFoot(l);
      if (!l.stepping) {
        const d = Math.hypot(l.fx - ideal.x, l.fy - ideal.y);
        const dBody = Math.hypot(l.fx - S.x, l.fy - S.y);
        const urgent = stepping < 6 && (d > l.thresh * 1.85 || dBody < l.rest * 0.6);
        if (urgent || (l.cool <= 0 && stepping < 5 && l.group === activeGroup && d > l.thresh)) {
          l.stepping = true; l.t = 0; l.fx0 = l.fx; l.fy0 = l.fy;
          l.tx2 = ideal.x + Math.cos(S.heading) * S.speed * 0.22 + (Math.random() * 8 - 4);
          l.ty2 = ideal.y + Math.sin(S.heading) * S.speed * 0.22 + (Math.random() * 8 - 4);
          let vx = l.tx2 - S.x, vy = l.ty2 - S.y, vd = Math.hypot(vx, vy) || 1;
          const minR = l.rest * 0.72, maxR = l.rest * 1.28;
          if (vd < minR) { const k = minR / vd; l.tx2 = S.x + vx * k; l.ty2 = S.y + vy * k; }
          else if (vd > maxR) { const k = maxR / vd; l.tx2 = S.x + vx * k; l.ty2 = S.y + vy * k; }
          for (const o of legs) {
            if (o === l) continue;
            const ox = l.tx2 - o.fx, oy = l.ty2 - o.fy, od = Math.hypot(ox, oy);
            if (od < 24 && od > 0.01) { const k = 24 / od; l.tx2 = o.fx + ox * k; l.ty2 = o.fy + oy * k; }
          }
          stepping++;
        } else if (S.speed < 14 && l.cool <= 0 && Math.random() < dt * 0.8) {
          l.stepping = true; l.t = 0; l.fx0 = l.fx; l.fy0 = l.fy; l.fidget = true;
          l.tx2 = ideal.x + (Math.random() * 10 - 5); l.ty2 = ideal.y + (Math.random() * 10 - 5);
        }
      } else {
        l.t += dt / 0.13;
        const tt = Math.min(1, l.t), e = easeOut(tt);
        const lift = Math.sin(Math.PI * tt) * 5;
        const nx = Math.cos(S.heading + Math.PI / 2), ny = Math.sin(S.heading + Math.PI / 2);
        l.fx = l.fx0 + (l.tx2 - l.fx0) * e + nx * lift;
        l.fy = l.fy0 + (l.ty2 - l.fy0) * e + ny * lift;
        if (tt >= 1) {
          l.stepping = false; l.fx = l.tx2; l.fy = l.ty2; l.cool = 0.06;
          if (l.fidget) l.fidget = false; else activeGroup = 1 - activeGroup;
        }
      }
    }
  }

  /* ---------- 绘制：两段式 IK 逐级嵌套；腿用深色描边 + 白芯，深浅页面都看得清 ---------- */
  function solve2(bx, by, tx, ty, l1, l2, sgn) {
    let dx = tx - bx, dy = ty - by, d = Math.hypot(dx, dy) || 1e-4;
    const maxD = l1 + l2 - 0.5;
    if (d > maxD) { const k = maxD / d; dx *= k; dy *= k; d = maxD; tx = bx + dx; ty = by + dy; }
    const a = Math.max(0, (l1 * l1 - l2 * l2 + d * d) / (2 * d));
    const h = Math.sqrt(Math.max(0, l1 * l1 - a * a));
    const ux = dx / d, uy = dy / d;
    return { jx: bx + ux * a - uy * h * sgn, jy: by + uy * a + ux * h * sgn, tx, ty };
  }
  function drawLeg(l) {
    const s = shoulder(l);
    const j = l.jit;
    const L = [l.rest * 0.40 * j, l.rest * 0.32 * j, l.rest * 0.26 * j, l.rest * 0.24 * j];
    const ra = S.heading + l.a;
    const tdx = l.fx - s.x, tdy = l.fy - s.y, td = Math.hypot(tdx, tdy) || 1;
    let bx = Math.cos(ra) + tdx / td * 0.6, by = Math.sin(ra) + tdy / td * 0.6;
    const bn = Math.hypot(bx, by) || 1; bx /= bn; by /= bn;
    const j1x = s.x + bx * L[0], j1y = s.y + by * L[0];
    const r2 = solve2(j1x, j1y, l.fx, l.fy, L[1], L[2] + L[3], l.sgn);
    const r3 = solve2(r2.jx, r2.jy, r2.tx, r2.ty, L[2], L[3], -l.sgn);
    l.fx = r3.tx; l.fy = r3.ty;
    const px = [s.x, j1x, r2.jx, r3.jx, l.fx], py = [s.y, j1y, r2.jy, r3.jy, l.fy];
    ctx.lineCap = "round";
    ctx.shadowColor = "rgba(0,0,0,.5)"; ctx.shadowBlur = 4;
    const outerW = [4, 3.5, 3.1, 2.7], innerW = [2, 1.6, 1.3, 1.1];
    for (let i = 0; i < 4; i++) {
      ctx.strokeStyle = "rgba(8,9,14,.85)"; ctx.lineWidth = outerW[i];
      ctx.beginPath(); ctx.moveTo(px[i], py[i]); ctx.lineTo(px[i + 1], py[i + 1]); ctx.stroke();
    }
    ctx.shadowBlur = 0;
    for (let i = 0; i < 4; i++) {
      ctx.strokeStyle = "rgba(244,246,255,.95)"; ctx.lineWidth = innerW[i];
      ctx.beginPath(); ctx.moveTo(px[i], py[i]); ctx.lineTo(px[i + 1], py[i + 1]); ctx.stroke();
    }
    ctx.fillStyle = "#ff3b5e";
    for (let i = 1; i < 4; i++) { ctx.beginPath(); ctx.arc(px[i], py[i], 1.9, 0, TAU); ctx.fill(); }
    ctx.beginPath(); ctx.arc(l.fx, l.fy, 2.3, 0, TAU); ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,.85)";
    ctx.beginPath(); ctx.arc(s.x, s.y, 1.6, 0, TAU); ctx.fill();
  }
  function drawBody() {
    ctx.save();
    ctx.translate(S.x, S.y); ctx.rotate(S.heading);
    ctx.shadowColor = "rgba(0,0,0,.45)"; ctx.shadowBlur = 12;
    ctx.fillStyle = BODY_FILL; ctx.strokeStyle = BODY_EDGE; ctx.lineWidth = 2;
    ctx.beginPath();
    if (BODY_KIND === "bee") {
      /* 蜜蜂：椭圆蜂体 + 深色条纹 + 半透明翅 */
      ctx.ellipse(0, 0, 14, 8.5, 0, 0, TAU); ctx.fill(); ctx.stroke();
      ctx.shadowBlur = 0;
      ctx.fillStyle = "rgba(30,24,8,.85)";
      for (const sx of [-2, 4, 10]) { ctx.beginPath(); ctx.ellipse(sx, 0, 1.8, 6.2, 0, 0, TAU); ctx.fill(); }
      ctx.fillStyle = "rgba(255,255,255,.38)";
      ctx.beginPath(); ctx.ellipse(-3, -9, 7, 3.4, -0.5, 0, TAU); ctx.fill();
      ctx.beginPath(); ctx.ellipse(-3, 9, 7, 3.4, 0.5, 0, TAU); ctx.fill();
    } else if (BODY_KIND === "beetle") {
      /* 瓢虫：圆鞘翅 + 中线 + 斑点 */
      ctx.ellipse(0, 0, 13, 10, 0, 0, TAU); ctx.fill(); ctx.stroke();
      ctx.shadowBlur = 0;
      ctx.fillStyle = "rgba(30,8,10,.9)";
      ctx.fillRect(-1, -9.5, 2, 19);
      for (const spot of [[-5, -4], [-6, 3], [5, -3], [6, 4], [0, 0]]) {
        ctx.beginPath(); ctx.arc(spot[0], spot[1], 1.9, 0, TAU); ctx.fill();
      }
    } else {
      /* 蜘蛛：长圆躯体 + 背部高光 */
      if (ctx.roundRect) ctx.roundRect(-15, -7.5, 30, 15, 4.5); else ctx.rect(-15, -7.5, 30, 15);
      ctx.fill(); ctx.stroke();
      ctx.shadowBlur = 0;
      ctx.fillStyle = "rgba(255,255,255,.55)";
      ctx.fillRect(-11, -1.8, 7, 3.6);
    }
    const on = ((nowS * 2.5) | 0) % 2 === 0;
    ctx.fillStyle = on ? ACCENT : ACCENT_DIM;
    ctx.beginPath(); ctx.arc(6.5, 0, 2.2, 0, TAU); ctx.fill();
    ctx.restore();
  }
  function draw() {
    ctx.clearRect(0, 0, W, H);
    ctx.save();
    ctx.translate(-window.scrollX, -window.scrollY);
    for (let i = 1; i < trail.length; i++) {
      const a = i / trail.length;
      ctx.strokeStyle = `rgba(96,116,150,${(a * 0.20).toFixed(3)})`;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(trail[i - 1].x, trail[i - 1].y); ctx.lineTo(trail[i].x, trail[i].y); ctx.stroke();
    }
    for (const r of rings) {
      const t = r.t / 0.5;
      ctx.strokeStyle = accentA((1 - t) * 0.5);
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(r.x, r.y, 8 + t * 46, 0, TAU); ctx.stroke();
    }
    for (const l of legs) drawLeg(l);
    drawBody();
    for (const p of particles) {
      const a = p.life / p.max;
      ctx.fillStyle = accentA(a);
      ctx.beginPath(); ctx.arc(p.x, p.y, 1.6, 0, TAU); ctx.fill();
    }
    ctx.restore();
  }

  /* ---------- 主循环：rAF 驱动；被节流/遮挡时由看门狗定时器兜底 ---------- */
  let nowS = 0, prev = 0, measureT = 2.5;
  function tick(ms) {
    const t = ms / 1000;
    /* dt 按真实流逝时间计算（上限 0.2s）：rAF 被节流时（如 headless/遮挡窗口）仍能实时运动 */
    const dt = Math.min(0.2, (t - prev) || 0.016);
    prev = t; nowS = t;
    measureT -= dt;
    if (measureT <= 0) {
      /* 抓取进行中坐标变化更频繁（抽屉滑入/列表加载），加密测量节奏 */
      measureT = (S.fetchRect || S.sweep) ? 1.0 : 2.5;
      measure();
    }
    updateBody(dt);
    updateLegs(dt);
    stepSweep();   // 抵达目标后：剩余词条从宠物位置向外波纹补齐
    trail.push({ x: S.x, y: S.y });
    if (trail.length > 46) trail.shift();
    for (let i = particles.length - 1; i >= 0; i--) { const p = particles[i]; p.life -= dt; if (p.life <= 0) { particles.splice(i, 1); continue; } p.x += p.vx * dt; p.y += p.vy * dt; }
    for (let i = rings.length - 1; i >= 0; i--) { rings[i].t += dt; if (rings[i].t >= 0.5) rings.splice(i, 1); }
    draw();
  }

  /* ---------- 页内互动控件：互动入口从工作台搬进浏览器页面 ---------- */
  const playRoot = shadow.getElementById("wbs-play");
  const playBtn = shadow.getElementById("wbs-play-btn");
  const playMenu = shadow.getElementById("wbs-play-menu");
  let playOpen = false;
  function collapsePlay() { playOpen = false; playMenu.hidden = true; playBtn.classList.remove("wbs-open"); }
  function togglePlay() {
    playOpen = !playOpen;
    playMenu.hidden = !playOpen;
    playBtn.classList.toggle("wbs-open", playOpen);
    if (playOpen) ring(S.x, S.y);
  }
  function playAction(act) {
    if (act === "call") {
      const c = clampTarget(window.scrollX + W * 0.5, window.scrollY + H * 0.5);
      S.tx = c.x; S.ty = c.y; S.targetAt = nowS; S.dashUntil = nowS + 4.5; S.dashBoost = true;
      ring(c.x, c.y);
    } else if (act === "celebrate") {
      api.celebrate();
    } else if (act === "burst") {
      const c = clampTarget(window.scrollX + W * (0.3 + Math.random() * 0.4), window.scrollY + H * (0.3 + Math.random() * 0.35));
      burst(c.x, c.y, 22);
      S.tx = c.x; S.ty = c.y; S.targetAt = nowS; S.dashUntil = nowS + 4.5; S.dashBoost = true;
      ring(c.x, c.y);
    }
    collapsePlay();
  }
  playBtn.addEventListener("click", togglePlay);
  playMenu.addEventListener("click", (e) => {
    const btn = e.target && e.target.closest ? e.target.closest("button[data-act]") : null;
    if (btn) playAction(btn.dataset.act);
  });
  function onDocPointerClose(e) {
    if (!playOpen) return;
    try { if (e && e.composedPath && e.composedPath().indexOf(playRoot) >= 0) return; } catch (err) {}
    collapsePlay();
  }
  function onEscClose(e) { if (e && e.key === "Escape") collapsePlay(); }

  const listeners = [
    ["resize", onResize, undefined],
    ["pointerdown", onPointerDown, undefined],
    ["pointerdown", onDocPointerClose, true],
    ["keydown", onEscClose, undefined]
  ];
  let rafId = 0, lastRaf = 0;
  function loop(ms) { lastRaf = performance.now(); tick(ms); rafId = requestAnimationFrame(loop); }
  function destroy() {
    try { cancelAnimationFrame(rafId); } catch (e) {}
    try { clearInterval(watchdog); } catch (e) {}
    for (const [name, fn, opts] of listeners) removeEventListener(name, fn, opts);
    try { if (useHighlight && window.CSS && CSS.highlights) CSS.highlights.delete("wbs-crawl"); } catch (e) {}
    if (pageSheet) { try { doc.adoptedStyleSheets = doc.adoptedStyleSheets.filter(s => s !== pageSheet); } catch (e) {} }
    if (fallbackMode) {
      try { for (const w of words) if (w.el && w.el.parentNode) w.el.parentNode.replaceChild(doc.createTextNode(w.el.textContent), w.el); } catch (e) {}
    }
    try { root.remove(); } catch (e) {}
    delete window.__wbs;
  }
  const watchdog = setInterval(() => { if (performance.now() - lastRaf > 250) tick(performance.now()); }, 60);

  window.__wbs = api;
  for (const [name, fn, opts] of listeners) addEventListener(name, fn, opts);
  onResize();
  initSpider();
  rafId = requestAnimationFrame(loop);
  if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(() => { try { measure(); } catch (e) {} });
}
""".strip()

SPIDER_COLLECTOR_HARVEST = r"""
([accent, x, y, w, h, max]) => {
  /* 帧内词采器（多帧词采）：与顶层宠物配合的轻量采集端——只负责本帧文档的
     词条扫描、指向性高亮与计数；不建画布、不做动画，粒子/涟漪等视觉特效
     全部由顶层宠物绘制。跨域帧经 Playwright(CDP) evaluate 注入，不受同源
     策略限制。坐标均为本帧视口坐标（worker 侧负责主视口 → 帧视口换算）。
     注入即常驻（window.__wbs_collector），后续调用只按 DOM 签名增量重扫。
     注意：本函数参数列表是解构形式（非简单参数列表），不能写 "use strict" 指令。 */
  if (!window.__wbs_collector) {
    const doc = document;
    const useHighlight = typeof Highlight !== "undefined" && !!(window.CSS && CSS.highlights);
    const fallbackMode = !useHighlight;
    const highlightObj = useHighlight ? new Highlight() : null;
    const ACCENT = String(accent || "#54d7e8");
    let pageSheet = null;
    try {
      pageSheet = new CSSStyleSheet();
      pageSheet.replaceSync(useHighlight
        ? `::highlight(wbs-crawl){background-color:${ACCENT};color:#04161c}`
        : `.wbs-word{border-radius:1px}.wbs-word.wbs-sel{background:${ACCENT};color:#04161c}`);
      doc.adoptedStyleSheets = doc.adoptedStyleSheets.concat([pageSheet]);
      if (useHighlight) CSS.highlights.set("wbs-crawl", highlightObj);
    } catch (e) { pageSheet = null; }
    const SKIP = "script,style,noscript,template,iframe,svg,canvas,textarea,select,option,input,[contenteditable='true']";
    const WORD_LIMIT = 2500;
    let words = [];
    const domSignature = () => (doc.body ? doc.body.textContent.length * 31 + doc.body.querySelectorAll("*").length : 0);

    function collectWords() {
      if (fallbackMode) {
        for (const word of words) {
          if (word.el && word.el.parentNode) {
            try { word.el.parentNode.replaceChild(doc.createTextNode(word.el.textContent), word.el); } catch (e) {}
          }
        }
      } else if (highlightObj) {
        try { highlightObj.clear(); CSS.highlights.set("wbs-crawl", highlightObj); } catch (e) {}
      }
      words = [];
      if (!doc.body) return;
      if (fallbackMode) {
        const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT, null);
        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        outer:
        for (const node of nodes) {
          const p = node.parentElement;
          if (!p || p.closest(SKIP)) continue;
          const text = node.nodeValue;
          if (!text || !text.trim()) continue;
          const frag = doc.createDocumentFragment();
          for (const part of text.split(/(\s+)/)) {
            if (!part) continue;
            if (/^\s+$/.test(part)) { frag.appendChild(doc.createTextNode(part)); continue; }
            if (words.length >= WORD_LIMIT) break outer;
            const span = doc.createElement("span");
            span.className = "wbs-word";
            span.textContent = part;
            words.push({ el: span, range: null, sel: false });
            frag.appendChild(span);
          }
          node.parentNode.replaceChild(frag, node);
        }
      } else {
        const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT, {
          acceptNode(node) {
            if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
            const p = node.parentElement;
            if (!p || p.closest(SKIP)) return NodeFilter.FILTER_REJECT;
            return NodeFilter.FILTER_ACCEPT;
          }
        });
        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        outer:
        for (const node of nodes) {
          const text = node.nodeValue;
          const re = /\S+/g;
          let m;
          while ((m = re.exec(text))) {
            if (words.length >= WORD_LIMIT) break outer;
            const range = doc.createRange();
            range.setStart(node, m.index);
            range.setEnd(node, m.index + m[0].length);
            words.push({ el: null, range, sel: false });
          }
        }
      }
    }

    window.__wbs_collector = {
      version: 1,
      words: () => words.length,
      /* DOM 变化（SPA 重渲染）时重扫词条；无变化时零开销 */
      sync() {
        const sig = domSignature();
        if (sig === window.__wbs_collector._sig) return;
        window.__wbs_collector._sig = sig;
        collectWords();
      },
      /* 原地采集矩形内词条并高亮；返回采集数量（坐标为本帧视口坐标） */
      harvestRect(rx, ry, rw, rh, cap) {
        const x0 = Number(rx) || 0, y0 = Number(ry) || 0;
        const x1 = x0 + (Number(rw) || 0), y1 = y0 + (Number(rh) || 0);
        const limit = Math.max(1, Math.min(Number(cap) || 80, 200));
        let n = 0;
        for (const word of words) {
          if (n >= limit) break;
          if (word.sel) continue;
          let r;
          try { r = (word.el || word.range).getBoundingClientRect(); } catch (e) { continue; }
          if (r.width <= 0 || r.height <= 0) continue;
          if (r.right < x0 || r.left > x1 || r.bottom < y0 || r.top > y1) continue;
          word.sel = true; n++;
          if (word.el) { try { word.el.classList.add("wbs-sel"); } catch (e) {} }
          else if (useHighlight && highlightObj) {
            try { highlightObj.add(word.range); CSS.highlights.set("wbs-crawl", highlightObj); } catch (e) {}
          }
        }
        return n;
      },
      destroy() {
        try { if (useHighlight && window.CSS && CSS.highlights) CSS.highlights.delete("wbs-crawl"); } catch (e) {}
        if (pageSheet) { try { doc.adoptedStyleSheets = doc.adoptedStyleSheets.filter(s => s !== pageSheet); } catch (e) {} }
        if (fallbackMode) {
          for (const word of words) {
            if (word.el && word.el.parentNode) {
              try { word.el.parentNode.replaceChild(doc.createTextNode(word.el.textContent), word.el); } catch (e) {}
            }
          }
        }
        delete window.__wbs_collector;
      }
    };
    window.__wbs_collector._sig = 0;
    window.__wbs_collector.sync();
  }
  try {
    window.__wbs_collector.sync();
    return window.__wbs_collector.harvestRect(x, y, w, h, max);
  } catch (e) { return -1; }
}
""".strip()

SPIDER_COLLECTOR_DESTROY = r"""
() => { try { if (window.__wbs_collector) window.__wbs_collector.destroy(); } catch (e) {} }
""".strip()
