/* 带读面板：作答框在题内、页签模式、题面走 textContent，预填只填空栏。 */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(path.resolve(__dirname, "..", ".."), "web", "js", "app.js"),
  "utf8");

let fails = 0;
const check = (c, l, e = "") => { console.log(`  [${c ? "OK " : "!! "}] ${l} ${e}`); if (!c) fails++; };

function mkEl(tag) {
  const e = {
    tagName: String(tag || "div").toUpperCase(), children: [], attrs: {}, style: {},
    _text: "", _html: "", parent: null, className: "", type: "", disabled: false,
    hidden: false, dataset: {}, maxLength: 0, value: "", _on: {},
    set textContent(v) { this._text = String(v); this.children = []; },
    get textContent() { return this._text + (this._html || "") + this.children.map((c) => c.textContent).join(""); },
    set innerHTML(v) { this._html = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    addEventListener(n, fn) { this._on[n] = fn; },
    querySelector(sel) {
      return walk(this).find((c) => matchSel(c, sel)) || null;
    },
    querySelectorAll(sel) { return walk(this).filter((c) => matchSel(c, sel)); },
  };
  return e;
}
function matchSel(c, sel) {
  if (sel === "textarea" || sel === "TEXTAREA") return c.tagName === "TEXTAREA";
  if (sel.startsWith(".")) return (c.className || "").split(/\s+/).includes(sel.slice(1));
  return c.tagName === String(sel).toUpperCase();
}
const walk = (r, o = []) => { o.push(r); (r.children || []).forEach((c) => walk(c, o)); return o; };
const byClass = (r, cls) => walk(r).filter((c) => (c.className || "").split(/\s+/).includes(cls));

function extract(name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(src);
  if (!m) throw new Error("找不到函数: " + name);
  let d = 0, j = m.index + m[0].length - 1;
  for (; j < src.length; j++) {
    if (src[j] === "(") d++;
    else if (src[j] === ")") { d--; if (d === 0) { j++; break; } }
  }
  const i = src.indexOf("{", j);
  d = 0;
  for (j = i; j < src.length; j++) {
    if (src[j] === "{") d++;
    else if (src[j] === "}") { d--; if (d === 0) break; }
  }
  return src.slice(m.index, j + 1);
}

const el = (tag, cls, html) => {
  const e = mkEl(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = String(html);
  return e;
};
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const toast = () => {};
const S = { uid: "u1" };
const store = {};
const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };
let posted = null;
let currentSess = null;
function api(method, url, body) {
  posted = { method, url, body };
  return Promise.resolve({ session: currentSess, verdict: { pass: false, state: "fail" } });
}

eval(extract("applyTutorPrefill"));
eval(extract("pickBenchMode"));
eval(extract("tutorPanel"));

const fields = { claim: "已有主张", method: "" };
const alts = applyTutorPrefill(fields, { claim: "带读里的主张", method: "他们怎么做", change: "改一处" });
check(fields.claim === "已有主张", "onPrefill 不覆盖已填栏");
check(fields.method === "他们怎么做" && fields.change === "改一处", "onPrefill 只填空栏");
check(alts.length === 1 && alts[0].key === "claim", "已填栏列出可替换句");

const cases = [
  [{ saved: "tutor" }, "tutor"],
  [{ saved: "card" }, "card"],
  [{ session: { status: "active" } }, "tutor"],
  [{ hasDraft: true }, "card"],
  [{ canGrade: false }, "card"],
  [{ inKit: false }, "card"],
];
cases.forEach(([input, want], i) => {
  const got = pickBenchMode({ saved: "", session: null, hasDraft: false, hasCard: false, passedCount: 0, canGrade: true, inKit: true, ...input });
  check(got === want, `pickBenchMode ${i + 1} → ${want}`, got);
});

function slotOf(type, extra) {
  return {
    qid: "q_" + type, station: "abstract", type, field: "claim",
    question: "<b>leak</b>", hint: "提示", voice: "rules", attempts: 0, status: "open",
    answer: "", last: extra && extra.last || "", note: "", ...extra,
  };
}

function makeSess(slot, extra) {
  return {
    status: "active", rev: 1, voice: "rules", notice: "",
    cur: 0, total: 8, slots: [slot],
    stations: [{ key: "abstract", label: "摘要", heading: "Abstract", start: 0 }],
    ...extra,
  };
}

["find_quote", "own", "number", "limitation"].forEach((typ) => {
  const box = tutorPanel({
    aid: "2009.03300", kitId: "llm-eval",
    scrollTo() {}, selection() { return "sel"; },
    onPrefill() { return []; },
    session: makeSess(slotOf(typ)),
  });
  const ta = walk(box).find((c) => c.tagName === "TEXTAREA");
  check(!!ta && (ta.className || "").includes("tutor-answer"), `${typ} 有 textarea.tutor-answer`);
  check(ta && ta.maxLength === 600, `${typ} maxLength=600`);
  check(ta && ta.parent === box, `${typ} textarea 在 box 内`);
});

currentSess = makeSess(slotOf("own", { last: "上次作答还在" }));
const box2 = tutorPanel({
  aid: "2009.03300", kitId: "llm-eval",
  scrollTo() {}, selection() { return ""; },
  onPrefill() { return []; },
  session: currentSess,
});
const ta2 = walk(box2).find((c) => c.tagName === "TEXTAREA");
check(ta2 && ta2.value === "上次作答还在", "fail 后重画保留 last");
const send = walk(box2).find((c) => c.tagName === "BUTTON" && c.textContent === "作答" && typeof c.onclick === "function");
check(!!send, "有作答按钮");
if (send) {
  ta2.value = "手写作答内容";
  posted = null;
  send.onclick();
}
check(posted && posted.body && posted.body.answer === "手写作答内容", "作答提交 textarea 当前值");

currentSess = makeSess(slotOf("own", { status: "pending", last: "待判" }));
const box3 = tutorPanel({
  aid: "2009.03300", kitId: "llm-eval",
  scrollTo() {}, selection() { return ""; },
  onPrefill() { return []; },
  session: currentSess,
});
const rg = walk(box3).find((c) => c.textContent === "重判");
check(!!rg, "pending 渲染重判");

const q = byClass(box2, "tutor-q")[0];
check(q && q.textContent === "<b>leak</b>", "题面走 textContent");
check(q && q.innerHTML === "", "题面不进 innerHTML");

function paperHarness(cfg = {}) {
  const made = [];
  const raf = [];
  const flush = () => { for (let k = 0; raf.length && k < 100; k++) raf.shift()(); };
  const fire = (n, ev) => (n._ls[ev] || []).slice().forEach((fn) => fn({ type: ev }));
  const listen = {
    addEventListener(n, fn) { (this._ls[n] = this._ls[n] || []).push(fn); },
    removeEventListener(n, fn) { const a = this._ls[n] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); },
  };
  const scrolls = { win: [], pane: [] };
  const nav = { sticky: false, leftSticky: false };
  const window = {
    scrollY: 0, innerWidth: 390, _ls: {}, ...listen,
    scrollTo(o) { scrolls.win.push(o.top); this.scrollY = Math.max(0, o.top); },
    requestAnimationFrame(cb) { raf.push(cb); return raf.length; },
    matchMedia(q) { const m = /min-width:\s*(\d+)px/.exec(q); return { matches: m ? this.innerWidth >= Number(m[1]) : false }; },
    getSelection() { return ""; },
  };
  const isPane = (n) => (n.className || "").split(/\s+/).includes("paper-text");
  let appTop = null;
  let sideNav = null;
  const rectOf = (n) => {
    if (n === appTop) return { top: 0, bottom: 64 };
    if (n === sideNav) return { top: 0, bottom: 800 };
    if (isPane(n)) return { top: 240 - window.scrollY, bottom: 240 - window.scrollY + n.clientHeight };
    let p = n.parent;
    while (p && !isPane(p)) p = p.parent;
    if (!p) return { top: 0, bottom: 0 };
    const top = 240 - window.scrollY + n.offsetTop - p.scrollTop;
    return { top, bottom: top + 20 };
  };
  const sel = (c, q) => (q.startsWith(".") ? (c.className || "").split(/\s+/).includes(q.slice(1)) : c.tagName === q.toUpperCase());
  let $app = null;
  function mk(tag) {
    const e = {
      tagName: String(tag || "div").toUpperCase(), children: [], parent: null, className: "", dataset: {}, style: {},
      _text: "", _html: "", _ls: {}, hidden: false, disabled: false, open: false,
      offsetTop: 0, scrollTop: 0, clientHeight: 0, scrollHeight: 0, ...listen,
      set textContent(v) { this._text = String(v); this._html = ""; this.children = []; },
      get textContent() { return this._text + (this._html || "") + this.children.map((c) => c.textContent).join(""); },
      set innerHTML(v) { this._html = String(v); this._text = ""; this.children = []; },
      get innerHTML() { return this._html; },
      get classList() {
        const self = this;
        const list = () => (self.className || "").split(/\s+/).filter(Boolean);
        return {
          add(c) { if (!list().includes(c)) self.className = [...list(), c].join(" "); },
          remove(c) { self.className = list().filter((x) => x !== c).join(" "); },
          toggle(c, on) { if (on === undefined) on = !list().includes(c); if (on) this.add(c); else this.remove(c); },
          contains(c) { return list().includes(c); },
        };
      },
      appendChild(c) { if (c.parent) c.parent.children = c.parent.children.filter((x) => x !== c); c.parent = this; this.children.push(c); return c; },
      append(...cs) { cs.forEach((c) => this.appendChild(c)); },
      remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this); this.parent = null; },
      querySelector(q) { return walk(this).find((c) => sel(c, q)) || null; },
      querySelectorAll(q) { return walk(this).filter((c) => sel(c, q)); },
      contains(n) { return walk(this).includes(n); },
      get isConnected() {
        let n = this;
        while (n.parent) { if (!n.parent.children.includes(n)) return false; n = n.parent; }
        return n === $app;
      },
      getBoundingClientRect() { return rectOf(this); },
      scrollTo(o) { if (isPane(this)) scrolls.pane.push(o.top); this.scrollTop = Math.max(0, Math.min(o.top, this.scrollHeight - this.clientHeight)); },
      scrollIntoView() {},
    };
    made.push(e);
    return e;
  }
  const el = (tag, cls, html) => {
    const e = mk(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = String(html);
    return e;
  };
  $app = mk("main");
  appTop = el("header", "app-top");
  sideNav = el("nav", "side-nav");
  const document = {
    querySelectorAll(q) { const qs = q.split(","); return [...(qs.includes(".side-nav") ? [sideNav] : []), ...(qs.includes(".app-top") ? [appTop] : [])]; },
    createTextNode(t) { const n = mk("#text"); n._text = String(t); return n; },
  };
  const getComputedStyle = (n) => ({ position: (n === appTop || (n === sideNav && nav.sticky) || (nav.leftSticky && sel(n, ".bench-paper"))) ? "sticky" : "static" });
  const store = {};
  const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };
  const setView = () => {};
  const S = { uid: "u1", view: "card", cardId: "2009.03300", cardKit: "", cardFrom: "", renderSeq: 1 };
  const lines = cfg.lines || ["Alice Bob", "Introduction", "intro body", "Method", "method body", "Experiments", "exp body", "Conclusion", "end body"];
  const labels = cfg.labels || { Introduction: "引言", Method: "方法", Experiments: "实验", Conclusion: "结论" };
  const sections = [];
  let at = 0;
  lines.forEach((ln) => { if (labels[ln]) sections.push({ at, label: labels[ln] }); at += ln.length + 1; });
  const methodAt = sections.find((x) => x.label === "方法").at;
  const paper = { title: "T", title_zh: "", text: lines.join("\n"), sections, tables: [], source: cfg.source || "html", url: "https://arxiv.org/abs/2009.03300", keywords: [] };
  const sess = {
    status: "active", rev: 1, voice: "rules", notice: "", cur: 0, total: 1,
    slots: [{ qid: "q1", station: "method", type: "own", question: "q", hint: "", attempts: 0, status: "open", answer: "", last: "", note: "" }],
    stations: [{ key: "method", label: "方法", heading: "Method", start: methodAt }],
  };
  const tutorReply = { session: sess, can_grade: true, in_kit: true };
  const api = (method, url) => {
    if (url.startsWith("/api/papers/")) return Promise.resolve(paper);
    if (url.startsWith("/api/tutor/")) return cfg.tutor ? cfg.tutor() : Promise.resolve(tutorReply);
    return Promise.resolve({});
  };
  const renderToday = async () => { $app.innerHTML = ""; };
  let stopPaperSpy = null;
  let paperRenderEpoch = 0;
  eval(extract("stale"));
  eval(extract("splitZhTitle"));
  eval(extract("tutorPanel"));
  eval(extract("render"));
  const at0 = src.search(/async\s+function\s+renderCard\s*\(/);
  eval(src.slice(at0, src.indexOf("\n}\n", at0) + 2));

  const live = () => {
    const pane = walk($app).find(isPane);
    const toolBar = walk($app).find((c) => sel(c, ".paper-tools"));
    const chips = walk($app).filter((c) => sel(c, ".sec-chip"));
    const node = (t) => pane.children.find((c) => c.tagName === "H4" && c.textContent.trim() === t);
    const stationBtn = walk($app).find((c) => c.tagName === "BUTTON" && c.textContent === "看原文这一节");
    return { pane, toolBar, chips, node, stationBtn };
  };
  return { made, raf, flush, fire, window, sel, $app, S, render, renderCard, live, scrolls, nav, tutorReply, getStopPaperSpy: () => stopPaperSpy };
}

async function testPaperScrollTargets() {
  const { made, flush, fire, window, S, render, renderCard, live } = paperHarness();
  const layout = { Introduction: 100, Method: 480, Experiments: 800, Conclusion: 1300 };
  const mount = async () => {
    await renderCard();
    const v = live();
    v.pane.clientHeight = 1680;
    v.pane.scrollHeight = 1680;
    Object.keys(layout).forEach((t) => { v.node(t).offsetTop = layout[t]; });
    flush();
    return v;
  };
  const entries = (v) => [
    ["章节「方法」", () => v.chips.find((c) => c.textContent === "方法").onclick()],
    ["带读「看原文这一节」", () => v.stationBtn.onclick()],
  ];

  let v = null;
  for (const w of [390, 920]) {
    window.innerWidth = w;
    window.scrollY = 0;
    v = await mount();
    check(!!v.stationBtn, `${w}px 带读有「看原文这一节」`);
    for (const [name, act] of entries(v)) {
      window.scrollY = 0;
      v.pane.scrollTop = 0;
      act();
      check(window.scrollY === 648, `${w}px ${name} 整页滚到 648`, String(window.scrollY));
      check(v.pane.scrollTop === 0, `${w}px ${name} 原文框不动`, String(v.pane.scrollTop));
      const top = v.node("Method").getBoundingClientRect().top;
      check(top === 72, `${w}px ${name} 目标落在视口 72`, String(top));
    }
  }
  window.scrollY = 1000;
  fire(window, "scroll");
  flush();
  const lit = v.chips.filter((c) => c.classList.contains("on"));
  check(lit.length === 1 && lit[0].textContent === "实验", "整页滚到 1000 只亮「实验」", lit.map((c) => c.textContent).join(","));

  window.innerWidth = 921;
  v.pane.clientHeight = 654;
  for (const [name, act] of entries(v)) {
    window.scrollY = 1000;
    v.pane.scrollTop = 0;
    act();
    check(v.pane.scrollTop === 472, `921px ${name} 原文框滚到 472`, String(v.pane.scrollTop));
    check(window.scrollY === 1000, `921px ${name} 整页不动`, String(window.scrollY));
  }

  [920, 921, 920].forEach((w) => { window.innerWidth = w; fire(window, "resize"); flush(); });
  v.toolBar.open = !v.toolBar.open;
  fire(v.toolBar, "toggle");
  flush();
  S.view = "today";
  await render();
  S.view = "card";
  await mount();
  await mount();
  const count = (n, ev) => (n._ls[ev] || []).length;
  const sum = (ev) => made.reduce((a, n) => a + count(n, ev), 0);
  check(count(window, "scroll") === 1, "window scroll 监听只剩 1 个", String(count(window, "scroll")));
  check(count(window, "resize") === 1, "window resize 监听只剩 1 个", String(count(window, "resize")));
  check(sum("scroll") === 1, "原文框 scroll 监听只剩 1 个", String(sum("scroll")));
  check(sum("toggle") === 1, "工具栏 toggle 监听只剩 1 个", String(sum("toggle")));
  check(sum("resize") === 0, "元素上没有 resize 监听", String(sum("resize")));
}

async function testPaperSpyRetryRace() {
  const tick = () => new Promise((r) => setImmediate(r));
  const pending = [];
  const h = paperHarness({ source: "abstract", tutor: () => new Promise((r) => pending.push(r)) });
  const { raf, window, $app, S, render, renderCard, live } = h;
  const count = (n, ev) => ((n && n._ls[ev]) || []).length;
  for (const order of ["旧先", "新先"]) {
    S.view = "card";
    pending.length = 0;
    raf.length = 0;
    const first = renderCard();
    await tick();
    const retry = walk($app).find((c) => c.tagName === "BUTTON" && c.textContent === "重新取正文");
    check(!!retry && retry.isConnected && pending.length === 1, `${order} 第一次带读未返回时「重新取正文」在页上`);
    const second = retry.onclick();
    await tick();
    check(pending.length === 2, `${order} 重取发出第二次带读请求`, String(pending.length));
    for (const i of (order === "旧先" ? [0, 1] : [1, 0])) { pending[i](h.tutorReply); await tick(); }
    await Promise.all([first, second]);
    const v = live();
    const benches = walk($app).filter((c) => h.sel(c, ".bench"));
    check(benches.length === 1, `${order} 页上只有 1 个 bench`, String(benches.length));
    check(count(window, "scroll") === 1, `${order} window scroll 监听 1 个`, String(count(window, "scroll")));
    check(count(window, "resize") === 1, `${order} window resize 监听 1 个`, String(count(window, "resize")));
    check(count(v.pane, "scroll") === 1, `${order} 原文框 scroll 监听 1 个`, String(count(v.pane, "scroll")));
    check(count(v.toolBar, "toggle") === 1, `${order} 工具栏 toggle 监听 1 个`, String(count(v.toolBar, "toggle")));
    S.view = "today";
    await render();
    check(count(window, "scroll") === 0, `${order} 离页后 window scroll 监听 0 个`, String(count(window, "scroll")));
    check(count(window, "resize") === 0, `${order} 离页后 window resize 监听 0 个`, String(count(window, "resize")));
    check(count(v.pane, "scroll") === 0, `${order} 离页后原文框 scroll 监听 0 个`, String(count(v.pane, "scroll")));
    check(count(v.toolBar, "toggle") === 0, `${order} 离页后工具栏 toggle 监听 0 个`, String(count(v.toolBar, "toggle")));
    check(h.getStopPaperSpy() === null, `${order} 离页后 stopPaperSpy 为 null`);
  }

  S.view = "card";
  pending.length = 0;
  raf.length = 0;
  const p = renderCard();
  await tick();
  pending.forEach((r) => r(h.tutorReply));
  await p;
  const v = live();
  let spyRuns = 0;
  v.pane.children.filter((c) => c.tagName === "H4").forEach((n) => {
    const g = n.getBoundingClientRect;
    n.getBoundingClientRect = () => { spyRuns += 1; return g.call(n); };
  });
  check(raf.length === 1, "初次判定排了第一帧", String(raf.length));
  raf.shift()();
  S.view = "today";
  await render();
  let err = null;
  try { if (raf.length) raf.shift()(); } catch (e) { err = e; }
  check(spyRuns === 0, "离页后第二帧不再跑旧 spy", String(spyRuns));
  check(err === null, "第二帧不抛错", String(err));
}

async function testShortDesktopPaperSpy() {
  const lines = ["Abstract", "abs body", "Method", "method body", "Conclusion", "end body"];
  const labels = { Abstract: "摘要", Method: "方法", Conclusion: "结论" };
  const layout = { Abstract: 0, Method: 84, Conclusion: 168 };
  const { window, S, renderCard, live, flush, scrolls, nav } = paperHarness({ lines, labels });
  nav.leftSticky = true;
  for (const w of [921, 1440]) {
    for (const expanded of [true, false]) {
      const tag = `${w}px ${expanded ? "侧栏展开" : "侧栏收起"}`;
      window.innerWidth = w;
      window.scrollY = 0;
      nav.sticky = expanded;
      S.view = "card";
      await renderCard();
      const v = live();
      v.pane.clientHeight = 252;
      v.pane.scrollHeight = 252;
      Object.keys(layout).forEach((t) => { v.node(t).offsetTop = layout[t]; });
      scrolls.win.length = 0;
      scrolls.pane.length = 0;
      flush();
      const tops = Object.keys(layout).map((t) => v.node(t).getBoundingClientRect().top);
      check(tops.join(",") === "240,324,408", `${tag} 章节坐标`, tops.join(","));
      const lit = v.chips.filter((c) => c.classList.contains("on"));
      check(lit.length === 1 && lit[0].textContent === "摘要", `${tag} 初次只亮「摘要」`, lit.map((c) => c.textContent).join(","));
      check(!!v.stationBtn, `${tag} 带读有「看原文这一节」`);
      v.chips.find((c) => c.textContent === "方法").onclick();
      if (v.stationBtn) v.stationBtn.onclick();
      check(scrolls.pane.length === 2 && scrolls.pane.every((t) => t === 76), `${tag} 两处跳转都按原文框滚到 76`, JSON.stringify(scrolls.pane));
      check(scrolls.win.length === 0, `${tag} 从不滚整页`, JSON.stringify(scrolls.win));
    }
  }
}

async function testTutorDraftPersistsAcrossRerender() {
  const typed = "这一节想解决现有基准太窄的问题";
  const key = "rg_tutor_u1_k1_2009.03300";
  const quoteSlot = { qid: "q_claim_quote", type: "find_quote", status: "pass", last: "x" };
  const baseSess = () => ({ status: "active", cur: 1, total: 8, slots: [{ ...quoteSlot }, { qid: "q_problem", type: "own", status: "open" }] });
  const build = (aid) => tutorPanel({
    aid, kitId: "k1",
    scrollTo() {}, selection() { return ""; },
    onPrefill() { return []; },
    session: baseSess(),
  });
  const taOf = (b) => walk(b).find((c) => c.tagName === "TEXTAREA");
  const sendOf = (b) => walk(b).find((c) => c.tagName === "BUTTON" && c.textContent === "作答" && typeof c.onclick === "function");
  Object.keys(store).forEach((k) => { delete store[k]; });

  const b1 = build("2009.03300");
  const ta1 = taOf(b1);
  ta1.value = typed;
  ta1._on.input();
  let saved = null;
  try { saved = JSON.parse(store[key] || "null"); } catch (_) { saved = null; }
  check(!!saved && saved.q_problem === typed && Object.keys(saved).length === 1, "作答草稿存进 rg_tutor_<uid>_k1_2009.03300", String(store[key]));
  const dn = byClass(b1, "tutor-draft-note")[0];
  check(!!dn && dn.hidden === false, "草稿提示可见");

  const b2 = build("2009.03300");
  check(taOf(b2) && taOf(b2).value === typed, "重建面板后作答框还原草稿", taOf(b2) && taOf(b2).value);
  const b3 = build("2307.03172");
  check(taOf(b3) && taOf(b3).value === "", "别的论文作答框是空的", taOf(b3) && taOf(b3).value);

  const passSess = () => ({ ...baseSess(), cur: 2, slots: [{ ...quoteSlot }, { qid: "q_problem", status: "pass", last: typed }] });
  store[key] = JSON.stringify({ q_problem: typed, q_other: "别的草稿" });
  currentSess = passSess();
  await sendOf(build("2009.03300")).onclick();
  saved = JSON.parse(store[key] || "null");
  check(!!saved && !("q_problem" in saved) && saved.q_other === "别的草稿", "交卷过线后删掉这题草稿、留别的", String(store[key]));

  store[key] = JSON.stringify({ q_problem: typed });
  currentSess = passSess();
  await sendOf(build("2009.03300")).onclick();
  check(!(key in store), "没有别的草稿时整个键删掉", String(store[key]));

  store[key] = JSON.stringify({ q_problem: typed });
  currentSess = { ...baseSess(), slots: [{ ...quoteSlot }, { qid: "q_problem", type: "own", status: "retry", last: typed }] };
  await sendOf(build("2009.03300")).onclick();
  saved = JSON.parse(store[key] || "null");
  check(!!saved && saved.q_problem === typed, "判 retry 时草稿留着", String(store[key]));
}

async function testResumeTutorWithoutModel() {
  const slots = [0, 1, 2, 3, 4, 5, 6, 7].map((i) => ({ qid: "q" + i, station: "method", type: "own", question: "q" + i, status: i < 4 ? "pass" : "open", last: "" }));
  slots[4] = { qid: "q_evidence", station: "method", type: "number", question: "找数字句", status: "pending", last: "数字句", note: "模型暂时用不了：作答先存着，稍后可重判" };
  const sess = { status: "active", rev: 1, voice: "rules", notice: "", cur: 4, total: 8, slots, stations: [] };
  const run = async (reply) => {
    const h = paperHarness({ tutor: () => Promise.resolve(reply) });
    await h.renderCard();
    const btn = walk(h.$app).find((c) => c.tagName === "BUTTON" && c.textContent === "带读答题");
    const bench = walk(h.$app).find((c) => h.sel(c, ".bench"));
    const panel = walk(h.$app).find((c) => h.sel(c, ".tutor-panel"));
    return { btn, bench, panel };
  };

  let v = await run({ session: sess, can_grade: false, in_kit: true });
  check(!!v.btn && v.btn.disabled === false, "有会话没模型：「带读答题」可点");
  check(!!v.bench && v.bench.dataset.mode === "tutor", "有会话没模型：进带读", v.bench && v.bench.dataset.mode);
  const prog = v.panel && walk(v.panel).find((c) => (c.className || "").split(/\s+/).includes("tutor-prog"));
  check(!!prog && prog.textContent === "第 5 题 / 共 8 题", "有会话没模型：进度 第 5 题 / 共 8 题", prog && prog.textContent);
  const notice = v.panel && walk(v.panel).find((c) => (c.className || "").split(/\s+/).includes("tutor-notice"));
  check(!!notice && notice.textContent === "模型暂时用不了：可以看题、改答案、存草稿；现在作答会先存着，模型恢复后点「重判」。", "有会话没模型：显示模型不可用提示", notice && notice.textContent);
  check(!!v.panel && !walk(v.panel).some((c) => c.tagName === "BUTTON" && c.textContent === "现在重判"), "有会话没模型：不给「现在重判」");
  check(!!v.panel && walk(v.panel).some((c) => (c.className || "").split(/\s+/).includes("tutor-pending") && /已保存，等模型恢复后判分/.test(c.textContent)), "有会话没模型：待判行写「已保存，等模型恢复后判分」");

  v = await run({ session: null, can_grade: false, in_kit: true });
  check(!!v.btn && v.btn.disabled === true, "没会话没模型：「带读答题」禁用");
  check(!!v.btn && v.btn.title === "现在用不了模型（没配 / 没同意隐私说明 / 今天额度用完），先写阅读卡", "没会话没模型：原提示", v.btn && v.btn.title);
  check(!!v.bench && v.bench.dataset.mode === "card", "没会话没模型：进阅读卡", v.bench && v.bench.dataset.mode);

  v = await run({ session: null, can_grade: true, in_kit: true });
  check(!!v.btn && v.btn.disabled === false, "没会话有模型：「带读答题」可点");
}

async function testLastVerdictStrip() {
  const quoteSlot = { qid: "q_claim_quote", type: "find_quote", question: "引一句", status: "pass", last: "x" };
  const baseSess = () => ({ status: "active", rev: 1, cur: 1, total: 8, stations: [], slots: [{ ...quoteSlot }, { qid: "q_problem", type: "own", question: "要解决什么问题", status: "open" }, { qid: "q_next", type: "own", question: "下一题", status: "open" }] });
  const build = () => tutorPanel({
    aid: "2009.03300", kitId: "k_last",
    scrollTo() {}, selection() { return ""; },
    onPrefill() { return []; },
    session: baseSess(),
  });
  const realApi = api;
  const answer = async (reply) => {
    api = () => Promise.resolve(reply);
    const b = build();
    const ta = walk(b).find((c) => c.tagName === "TEXTAREA");
    ta.value = "我的作答";
    const sendBtn = walk(b).find((c) => c.tagName === "BUTTON" && c.textContent === "作答" && typeof c.onclick === "function");
    await sendBtn.onclick();
    api = realApi;
    return b;
  };
  const stripOf = (b) => byClass(b, "tutor-last")[0];
  const btnOf = (r, t) => r && walk(r).find((c) => c.tagName === "BUTTON" && c.textContent === t);

  const passed = baseSess();
  passed.cur = 2;
  passed.slots[1] = { ...passed.slots[1], status: "pass", last: "我的作答", feedback: "引得准" };
  const b1 = await answer({ verdict: { pass: true, state: "pass", feedback: "引得准" }, session: passed });
  const strip = stripOf(b1);
  const sp = strip && walk(strip).find((c) => c.tagName === "P");
  check(!!sp && sp.textContent === "上一题 ✓ 通过：引得准", "交题后顶部显示「上一题 ✓ 通过：引得准」", sp && sp.textContent);
  check(!!btnOf(strip, "看判语") && !!btnOf(strip, "知道了"), "上一题条有「看判语」「知道了」");
  const prog = byClass(b1, "tutor-prog")[0];
  check(!!prog && prog.textContent === "第 3 题 / 共 8 题", "通过后进度 第 3 题 / 共 8 题", prog && prog.textContent);
  btnOf(strip, "看判语").onclick();
  check(!stripOf(b1), "点「看判语」后上一题条消失");
  const fb = byClass(b1, "tutor-verdict")[0];
  check(!!fb && fb.textContent === "引得准", "点「看判语」后显示上一题判语", fb && fb.textContent);

  const pend = baseSess();
  pend.cur = 2;
  pend.slots[1] = { ...pend.slots[1], status: "pending", last: "我的作答", note: "模型暂时用不了：作答先存着，稍后可重判" };
  const b2 = await answer({ verdict: { pass: false, state: "pending", note: "模型暂时用不了：作答先存着，稍后可重判" }, session: pend });
  const strip2 = stripOf(b2);
  const sp2 = strip2 && walk(strip2).find((c) => c.tagName === "P");
  check(!!sp2 && sp2.textContent === "上一题已保存，等模型恢复后判分", "待判时顶部显示「上一题已保存，等模型恢复后判分」", sp2 && sp2.textContent);
}

testPaperScrollTargets().then(testPaperSpyRetryRace).then(testShortDesktopPaperSpy).then(testTutorDraftPersistsAcrossRerender).then(testResumeTutorWithoutModel).then(testLastVerdictStrip).then(() => {
  if (fails) {
    console.log("失败 " + fails + " 项");
    process.exit(1);
  }
  console.log("全部通过");
}, (e) => { console.log(e); process.exit(1); });
