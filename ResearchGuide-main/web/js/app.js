/* 启研 · 前端（无构建、经典脚本；设计规范 docs/DESIGN_SPEC.md）
   对话页的结构约定见 审计与方案/09-对话前端框架.md —— 改样式前先读那一篇，
   里面写清了哪些是「结构契约」（不能动）、哪些是「视觉表现」（随便换）。 */
"use strict";

/* ---------- 状态 ---------- */

const S = {
  uid: localStorage.getItem("rg_uid") || "",
  token: localStorage.getItem("rg_token") || "",
  nickname: localStorage.getItem("rg_nick") || "",
  wechat: false,
  guest: true,
  username: "",
  hasLlmKey: false,
  llmKeyTail: "",
  llmKeyInvalid: false,
  auth: null,
  view: "home",
  entered: false,
  lastTask: null,
  lastFeedback: null,
  newFactIds: [],
  cardsPane: "direction",
  kitId: localStorage.getItem("rg_kit") || "",
  cardKit: "",
  readTab: "kit",
  cardId: "",
  posTab: "edges",
  posDraft: null,
  mapSort: "order",
  mePortrait: "",
  portraitId: "",
  portraitName: "",
};

/* ---------- 进 HTML 之前先消毒 ----------
   页面里大约一百六十处把字符串当 HTML 塞进去（innerHTML、el() 的第三个参数）。里面有模型写的理由、
   学生粘的成绩单课名、外面抓来的论文标题。Codex 用一个 <img onerror> 课名就让脚本跑起来了。
   逐处 esc 是本分（下面也逐处补了），这里再兜一层：所有写 innerHTML 的地方都先在一个不会执行的
   <template> 里解析，删掉脚本类元素、on* 事件属性、javascript: 之类的链接，再放进页面。
   我们没有第三方前端库，所以直接接管 Element 的 innerHTML 写入是安全的。 */
const HTML_BLOCK = new Set(["SCRIPT", "IFRAME", "FRAME", "OBJECT", "EMBED", "LINK", "META", "BASE", "STYLE", "FORM", "NOSCRIPT", "TEMPLATE"]);
const URL_ATTRS = new Set(["href", "src", "xlink:href", "action", "formaction", "poster", "background", "srcset"]);
const SAFE_URL = /^(?:https?:|mailto:|#|\/(?!\/)|\.{0,2}\/|data:image\/(?:png|jpe?g|gif|webp);)/i;
const _htmlSetter = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML").set;

function sanitizeInto(holder, html) {
  const t = document.createElement("template");
  _htmlSetter.call(t, String(html ?? ""));  // template 的内容是惰性的：脚本不跑、图片不加载
  t.content.querySelectorAll("*").forEach((n) => {
    if (HTML_BLOCK.has(n.tagName.toUpperCase())) { n.remove(); return; }
    for (const a of [...n.attributes]) {
      const name = a.name.toLowerCase();
      const value = a.value.replace(/[\u0000- ]/g, "");
      if (name.startsWith("on") || name === "srcdoc" || name === "formaction"
        || (URL_ATTRS.has(name) && value && !SAFE_URL.test(value))) {
        n.removeAttribute(a.name);
      }
    }
  });
  return t.content;
}

Object.defineProperty(Element.prototype, "innerHTML", {
  configurable: true,
  get: Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML").get,
  set(html) {
    // template 本身、SVG 里的元素（模板按 HTML 解析会把 <path> 变成普通元素）走原生写法；页面里的 SVG 都是 createElementNS 画的
    if (this instanceof HTMLTemplateElement || this.namespaceURI !== "http://www.w3.org/1999/xhtml") {
      _htmlSetter.call(this, html);
      return;
    }
    if (html === "" || html == null) { this.textContent = ""; return; }
    this.replaceChildren(sanitizeInto(this, html));
  },
});
Element.prototype.insertAdjacentHTML = function (where, html) {
  const frag = sanitizeInto(this, html);  // 直接放消过毒的节点，不再序列化后重新解析
  const at = String(where).toLowerCase();
  if (at === "beforebegin") this.before(frag);
  else if (at === "afterbegin") this.prepend(frag);
  else if (at === "beforeend") this.append(frag);
  else if (at === "afterend") this.after(frag);
  else throw new SyntaxError(`insertAdjacentHTML: ${where}`);
};

const $app = document.getElementById("app");
const $nav = document.getElementById("mainNav");
const $header = document.getElementById("headerRight");

/* ---------- API ---------- */

/* 后端报错体可能是字符串、数组或对象——FastAPI 的 422 detail 是数组。
   直接 String() 会变成 "[object Object]"，曾经把「缺 action_id」显示成这个，白白多花时间排查。
   这里统一成人能读的一句话。 */
function errText(data, status) {
  const d = data && data.detail;
  if (typeof d === "string" && d) return d;
  if (Array.isArray(d)) {
    const parts = d.map((x) => (typeof x === "string" ? x : x && (x.msg || x.message)))
      .filter(Boolean);
    if (parts.length) return parts.join("；");
  }
  if (d && typeof d === "object") {
    try { return JSON.stringify(d); } catch (_) { /* 循环引用等，落到下面 */ }
  }
  return `请求失败 (${status})`;
}

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (S.token && !h.Authorization) h.Authorization = `Bearer ${S.token}`;  // 调用方指定了就用调用方的（吊销旧令牌时）
  return h;
}

/* 吊销一个已经不用的令牌（比如访客令牌换成微信登录的新令牌之后）。失败也无所谓：它最多三十天后自己过期。 */
function revokeToken(token) {
  if (!token) return;
  apiFetch("/api/auth/logout", { method: "POST", headers: { Authorization: `Bearer ${token}` } }).catch(() => {});
}

/* 改了这段就把 server/auth.py 的 PRIVACY_VERSION 换成当天日期 */
const PRIVACY_HTML = `
  <h4>存了什么</h4>
  <ul>
    <li><b>账号</b>：用户名和称呼；密码只存哈希。用微信登录的话还有公众号给的 openid——一串只对「启研」公众号有效的编号，不是你的微信号。我们拿不到你的手机号和微信资料。</li>
    <li><b>你写下和做过的</b>：对话；从对话里记下的画像（每条都标来源，在「我的」里能改能删）；你粘贴的成绩单；任务和项目提交；阅读卡；定位里的边、陈述和下注；作业草稿和项目路径进度。</li>
    <li><b>你导入的 PDF</b>：全文只存在你的账号下，别人打不开。带读出题会把节选发给大模型。</li>
    <li><b>带读里的作答</b>：存在你的账号下，用来判对错和预填阅读卡草稿。</li>
    <li><b>你上传的模型密钥</b>：封装后存在你自己的账号下，不出现在页面回显和导出全文里。对话、作业提示、带读出题和判卷会用这把密钥请求大模型。</li>
    <li><b>登录会话</b>：只存令牌的哈希，三十天不用就失效。</li>
  </ul>
  <h4>谁能看到</h4>
  <ul>
    <li>只有登录的你能看到自己的记录。开发团队能在服务器上看到原始数据，只用来排查问题，不给别人。</li>
    <li>公众号只用来登录，不推营销消息。</li>
    <li>对话和成绩单会发给大模型服务（DeepSeek）生成回复；考察里的作答会和论文节选一起发给大模型判对错。</li>
    <li>「定位」的竞争地图和稀有度用的是所有人的匿名计数，只出数字，不出名字和原话。</li>
  </ul>
  <h4>存在哪、存多久</h4>
  <ul>
    <li>存在团队租用的阿里云服务器上。不卖数据，不做广告。</li>
    <li>「我的」页随时可以导出全部数据或删除账号。删除会立刻清掉库里你的所有记录，备份最多再留 7 天。</li>
  </ul>`;

function lostUser(err) {
  const m = String(err && err.message || err || "");
  return /user not found|请先登录/i.test(m);
}

function sessionGoneMsg() {
  return "登录已失效，请重新登录；提示用户名不存在的话，说明服务器数据被重置过，需要重新注册";
}

async function recoverUser(err) {
  if (!lostUser(err)) return false;
  if (S.token) {
    try {
      const me = await apiFetch("/api/auth/me");
      if (me.ok) return false;
    } catch (_) { return false; }
  }
  signedOut("登录已失效，请用用户名和密码重新登录。提示用户名不存在的话，说明服务器数据被重置过，需要重新注册");
  return true;
}

/* 函数多实例时，登录写在 A、下一请求落到 B，B 的临时库里没有这份会话，会回 401。
   先重试一次；仍 401 再问 /api/auth/me。会话其实还在就不踢人，避免点一下就被踢回登录。 */
let sessionKick = null;

async function confirmSessionGone() {
  try {
    const me = await apiFetch("/api/auth/me");
    if (me.ok) return;
    signedOut("登录已失效，请用用户名和密码重新登录。提示用户名不存在的话，说明服务器数据被重置过，需要重新注册");
  } catch (_) { /* 网络抖一下不踢人 */ }
}

async function apiFetch(path, opt = {}) {
  const send = () => fetch(apiUrl(path), { ...opt, headers: authHeaders(opt.headers) });
  let res = await send();
  if (res.status !== 401 || !S.token || String(path).startsWith("/api/auth/")) return res;
  res = await send();
  if (res.status !== 401) return res;
  if (!sessionKick) {
    sessionKick = confirmSessionGone().finally(() => { sessionKick = null; });
  }
  await sessionKick;
  return res;
}

async function api(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const res = await apiFetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) {
    const msg = errText(data, res.status);
    if (res.status === 404 && /user not found/i.test(msg)) await recoverUser(new Error(msg));
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ---------- 工具 ---------- */

function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(msg, ms = 2600) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const t = el("div", "toast", esc(msg));
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}
function skeleton(rows = 2) {
  const box = el("div", "ws-skeleton");
  box.setAttribute("aria-hidden", "true");
  for (let i = 0; i < rows; i++) box.appendChild(el("div", "sk-block"));
  return box;
}
const CAT_CN = { background: "背景", interest: "兴趣", capability: "能力", preference: "偏好", experience: "经历" };
const SRC_CN = { declared: "自述", inferred: "推断", behavior: "行为" };

const WORKSPACE = {
  today: "today",
  dialogue: "dialogue",
  confirm: "me",
  cards: "cards",
  workbench: "today",
  projects: "projects",
  project: "projects",
  position: "position",
  read: "read",
  card: "read",
  me: "me",
  feedback: "feedback",
};

/* ---------- 视图切换 ---------- */

/* 刷新停在原来那一栏：记在这个标签页的 sessionStorage（新开标签页照样从首页进）。
   原来已登录的学生一刷新就回到粒子首页（Codex 复现）。要 id 的详情页回到它所在的栏目。 */
const RESUMABLE = { today: "today", workbench: "workbench", projects: "projects", project: "projects", read: "read",
  card: "read", position: "position", dialogue: "dialogue", confirm: "me", me: "me", cards: "cards" };

function firstView() {
  return S.entered ? "today" : "dialogue";
}

let enteringApp = false;
function confirmEarlyEntry(profile) {
  return new Promise((resolve) => {
    const dialog = el("dialog", "entry-confirm");
    dialog.setAttribute("aria-labelledby", "entryConfirmTitle");
    const title = el("h2", "", "现在就进入启研吗？");
    title.id = "entryConfirmTitle";
    dialog.appendChild(title);
    dialog.appendChild(el("p", "", profile
      ? "我们对你的了解还不多。再聊一聊，系统会更懂你的学习基础和阅读需求；你也可以直接进入，之后慢慢补充。"
      : "暂时无法确认已了解的信息。你仍可以直接进入，或留在这里继续聊。"));
    if (profile) {
      const missing = (profile.areas || []).filter((a) => !a.known).map((a) => a.label);
      if (missing.length) dialog.appendChild(el("p", "form-note", "还可以聊聊：" + esc(missing.join("、")) + "。这些都不是必填项。"));
    }
    const row = el("div", "submit-actions");
    const more = el("button", "btn", "再聊一聊");
    const go = el("button", "btn secondary", "直接进入");
    more.type = go.type = "button";
    const finish = (yes) => { dialog.close(); dialog.remove(); resolve(yes); };
    more.onclick = () => finish(false);
    go.onclick = () => finish(true);
    dialog.addEventListener("cancel", (e) => { e.preventDefault(); finish(false); });
    row.append(more, go);
    dialog.appendChild(row);
    document.body.appendChild(dialog);
    dialog.showModal();
    more.focus();
  });
}

async function enterApp(next) {
  if (S.entered) { setView(next || "today"); return true; }
  if (enteringApp || !S.uid) return false;
  enteringApp = true;
  const uid = S.uid;
  try {
    const profile = await api("GET", `/api/onboard/readiness?uid=${encodeURIComponent(uid)}`).catch(() => null);
    if ((!profile || !profile.ready) && !await confirmEarlyEntry(profile)) return false;
    if (S.uid !== uid) return false;
    await api("POST", "/api/onboard/enter", { uid });
    S.entered = true;
    setView(next || "today");
    return true;
  } catch (e) { toast(e.message); return false; }
  finally { enteringApp = false; }
}

function setView(name) {
  if (name === "confirm") {
    S.scrollToConfirm = true;
    name = "me";
  }
  const stay = name === "dialogue" || name === "me" || name === "login" || name === "home";
  if (S.uid && !stay && !S.entered) {
    enterApp(name);
    return;
  }
  S.view = name;
  try { if (S.uid && RESUMABLE[name]) sessionStorage.setItem("rg_last_view", RESUMABLE[name]); } catch (_) { /* 无痕模式等 */ }
  document.body.dataset.view = name;
  const home = name === "home";
  const inApp = !home && name !== "login" && !!S.uid;
  document.body.classList.toggle("is-home", home);
  document.body.classList.toggle("app-mode", inApp);
  document.body.classList.toggle("is-onboarding", inApp && !S.entered);
  if (!home && stopField) { stopField(); stopField = null; }
  if (name !== "cards") document.getElementById("nodeSheet")?.remove();
  closeUserMenu();
  document.querySelectorAll(".nav-btn").forEach((b) => {
    const on = b.dataset.workspace === WORKSPACE[name];
    b.classList.toggle("active", on);
    if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    b.disabled = !S.uid;
  });
  const dlgBtn = document.querySelector('.nav-btn[data-view="dialogue"]');
  if (dlgBtn) dlgBtn.hidden = !S.entered;
  const dirChip = document.getElementById("dirChip");
  if (dirChip) dirChip.hidden = !S.entered;
  $nav.hidden = !inApp || !S.entered;
  $header.hidden = !inApp;
  paintChrome();
  render();
  window.scrollTo({ top: 0 });
}

/* 每次切换视图都有一个序号。异步渲染在每个 await 之后用 stale(seq) 检查：
   用户已经切走了，就不再往页面上写，避免两个视图叠在一起。 */
function stale(seq) {
  return seq !== S.renderSeq;
}

async function render() {
  const seq = S.renderSeq = (S.renderSeq || 0) + 1;
  if (S.view === "home") { renderHome(); return; }
  if (!S.uid && S.view !== "login") { renderLogin(); return; }
  try {
    switch (S.view) {
      case "login": renderLogin(); break;
      case "dialogue": await ChatView.render($app); break;  // 要 await：不然它取画像失败时绕过下面的提示
      case "confirm": await renderMe(); break;
      case "cards": await renderCards(); break;
      case "workbench": await renderWorkbench(); break;
      case "projects":
      case "project": renderProjectsSoon(); break;
      case "position": renderPositionSoon(); break;
      case "read": await renderRead(); break;
      case "card": await renderCard(); break;
      case "feedback": await renderFeedback(); break;
      case "me": await renderMe(); break;
      case "today":
      default: await renderToday(); break;
    }
  } catch (e) {
    // 这一页要的数据没取到：原来变成没人接的 Promise 错误，菜单关了、页面却还停在上一页，也没有提示（Codex 复现）
    if (stale(seq)) return;
    $app.innerHTML = "";
    $app.appendChild(emptyPanel(`这一页没打开：${esc((e && e.message) || e)}`, "再试一次", S.view));
  }
}

/* ---------- 首页：六屏下滑，点线图随滚动重画 ---------- */

const HOME_PAGES = [
  {
    kicker: "启研",
    title: "先认识你，<br>再走下一步。",
    lead: "给北大本科一、二年级。先聊你现在卡在哪，再给一件二十分钟里能做完的事。",
  },
  {
    kicker: "01",
    title: "先聊聊",
    lead: "年级、已经会的、想试什么。说不清就说不知道。只记你亲口说的。",
    points: [
      ["你在哪", "几年级、哪个院，或者现在停在哪一步。"],
      ["会什么", "已经上手的，和明确还没碰过的。"],
      ["想试什么", "一个具体问题就够。没有也没关系。"],
    ],
  },
  {
    kicker: "02",
    title: "给一个方向",
    lead: "按你刚说的，标出值得先试的方向。对不上你的话，就不给。",
    points: [
      ["对得上你", "理由里能看见你的原话。"],
      ["真有这门课", "从北大教务公开课里找。没有就空着。"],
      ["先读一篇", "上手用的那一篇，不是一份书单。"],
    ],
  },
  {
    kicker: "03",
    title: "交一份小作业",
    lead: "不展开成阅读清单。一次只给一件，二十分钟里能做完。",
    points: [
      ["做什么", "读一小节、跑一个小例子，或回答一个具体问题。"],
      ["交什么", "一段话、一张图或一段输出，别人能看见。"],
      ["记下", "实际做了什么，下次不用重说。"],
    ],
  },
  {
    kicker: "04",
    title: "对一下交上来的",
    lead: "按事先说好的几条看这件事做成了没有。不评价你这个人。",
    points: [
      ["对事", "只看这一次交上来的。"],
      ["做到哪条", "做到的、缺的分开写，并指出下一处改哪里。"],
      ["没结果就说", "缺证据就写缺证据。"],
    ],
  },
  {
    kicker: "05",
    title: "下次还认得你",
    lead: "你说过的还在。做完这一件，再给下一件。",
    points: [
      ["不用重介绍", "年级、基础、好奇还在。"],
      ["带着上次的结果", "反馈写回来，下次先看这些。"],
      ["仍然是一件事", "不是一份新计划。"],
    ],
    closing: "做完这一圈，下次还认得你。",
  },
];

let stopField = null;

function renderHome() {
  if (stopField) stopField();
  $app.innerHTML = "";
  const land = el("div", "land");
  const canvas = el("canvas", "land-field");
  canvas.setAttribute("aria-hidden", "true");
  const snap = el("div", "land-snap");
  HOME_PAGES.forEach((page, i) => {
    const sec = el("section", "snap");
    sec.dataset.index = String(i);
    const points = (page.points || [])
      .map(([k, v]) => `<li><b>${k}</b><span>${v}</span></li>`).join("");
    sec.innerHTML = `<div class="snap-inner">`
      + (i === 0 ? `<p class="hero-kicker">${page.kicker}</p>` : `<p class="snap-no">${page.kicker}</p>`)
      + `<h2>${page.title}</h2>`
      + `<p class="land-lead">${page.lead}</p>`
      + (points ? `<ul class="land-points">${points}</ul>` : "")
      + (page.closing ? `<p class="land-closing">${page.closing}</p>` : "")
      + `</div>`;
    if (i === HOME_PAGES.length - 1) {
      const btn = el("button", "btn land-cta", "立即开始体验");
      btn.type = "button";
      btn.onclick = beginExperience;
      sec.querySelector(".snap-inner").appendChild(btn);
    }
    snap.appendChild(sec);
  });
  const hintEl = el("div", "land-hint");
  hintEl.setAttribute("aria-hidden", "true");
  hintEl.append(el("span", "", "滚动以继续"), el("i"));
  const skip = el("button", "land-skip", "跳过，直接开始");
  skip.type = "button";
  skip.onclick = beginExperience;
  land.append(canvas, buildSteps(), hintEl, skip, snap);
  $app.appendChild(land);
  stopField = mountSketch(canvas, snap);
}

function buildSteps() {
  const box = el("div", "land-steps");
  box.setAttribute("aria-hidden", "true");
  const cur = el("b", "", "00");
  const track = el("i", "land-steps-track");
  track.appendChild(el("i"));
  const total = el("span", "", `/ ${String(HOME_PAGES.length - 1).padStart(2, "0")}`);
  box.append(cur, track, total);
  return box;
}

function beginExperience() {
  if (stopField) stopField();
  if (S.uid) {
    document.getElementById("userNickname").textContent = S.nickname || "";
    setView(firstView());
    return;
  }
  setView("login");
}

/* 图形：[-1, 1] 坐标系，y 向下。每张图是若干笔画（折线），accent 笔画用主色。
   粒子按笔画顺序均匀排布，换图时第 i 颗粒子从旧图第 i 个位置走到新图第 i 个位置，
   看起来像一支笔把旧图擦掉、再把新图画出来。 */

function unitHash(i, salt) {
  const x = Math.sin(i * 127.1 + salt * 311.7) * 43758.5453;
  return x - Math.floor(x);
}

function arcPts(cx, cy, r, a0, a1, n = 48) {
  const pts = [];
  for (let i = 0; i <= n; i++) {
    const a = a0 + (a1 - a0) * (i / n);
    pts.push([cx + Math.cos(a) * r, cy + Math.sin(a) * r]);
  }
  return pts;
}

function ring(cx, cy, r, n = 72) {
  return arcPts(cx, cy, r, 0, Math.PI * 2, n);
}

function curve(a, b, bend = 0.5) {
  const pts = [];
  const mx = a[0] + (b[0] - a[0]) * bend;
  for (let i = 0; i <= 24; i++) {
    const t = i / 24;
    const u = 1 - t;
    pts.push([
      u * u * u * a[0] + 3 * u * u * t * mx + 3 * u * t * t * mx + t * t * t * b[0],
      u * u * u * a[1] + 3 * u * u * t * a[1] + 3 * u * t * t * b[1] + t * t * t * b[1],
    ]);
  }
  return pts;
}

function rrect(x0, y0, x1, y1, r) {
  const pts = [];
  [[x1 - r, y0 + r, -Math.PI / 2, 0], [x1 - r, y1 - r, 0, Math.PI / 2],
    [x0 + r, y1 - r, Math.PI / 2, Math.PI], [x0 + r, y0 + r, Math.PI, Math.PI * 1.5]]
    .forEach(([cx, cy, a0, a1]) => arcPts(cx, cy, r, a0, a1, 8).forEach((p) => pts.push(p)));
  pts.push(pts[0]);
  return pts;
}

function bubble(x0, y0, x1, y1, r, tx, dir) {
  // 圆角气泡，尾巴开在底边 tx 处，dir = -1 朝左下，1 朝右下
  const pts = [];
  const corner = (cx, cy, a0, a1) => arcPts(cx, cy, r, a0, a1, 8).forEach((p) => pts.push(p));
  corner(x0 + r, y0 + r, Math.PI, Math.PI * 1.5);
  corner(x1 - r, y0 + r, -Math.PI / 2, 0);
  corner(x1 - r, y1 - r, 0, Math.PI / 2);
  pts.push([tx + 0.1, y1], [tx + dir * 0.12 + (dir > 0 ? 0.1 : 0), y1 + 0.16], [tx - 0.02, y1]);
  corner(x0 + r, y1 - r, Math.PI / 2, Math.PI);
  pts.push(pts[0]);
  return pts;
}

const SKETCHES = [
  // 00 台阶通向一扇门：先认识你，再走下一步（白门框不动，绿门板绕左铰链带透视向里推开；
  //     门内地板两条透视线收向消失点，门开了才渐显——走进去的路）
  () => {
    const door = [[0.22, 0.12], [0.22, -0.46], ...arcPts(0.5, -0.46, 0.28, Math.PI, Math.PI * 2, 32), [0.78, 0.12]];
    const inner = [[0.3, 0.12], [0.3, -0.44], ...arcPts(0.5, -0.44, 0.2, Math.PI, Math.PI * 2, 28), [0.7, 0.12]];
    const stairs = [[-0.95, 0.74], [-0.62, 0.74], [-0.62, 0.53], [-0.3, 0.53], [-0.3, 0.32], [0.02, 0.32], [0.02, 0.12], [0.95, 0.12]];
    const pathL = [[0.3, 0.12], [0.385, -0.05]];
    const pathR = [[0.7, 0.12], [0.615, -0.05]];
    return [
      { pts: stairs },
      { pts: door },
      { pts: inner, accent: true, part: "door" },
      { pts: ring(-0.46, 0.38, 0.05, 20), accent: true },
      { pts: pathL, accent: true, part: "path" },
      { pts: pathR, accent: true, part: "path" },
    ];
  },
  // 01 一问一答的两个气泡（问号绕自己的底部轻微摆动）
  () => {
    const q = [...arcPts(-0.36, -0.5, 0.1, Math.PI * 1.05, Math.PI * 2.25, 28), [-0.36, -0.33], [-0.36, -0.28]];
    return [
      { pts: bubble(-0.92, -0.78, 0.18, -0.12, 0.12, -0.62, -1) },
      { pts: q, accent: true, part: "q" },
      { pts: ring(-0.36, -0.2, 0.018, 8), accent: true, part: "q" },
      { pts: bubble(-0.18, 0.06, 0.92, 0.62, 0.12, 0.56, 1) },
      { pts: [[0.0, 0.22], [0.72, 0.22]] },
      { pts: [[0.0, 0.34], [0.6, 0.34]] },
      { pts: [[0.0, 0.46], [0.38, 0.46]] },
    ];
  },
  // 02 罗盘：指针指向一个方向（指针绕盘心来回摆动）
  () => {
    const out = [{ pts: ring(0, 0.04, 0.74, 96) }];
    for (let k = 0; k < 8; k++) {
      const a = (k / 8) * Math.PI * 2 - Math.PI / 2;
      const r0 = k % 2 === 0 ? 0.6 : 0.66;
      out.push({ pts: [[Math.cos(a) * r0, 0.04 + Math.sin(a) * r0], [Math.cos(a) * 0.74, 0.04 + Math.sin(a) * 0.74]] });
    }
    const a = -Math.PI / 4;
    const tip = (ang, r) => [Math.cos(ang) * r, 0.04 + Math.sin(ang) * r];
    const n = tip(a, 0.52);
    const s = tip(a + Math.PI, 0.52);
    const l = tip(a - Math.PI / 2, 0.1);
    const rr = tip(a + Math.PI / 2, 0.1);
    out.push({ pts: [l, n, rr], accent: true, part: "needle" });
    out.push({ pts: [l, s, rr], part: "needle" });
    out.push({ pts: ring(0, 0.04, 0.035, 12) });
    out.push({ pts: [[-0.05, -0.8], [-0.05, -0.96], [0.05, -0.8], [0.05, -0.96]] });
    return out;
  },
  // 03 秒表：二十分钟（指针转回 12 点方向）
  () => {
    const c = [0, 0.14];
    const out = [
      { pts: ring(c[0], c[1], 0.66, 96) },
      { pts: [[0, -0.52], [0, -0.62]] },
      { pts: rrect(-0.11, -0.76, 0.11, -0.62, 0.03) },
      { pts: [[0.47, -0.36], [0.55, -0.44]] },
    ];
    for (let k = 0; k < 12; k++) {
      const a = (k / 12) * Math.PI * 2 - Math.PI / 2;
      const r0 = k % 3 === 0 ? 0.5 : 0.56;
      out.push({ pts: [[c[0] + Math.cos(a) * r0, c[1] + Math.sin(a) * r0], [c[0] + Math.cos(a) * 0.62, c[1] + Math.sin(a) * 0.62]] });
    }
    out.push({ pts: arcPts(c[0], c[1], 0.4, -Math.PI / 2, Math.PI * 1.5 - 0.02, 120), accent: true, part: "arc" });
    out.push({ pts: [c, [c[0] + Math.cos(Math.PI / 6) * 0.44, c[1] + Math.sin(Math.PI / 6) * 0.44]], accent: true, part: "hand" });
    out.push({ pts: ring(c[0], c[1], 0.03, 12) });
    return out;
  },
  // 04 一页提交，逐条打勾（第三条的待办圈滚动时变成对勾、换成主题色）
  () => {
    const page = [[-0.58, -0.8], [0.2, -0.8], [0.44, -0.56], [0.44, 0.8], [-0.58, 0.8], [-0.58, -0.8]];
    const out = [{ pts: page }, { pts: [[0.2, -0.8], [0.2, -0.56], [0.44, -0.56]] }];
    [-0.3, 0.02, 0.34].forEach((y, i) => {
      if (i < 2) out.push({ pts: [[-0.42, y], [-0.35, y + 0.07], [-0.22, y - 0.08]], accent: true });
      else out.push({ pts: ring(-0.33, y, 0.06, 20), part: "todo" });
      out.push({ pts: [[-0.1, y], [0.28, y]] });
    });
    out.push({ pts: [[-0.42, 0.6], [0.1, 0.6]] });
    return out;
  },
  // 05 一棵往右长的树，走过的路用主色（R→A→A2→C2 四个节点依次点亮）
  () => {
    const R = [-0.82, 0.06];
    const A = [-0.3, -0.38];
    const B = [-0.3, 0.5];
    const A1 = [0.24, -0.64];
    const A2 = [0.24, -0.12];
    const B1 = [0.24, 0.34];
    const B2 = [0.24, 0.74];
    const C1 = [0.8, -0.34];
    const C2 = [0.8, 0.1];
    const node = (p, r, accent) => ({ pts: ring(p[0], p[1], r, 24), accent });
    const edge = (a, b, accent) => ({ pts: curve([a[0] + 0.06, a[1]], [b[0] - 0.06, b[1]]), accent });
    const arr = [
      node(R, 0.06, true), edge(R, A, true), node(A, 0.055, true), edge(A, A2, true), node(A2, 0.055, true),
      edge(A2, C2, true), node(C2, 0.05, true),
      edge(A, A1), node(A1, 0.05), edge(A2, C1), node(C1, 0.05),
      edge(R, B), node(B, 0.055), edge(B, B1), node(B1, 0.05), edge(B, B2), node(B2, 0.05),
    ];
    arr[0].part = "n0"; arr[2].part = "n1"; arr[4].part = "n2"; arr[6].part = "n3";
    return arr;
  },
];

/* 每屏主题色（参考彩色 WebGL 粒子站）：强调笔画、图形后柔光、背景微染、文字点缀共用，
   随滚动相位在相邻两色间连续过渡 */
const TONES = [
  [127, 209, 194], // 00 起点 · 青
  [143, 189, 246], // 01 画像 · 蓝
  [195, 174, 245], // 02 方向 · 紫
  [242, 205, 126], // 03 任务 · 金
  [242, 160, 138], // 04 反馈 · 珊瑚
  [164, 222, 138], // 05 成长 · 绿
];

/* 每图的聚形后动画：聚形完成、文字出完，随本屏向下滚动推进（act 0→1，scrub 可逆）。
   persp = 门板绕竖轴向里推的透视旋转；sweep = 弧线随指针扫过逐渐显出；fill = 点亮时填实心 */
const FIG_ANIMS = [
  // 00 绿门板绕自身左铰链向里推开 78°：远端向铰链收拢、向门高中线收缩（白门框不动）；
  //    门内地板两条透视线（收向消失点）随开门渐显——走进去的路
  { parts: {
    door: { persp: { hinge: [0.3, 0.12], w: 0.4, yc: -0.26, pf: 1.4, ang: (act) => 1.36 * (1 - Math.pow(1 - act, 3)) } },
    path: { reveal: (act) => 1 - Math.pow(1 - act, 2) },
  } },
  { parts: { q: { pivot: [-0.36, -0.28], ang: (act, tm) => Math.sin(tm * 2.2) * 0.14 * (0.15 + 0.85 * act) } } },
  // 02 指北针：以正北为中心，在相邻的两个刻度（东北—西北）之间来回摆
  { parts: { needle: { pivot: [0, 0.04], ang: (act, tm) => -Math.PI / 4 + (Math.PI / 4) * (0.15 + 0.85 * act) * Math.sin(tm * 1.6) } } },
  // 03 时针顺时针转 240° 回到 12 点；金色进度弧从 12 点起随时针扫过的位置延长
  { parts: {
    hand: { pivot: [0, 0.14], ang: (act) => 4.19 * (1 - Math.pow(1 - act, 3)) },
    arc: { pivot: [0, 0.14], sweep: { from: -Math.PI / 2, base: 2.09, span: 4.19 } },
  } },
  { parts: { todo: { morphTo: [[-0.42, 0.34], [-0.35, 0.41], [-0.22, 0.26]] } } },
  // 05 四个节点依次点亮：环内填成实心并发光
  { parts: {
    n0: { light: 0, fill: [-0.82, 0.06, 0.06] },
    n1: { light: 1, fill: [-0.3, -0.38, 0.055] },
    n2: { light: 2, fill: [0.24, -0.12, 0.055] },
    n3: { light: 3, fill: [0.8, 0.1, 0.05] },
  } },
];

function strokeLength(pts) {
  let L = 0;
  for (let i = 1; i < pts.length; i++) L += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  return L;
}

function sampleSketch(strokes, n) {
  // 沿全部笔画等距取 n 个点；返回 {xy, acc, sid}，顺序即笔画顺序；sid 记粒子属于哪一笔（供图形动画分组）
  const lens = strokes.map((s) => strokeLength(s.pts));
  const total = lens.reduce((a, b) => a + b, 0) || 1;
  const xy = new Float32Array(n * 2);
  const acc = new Uint8Array(n);
  const sid = new Uint16Array(n);
  let k = 0;
  strokes.forEach((s, si) => {
    const want = si === strokes.length - 1 ? n - k : Math.max(2, Math.round((lens[si] / total) * n));
    const count = Math.min(want, n - k);
    let seg = 1;
    let walked = 0;
    for (let j = 0; j < count; j++) {
      const d = count === 1 ? 0 : (j / (count - 1)) * lens[si];
      while (seg < s.pts.length - 1 && walked + Math.hypot(s.pts[seg][0] - s.pts[seg - 1][0], s.pts[seg][1] - s.pts[seg - 1][1]) < d) {
        walked += Math.hypot(s.pts[seg][0] - s.pts[seg - 1][0], s.pts[seg][1] - s.pts[seg - 1][1]);
        seg += 1;
      }
      const a = s.pts[seg - 1];
      const b = s.pts[seg];
      const sl = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
      const t = Math.min(1, Math.max(0, (d - walked) / sl));
      xy[k * 2] = a[0] + (b[0] - a[0]) * t;
      xy[k * 2 + 1] = a[1] + (b[1] - a[1]) * t;
      acc[k] = s.accent ? 1 : 0;
      sid[k] = si;
      k += 1;
    }
  });
  return { xy, acc, sid, length: total };
}

function mountSketch(canvas, scroller) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return () => {};
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let W = 0; let H = 0; let size = 0; let dot = 1.15;
  let gy = 0;    // 图形锚点 y（恒为内容带中心）
  let gxA = [];  // 各屏图形锚点 x（与文字逐屏左右对调；转场时随相位插值横移）
  let figs = [];
  let N = 0;
  let scatter = []; // 换图途中粒子均匀散布的全屏目标位（也是开场出发点）
  let dust = [];    // 漂浮星尘：随主题色的氛围层，缓慢漂移明灭
  let plex = [];    // 星座网：漂移节点 + 近邻连线，背景的图案结构层
  let lastActive = -1; // 上次写进 CSS 的主题色屏号
  const stepsBox = canvas.parentElement ? canvas.parentElement.querySelector(".land-steps") : null;
  const stepCur = stepsBox ? stepsBox.querySelector("b") : null;
  const stepFill = stepsBox ? stepsBox.querySelector(".land-steps-track i") : null;

  /* 发光粒子 sprite：热芯型径向渐变（核心占 45%、外围快速衰减）画一次，逐点 drawImage；
     粒子层用加法混合，交叠处亮度叠加出「燃烧」感，而不是靠大光晕 */
  const glowSprite = (rgb) => {
    const c = document.createElement("canvas");
    c.width = 32; c.height = 32;
    const g = c.getContext("2d");
    const grad = g.createRadialGradient(16, 16, 0, 16, 16, 16);
    grad.addColorStop(0, `rgba(${rgb},1)`);
    grad.addColorStop(0.45, `rgba(${rgb},0.85)`);
    grad.addColorStop(1, `rgba(${rgb},0)`);
    g.fillStyle = grad;
    g.fillRect(0, 0, 32, 32);
    return c;
  };
  const SPR_INK = glowSprite("237,241,238");
  const SPRS = TONES.map((t) => glowSprite(t.join(","))); // 每屏一色的强调笔画 sprite
  /* 胶片颗粒：一张静态噪点瓦片（中灰上下抖动，overlay 才能双向），低透明度铺满全屏 */
  const grainTile = document.createElement("canvas");
  grainTile.width = grainTile.height = 160;
  {
    const g = grainTile.getContext("2d");
    const id = g.createImageData(160, 160);
    for (let i = 0; i < id.data.length; i += 4) {
      const v = 90 + Math.random() * 76;
      id.data[i] = id.data[i + 1] = id.data[i + 2] = v;
      id.data[i + 3] = 255;
    }
    g.putImageData(id, 0, 0);
  }
  const grainPat = ctx.createPattern(grainTile, "repeat");
  /* 相位 → 相邻两屏主题色的线性插值（背景微染/柔光/文字点缀共用） */
  const mixTone = (ph) => {
    const a = Math.min(TONES.length - 1, Math.floor(ph));
    const b = Math.min(TONES.length - 1, a + 1);
    const k = ph - a;
    return TONES[a].map((v, i) => v + (TONES[b][i] - v) * k);
  };
  /* 一团径向色斑（氛围光斑 / bokeh 共用画法） */
  const blob = (x, y, r, c, al) => {
    const g = ctx.createRadialGradient(x, y, 0, x, y, r);
    g.addColorStop(0, `rgba(${Math.round(c[0])},${Math.round(c[1])},${Math.round(c[2])},${al})`);
    g.addColorStop(1, `rgba(${Math.round(c[0])},${Math.round(c[1])},${Math.round(c[2])},0)`);
    ctx.fillStyle = g;
    ctx.fillRect(x - r, y - r, r * 2, r * 2);
  };
  let phase = 0;
  let target = 0;
  let intro = still ? 1 : 0;
  let raf = 0;
  let idleT = 0; // 静止期的星尘心跳定时器
  let introStart = 0;
  let step = 0;     // 已落定的屏号
  let actCur = 0;   // 当前屏聚形后动画的剧本进度 0→1
  let actStart = 0; // 动画起播时间戳（0 = 未起播）
  let busy = true;  // 剧本进行中（锁输入）；开场聚形后自动播 00 屏动画再亮提示
  let phaseFrom = 0; // 本段换屏的相位起点
  let transStart = 0; // 本段换屏起播时间
  let queued = 0;
  let lastWheelT = 0;
  const SWAP_MS = 1400;
  const hintEl = canvas.parentElement ? canvas.parentElement.querySelector(".land-hint") : null;
  const skipEl = canvas.parentElement ? canvas.parentElement.querySelector(".land-skip") : null;
  const hint = (show) => {
    if (hintEl) hintEl.classList.toggle("show", show && !busy && step < figs.length - 1);
  };
  const syncSkip = () => {
    if (skipEl) skipEl.style.visibility = step >= figs.length - 1 ? "hidden" : "";
  };

  const build = () => {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const narrow = W < 920;
    const mid = !narrow && W < 1280;
    /* 文字与图形逐屏左右对调（偶数屏文字左/图形右，奇数屏反之）；
       竖直位置由 JS 写 top、与图形中心对齐，整列放不下时向上收敛：
       顶部最少 72px、底部留 24px，保证不裁字（短屏配合 CSS max-height 收紧一档） */
    const inners = [...scroller.querySelectorAll(".snap-inner")];
    const hh = inners.reduce((m, el) => Math.max(m, el.offsetHeight), 0) || H * 0.5;
    let textTop;
    if (narrow) {
      const figSpace = Math.max(140, H - hh - 40);
      size = Math.min(W * 0.5, figSpace * 0.5);
      gy = 10 + figSpace / 2;
      gxA = inners.map(() => W * 0.5);
      textTop = Math.max(72, Math.min(H - 24 - hh, 10 + figSpace + 16));
    } else {
      const mL = Math.max(120, W * 0.13); // 左列文字边距（与 CSS --col-pad 一致，更靠中）
      const mR = Math.max(88, W * 0.09);  // 右列文字边距
      const colW = mid ? 360 : 440;
      const figM = Math.max(72, W * 0.05); // 图形区域的左右边距（宽图如「树」也不贴边）
      const rightRegion = [mL + colW + 48, W - figM]; // 文字在左时图形的区域
      const leftRegion = [figM, W - mR - colW - 48];  // 文字在右时图形的区域
      const availEven = rightRegion[1] - rightRegion[0];
      const availOdd = leftRegion[1] - leftRegion[0];
      size = Math.min(H * 0.42, availEven * 0.38, availOdd * 0.38);
      gy = H * 0.5;
      gxA = inners.map((_, i) => {
        const r = i % 2 === 0 ? rightRegion : leftRegion;
        return (r[0] + r[1]) / 2;
      });
      textTop = Math.max(72, (H - hh) / 2);
    }
    inners.forEach((el) => { el.style.top = `${textTop}px`; });
    dot = narrow ? 0.85 : 0.95;
    const spacing = narrow ? 1.8 : 1.6; // 更密：细粒子密排 + 横向散布铺成粗笔画
    const raws = SKETCHES.map((f) => f());
    const totalLen = (st) => st.reduce((s, x) => s + strokeLength(x.pts), 0);
    const need = raws.map((st) => Math.ceil((totalLen(st) * size) / spacing));
    N = Math.max(...need);
    // 每张图只点亮自己的 need 颗，保证各图点距一致；其余粒子跟着走但不可见
    const sample = (st, want) => {
      const f = sampleSketch(st, N);
      const vis = new Uint8Array(N);
      for (let j = 0; j < want; j++) vis[Math.floor((j * N) / want)] = 1;
      // 带 part 标记的笔画 → 粒子分组表（1 起，0 = 无分组），供聚形后动画用
      const names = [];
      const pid = new Uint8Array(N);
      st.forEach((s2, si) => {
        if (!s2.part) return;
        let id = names.indexOf(s2.part) + 1;
        if (!id) { names.push(s2.part); id = names.length; }
        for (let j = 0; j < N; j++) if (f.sid[j] === si) pid[j] = id;
      });
      f.pid = pid;
      f.pnames = names;
      f.vis = vis;
      return f;
    };
    figs = raws.map((st, k) => sample(st, need[k]));
    // 04「待办圈 → 对勾」的形变目标：沿对勾折线按弧长均匀取点，与圈上粒子一一对应
    const spec4 = FIG_ANIMS[4] && FIG_ANIMS[4].parts.todo;
    if (figs[4] && figs[4].pid && spec4) {
      const f4 = figs[4];
      const mid = f4.pnames.indexOf("todo") + 1;
      const idxs = [];
      for (let j = 0; j < N; j++) if (f4.pid[j] === mid) idxs.push(j);
      const [p0, p1, p2] = spec4.morphTo;
      const L1 = Math.hypot(p1[0] - p0[0], p1[1] - p0[1]);
      const L2 = Math.hypot(p2[0] - p1[0], p2[1] - p1[1]);
      const TT = L1 + L2;
      const tgt = new Float32Array(N * 2);
      idxs.forEach((j, q2) => {
        const d = idxs.length === 1 ? 0 : (q2 / (idxs.length - 1)) * TT;
        let k2;
        if (d <= L1) {
          k2 = L1 ? d / L1 : 0;
          tgt[j * 2] = p0[0] + (p1[0] - p0[0]) * k2;
          tgt[j * 2 + 1] = p0[1] + (p1[1] - p0[1]) * k2;
        } else {
          k2 = L2 ? (d - L1) / L2 : 0;
          tgt[j * 2] = p1[0] + (p2[0] - p1[0]) * k2;
          tgt[j * 2 + 1] = p1[1] + (p2[1] - p1[1]) * k2;
        }
      });
      f4.morph = tgt;
    }
    // 每个采样点的路径法线：细粒子沿法线横向散布，铺出有颗粒感的粗笔画
    figs.forEach((f) => {
      const nx = new Float32Array(N); const ny = new Float32Array(N);
      for (let i = 0; i < N; i++) {
        const i0 = Math.max(0, i - 2); const i1 = Math.min(N - 1, i + 2);
        const dx = f.xy[i1 * 2] - f.xy[i0 * 2];
        const dy = f.xy[i1 * 2 + 1] - f.xy[i0 * 2 + 1];
        const L = Math.hypot(dx, dy) || 1;
        nx[i] = -dy / L; ny[i] = dx / L;
      }
      f.nx = nx; f.ny = ny;
    });
    scatter = Array.from({ length: N }, (_, i) => [
      W * (0.06 + 0.88 * unitHash(i, 11)),
      H * (0.06 + 0.88 * unitHash(i, 13)),
    ]);
    // 星尘三层：远景细点（多而暗）→ 中景点 → 近景 bokeh 软斑（大而更淡、漂移更快），带出纵深
    dust = [];
    const addDust = (n, rr, ar, sp, soft) => {
      for (let k = 0; k < n; k++) {
        const s = dust.length;
        dust.push({
          x: unitHash(s, 19), y: unitHash(s, 23),
          r: rr[0] + unitHash(s, 29) * (rr[1] - rr[0]),
          p: unitHash(s, 31) * Math.PI * 2,
          w: 0.4 + unitHash(s, 37) * 0.9,
          a: ar[0] + unitHash(s, 41) * (ar[1] - ar[0]),
          sp, soft,
        });
      }
    };
    const nn = narrow ? 0.55 : 1;
    addDust(Math.round(74 * nn), [0.5, 1.3], [0.035, 0.09], 0.5, false);
    addDust(Math.round(34 * nn), [0.9, 1.9], [0.05, 0.12], 1, false);
    addDust(narrow ? 7 : 13, [6, 16], [0.028, 0.06], 1.7, true);
    // 星座网节点：向画面中央聚拢（左右两侧留给文字列），小幅漂移，连线随距离实时增减
    plex = Array.from({ length: narrow ? 12 : 24 }, (_, i) => ({
      x: 0.5 + (unitHash(i, 53) - 0.5) * 0.78,
      y: 0.08 + 0.84 * unitHash(i, 59),
      p: unitHash(i, 61) * Math.PI * 2,
      sp: 0.4 + unitHash(i, 67) * 0.7,
      r: 1.1 + unitHash(i, 71),
    }));
  };

  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

  /* 聚形后动画进度：当前屏由剧本播放（actCur），已越过的屏视为播完，未到的屏为 0 */
  const actOf = (idx) => (idx === step ? actCur : idx < step ? 1 : 0);

  /* 聚形后动画的逐帧参数；ang 类预乘好 cos/sin，sweep/fill/flat 按各自类型展开 */
  const animsOf = (idx, tm2) => {
    const spec = FIG_ANIMS[idx];
    if (!spec) return null;
    const act = actOf(idx);
    const out = {};
    for (const name of Object.keys(spec.parts)) {
      const c = spec.parts[name];
      if (c.ang) {
        const ang = c.ang(act, tm2);
        out[name] = { ca: Math.cos(ang), sa: Math.sin(ang), px: c.pivot[0], py: c.pivot[1] };
      } else if (c.persp) {
        const ang = c.persp.ang(act);
        out[name] = {
          hx: c.persp.hinge[0], yc: c.persp.yc, w: c.persp.w, pf: c.persp.pf,
          co: Math.cos(ang), si: Math.sin(ang),
        };
      } else if (c.sweep) {
        out[name] = {
          sweep: c.sweep.base + c.sweep.span * (1 - Math.pow(1 - act, 3)),
          from: c.sweep.from, sx: c.pivot[0], sy: c.pivot[1],
        };
      } else if (c.reveal) {
        out[name] = { reveal: c.reveal(act) }; // 门内路面：随开门渐显
      } else if (c.morphTo) {
        out[name] = { morph: true, k: 1 - Math.pow(1 - act, 3) };
      } else if (c.light !== undefined) {
        out[name] = { light: Math.min(1, Math.max(0, act * 4 - c.light)), fill: c.fill || null };
      }
    }
    return out;
  };

  /* 每屏一色（参考彩色 WebGL 粒子站）：背景向主题色微染、图形后一团柔光、强调笔画换色，
  全部随相位在相邻两色间连续过渡；粒子换图途中先均匀散布全屏再聚回 */
  const draw = (now) => {
    if (!figs.length) return;
    const tone = mixTone(phase);
    const bg = mixTone(phase).map((v, i) => Math.round(v * 0.09 + [12, 15, 14][i] * 0.91));
    ctx.fillStyle = `rgb(${bg[0]},${bg[1]},${bg[2]})`;
    ctx.fillRect(0, 0, W, H);
    const a = Math.min(figs.length - 1, Math.floor(phase));
    const b = Math.min(figs.length - 1, a + 1);
    const t = phase - a;
    /* 图形锚点随相位在左右两个区域间插值横移（与文字对调），粒子散开途中完成换位 */
    const anchorT = still ? (t >= 0.5 ? 1 : 0) : ease(Math.min(1, Math.max(0, t / 0.8)));
    const gxCur = gxA.length ? gxA[a] + (gxA[b] - gxA[a]) * anchorT : W * 0.5;

    /* 氛围：四团大范围色斑几乎铺满画面（图形后主光斑 + 本屏色×2 + 下一屏色对角）缓慢漂移，
       背景有了色彩空间而不是单点光源（reduced-motion 时静止） */
    const tm = still ? 0 : (now || 0) / 1000;
    const next = mixTone(Math.min(phase + 1, TONES.length - 1));
    const dr = (p, amp) => (still ? 0 : Math.sin(tm * 0.05 + p) * amp);
    blob(gxCur + dr(0.3, 26), gy + dr(1.1, 20), Math.max(size * 2, H * 0.6), tone, 0.2);
    blob(gxCur - W * 0.22 + dr(2.2, 22), gy + H * 0.22 + dr(3.1, 18), H * 0.85, tone, 0.1);
    blob(W * 0.12 + dr(4.0, 20), H * 0.82 + dr(5.2, 16), H * 0.9, next, 0.09);
    blob(W - gxCur + dr(6.1, 18), H * 0.16 + dr(7.3, 14), H * 0.78, tone, 0.07);

    /* 星座网：节点缓慢漂移，近邻之间牵起随距离淡出的细线——背景有可辨的图案结构 */
    const linkR = Math.min(W, H) * 0.22;
    const pts = plex.map((n) => [
      n.x * W + Math.sin(tm * 0.05 * n.sp + n.p) * 16,
      n.y * H + Math.cos(tm * 0.04 * n.sp + n.p * 1.3) * 12,
    ]);
    const tcol = `rgba(${Math.round(tone[0])},${Math.round(tone[1])},${Math.round(tone[2])},1)`;
    /* 当前屏文字列所在的纵带：穿过去的连线淡化，别在字底下拉线 */
    const tx0 = (a % 2 === 0 ? 0.06 : 0.55) * W;
    const tx1 = tx0 + W * 0.39;
    const inTextBand = (p) => p[0] > tx0 && p[0] < tx1 && p[1] > H * 0.15 && p[1] < H * 0.88;
    ctx.lineWidth = 1;
    ctx.strokeStyle = tcol;
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const d = Math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]);
        if (d >= linkR) continue;
        ctx.globalAlpha = (1 - d / linkR) * 0.14 * (inTextBand(pts[i]) || inTextBand(pts[j]) ? 0.3 : 1);
        ctx.beginPath();
        ctx.moveTo(pts[i][0], pts[i][1]);
        ctx.lineTo(pts[j][0], pts[j][1]);
        ctx.stroke();
      }
    }
    ctx.fillStyle = tcol;
    plex.forEach((n, i) => {
      ctx.globalAlpha = 0.12 + 0.08 * n.sp;
      ctx.beginPath();
      ctx.arc(pts[i][0], pts[i][1], n.r, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.globalAlpha = 1;

    /* 星尘三层：远景细点 / 中景点 / 近景 bokeh 软斑，漂移速度不同带出纵深 */
    for (const d of dust) {
      const x = d.x * W + Math.sin(tm * 0.12 + d.p) * 18 * d.sp;
      const y = d.y * H + Math.cos(tm * 0.09 + d.p * 1.7) * 14 * d.sp;
      const tw = still ? 0.8 : 0.55 + 0.45 * Math.sin(tm * d.w + d.p);
      ctx.globalAlpha = d.a * tw;
      if (d.soft) {
        blob(x, y, d.r, tone, 1);
        ctx.globalAlpha = 1;
        continue;
      }
      ctx.fillStyle = `rgb(${Math.round(tone[0])},${Math.round(tone[1])},${Math.round(tone[2])})`;
      ctx.beginPath();
      ctx.arc(x, y, d.r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;

    /* 暗角：四周压暗，视线聚到画面中部（DOM 文字在 canvas 之上，不受影响） */
    const vg = ctx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.36, W / 2, H / 2, Math.hypot(W, H) / 2);
    vg.addColorStop(0, "rgba(0,0,0,0)");
    vg.addColorStop(1, "rgba(0,0,0,0.18)");
    ctx.fillStyle = vg;
    ctx.fillRect(0, 0, W, H);

    const A = still && t >= 0.5 ? figs[b] : figs[a];
    const B = still ? A : figs[b];
    const fxA = still ? null : animsOf(a, tm);
    const fxB = still ? null : animsOf(b, tm);
    /* 05 点亮的节点：环内填成实心并发光（实心核 + 柔光晕，画在环粒子下面） */
    const drawFills = (fx) => {
      if (!fx) return;
      for (const nm2 of Object.keys(fx)) {
        const f2 = fx[nm2];
        if (f2.fill && f2.light > 0) {
          const fxp = gxCur + f2.fill[0] * size;
          const fyp = gy + f2.fill[1] * size;
          const fr = f2.fill[2] * size;
          blob(fxp, fyp, fr * 2.6, tone, 0.3 * f2.light);
          blob(fxp, fyp, fr * 0.95, tone, 0.85 * f2.light);
        }
      }
    };
    drawFills(fxA);
    drawFills(fxB);
    for (let i = 0; i < N; i++) {
      const f = i / N;
      const local = still ? (t >= 0.5 ? 1 : 0) : ease(Math.min(1, Math.max(0, (t - f * 0.35) / 0.65)));
      const ax = A.xy[i * 2]; const ay = A.xy[i * 2 + 1];
      const bx = B.xy[i * 2]; const by = B.xy[i * 2 + 1];
      const x = ax + (bx - ax) * local;
      const y = ay + (by - ay) * local;
      /* 聚形后动画：主导图形里带分组的粒子做 旋转（问号/罗盘/时针）/ 向里推（门）/
         弧线扫过显出（进度弧）/ 形变换色（待办圈→对勾）/ 依次点亮（树节点） */
      let xx = x; let yy = y; let lightK = 0; let recolor = false; let cut = false; let revealK = 1;
      const dom = local < 0.5 ? A : B;
      const domIdx = local < 0.5 ? a : b;
      const fx = domIdx === a ? fxA : fxB;
      if (fx && dom.pid && dom.pid[i]) {
        const f2 = fx[dom.pnames[dom.pid[i] - 1]];
        if (f2) {
          if (f2.ca !== undefined) {
            const dx0 = xx - f2.px; const dy0 = yy - f2.py;
            xx = f2.px + dx0 * f2.ca - dy0 * f2.sa;
            yy = f2.py + dx0 * f2.sa + dy0 * f2.ca;
          } else if (f2.hx !== undefined) {
            /* 绿门向里推：绕左铰链竖轴旋转 + 透视——远端向铰链收拢、向门高中线收缩 */
            const u0 = Math.min(1.2, Math.max(0, (xx - f2.hx) / f2.w));
            const p = 1 / (1 + u0 * f2.w * f2.si * f2.pf);
            xx = f2.hx + u0 * f2.w * f2.co * p;
            yy = f2.yc + (yy - f2.yc) * p;
          } else if (f2.sweep !== undefined) {
            const aP = Math.atan2(yy - f2.sy, xx - f2.sx);
            if ((aP - f2.from + Math.PI * 2.001) % (Math.PI * 2) > f2.sweep) cut = true;
          } else if (f2.morph && dom.morph) {
            xx += (dom.morph[i * 2] - xx) * f2.k;
            yy += (dom.morph[i * 2 + 1] - yy) * f2.k;
            if (f2.k > 0.5) recolor = true;
          } else if (f2.light > 0) {
            lightK = f2.light;
          } else if (f2.reveal !== undefined) {
            revealK = f2.reveal; // 门内路面随开门渐显
          }
        }
      }
      /* 横向散布：细粒子沿笔画法线铺开成粗带（±2.6×dot），转场时法线归零收成细线飞走 */
      const lat = (unitHash(i, 47) * 2 - 1) * 2.6 * dot;
      const nxv = A.nx[i] + (B.nx[i] - A.nx[i]) * local;
      const nyv = A.ny[i] + (B.ny[i] - A.ny[i]) * local;
      let px = gxCur + xx * size + nxv * lat;
      let py = gy + yy * size + nyv * lat;
      const lift = Math.sin(Math.PI * local); // 散开程度：0 聚成图形、1 均匀铺满全屏
      if (lift > 0.001) {
        px += (scatter[i][0] - px) * lift;
        py += (scatter[i][1] - py) * lift;
      }
      if (intro < 1) { // 开场：从全屏散布的暗点聚成首图
        const k2 = Math.min(1, Math.max(0, (intro - f * 0.3) / 0.7));
        px += (scatter[i][0] - px) * (1 - k2);
        py += (scatter[i][1] - py) * (1 - k2);
      }
      const vis = A.vis[i] + (B.vis[i] - A.vis[i]) * local;
      const focus = 1 - lift; // 聚形度：散开暗而细，聚形亮而粗，聚拢完成时最强
      let alpha = vis * (0.28 + 0.72 * Math.pow(focus, 1.4)) * Math.min(1, intro * 1.4) * revealK;
      if (cut || alpha < 0.03) continue; // 进度弧只显出指针扫过的部分；门内路面随开门渐显
      let spr = (local < 0.5 ? A.acc[i] : B.acc[i]) ? (local < 0.5 ? SPRS[a] : SPRS[b]) : SPR_INK;
      if (recolor) spr = SPRS[domIdx]; // 待办圈形变过半后换成本屏主题色
      /* 背景回声：同一图形放大 2.1 倍铺在画面中心后面，淡而可辨的大轮廓（每 3 颗画 1 颗） */
      if (i % 3 === 0) {
        const gpx = W / 2 + x * size * 2.1;
        const gpy = H / 2 + y * size * 2.1;
        const gs = dot * 3;
        ctx.globalAlpha = alpha * 0.15;
        ctx.drawImage(spr, gpx - gs, gpy - gs, gs * 2, gs * 2);
      }
      const jit = 0.75 + unitHash(i, 17) * 0.6; // 粒径抖动：大小参差，聚形后不是均匀的「灯管」
      let r = dot * (0.55 + 1.05 * focus) * jit;
      if (lightK) { // 树节点依次点亮：变亮变大，点亮的瞬间鼓一下再落定
        alpha = Math.min(1, alpha * (1 + 0.8 * lightK));
        r *= 1 + 0.35 * lightK + Math.sin(lightK * Math.PI) * 0.55;
      }
      const s = r * 2.2; // 光晕收小：亮在芯、不在晕
      ctx.globalAlpha = alpha;
      ctx.drawImage(spr, px - s, py - s, s * 2, s * 2);
    }
    ctx.globalAlpha = 1;

    /* 胶片颗粒：静态噪点以 overlay 叠全屏——亮部见纹理、暗部保持干净 */
    if (grainPat) {
      ctx.globalCompositeOperation = "overlay";
      ctx.globalAlpha = 0.08;
      ctx.fillStyle = grainPat;
      ctx.fillRect(0, 0, W, H);
      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = 1;
    }
  };

  const sections = [...scroller.querySelectorAll(".snap")];

  const syncRail = () => {
    const active = Math.min(figs.length - 1, Math.round(phase));
    /* 文字点缀（kicker/大编号/滚动提示线/进度条）的 --tone 随屏换主题色、步号随屏跳；
       换屏发生在文字淡出的间隙，跳变不会被看见 */
    if (active !== lastActive) {
      lastActive = active;
      const tn = TONES[active];
      (canvas.parentElement || canvas).style.setProperty("--tone", `rgb(${tn[0]},${tn[1]},${tn[2]})`);
      if (stepCur) stepCur.textContent = String(active).padStart(2, "0");
    }
    /* 左缘纵向进度轨道随换屏相位连续生长 */
    if (stepFill) {
      stepFill.style.height = `${Math.min(1, phase / Math.max(1, figs.length - 1)) * 100}%`;
    }
    /* 文字钉在视口不动，只按相位渐显渐隐：行进后段（聚形近完成）渐入，
       开始滚向下一步就渐出；首屏等开场粒子聚完再出现 */
    sections.forEach((sec, i) => {
      const d = phase - i;
      let vis;
      if (still) vis = d > -0.5 && d < 0.5 ? 1 : 0;
      else if (d <= -0.36 || d >= 0.18) vis = 0;
      else if (d < -0.03) vis = (d + 0.36) / 0.33;
      else if (d <= 0.02) vis = 1;
      else vis = 1 - (d - 0.02) / 0.16;
      if (i === 0) vis *= Math.min(1, Math.max(0, (intro - 0.5) / 0.4));
      sec.style.opacity = String(Math.min(1, Math.max(0, vis)));
      sec.classList.toggle("lit", vis > 0.5);
    });
  };

  /* 一次滚轮/滑动/按键 = 走一整段剧本：粒子散开聚形换位 → 文字两段进场 →
     聚形后动画 → 亮「滚动以继续」提示；期间锁输入，不吃连续滚动 */
  const go = (n) => {
    const next = Math.min(figs.length - 1, Math.max(0, n));
    if (next === step && !busy) return;
    if (busy || intro < 1) {
      if (intro >= 1) queued = Math.sign(n - step);
      return;
    }
    queued = 0;
    busy = true;
    actCur = 0;
    actStart = 0;
    phaseFrom = phase;
    transStart = 0;
    target = next;
    hint(false);
    wake();
  };

  const tick = (now) => {
    raf = 0;
    if (!canvas.isConnected) return;
    let moving = false;
    if (intro < 1) {
      const boot = document.getElementById("boot");
      const bootGone = !boot || boot.classList.contains("out");
      if (bootGone) {
        if (!introStart) introStart = now;
        intro = Math.min(1, (now - introStart) / 900);
      }
      moving = true;
    }
    if (busy && intro >= 1) { // 开场聚形先走完，剧本才开始——00 屏的过程要看得见
      if (!transStart) transStart = now;
      const kk = still ? 1 : Math.min(1, (now - transStart) / SWAP_MS);
      const ee = kk * kk * (3 - 2 * kk); // smoothstep：缓起缓收，全程可见的运动
      phase = phaseFrom + (target - phaseFrom) * ee;
      moving = true;
      /* 文字进场刚收尾（差 0.02 相位）就起播本屏动画——提示与动画同时亮、输入同时解锁 */
      if (target - phase <= 0.02) {
        phase = target;
        step = target;
        syncSkip();
        if (!actStart) {
          actStart = now;
          if (queued) {
            const q = queued;
            queued = 0;
            busy = false;
            go(step + q);
          } else {
            busy = false;
            hint(true);
          }
        }
      }
    }
    if (!busy && actStart && actCur < 1) { // 动画与收尾并行，播完转入空闲心跳
      actCur = still ? 1 : Math.min(1, (now - actStart) / 900);
      moving = true;
    }
    draw(now);
    syncRail();
    if (moving || busy) raf = requestAnimationFrame(tick);
    else if (!still && dust.length) {
      idleT = setTimeout(() => { idleT = 0; raf = requestAnimationFrame(tick); }, 80);
    }
  };
  const wake = () => {
    if (idleT) { clearTimeout(idleT); idleT = 0; }
    if (!raf) raf = requestAnimationFrame(tick);
  };
  const onResize = () => { build(); wake(); };

  /* 步进输入：滚轮（阈值 + busy 锁）、触摸滑动、方向键/翻页键/空格 */
  const onWheel = (e) => {
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
    e.preventDefault();
    if (e.timeStamp - lastWheelT < 200) return;
    lastWheelT = e.timeStamp;
    if (e.deltaY > 12) go(step + 1);
    else if (e.deltaY < -12) go(step - 1);
  };
  let touchY = null;
  const onTouchStart = (e) => { touchY = e.touches[0].clientY; };
  const onTouchMove = (e) => { e.preventDefault(); };
  const onTouchEnd = (e) => {
    if (touchY == null) return;
    const d = touchY - e.changedTouches[0].clientY;
    touchY = null;
    if (d > 46) go(step + 1);
    else if (d < -46) go(step - 1);
  };
  const onKey = (e) => {
    if (e.target && e.target.closest && e.target.closest("button, input, textarea")) return;
    if (e.repeat) return;
    if (e.key === "ArrowDown" || e.key === "PageDown" || e.key === " ") { e.preventDefault(); go(step + 1); }
    else if (e.key === "ArrowUp" || e.key === "PageUp") { e.preventDefault(); go(step - 1); }
    else if (e.key === "Home") { e.preventDefault(); go(0); }
    else if (e.key === "End") { e.preventDefault(); go(figs.length - 1); }
  };

  build();
  target = 0;
  phase = 0;
  step = 0;
  window.addEventListener("wheel", onWheel, { passive: false });
  window.addEventListener("touchstart", onTouchStart, { passive: true });
  window.addEventListener("touchmove", onTouchMove, { passive: false });
  window.addEventListener("touchend", onTouchEnd, { passive: true });
  window.addEventListener("keydown", onKey);
  window.addEventListener("resize", onResize);
  // 衬线字体到位后行高会变：重新量内容高度再收敛钉位（采样带 unitHash，是确定性的，重建无跳变）
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => { if (canvas.isConnected) { build(); wake(); } });
  }
  wake();

  return () => {
    cancelAnimationFrame(raf);
    raf = 0;
    if (idleT) clearTimeout(idleT);
    idleT = 0;
    window.removeEventListener("wheel", onWheel);
    window.removeEventListener("touchstart", onTouchStart);
    window.removeEventListener("touchmove", onTouchMove);
    window.removeEventListener("touchend", onTouchEnd);
    window.removeEventListener("keydown", onKey);
    window.removeEventListener("resize", onResize);
  };
}

function workspaceHead(title, lead) {
  const h = el("header", "ws-head");
  h.appendChild(el("h2", "", title));
  if (lead) h.appendChild(el("p", "", lead));
  return h;
}

const ROOM_GUIDES = {
  workbench: {
    key: "rg_guide_workbench",
    steps: ["先看题目要你交什么。", "做完写在下面，按三条对一下。", "卡住再点「要提示」，标准与反馈可以后看。"],
  },
  projects: {
    key: "rg_guide_projects",
    steps: ["这一栏还在做。先用今日和研读。"],
  },
  read: {
    key: "rg_guide_read",
    steps: ["挑一篇能打开的论文。", "先读原文，再在阅读卡里写；原句从正文里圈。", "过线之后再看矩阵。一张卡也能看。"],
  },
  position: {
    key: "rg_guide_position",
    steps: ["这一栏还在做。先用今日和研读。"],
  },
};

function guideSeen(key) {
  try { return localStorage.getItem(key) === "1"; } catch (_) { return false; }
}

function setGuideSeen(key, on) {
  try {
    if (on) localStorage.setItem(key, "1");
    else localStorage.removeItem(key);
  } catch (_) { /* 隐私模式写不了就每次再显示 */ }
}

function roomGuide(room, hasProgress) {
  const spec = ROOM_GUIDES[room];
  const box = el("aside", "room-guide");
  if (!spec) return box;
  const paint = () => {
    box.innerHTML = "";
    if (hasProgress || guideSeen(spec.key)) {
      box.classList.add("is-hidden");
      const again = el("button", "linkish room-guide-again", "为什么是这几步");
      again.type = "button";
      again.onclick = () => { setGuideSeen(spec.key, false); paint(); };
      box.appendChild(again);
      return;
    }
    box.classList.remove("is-hidden");
    const top = el("div", "room-guide-top");
    top.appendChild(el("p", "room-guide-kicker", "这一页先做什么"));
    const hide = el("button", "linkish", "知道了");
    hide.type = "button";
    hide.onclick = () => { setGuideSeen(spec.key, true); paint(); };
    top.appendChild(hide);
    const ol = el("ol");
    spec.steps.forEach((s) => ol.appendChild(el("li", "", s)));
    box.append(top, ol);
  };
  paint();
  return box;
}

/* ---------- 账号：微信登录（公众号发数字）、访客、隐私说明 ---------- */

/* 换人（退出、删号、登录过期、登进另一个账号）一律整页重载：S 里有几十个字段、对话视图里有进行中的流，
   逐个清容易漏，上一个人没发的草稿就会出现在下一个人的表单里。要去哪一页、要提示什么，经 sessionStorage 带过去。 */
const PARK_KEY = "rg_parked";  // 停在这台浏览器上的另一个会话，见 parkedSession

function reloadInto(view, msg) {
  try {
    sessionStorage.setItem("rg_next_view", view || "home");
    if (msg) sessionStorage.setItem("rg_toast", msg);
  } catch (_) { /* 存不了就回首页、不提示 */ }
  location.reload();
}

/* 会话写进这台浏览器：刷新、新标签页照样是同一个人（30 天不用才失效）。 */
function persistSession() {
  try {
    localStorage.setItem("rg_uid", S.uid);
    localStorage.setItem("rg_token", S.token);
    localStorage.setItem("rg_nick", S.nickname);
  } catch (_) { /* 存不了就只在这一页有效 */ }
}

/* 记下会话。原来是另一个人（uid 变了）就返回 true，并且已经开始重载：调用方要立刻停手。 */
function setSession(r, nextView, msg) {
  ++directionEpoch;
  const switching = !!S.uid && r.uid !== S.uid;
  S.uid = r.uid;
  if (r.token) S.token = r.token;
  S.nickname = r.nickname || S.nickname;
  S.wechat = !!r.wechat;
  S.guest = !!r.guest;
  S.username = r.username || "";
  S.hasLlmKey = !!r.has_llm_key;
  S.llmKeyTail = r.llm_key_tail || "";
  S.llmKeyInvalid = !!r.llm_key_invalid;  // 换过 QIYAN_SECRET 后解不开的个人密钥：提示重存（16-F03）
  if (r.entered !== undefined) S.entered = !!r.entered;  // 进入按人记在服务端（上游 w72），刷新后照它恢复
  persistSession();
  if (switching) {
    // 上一个人的草稿不留给这个人；这个人自己的、停在这台浏览器上的另一个号（parkedSession）的留着
    let parked = "";
    try { parked = (JSON.parse(localStorage.getItem(PARK_KEY) || "null") || {}).uid || ""; } catch (_) { /* 坏数据当没有 */ }
    clearLocalDrafts(["rg_uid", "rg_token", "rg_nick", PARK_KEY], [S.uid, parked]);
    reloadInto(nextView || "today", msg);
    return true;
  }
  const chip = document.getElementById("userNickname");
  if (chip) chip.textContent = S.guest ? `${S.nickname} · 访客` : S.nickname;
  applyLlmPill(S.llmInfo || { enabled: false });  // 能找回的账号看到的是「我的 DeepSeek」，访客看到的还是服务器的模型
  return false;
}

/* 这台浏览器里存的个人草稿（任务草稿、阅读卡草稿、教程进度）。退出、删号、换人时清掉：
   公用电脑上，上一个人没交的阅读卡草稿原来会出现在下一个人的表单里。keep 是要留下的键（界面偏好、新登录的身份）。 */
function clearLocalDrafts(keep, owners) {
  try {
    const drop = [];
    const mine = (k) => (owners || []).some((u) => u && k.includes(u));  // 草稿的键都带着 uid
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i);
      if (k && k.startsWith("rg_") && k !== "rg_kit" && !(keep || []).includes(k) && !mine(k)) drop.push(k);
    }
    drop.forEach((k) => localStorage.removeItem(k));
  } catch (_) { /* 无痕模式等 */ }
}

/* purge：连这台浏览器里这个人的草稿一起清。只在明确退出、删号时清；登录过期（401）不清——
   草稿的键带着 uid，别人看不到，同一个人重新登录还能接着写。 */
function clearSession(purge) {
  ++directionEpoch;
  S.uid = ""; S.token = ""; S.wechat = false; S.guest = true;
  S.username = ""; S.hasLlmKey = false; S.llmKeyTail = ""; S.llmKeyInvalid = false;
  S.entered = false;
  S.myDir = undefined; S.portraitId = ""; S.portraitName = ""; S.mePortrait = "";
  S.kitId = "";
  try { localStorage.removeItem("rg_kit"); } catch (_) { /* 无痕模式等 */ }
  if (purge) clearLocalDrafts();
  else ["rg_uid", "rg_token", "rg_nick"].forEach((k) => {
    try { localStorage.removeItem(k); } catch (_) { /* 无痕模式等 */ }
  });
}

function signedOut(msg) {
  clearSession();
  reloadInto("login", msg);
}

/* gate=true：老用户或说明更新后必须先同意才能继续用 */
function openPrivacy(gate) {
  document.querySelectorAll(".connect-mask").forEach((n) => n.remove());
  const mask = el("div", "connect-mask");
  const box = el("div", "connect-box privacy-box");
  box.appendChild(el("p", "hero-kicker", "PRIVACY"));
  box.appendChild(el("h3", "", gate ? "继续之前，看一眼我们存什么" : "我们存什么"));
  box.appendChild(el("div", "privacy-body", PRIVACY_HTML));
  const acts = el("div", "connect-actions");
  if (gate) {
    const out = el("button", "btn secondary", "退出登录");
    out.type = "button";
    out.onclick = async () => { mask.remove(); await logout(); };
    const ok = el("button", "btn", "同意并继续");
    ok.type = "button";
    ok.onclick = async () => {
      try {
        await api("POST", "/api/auth/consent", { version: S.auth ? S.auth.privacy_version : "" });
        mask.remove();
      } catch (e) { toast(e.message); }
    };
    acts.append(out, ok);
  } else {
    const ok = el("button", "btn", "知道了");
    ok.type = "button";
    ok.onclick = () => mask.remove();
    acts.appendChild(ok);
    mask.addEventListener("click", (e) => { if (e.target === mask) mask.remove(); });
  }
  box.appendChild(acts);
  mask.appendChild(box);
  document.body.appendChild(mask);
}

function consentRow() {
  const row = el("label", "consent-row");
  const box = el("input");
  box.type = "checkbox";
  const read = el("button", "linkish", "我们存什么");
  read.type = "button";
  read.onclick = (e) => { e.preventDefault(); openPrivacy(false); };
  row.append(box, document.createTextNode("我读过"), read, document.createTextNode("，同意按这个方式保存我的记录"));
  return { row, ok: () => box.checked };
}

let wxPoll = 0;  // 微信登录的轮询；离开登录页或换数字时停掉

let wxAttempt = 0;  // 第几次微信登录尝试；旧尝试的响应回来时编号对不上就丢掉

function stopWxPoll() {
  clearTimeout(wxPoll);
  wxPoll = 0;
  wxAttempt += 1;  // 清掉定时器拦不住已经发出去的请求：靠编号让它回来时作废
}

/* 微信登录：网页拿一个 6 位数字，学生在公众号里发它，网页轮询到了就登进去 */
async function startWechat(box, opts) {
  stopWxPoll();
  const attempt = wxAttempt;
  const live = () => attempt === wxAttempt && box.isConnected;
  box.innerHTML = "";
  box.hidden = false;
  let r;
  try {
    r = await api("POST", "/api/auth/wechat/start", { nickname: opts.nickname, consent: true });
  } catch (e) { if (live()) { box.hidden = true; toast(e.message); } return; }
  if (!live()) return;
  const name = r.account_name ? `「${esc(r.account_name)}」` : "启研";
  if (r.qr_url) {
    const qr = el("img", "wx-qr");
    qr.src = r.qr_url;
    qr.alt = "公众号二维码";
    box.appendChild(qr);
  }
  const steps = el("ol", "wx-steps");
  steps.appendChild(el("li", "", `微信扫码关注${name}公众号（已经关注的，直接打开它）`));
  const li = el("li", "", "在公众号里发送这个数字：");
  li.appendChild(el("b", "wx-code", `${r.code.slice(0, 3)} ${r.code.slice(3)}`));
  steps.appendChild(li);
  box.appendChild(steps);
  const status = el("p", "wx-status", "等你在微信里发送…");
  box.appendChild(status);
  const acts = el("div", "account-actions");
  const again = el("button", "btn small secondary", "换一个数字");
  again.type = "button";
  again.onclick = () => startWechat(box, opts);
  acts.appendChild(again);
  if (S.auth && S.auth.dev) {  // 本机开发没有公众号：假装从微信发了这个数字
    const dev = el("button", "btn small ghost", "（开发）模拟微信发送");
    dev.type = "button";
    dev.onclick = () => api("POST", "/api/auth/wechat/dev-send", { code: r.code }).catch((e) => toast(e.message));
    acts.appendChild(dev);
  }
  box.appendChild(acts);

  const deadline = Date.now() + r.expires_in * 1000;
  const tick = async () => {
    if (!live()) return;  // 换了数字、走了访客、离开了登录页
    let res;
    try {
      res = await api("POST", "/api/auth/wechat/poll", { ticket: r.ticket });
    } catch (e) {
      if (live()) status.textContent = `${e.message}`;
      return;  // 过期或用过了：不再轮询，等学生点「换一个数字」
    }
    if (!live()) return;  // 请求在路上时用户已经换了别的登录方式：这个结果不能再生效
    if (res.pending) {
      const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
      status.textContent = `等你在微信里发送…（还剩 ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}）`;
      wxPoll = setTimeout(tick, 2000);
      return;
    }
    stopWxPoll();
    if (res.left_guest && S.uid && S.guest && res.uid !== S.uid) { askLeaveGuest(box, res); return; }
    const previous = S.token;  // 采用新令牌之后再吊销旧的；被当成过期尝试丢掉的结果不会走到这里
    if (previous && previous !== res.token) revokeToken(previous);
    if (setSession(res, "today")) return;
    if (res.bound) { toast("绑定好了，记录都在"); setView("me"); return; }
    await afterEnter();
  };
  wxPoll = setTimeout(tick, 2000);
}

/* 访客来绑微信，这个微信却已经有账号：访客的记录不会合并过去。先问；要登进微信账号，访客号的会话停在这台浏览器上，
   「记录」页可以切回去。原来直接切走、吊销访客令牌、清掉草稿，访客的记录再也进不去（Codex 复现）。 */
function askLeaveGuest(box, res) {
  box.innerHTML = "";
  box.appendChild(el("p", "wx-status",
    `这个微信已经有账号「${esc(res.nickname)}」了。访客号「${esc(S.nickname)}」的记录不会合并过去。`));
  const acts = el("div", "account-actions");
  const go = el("button", "btn small", "登进微信账号");
  const stay = el("button", "btn small secondary", "留在访客号");
  go.type = "button"; stay.type = "button";
  go.onclick = () => {
    parkCurrent(res.uid);
    setSession(res, "me", "已登进微信账号。访客号的记录还在，在这一页可以切回去看");
  };
  stay.onclick = () => {
    revokeToken(res.token);
    box.hidden = true;
    toast("还在访客号里");
  };
  acts.append(go, stay);
  box.appendChild(acts);
}

/* 停在这台浏览器上的另一个会话（目前只有上面这种情况会停）。只给停放它的那个账号看：
   公用电脑上，下一个人登进自己的号看不到、也切不过去。退出、删号时跟着清掉。键 PARK_KEY 在 reloadInto 上面。 */
function parkedSession() {
  try {
    const p = JSON.parse(localStorage.getItem(PARK_KEY) || "null");
    return p && p.owner === S.uid && p.uid && p.token && p.uid !== S.uid ? p : null;
  } catch (_) { return null; }
}
function parkCurrent(owner) {
  try {
    localStorage.setItem(PARK_KEY, JSON.stringify({
      uid: S.uid, token: S.token, nickname: S.nickname, guest: S.guest, wechat: S.wechat, owner }));
  } catch (_) { /* 存不了：切过去之后就切不回来了，和原来一样 */ }
}

function renderLogin() {
  $nav.hidden = true; $header.hidden = true;
  $app.innerHTML = "";
  stopWxPoll();
  const binding = !!(S.uid && S.guest);  // 访客来绑微信：start 时带着访客会话，记录跟着走
  const wxOn = !S.auth || S.auth.wechat_login;
  const hero = el("section", "hero stagger");
  hero.appendChild(el("p", "hero-kicker", binding ? "启研 · 绑定微信" : "启研 · 登录"));
  hero.appendChild(el("h2", "", binding ? "绑定微信" : "登录"));
  hero.appendChild(el("p", "hero-lead", binding
    ? "绑定后换设备、清了浏览器也能接着用，访客期间的记录都会带过去。"
    : "换手机、清了浏览器也能接着用。微信不用密码、不要手机号；没有微信的设备用用户名和密码。"));
  const consent = consentRow();
  const need = () => { if (consent.ok()) return true; toast("先勾选同意隐私说明"); return false; };
  const onEnter = (btn) => (e) => { if (e.key === "Enter" && !e.isComposing) btn.click(); };

  // 微信：昵称（选填）+ 开一次登录，等学生在公众号里发数字
  const wxStep = el("div", "login-step");
  const nick = el("input", "login-nick");
  nick.placeholder = "怎么称呼你（选填，新账号用）"; nick.maxLength = 24;
  nick.setAttribute("aria-label", "昵称");
  const go = el("button", "btn", binding ? "绑定微信" : "用微信登录");
  go.type = "button";
  const wxBox = el("div", "wx-box");
  wxBox.hidden = true;
  go.onclick = () => { if (need()) startWechat(wxBox, { nickname: nick.value.trim() }); };
  if (!binding) wxStep.appendChild(nick);
  wxStep.append(go, wxBox);

  // 用户名密码（上游 w68 加的注册登录）：和微信并列。登录不重复要同意——隐私说明更新过的，登进去后闸门会再问一次
  const signinStep = el("div", "login-step login-body");
  const suser = el("input"); suser.placeholder = "用户名"; suser.autocomplete = "username"; suser.maxLength = 24;
  suser.setAttribute("aria-label", "用户名");
  const spass = el("input"); spass.type = "password"; spass.placeholder = "密码"; spass.autocomplete = "current-password";
  spass.setAttribute("aria-label", "密码");
  const sbtn = el("button", "btn", "登录");
  sbtn.type = "button";
  sbtn.onclick = async () => {
    if (!suser.value.trim() || !spass.value) { toast("先填用户名和密码"); return; }
    sbtn.disabled = true; sbtn.textContent = "登录中…";
    stopWxPoll();  // 走密码就作废正在等的微信登录，免得它的结果回来盖掉这次会话
    try {
      const r = await api("POST", "/api/auth/signin", { username: suser.value.trim(), password: spass.value });
      const previous = S.token;  // 访客登进自己的账号：采用新令牌之后再吊销访客的
      if (previous && previous !== r.token) revokeToken(previous);
      if (setSession(r, "today")) return;
      await afterEnter();
      if (!r.consent_ok) openPrivacy(true);
    } catch (e) { toast(e.message); sbtn.disabled = false; sbtn.textContent = "登录"; }
  };
  spass.addEventListener("keydown", onEnter(sbtn));
  signinStep.append(suser, spass, sbtn);

  const registerStep = el("div", "login-step login-body");
  const ruser = el("input"); ruser.placeholder = "用户名，例如 xiaobei（2–24 个字母、数字、下划线）"; ruser.autocomplete = "username"; ruser.maxLength = 24;
  ruser.setAttribute("aria-label", "用户名");
  const rnick = el("input"); rnick.placeholder = "怎么称呼你（可与用户名相同）"; rnick.maxLength = 24;
  rnick.setAttribute("aria-label", "昵称");
  const rpass = el("input"); rpass.type = "password"; rpass.placeholder = "密码，至少 8 位"; rpass.autocomplete = "new-password";
  rpass.setAttribute("aria-label", "密码");
  const rbtn = el("button", "btn", "注册并进入");
  rbtn.type = "button";
  rbtn.onclick = async () => {
    if (!need()) return;
    if (!ruser.value.trim() || rpass.value.length < 8) { toast("用户名和至少 8 位密码"); return; }
    rbtn.disabled = true; rbtn.textContent = "注册中…";
    stopWxPoll();
    try {
      const r = await api("POST", "/api/auth/register", {
        username: ruser.value.trim(), password: rpass.value,
        nickname: rnick.value.trim() || ruser.value.trim(), consent: true,
      });
      if (setSession(r, "dialogue")) return;
      await afterEnter();
    } catch (e) { toast(e.message); rbtn.disabled = false; rbtn.textContent = "注册并进入"; }
  };
  rpass.addEventListener("keydown", onEnter(rbtn));
  registerStep.append(ruser, rnick, rpass, rbtn);

  // 访客：只要昵称，记录只能在这台浏览器里找回；之后可以绑微信或设用户名密码
  const guestBox = el("div", "login-step");
  const guestRow = el("div", "login-row");
  const gname = el("input");
  gname.placeholder = "你的昵称，例如：小北"; gname.maxLength = 24;
  gname.setAttribute("aria-label", "访客昵称");
  const gbtn = el("button", "btn", "以访客进入");
  gbtn.type = "button";
  gbtn.onclick = async () => {
    if (!need()) return;
    const n = gname.value.trim();
    if (!n) { toast("先起个昵称吧"); gname.focus(); return; }
    gbtn.disabled = true;
    stopWxPoll();  // 走访客就作废正在等的微信登录，免得它的结果回来盖掉访客会话
    try {
      if (setSession(await api("POST", "/api/auth/login", { nickname: n, consent: true }), "dialogue")) return;
      await afterEnter();
    } catch (e) { toast(e.message); gbtn.disabled = false; }
  };
  gname.addEventListener("keydown", onEnter(gbtn));
  guestRow.append(gname, gbtn);
  guestBox.append(guestRow, el("p", "form-note", "访客的记录只能在这台浏览器里找回，之后可以在「记录」页绑定微信或设用户名密码。"));

  const steps = { wechat: wxStep, signin: signinStep, register: registerStep };
  let mode = wxOn ? "wechat" : "signin";
  const tabs = el("div", "login-tabs");
  const paint = () => {
    tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
    Object.entries(steps).forEach(([k, node]) => { node.hidden = k !== mode; });
    if (mode !== "wechat") { stopWxPoll(); wxBox.hidden = true; }  // 离开微信这一栏就作废正在等的数字
  };
  [["wechat", "微信"], ["signin", "账号登录"], ["register", "注册"]].forEach(([id, label]) => {
    if (id === "wechat" && !wxOn) return;
    const b = el("button", "login-tab", label);
    b.type = "button";
    b.dataset.mode = id;
    b.onclick = () => { mode = id; paint(); };
    tabs.appendChild(b);
  });

  if (binding) {
    hero.append(consent.row, wxStep);
  } else {
    guestBox.hidden = true;
    if (!wxOn) hero.appendChild(el("p", "login-note", "微信登录还没开通：用用户名密码，或先以访客进入。"));
    hero.append(consent.row, tabs, wxStep, signinStep, registerStep, guestBox);
    paint();
  }

  const links = el("div", "hero-links");
  if (!binding) {
    const asGuest = el("button", "linkish", "先不登录，以访客进入");
    asGuest.type = "button";
    asGuest.onclick = () => { guestBox.hidden = false; asGuest.remove(); gname.focus(); };
    links.appendChild(asGuest);
  }
  const link = el("button", "linkish", "服务器共用模型（管理员）");
  link.type = "button";
  link.onclick = () => openConnect();
  const back = el("button", "linkish", binding ? "返回" : "返回首页");
  back.type = "button";
  back.onclick = () => setView(binding ? "me" : "home");
  links.append(link, back);
  hero.appendChild(links);
  $app.appendChild(hero);
}

function confirmLeave() {
  return window.confirm("退出后需要重新登录。确定？");
}

function clearChatDraft() {
  try { if (S.uid) sessionStorage.removeItem("rg_chat_draft_" + S.uid); } catch (_) { /* 无痕模式等 */ }
}

async function logout(next) {
  clearChatDraft();
  stopWxPoll();
  try { await api("POST", "/api/auth/logout"); } catch (_) { /* 会话已经没了也照样退 */ }
  clearSession(true);
  reloadInto(next === "login" ? "login" : "home");
}

function accountPanel() {
  const box = el("section", "panel account-panel");
  box.appendChild(el("h3", "section-label", "账号"));
  const who = S.username
    ? `已登录：${esc(S.username)}（${esc(S.nickname)}）`
    : S.wechat
      ? `已用微信登录：${esc(S.nickname)}`
      : `访客「${esc(S.nickname)}」。记录只在这台浏览器里；绑定微信或设一个用户名密码，换设备也能找回。`;
  box.appendChild(el("p", "panel-sub", who));
  if (S.llmKeyInvalid) {
    box.appendChild(el("p", "panel-sub warn", "你的 DeepSeek 密钥无法解开，请重新保存。"));
  }
  const acts = el("div", "account-actions");
  const add = (label, cls, fn) => {
    const b = el("button", cls, label);
    b.type = "button";
    b.onclick = fn;
    acts.appendChild(b);
  };
  if (S.guest && (!S.auth || S.auth.wechat_login)) add("绑定微信", "btn small", () => setView("login"));
  if (!S.username) add("设用户名密码", "btn small", () => openUpgrade());  // 上游 w68：有了用户名密码，没有微信的设备也能登回来
  if (!S.guest) add("我的 DeepSeek", "btn small", () => openMineKey());
  const parked = parkedSession();
  if (parked) {
    box.appendChild(el("p", "form-note", parked.guest
      ? `这台浏览器上还有访客号「${esc(parked.nickname)}」的记录（绑微信时这个微信已经有账号，没有合并过去）。`
      : `这台浏览器上还登着账号「${esc(parked.nickname)}」。`));
    add(parked.guest ? "切回访客号" : "切回那个账号", "btn small secondary", () => {
      parkCurrent(parked.uid);
      setSession(parked, "me", parked.guest ? "已切回访客号" : "已切回那个账号");
    });
  }
  add("我们存什么", "btn small ghost", () => openPrivacy(false));
  add("导出我的数据", "btn small secondary", async () => {
    const res = await apiFetch("/api/me/export");
    if (!res.ok) { toast("导出失败"); return; }
    downloadBlob(await res.blob(), "启研-我的数据.json");
  });
  add("退出登录", "btn small secondary", async () => {
    if (S.guest && !window.confirm("访客退出后，这些记录就找不回来了（除非先绑定微信或设用户名密码）。确定退出？")) return;
    if (parked && parked.guest && !window.confirm(`退出后，访客号「${parked.nickname}」的记录就找不回来了。确定退出？`)) return;
    if (parked) revokeToken(parked.token);
    await logout();
  });
  add("删除账号", "btn small ghost danger", async () => {
    const back = parked && parked.guest ? `删的只是这个账号；这台浏览器上停着的访客号「${parked.nickname}」不受影响，删完切回它。` : "";
    const typed = window.prompt(`删除后，库里你的所有记录会立刻清掉，不能恢复。${back}确定的话输入「删除」两个字：`);
    if (typed === null) return;
    if (typed.trim() !== "删除") { toast("没有删除"); return; }
    try {
      await api("DELETE", "/api/me");
      if (back) {
        // 停着的访客号是它唯一的凭证：原来跟着一起清掉，访客的记录还在库里却再也进不去（Codex 复现）
        try { localStorage.removeItem(PARK_KEY); } catch (_) { /* 无痕模式等 */ }
        setSession(parked, "me", `账号删了，已切回访客号「${parked.nickname}」`);
        return;
      }
      clearSession(true);
      reloadInto("home", "账号和记录都删了");
    } catch (e) { toast(e.message); }
  });
  box.appendChild(acts);
  return box;
}

/* 访客或微信账号补一个用户名和密码（上游 w68 的 /api/auth/upgrade）：同一个号，换设备也能用密码登回来。 */
function openUpgrade() {
  document.querySelectorAll(".connect-mask").forEach((n) => n.remove());
  const mask = el("div", "connect-mask");
  const box = el("form", "connect-box");
  box.innerHTML = `
    <p class="hero-kicker">ACCOUNT</p>
    <h3>设用户名密码</h3>
    <p class="panel-sub">还是这个账号，记录都在。之后在没有微信的设备上也能用用户名和密码登录。用户名 2–24 个字母、数字、下划线；密码至少 8 位，只存哈希。</p>
    <label>用户名<input name="user" autocomplete="username" maxlength="24" /></label>
    <label>密码<input name="pass" type="password" autocomplete="new-password" /></label>
    <div class="connect-actions">
      <button type="button" class="btn secondary" id="upCancel">取消</button>
      <button type="submit" class="btn" id="upGo">保存</button>
    </div>`;
  mask.appendChild(box);
  document.body.appendChild(mask);
  box.querySelector("#upCancel").onclick = () => mask.remove();
  mask.addEventListener("click", (e) => { if (e.target === mask) mask.remove(); });
  box.onsubmit = async (e) => {
    e.preventDefault();
    const go = box.querySelector("#upGo");
    if (!box.user.value.trim() || box.pass.value.length < 8) { toast("用户名和至少 8 位密码"); return; }
    go.disabled = true; go.textContent = "保存中…";
    try {
      const r = await api("POST", "/api/auth/upgrade", { username: box.user.value.trim(), password: box.pass.value });
      setSession(r);
      mask.remove();
      toast(`好了，之后可以用「${S.username}」登录`);
      if (S.view === "me") setView("me");  // 账号面板上的按钮和说明跟着变
    } catch (err) {
      toast(err.message);
      go.disabled = false; go.textContent = "保存";
    }
  };
}

async function afterEnter() {
  await loadPrefs();
  await ensurePortrait();
  if (!S.entered) {
    try {
      const h = await api("GET", `/api/dialogue/history?uid=${encodeURIComponent(S.uid)}`);
      if (!(h.messages || []).length) {
        await api("POST", "/api/onboard/start", { uid: S.uid }).catch(() => {});
      }
    } catch (_) {
      await api("POST", "/api/onboard/start", { uid: S.uid }).catch(() => {});
    }
    toast(`你好，${S.nickname}`);
  } else {
    await ensurePortrait().catch(() => {});
    toast(`欢迎回来，${S.nickname}`);
  }
  setView(firstView());
}

function openConnect() {
  document.querySelectorAll(".connect-mask").forEach((n) => n.remove());
  const mask = el("div", "connect-mask");
  const box = el("form", "connect-box");
  box.innerHTML = `
    <p class="hero-kicker">MODEL</p>
    <h3>连接模型</h3>
    <p class="panel-sub">OpenAI 兼容接口，改的是整台服务器用的模型。密钥只写在服务器的 .env，不会出现在页面回显里。只有在服务器本机，或填了管理员口令才能改。</p>
    <label>接口地址<input name="base" value="https://api.deepseek.com/v1" /></label>
    <label>API Key<input name="key" type="password" placeholder="sk-…" autocomplete="off" /></label>
    <label>模型名<input name="model" value="deepseek-flash" /></label>
    <label>管理员口令<input name="admin" type="password" placeholder="服务器设了 ADMIN_TOKEN 才需要" autocomplete="off" /></label>
    <div class="connect-actions">
      <button type="button" class="btn secondary" id="connectCancel">取消</button>
      <button type="submit" class="btn" id="connectGo">测试并保存</button>
    </div>`;
  mask.appendChild(box);
  document.body.appendChild(mask);
  box.querySelector("#connectCancel").onclick = () => mask.remove();
  mask.addEventListener("click", (e) => { if (e.target === mask) mask.remove(); });
  box.onsubmit = async (e) => {
    e.preventDefault();
    const go = box.querySelector("#connectGo");
    go.disabled = true; go.textContent = "正在连通…";
    try {
      // 管理员口令走请求头，所以这里不用 api()
      const res = await apiFetch("/api/llm/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Admin-Token": box.admin.value.trim() },
        body: JSON.stringify({ base_url: box.base.value.trim(), api_key: box.key.value.trim(), model: box.model.value.trim() }),
      });
      let r = null;
      try { r = await res.json(); } catch (_) { /* no body */ }
      if (!res.ok) throw new Error((r && r.detail) || `请求失败 (${res.status})`);
      S.llmInfo = { enabled: true, model: r.model };
      applyLlmPill(S.llmInfo);
      toast(`已连接 ${r.model}`);
      mask.remove();
    } catch (err) {
      toast(err.message);
      go.disabled = false; go.textContent = "测试并保存";
    }
  };
}

function privacyBox() {
  const box = el("div", "privacy-box");
  const body = el("div", "privacy-body");
  body.innerHTML = PRIVACY_HTML;
  box.appendChild(body);
  return box;
}

function applyLlmPill(llm) {
  const pill = document.getElementById("llmPill");
  if (!pill) return;
  if (S.uid && !S.guest) {
    pill.hidden = false;
    pill.textContent = S.llmKeyInvalid
      ? "密钥失效，请重存"
      : (S.hasLlmKey ? `我的模型 · ${S.llmKeyTail || "已保存"}` : "我的 DeepSeek");
    pill.classList.toggle("on", !!S.hasLlmKey && !S.llmKeyInvalid);
    pill.onclick = () => openMineKey();
    return;
  }
  pill.hidden = true;
  pill.textContent = "我的 DeepSeek";
  pill.classList.remove("on");
  pill.onclick = null;
}

function openMineKey() {
  if (S.guest) { toast("先注册一个账号，再保存自己的密钥"); return; }
  document.querySelectorAll(".connect-mask").forEach((n) => n.remove());
  const mask = el("div", "connect-mask");
  const box = el("form", "connect-box");
  const saved = S.llmKeyInvalid
    ? "当前密钥无法解开，请重新保存"
    : (S.hasLlmKey ? `已保存 · 末四位 ${esc(S.llmKeyTail)}` : "还没有保存自己的密钥");
  box.innerHTML = `
    <p class="hero-kicker">MINE</p>
    <h3>我的 DeepSeek</h3>
    <p class="panel-sub">${saved}。密钥只存在你的账号下，探测通过才保存，页面上看不到全文。</p>
    <label>接口地址<input name="base" value="https://api.deepseek.com/v1" /></label>
    <label>API Key<input name="key" type="password" placeholder="sk-…" autocomplete="off" /></label>
    <label>模型名<input name="model" value="deepseek-flash" /></label>
    <div class="connect-actions">
      <button type="button" class="btn secondary" id="mineCancel">取消</button>
      ${S.hasLlmKey ? '<button type="button" class="btn ghost" id="mineDel">删除</button>' : ""}
      <button type="submit" class="btn" id="mineGo">测试并保存</button>
    </div>`;
  mask.appendChild(box);
  document.body.appendChild(mask);
  box.querySelector("#mineCancel").onclick = () => mask.remove();
  const del = box.querySelector("#mineDel");
  if (del) del.onclick = async () => {
    try {
      await api("DELETE", "/api/llm/mine");
      S.hasLlmKey = false; S.llmKeyTail = ""; S.llmKeyInvalid = false;
      applyLlmPill({ enabled: false });
      toast("已删除自己的密钥");
      mask.remove();
    } catch (e) { toast(e.message); }
  };
  mask.addEventListener("click", (e) => { if (e.target === mask) mask.remove(); });
  box.onsubmit = async (e) => {
    e.preventDefault();
    const go = box.querySelector("#mineGo");
    go.disabled = true; go.textContent = "正在连通…";
    try {
      const r = await api("POST", "/api/llm/mine", {
        base_url: box.base.value.trim(), api_key: box.key.value.trim(), model: box.model.value.trim(),
      });
      S.hasLlmKey = true; S.llmKeyTail = r.tail || ""; S.llmKeyInvalid = false;
      applyLlmPill({ enabled: true, model: r.model });
      toast("已保存你的 DeepSeek");
      mask.remove();
    } catch (err) {
      toast(err.message);
      go.disabled = false; go.textContent = "测试并保存";
    }
  };
}

/* 右上角按钮显示画像名：自己起的名优先；没起过就按方向生成默认名
   （一个方向 = 方向名；多个 = 最先加的两个，形如 方向1·方向2）。 */
function portraitChipLabel() {
  if (S.portraitName) return S.portraitName;
  const t = trail();
  const names = t.dirs.slice(0, 2).map((id) => dirTitle(id) || id).filter(Boolean);
  return names.join("·");
}

function navCollapsed() {
  try { return localStorage.getItem("rg_nav_collapsed") === "1"; } catch (_) { return false; }
}

function setNavCollapsed(on) {
  try { localStorage.setItem("rg_nav_collapsed", on ? "1" : "0"); } catch (_) { /* 记不住也不挡用 */ }
  applyNavCollapsed();
}

function applyNavCollapsed() {
  const on = navCollapsed();
  document.body.classList.toggle("nav-collapsed", on);
  const exp = document.getElementById("navExpand");
  if (exp) exp.hidden = !on;
}

function paintChrome() {
  const nick = document.getElementById("userNickname");
  if (nick) nick.textContent = S.nickname || "";
  applyNavCollapsed();
  const chip = document.getElementById("dirChip");
  if (!chip) return;
  const name = portraitChipLabel();
  chip.textContent = name || "还没有方向";
  chip.classList.toggle("empty", !name);
}

let menuSeq = 0;  // 菜单每开、关一次加一：画像列表回来时菜单已经关了（或者换了一个），就别再往里塞

function closeUserMenu() {
  menuSeq += 1;
  document.querySelectorAll("#userMenu").forEach((m) => m.remove());
  const btn = document.getElementById("userMenuBtn");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

function accountKind() {
  if (S.username) return "注册账号";
  if (S.wechat) return "微信登录";
  return "访客";
}

async function openUserMenu() {
  const wrap = document.querySelector(".user-menu-wrap");
  const btn = document.getElementById("userMenuBtn");
  if (!wrap || !btn || !S.uid) return;
  if (document.getElementById("userMenu")) { closeUserMenu(); return; }
  const mine = ++menuSeq;
  btn.setAttribute("aria-expanded", "true");
  const menu = el("div", "user-menu");
  menu.id = "userMenu";
  menu.setAttribute("role", "menu");
  menu.appendChild(el("p", "user-menu-kicker", `${esc(S.nickname || "未命名")} · ${accountKind()}`));
  const go = (label, fn) => {
    const b = el("button", "user-menu-item", label);
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.onclick = () => { closeUserMenu(); fn(); };
    return b;
  };
  menu.appendChild(go("我的", () => setView("me")));
  if (!S.guest) menu.appendChild(go("我的 DeepSeek", () => openMineKey()));
  menu.appendChild(go("退出", () => { if (confirmLeave()) logout("home"); }));
  menu.appendChild(go("换账号", () => { if (confirmLeave()) logout("login"); }));
  wrap.appendChild(menu);  // 画像切换不再放菜单里（11 号：在「我的」页）
}

async function loadPrefs(c) {
  try {
    const run = () => api("GET", "/api/me/prefs");
    const p = await (c ? dirAwait(c, run) : run());
    S.myDirs = Array.isArray(p.directions) ? p.directions : [];
    if (p.kit_id) useKit(p.kit_id);
    return true;
  } catch (e) {
    if (e === STALE_DIR) throw e;
    S.myDirs = undefined;  // 渲染缓存不是写入授权：读失败就当不知道
    return false;
  }
}

async function ensureMyDirs() {
  return loadPrefs();  // 全量 PUT 之前每次都重新读
}

async function putPrefs(patch, c) {
  const run = () => api("PUT", "/api/me/prefs", patch);
  const r = await (c ? dirAwait(c, run) : run());
  S.myDirs = Array.isArray(r.directions) ? r.directions : (S.myDirs || []);
  return r;
}

/* 一份画像的方向写进 trail：trail 本来就按画像存在服务端，方向跟着画像走。
   dirs = 添加过的方向（菜单里的选项池，常驻可勾）；sel = 勾选中的，推荐按 sel 来。
   加方向默认勾上；全部取消也可以（推荐回落到泛兴趣）。
   S.myDirs（全局 prefs）继续同步维护。 */
function selectedDirs(t) {
  const x = t || trail();
  return x.sel.filter((id) => x.dirs.includes(id));
}

function portraitDirsAdd(nodeId) {
  const t = trail();
  const dirs = t.dirs.includes(nodeId) ? t.dirs : t.dirs.concat(nodeId);
  const sel = selectedDirs(t);
  return { ...t, dirs, sel: sel.includes(nodeId) ? sel : sel.concat(nodeId) };
}

/* 菜单里勾/取消不打 toast：✓ 本身就是反馈，角上的画像名也不因此变 */
async function toggleDirSel(nodeId) {
  const t = trail();
  const sel = selectedDirs(t);
  const next = sel.includes(nodeId) ? sel.filter((x) => x !== nodeId) : sel.concat(nodeId);
  await saveTrail({ ...t, sel: next });
  paintChrome();
}

/* 方向操作绑定发起时的会话/画像/纪元：切画像、换人、退出后，旧操作的回包不得写进新画像（R5-01） */
const STALE_DIR = Symbol("stale-direction");
let directionEpoch = 0;
function directionContext() {
  const c = { uid: S.uid, token: S.token, pid: S.portraitId || "default", key: trailStorageKey(), epoch: directionEpoch };
  c.check = () => {
    if (portraitBusy || c.epoch !== directionEpoch || c.uid !== S.uid || c.token !== S.token
      || c.pid !== (S.portraitId || "default") || c.key !== trailStorageKey()) throw STALE_DIR;
  };
  return c;
}

async function dirAwait(c, run) {
  c.check();
  try { return await run(); }
  finally { c.check(); }
}

async function readTrailForWrite(c) {
  try {
    const t = await dirAwait(c, () => api("GET", "/api/projects/" + c.pid + "/trail"));
    localStorage.setItem(c.key, JSON.stringify(t));
    return t;
  } catch (e) {
    if (e === STALE_DIR) throw e;
    toast("没读到已收藏的方向，这次没有改"); return null;
  }
}

async function addMyDirection(nodeId) {
  if (!window.RG_DIR || !RG_DIR.byId[nodeId]) { toast("没有这个方向"); return; }
  const c = directionContext();
  try {
    const fresh = await readTrailForWrite(c);
    if (!fresh) return;
    const prev = trail();
    if (prev.dirs.includes(nodeId)) { toast("已经在我的方向里"); return; }
    if (!(await loadPrefs(c))) { toast("没读到已收藏的方向，这次没有改"); return; }
    await saveTrail(portraitDirsAdd(nodeId), false, c);
    if (!S.myDirs.includes(nodeId)) await putPrefs({ directions: S.myDirs.concat(nodeId) }, c);
    c.check();
    paintChrome();
    toast("已添加到我的方向");
    if (S.view === "me") setView("me");   // 「我的」页正开着就要当场能看到
  } catch (e) { if (e !== STALE_DIR) throw e; }
}

async function removeMyDirection(nodeId) {
  const c = directionContext();
  try {
    const fresh = await readTrailForWrite(c);
    if (!fresh) return;
    if (!(await loadPrefs(c))) { toast("没读到已收藏的方向，这次没有改"); return; }
    const t = trail();
    const dirs = t.dirs.filter((x) => x !== nodeId);
    const sel = selectedDirs(t).filter((x) => x !== nodeId);
    const next = { ...t, dirs, sel };
    if (t.dir === nodeId) {
      const fallback = dirs[0];
      if (fallback && window.RG_DIR) {
        // 教程/这一步要有一条路可走：切到剩下的第一个，进度从 byDir 里接回来
        const saved = (t.byDir || {})[fallback] || { done: [], tasks: {} };
        next.code = RG_DIR.backendCode(fallback);
        next.dir = fallback;
        next.done = saved.done || [];
        next.tasks = saved.tasks || {};
      } else {
        next.code = ""; next.dir = ""; next.done = []; next.tasks = {};
      }
    }
    await saveTrail(next, true, c);
    c.check();
    const kept = S.myDirs.filter((x) => x !== nodeId);
    if (kept.length !== S.myDirs.length) await putPrefs({ directions: kept }, c);
    c.check();
    S.myDir = undefined;
    paintChrome();
  } catch (e) { if (e !== STALE_DIR) throw e; }
}

async function chooseDirection(nodeId) {
  if (!window.RG_DIR || !RG_DIR.byId[nodeId]) { toast("没有这个方向"); return; }
  const c = directionContext();
  try {
    const fresh = await readTrailForWrite(c);
    if (!fresh) return;
    const backend = RG_DIR.backendCode(nodeId);
    await dirAwait(c, () => api("POST", "/api/directions/choose", { uid: c.uid, code: backend }));
    const withDirs = portraitDirsAdd(nodeId);
    const prev = trail();
    const byDir = Object.assign({}, prev.byDir || {});
    if (prev.dir) byDir[prev.dir] = { done: prev.done || [], tasks: prev.tasks || {} };
    const nxt = byDir[nodeId] || { done: [], tasks: {} };
    await saveTrail({
      code: backend, dir: nodeId, done: nxt.done || [], tasks: nxt.tasks || {}, byDir,
      dirs: withDirs.dirs, sel: withDirs.sel,
    }, true, c);
    if (!(await loadPrefs(c))) {
      S.myDir = undefined;
      paintChrome();
      toast("方向已确认，收藏列表没读到，没有改「我的」");
      return;
    }
    if (!S.myDirs.includes(nodeId)) await putPrefs({ directions: S.myDirs.concat(nodeId) }, c);
    c.check();
    S.myDir = undefined;
    paintChrome();
    toast(`已确认「${dirTitle(nodeId)}」`);
  } catch (e) { if (e !== STALE_DIR) throw e; }
}

/* ---------- 画像 × 方向（我的页） ----------
   画像 = 一套完整记录（对话、事实、任务、方向、进度）。
   切画像是换整套记录；同画像里的「可见组合」只换推荐口径——
   这两件事容易混，所以这里把画像摆在方向卡上方，卡里只讲方向。 */

async function portraitSection() {
  const box = el("section", "panel portrait-section");
  box.appendChild(el("h3", "section-label", "画像与方向"));
  let portraits = [];
  try {
    portraits = (await api("GET", `/api/portraits?uid=${S.uid}`)).portraits || [];
  } catch (e) {
    box.appendChild(el("p", "panel-sub", `画像列表没读到：${esc(e.message || "接口没有响应")}`));
    return box;
  }
  if (!portraits.length) {
    box.appendChild(el("p", "panel-sub", "还没有画像。"));
    return box;
  }
  const active = portraits.find((p) => p.active) || portraits[0];
  box.dataset.activeLabel = active.label;
  const previewId = S.mePortrait && portraits.some((p) => p.id === S.mePortrait) ? S.mePortrait : "";
  const shown = previewId ? portraits.find((p) => p.id === previewId) : active;
  const isActive = shown.id === active.id;

  box.appendChild(el("p", "panel-sub",
    "一份画像可以选几个方向（多选），推荐按所选的组合来。点别的画像是先看它选了哪些方向，确认了再换。"));

  // 画像条：在方向卡上方，不与卡片重叠
  const bar = el("div", "portrait-bar");
  portraits.forEach((p) => {
    const chip = el("div", "portrait-chip" + (p.id === shown.id ? " on" : ""));
    const name = el("button", "portrait-name", esc(p.label));
    name.type = "button";
    name.title = p.id === active.id ? "当前画像" : "看看这份画像收了哪些方向";
    name.onclick = () => {
      if (p.id === shown.id) return;
      S.mePortrait = p.id === active.id ? "" : p.id;
      setView("me");
    };
    chip.appendChild(name);
    bar.appendChild(chip);
  });
  const add = el("button", "portrait-add", "新建画像");
  add.type = "button";
  add.onclick = () => portraitOp(() => api("POST", "/api/portraits", { uid: S.uid }), "dialogue", false, async (r) => {
    const next = (r.portraits || []).find((item) => item.active);
    S.portraitName = (next && next.name) || "";
    S.mePortrait = "";
    await pullTrail();
    paintChrome();
  });
  bar.appendChild(add);
  box.appendChild(bar);

  // 方向卡：显示所选画像收的方向
  const card = el("div", "portrait-card");
  const head = el("div", "portrait-card-head");
  const titleRow = el("p", "portrait-card-title");
  titleRow.appendChild(el("span", "", esc(shown.label)));
  if (!isActive) titleRow.appendChild(el("em", "portrait-preview-tag", "预览中，还没有切换"));
  head.appendChild(titleRow);
  const renameBtn = el("button", "linkish", shown.custom ? "改名" : "起个名字");
  renameBtn.type = "button";
  head.appendChild(renameBtn);
  card.appendChild(head);

  const renameBox = el("form", "portrait-rename");
  renameBox.hidden = true;
  const input = el("input");
  input.maxLength = 24;
  input.placeholder = shown.custom ? "" : `默认叫「${shown.default_label}」，可以自己起一个`;
  input.value = shown.custom ? shown.name : "";
  input.setAttribute("aria-label", "画像名");
  const saveName = el("button", "btn small", "保存");
  saveName.type = "submit";
  const cancelName = el("button", "btn small ghost", "取消");
  cancelName.type = "button";
  cancelName.onclick = () => { renameBox.hidden = true; };
  renameBox.append(input, saveName, cancelName);
  renameBtn.onclick = () => { renameBox.hidden = false; input.focus(); };
  renameBox.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("POST", "/api/portraits/rename", { uid: S.uid, id: shown.id, name: input.value });
      if (isActive) { S.portraitName = input.value.trim(); paintChrome(); }
      toast("名字已保存");
      setView("me");
    } catch (err) { toast(err.message); }
  };
  card.appendChild(renameBox);

  // 活画像读本机 trail（最新），预览的画像读后端快照
  const t = trail();
  const backendDirs = shown.dirs || [];
  const localIds = isActive
    ? (t.dirs.length ? t.dirs : (t.dir ? [t.dir] : []))
      .filter((id) => !window.RG_DIR || RG_DIR.byId[id])
    : backendDirs.map((d) => d.node);
  const nameOf = (nodeId) => {
    const hit = backendDirs.find((x) => x.node === nodeId);
    return (hit && hit.name) || dirTitle(nodeId) || nodeId;
  };

  if (S.myDirs === undefined) {
    card.appendChild(el("p", "panel-sub", "收藏未加载，请重试"));
    const b = el("button", "btn small", "收藏未加载，请重试");
    b.onclick = async () => { if (await loadPrefs()) { await pullTrail(); setView("me"); } };
    card.appendChild(b);
  }
  if (!localIds.length && S.myDirs !== undefined) {
    const empty = el("p", "panel-sub",
      "这份画像还没有加方向。换一份画像，或先回到当前画像去方向树添加。");
    card.appendChild(empty);
    if (isActive) {
      const go = el("button", "btn small", "去方向树添加");
      go.type = "button";
      go.onclick = () => (S.entered ? setView("cards") : enterApp("cards"));
      card.appendChild(go);
    }
  } else if (localIds.length) {
    const list = el("div", "my-dir-list");
    const selSet = new Set(isActive ? selectedDirs(t) : (shown.sel || []).map((x) => x.node || x));
    localIds.forEach((nodeId) => {
      const row = el("div", "my-dir-row");
      const mark = selSet.has(nodeId) ? "✓ " : "○ ";
      const label = el("span", "", `${mark}${esc(nameOf(nodeId))}`);
      row.appendChild(label);
      if (isActive) {
        const drop = el("button", "btn small ghost", "移出");
        drop.type = "button";
        drop.onclick = async () => {
          const c = directionContext();
          try { await removeMyDirection(nodeId); c.check(); setView("me"); } catch (e) { if (e !== STALE_DIR) throw e; }
        };
        row.appendChild(drop);
      }
      list.appendChild(row);
    });
    card.appendChild(list);
    if (isActive) {
      const hint = el("p", "form-note", "✓ = 勾选进推荐，在右上角的「方向」菜单里勾选或取消。");
      card.appendChild(hint);
      const go = el("button", "linkish", "去方向树添加");
      go.type = "button";
      go.onclick = () => (S.entered ? setView("cards") : enterApp("cards"));
      card.appendChild(go);
    }
  }

  if (!isActive) {
    const swapRow = el("div", "submit-actions");
    const swap = el("button", "btn", "更换到该画像");
    swap.type = "button";
    swap.onclick = () => portraitOp(() => api("POST", "/api/portraits/activate", { uid: S.uid, id: shown.id }), "me", false, async (r) => {
      // 服务器切成功了才改前端记着的画像（portraitOp 里做）；换了画像要清掉上一份的方向/项目/定位草稿（Codex 复现）
      const next = (r.portraits || []).find((item) => item.id === shown.id);
      S.portraitName = (next && next.name) || "";
      S.mePortrait = "";
      await pullTrail();
      paintChrome();
      toast(`已换到「${shown.label}」`);
    });
    swapRow.appendChild(swap);
    card.appendChild(swapRow);
  } else if (portraits.length > 1) {
    const delRow = el("div", "submit-actions");
    const del = el("button", "btn small ghost", `删除当前画像「${shown.label}」`);
    del.type = "button";
    del.onclick = async () => {
      if (!window.confirm(`删除「${shown.label}」？这份画像的对话和记录都会清掉，不能恢复。`)) return;
      await portraitOp(() => api("DELETE", `/api/portraits/${encodeURIComponent(shown.id)}?uid=${S.uid}`), "me", true, async () => {
        S.mePortrait = "";
        await ensurePortrait();
        paintChrome();
        toast("已删除，换到剩下的画像");
      });
    };
    delRow.appendChild(del);
    card.appendChild(delRow);
  }
  box.appendChild(card);
  return box;
}

/* ② 原「问卷」视图已删除：问卷现在全靠对话实现（server/onboarding.py 的固定五问
   已由 dialogue 内核的提问阶梯取代）。原来的事实写入、待核对列表都在对话页里做完了，
   所以这一页只剩重复劳动，删掉可以少维护一份流程。
   后端 /api/onboard/* 仍然保留：它现在是「读画像事实」的通用出口，
   核对页、方向页、任务页都在用它拿 facts。 */

function confirmDrafts(facts) {
  const box = el("section", "panel");
  box.id = "me-confirm";
  box.appendChild(el("h3", "section-label", "核对"));
  const drafts = facts.filter((f) => f.status === "draft");
  const list = el("div", "fact-list");
  const edits = {};
  const dismissed = new Set();
  if (drafts.length) {
    box.appendChild(el("p", "panel-sub", "这些是它从对话里记下的。说得不对就直接改，不属实就划掉。保存之后才会用来推荐方向。"));
  } else {
    box.appendChild(el("p", "panel-sub", "没有待核对的草稿。聊过的已生效条目在下面的记忆里。"));
  }
  drafts.forEach((f) => {
    const card = factCard(f, true);
    const input = card.querySelector("input");
    input.value = f.value;
    input.setAttribute("aria-label", "修改这条记录");
    input.addEventListener("input", () => { edits[f.id] = input.value; });
    const del = el("button", "linkish danger", "不属实，划掉");
    del.type = "button";
    del.onclick = () => {
      if (dismissed.has(f.id)) dismissed.delete(f.id); else dismissed.add(f.id);
      const off = dismissed.has(f.id);
      card.classList.toggle("is-dismissed", off);
      input.disabled = off;
      del.textContent = off ? "恢复" : "不属实，划掉";
    };
    card.querySelector(".fact-actions").appendChild(del);
    list.appendChild(card);
  });
  box.appendChild(list);
  if (drafts.length) {
    const actions = el("div", "submit-actions");
    const ok = el("button", "btn", "保存核对");
    ok.type = "button";
    ok.onclick = async () => {
      const payload = Object.entries(edits).map(([id, value]) => ({ id, value }))
        .concat([...dismissed].map((id) => ({ id, dismissed: true })));
      ok.disabled = true;
      try {
        await api("POST", "/api/onboard/confirm", { uid: S.uid, edits: payload });
        toast("已保存，接下来看方向");
        setView("cards");
      } catch (e) { toast(e.message); ok.disabled = false; }
    };
    actions.appendChild(ok);
    box.appendChild(actions);
  }
  return box;
}

/* 把「还缺什么、填了能得到什么」摊开给用户看。
   空格本身是邀请，但只列空格不说收益，就变成一张逼人填的表——
   所以每个空格都带一句 why（来自后端 memory.COVERAGE_GROUPS）。 */
function coverageBoard() {
  const box = el("div", "coverage");
  box.appendChild(el("p", "coverage-hint", "以下信息都可按需补充，不需要填满才能开始阅读。"));
  const grid = el("div", "cov-grid");
  box.appendChild(grid);

  (async () => {
    let cov;
    try {
      cov = await api("GET", `/api/me/coverage?uid=${encodeURIComponent(S.uid)}`);
    } catch (e) {
      // 取不到**要说出来**，不能悄悄把自己删掉。
      // 之前这里写的是 box.remove()，接口一有问题用户就只看到一片空白，
      // 既不知道发生了什么、也没法告诉我——「核对没有分类」就是这么来的。
      grid.innerHTML = "";
      const err = el("p", "cov-bad",
        `字段清单没加载出来：${e && e.message ? e.message : "接口没有响应"}。`
        + "刷新一下试试；如果一直这样，把这个提示截图给我。");
      grid.appendChild(el("div", "cov-col cov-col-transcript")).appendChild(err);
      return;
    }
    (cov.groups || []).forEach((G) => {
      const col = el("div", `cov-col cov-col-${G.group}`);
      const h = el("p", "cov-head");
      h.appendChild(el("span", "cov-title", esc(G.label)));
      h.appendChild(el("span", "cov-count", G.filled ? "有记录" : "选填"));
      col.appendChild(h);


      (G.slots || []).forEach((s) => {
        const row = el("button", "cov-slot" + (s.filled ? " on" : ""));
        row.type = "button";
        row.appendChild(el("span", "cov-mark", s.filled ? "✓" : "○"));
        const body = el("div", "cov-body");
        const line = el("p", "cov-label");
        line.appendChild(el("span", "", esc(s.key === "capability:" ? "能力相关描述" : s.label)));
        if (s.filled && s.value) {
          line.appendChild(el("span", "cov-value", esc(String(s.value))));
          // 默认前提（学校=北京大学）：显示出来，但标清它是默认值、不是他说的
          if (s.presumed) line.appendChild(el("span", "cov-value", "（默认）"));
        }
        body.appendChild(line);
        // 已填的不再重复「填了有什么用」，省得整页都是废话
        if (!s.filled) line.appendChild(el("span", "cov-value cov-empty", "未提供"));
        row.appendChild(body);
        // 点空格 → 去对话里补。不在核对页做输入框：
        // 这些字段大多需要上下文（成绩单要粘贴、方向要聊），
        // 摆一排输入框只会让人填一半就走。
        row.onclick = () => {
          if (s.presumed) { toast(`「${s.label}」是默认前提（${s.value}），不用填。`); return; }
          toast(s.filled ? `「${s.label}」已经记下了，想改就在下面的记忆里改。`
            : `可以在对话中提到「${s.label}」，也可以暂时留空。`);
          if (!s.filled) setView("dialogue");
        };
        col.appendChild(row);
      });
      grid.appendChild(col);
    });


  })();

  return box;
}

/* ---------- ④ 方向：生长的边界 ---------- */

function twig(label, intro, children) {
  return { label, intro, children: children || [] };
}

function stampTree(node, id) {
  node.id = id;
  (node.children || []).forEach((child, i) => stampTree(child, `${id}.${i}`));
  return node;
}

const FIELD_TREES = {
  ai: {
    name: "人工智能",
    query: "人工智能",
    root: stampTree(twig("看懂机器学习", "机器学习是让程序从例子里找出规律，而不是把每条规则手写死。入门先分清：数据是什么、模型在学什么、你怎么知道它学对了。", [
      twig("数据", "没有数据就没有可检查的结果。先弄清例子从哪来、每个例子长什么样，再谈模型。", [
        twig("例子从哪来", "数据可以是你自己收集的，也可以是别人公开的。来源决定你能下什么结论。", [
          twig("自己标一份", "拿二三十条真实例子，自己写下标签。你会立刻碰到：标准含糊、两类例子长得很像。"),
          twig("公开数据集", "先读数据说明，而不是直接训练。看它收集了谁、缺了谁、标签是谁打的。"),
        ]),
        twig("特征", "特征是你决定让模型看见的那一部分。选错了，后面的模型再复杂也学不到你想问的事。", [
          twig("表格", "一行一个例子，一列一个属性。先画分布，再决定哪一列能用、哪一列不该用。"),
          twig("文本和图像", "文字和图片要先变成数字。这一步叫表示，入门时知道它存在即可，不必先写网络。"),
        ]),
      ]),
      twig("模型怎么学", "模型把「猜错了多少」变成一个数，再按这个数改自己。你要看的是它改完以后，对新例子还对不对。", [
        twig("损失", "损失是猜错的程度。训练就是想把这个数变小，但变小不等于真正学会了。", [
          twig("过拟合", "训练例子上很准、新例子上很差。通常是记得太死，而不是方法更高级。"),
          twig("训练曲线", "把损失随步数画出来。一直降、突然炸、或者很快不动，分别是三种不同的问题。"),
        ]),
        twig("评价", "先定「怎样算做对」，再跑模型。标准要在看结果之前写下来。", [
          twig("别只看准确率", "某一类特别多时，全猜那一类也会有很高的准确率。要看每一类分别怎样。"),
          twig("误差从哪来", "把判错的例子摊开。是标签有问题、特征没覆盖，还是这类例子根本太少。"),
        ]),
      ]),
      twig("用起来", "先做一个小而完整的预测，再碰生成。生成看起来聪明，但更难核对它有没有胡说。", [
        twig("预测一件事", "选一个能在二十分钟里核对的问题，比如一段话是积极还是消极。留下输入、输出和你不同意的例子。"),
        twig("生成", "生成是在续写，不是在检索事实。你要单独检查它说的每件可核对的事。", [
          twig("提示", "把任务、例子和限制写进提示。改一个词，输出就会变，所以提示本身也是实验条件。"),
          twig("幻觉", "说得流畅不等于有依据。对课程名、论文、数字，要回到原处核对，查不到就当没有。"),
        ]),
      ]),
    ]), "ai"),
  },
  math: {
    name: "数学",
    query: "数学",
    root: stampTree(twig("证明是什么", "数学入门不是多做题，而是把一句「显然」拆成别人能检查的步骤。先从定义出发，再谈证明和例子。", [
      twig("定义", "定义规定一个词在这里到底指什么。后面的证明只能用已经定义过的东西。", [
        twig("对象", "先说清你在谈论哪一类东西：数、集合、函数，还是一种关系。", [
          twig("集合", "集合是把对象放在一起的一种说法。属于、子集、空集，是后面所有话的地基。"),
          twig("函数", "函数是一条对应规则：每个输入对应唯一输出。先别急着算，先说清定义域。"),
        ]),
        twig("说法", "把日常句子改成「对所有」或「存在一个」。这句话的范围变了，真假也会变。", [
          twig("量词", "「所有人都会」和「有人会」不是同一句话。写证明前先把量词写对。"),
          twig("反例", "要否定「所有」，举一个不行的例子就够了。这个例子必须真的落在定义里。"),
        ]),
      ]),
      twig("证明", "证明是从定义和已知走到结论的一条路。每一步都要能指出它用了哪一条。", [
        twig("直接证", "假设条件成立，按定义推出结论。写的时候别跳步，跳步通常藏着还没定义的词。", [
          twig("一步一由", "每写一行，旁边注明用的是定义、上一步，还是一条已经证过的事实。"),
        ]),
        twig("反证", "先假设结论不成立，推出和已知矛盾。矛盾必须是明确的，不能只是「看起来怪」。", [
          twig("矛盾要落地", "写出互相否定的那两句话。说「这不可能」之前，先指出不可能的是哪一句。"),
        ]),
      ]),
      twig("结构", "很多新对象其实是旧对象加上一种运算或关系。看懂结构，就是看懂什么被保留了。", [
        twig("关系", "相等、大小、整除，都是关系。先问它是否自反、对称、传递。"),
        twig("不变量", "操作之后仍然不变的量，常常就是问题真正在问的东西。"),
      ]),
    ]), "math"),
  },
  stat: {
    name: "统计",
    query: "统计",
    root: stampTree(twig("数据在说什么", "统计是在不确定里做判断：这批数据支持哪一种说法，以及这个支持有多不稳。", [
      twig("描述", "先把数据本身看清楚，再谈推断。均值会掩盖少数极端值。", [
        twig("分布", "看数据堆在哪里、散得有多开、有没有孤零零的点。", [
          twig("中心", "均值、中位数回答「典型值在哪」。有极端值时，两者会分开。"),
          twig("散开", "同一均值可以很集中，也可以很散。散开程度决定你敢不敢用这个典型值。"),
        ]),
        twig("图", "直方图、散点图比一张表更容易看见形状。图的坐标和分组会改变你看见的故事。", [
          twig("分组", "直方图的箱子宽度变了，峰的个数可能也变。先试两种宽度再下结论。"),
        ]),
      ]),
      twig("推断", "你手里的是样本，想说的是更大的总体。样本不是总体的缩小复印件。", [
        twig("抽样", "谁有机会被抽到，决定你能把结论推到谁身上。", [
          twig("偏差", "方便抽到的人，往往不是你想代表的那群人。先写出没被抽到的是谁。"),
          twig("样本量", "样本越大，波动通常越小，但不能自动消除收集方式带来的偏差。"),
        ]),
        twig("不确定", "用区间或误差说明「还可能差多少」，而不是只给一个点。", [
          twig("区间", "区间越窄不一定越好。要看它是在什么假设下算出来的。"),
        ]),
      ]),
      twig("相关不是因果", "两件事一起变化，可能是第三件事在推动，也可能只是一起出现。", [
        twig("混杂", "找出一个同时影响两边的因素，再问：扣掉它之后，关系还在不在。"),
        twig("实验", "如果能主动分组而不是只观察，因果才比较站得住。分组方式本身要先写清。"),
      ]),
    ]), "stat"),
  },
  psy: {
    name: "认知",
    query: "心理",
    root: stampTree(twig("人如何做判断", "认知科学用可重复的任务，看人在知觉、记忆和决定上实际怎么做，而不是只问他觉得自己怎么做。", [
      twig("知觉", "你看见的不是原始刺激的复印件。大脑会补全、会忽略、会受上下文影响。", [
        twig("注意", "注意是选择。没被选中的信息，常常进不了后面的判断。", [
          twig("漏看", "专心找一件东西时，明显的另一件也可能看不见。这是任务设计，不是粗心的道德问题。"),
        ]),
        twig("错觉", "错觉说明知觉规则在什么时候会给出和物理事实不同的结果。", [
          twig("上下文", "同一个灰块，放在不同背景里明暗会变。判断依赖周围，不依赖孤立的一点。"),
        ]),
      ]),
      twig("记忆", "记忆是重建，不是回放。提问方式和间隔会改变你「记得」的内容。", [
        twig("编码", "当时怎么理解，决定后来能提取出什么。只反复读，不如试着回忆。", [
          twig("提取练习", "合上材料试着写出来，比再看一遍更能暴露你其实没记住的部分。"),
        ]),
        twig("扭曲", "事后信息会改写原先的记忆。问句里的一个词就可能改变回答。", [
          twig("误导提问", "「撞碎」和「碰到」问的不是同一件事。记录原始用词，再比较回答。"),
        ]),
      ]),
      twig("决策", "人常用快捷判断。快捷在熟悉情境里有用，在概率和罕见事件上容易偏。", [
        twig("直觉", "直觉是很快的模式匹配。它擅长你见过很多次的情况。"),
        twig("偏差", "容易想到的例子会被觉得更常见。先把基数写下来，再相信感觉。", [
          twig("基数", "一百个人里真正有多少，比你刚听到的一个生动故事更该先看。"),
        ]),
      ]),
    ]), "psy"),
  },
  econ: {
    name: "经济",
    query: "经济学",
    root: stampTree(twig("选择与激励", "经济学看人在约束下怎么选，以及规则一变，选择会怎么变。先别背模型名字，先写清谁在选、成本是什么。", [
      twig("约束", "任何选择都有放弃的另一项。没写出放弃了什么，就还没开始分析。", [
        twig("机会成本", "成本是你为此没做成的最好的那件事，不只是花出去的钱。", [
          twig("时间也算", "免费的活动如果占掉唯一的晚上，成本就是那个晚上能做的另一件事。"),
        ]),
        twig("预算", "预算是硬边界。边界内怎么分配，取决于每一元换来的增量，不是总价值。", [
          twig("边际", "再多一单位值不值得，和前面已经消费的不是同一个问题。"),
        ]),
      ]),
      twig("激励", "人会对规则做出反应，而且常常反应在你没写进规则的那一面。", [
        twig("奖励", "奖励什么，人们就多做什么。如果指标和你真正想要的不是一回事，行为会对着指标走。", [
          twig("指标被对付", "只奖数量时，质量可能下降。改规则之前，先猜人们会钻哪一个空。"),
        ]),
        twig("Tradeoff", "多得到一项，通常要少得到另一项。政策争论常常是在争这个交换值不值。"),
      ]),
      twig("市场", "价格把分散的信息收成一个信号：哪里缺、哪里多。价格也会漏掉没有被买卖的影响。", [
        twig("供需", "一边更想要、另一边更难提供，价格通常会动。先说清动的是需求还是供给。"),
        twig("外部性", "一笔交易影响到没参加的人时，价格里没有这部分。拥堵、噪音、污染是典型例子。", [
          twig("谁没被算进去", "列出没付钱也没收款、但被影响到的人。他们的损益不在价格里。"),
        ]),
      ]),
    ]), "econ"),
  },
  se: {
    name: "系统",
    query: "软件",
    root: stampTree(twig("程序到底在做什么", "系统入门是把「我写的东西」和「机器实际做的事」对上。先走通一条小路径，再谈结构和可靠性。", [
      twig("表示", "所有数据在机器里都是位。文字、数字、图片只是不同的解释方式。", [
        twig("位和字节", "八个位是一个字节。数字溢出、符号搞反，都发生在这个很底层的约定上。", [
          twig("整数范围", "固定位数能表示的整数有上限。再加一会绕回去，这不是数学里的整数。"),
        ]),
        twig("文本", "一个字对应哪个数字，由编码决定。编码不一致，看到的就是乱码，不是内容变了。", [
          twig("编码", "同一串字节，用 UTF-8 和用别的编码读，会得到不同的字。读写两边要说好。"),
        ]),
      ]),
      twig("执行", "程序是一条条指令。调用函数、读文件、发请求，都是在某个时刻真正发生的事。", [
        twig("调用", "函数调用会把参数和返回地址压进一条路径。你要能说出：谁调用了谁，结果回到哪。", [
          twig("调用栈", "出错时从上往下读栈，最上面是真正炸的地方，下面是谁把它叫起来的。"),
        ]),
        twig("状态", "程序记住的东西会变。同一个函数，状态不同，结果就不同。", [
          twig("可变数据", "改了一处共享的数据，另一处会跟着变。先找出这份数据被谁拿着。"),
        ]),
      ]),
      twig("可靠", "能跑一次不算完成。输入变一点、失败一次、两个人同时用，系统还是否说得通。", [
        twig("边界", "空的、特别长的、刚好差一的输入，最容易露出你没处理的假设。"),
        twig("失败", "网络、文件、用户输入都会失败。失败时留下能看懂的记录，比假装成功有用。", [
          twig("说清楚失败", "告诉使用者失败了什么、你已经知道什么、什么还不知道。不要编一个正常结果。"),
        ]),
      ]),
    ]), "se"),
  },
  med: {
    name: "基础医学",
    query: "基础医学",
    root: stampTree(twig("从分子到症状", "基础医学先问结构在哪、反应怎么维持、哪一环偏离，而不是先背病名。本路径不含临床各科。", [
      twig("结构", "解剖名词是位置和连接，不是病因。先能指到，再谈功能。", [
        twig("系统与局部", "同一结构可以按功能归进系统，也可以按位置归进局部。两套说法都要能对上。"),
        twig("连接", "管道、神经、相邻器官：写不清和谁连着，后面的机制没有落点。"),
      ]),
      twig("分子", "生化把「能活」写成一串可检查的反应。少了哪一步，要能猜会在哪个结构上先出问题。", [
        twig("通路", "底物、产物、酶。通路是步骤，不是一个词。"),
      ]),
      twig("稳态", "正常是一个范围。身体用反馈把自己拉回来，数字不是对错题。", [
        twig("反馈", "感受、整合、效应。断在不同位置，先看到的偏离不一样。"),
      ]),
      twig("机制", "免疫、微生物、病理是同一条证据链上的三种角色：宿主、外来物或损伤、组织上的偏离。", [
        twig("三角", "三侧都要能指回结构或分子。只写病名，还没有机制。"),
      ]),
    ]), "med"),
  },
};

Object.entries(FIELD_TREES).forEach(([code, f]) => { f.code = code; });

/* 任务 4 的方向路径（docs/paths/）：有路径的方向画成「6 步主干 + 原有节点」。
   主干讲「这一学期走到哪」，原有节点讲「二十分钟能做什么」，两种尺度分开画。
   原有节点挂到哪一步，照 docs/paths/改树建议.md §3、§4、§7、§8 的改动清单。 */
const CHAIN_ATTACH = {
  math: { 1: ["定义", "证明"], 3: ["结构"] },
  ai: { 2: ["数据", "模型怎么学/评价"], 3: ["模型怎么学/损失"], 6: ["用起来"] },
  psy: { 1: ["知觉/错觉"], 2: ["知觉/注意", "记忆", "决策"] },
  econ: { 1: ["约束"], 2: ["激励"], 3: ["市场"] },
  med: { 1: ["结构"], 2: ["分子"], 3: ["稳态"], 4: ["机制"] },
};

function findTwig(root, labelPath) {
  let node = root;
  for (const label of labelPath.split("/")) {
    node = (node.children || []).find((c) => c.label === label);
    if (!node) return null;
  }
  return node;
}

function cloneTwig(node) {
  return { label: node.label, intro: node.intro, children: (node.children || []).map(cloneTwig) };
}

function applyPaths(paths) {
  Object.entries(CHAIN_ATTACH).forEach(([code, attach]) => {
    const field = FIELD_TREES[code];
    const path = paths && paths[code];
    if (!field || !path || field.chain || !(path.steps || []).length) return;
    const old = field.root;
    const steps = path.steps.map((st) => ({
      label: st.name,
      intro: `先弄懂：${st.focus}`,
      done_when: st.done_when,
      stage: st.step,
      main: true,
      // 挂上来的整支和这一步同名（如经济第 2 步「激励」挂「激励」）时，直接挂它的子节点，免得出现「激励 — 激励」
      children: (attach[st.step] || []).map((lp) => findTwig(old, lp)).filter(Boolean).map(cloneTwig)
        .flatMap((tw) => (tw.label === st.name.split("：")[0] && tw.children.length ? tw.children : [tw])),
    }));
    field.root = stampTree({ label: path.name, intro: path.goal, virtual: true, children: steps }, code);
    field.chain = true;
    field.sourceDoc = path.source_doc;
  });
}

const BACKEND_CODES = ["ai", "math", "stat", "psy", "econ", "se", "med"];
const TUTORIALS = {};

function chainFromSpec(code, spec) {
  const steps = (spec.steps || []).map((row, i) => ({
    label: row[0],
    intro: `先弄懂：${row[1]}`,
    done_when: row[2],
    stage: i + 1,
    main: true,
    children: [],
  }));
  return {
    code,
    name: spec.name,
    query: spec.query,
    chain: true,
    root: stampTree({ label: spec.name, intro: spec.goal, virtual: true, children: steps }, code),
  };
}

function buildTutorials() {
  Object.keys(TUTORIALS).forEach((k) => { delete TUTORIALS[k]; });
  Object.assign(TUTORIALS, FIELD_TREES);
  const extra = (window.RG_DIR && RG_DIR.extra) || {};
  Object.entries(extra).forEach(([key, spec]) => {
    TUTORIALS[key] = chainFromSpec(key, spec);
  });
}

function tutorialOf(dirId) {
  if (window.RG_DIR && dirId) {
    const key = RG_DIR.tutorialKey(dirId);
    if (key && TUTORIALS[key]) return TUTORIALS[key];
  }
  return FIELD_TREES[dirId] || null;
}

function activeField(t) {
  return tutorialOf(t && t.dir) || FIELD_TREES[t && t.code] || null;
}

function dirTitle(dirId, field) {
  if (window.RG_DIR && dirId && RG_DIR.displayName(dirId)) return RG_DIR.displayName(dirId);
  return (field && field.name) || "";
}

function entryNode(field) {
  return pathNodes(field)[0];
}

function stepOf(field, nodeId) {
  // 节点所在的主干步骤：主干步骤的 id 是「方向.序号」，往下的节点都以它为前缀
  if (!field.chain || !nodeId) return null;
  const parts = nodeId.split(".");
  return flattenField(field.root).byId[parts.slice(0, 2).join(".")] || null;
}

function findProjectsForStep(code, step) {
  S.projectForm = { direction: code, stage: { 1: 0, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3 }[step] || 0, pathStep: step, keywords: "" };
  S.projectResult = null;
  S.projectTab = "find";
  setView("projects");
}

function flattenField(root) {
  const levels = [];
  const byId = {};
  const walk = (node, depth, parent) => {
    const item = { ...node, depth, parent, children: node.children || [] };
    byId[node.id] = item;
    (levels[depth] = levels[depth] || []).push(item);
    item.children.forEach((child) => walk(child, depth + 1, node.id));
  };
  walk(root, 0, null);
  return { levels, byId };
}

function trailStorageKey() {
  return "rg_trail_" + S.uid + (S.portraitId ? "_" + S.portraitId : "");
}

/* 新建、切换、删除画像一次只做一件：连点 B 再点 C，两个请求的响应可能倒着回来，
   原来按回来的顺序记，前端停在 B、服务器在 C，任务区就拿 B 的教程在 C 里建任务（Codex 复现）。
   做完以服务器回的「哪一份是当前的」为准，不以点了哪个为准。 */
let portraitBusy = false;
async function portraitOp(run, nextView, wasActive = false, after = null) {
  if (portraitBusy) { toast("正在切换画像，稍等"); return; }
  portraitBusy = true;
  ++directionEpoch;
  try {
    const r = await run();
    if (r === null) return;
    const active = ((r && r.portraits) || []).find((item) => item.active);
    const before = S.portraitId;
    S.portraitId = active ? active.id : "";
    // 只有当前画像真的换了（切换、新建、删掉的正是当前那份）才清：删一份不在用的画像，当前这份没写完的定位陈述不能跟着丢（Codex 复现）
    if (wasActive || S.portraitId !== before || !before) {  // 删的是当前那份（最后一份删掉是原地清空、id 不变）也算换了
      S.myDir = undefined;  // 方向是画像的：换了画像要重新问（原来切到数学画像，研读和信息源还按 AI 提示，Codex 复现）
      // 下面这些也都属于上一份画像：项目表单（方向、阶段、关键词）和搜到的项目、打开的项目和任务、定位陈述草稿、
      // 方向树上点着的节点。原来切到数学画像，项目表单还停在 AI、第 6 步和旧关键词（Codex 复现）
      Object.assign(S, { projectForm: null, projectResult: null, projectKeywords: "", projectId: "",
        openTaskId: "", posDraft: null, dirPicked: null });
    }
    if (after) await after(r);  // 上游 w97 的画像面板要在切完之后刷画像名、拉 trail
    setView(nextView);
  } catch (e) {
    toast(e.message);
  } finally {
    portraitBusy = false;
  }
}

async function ensurePortrait() {
  if (S.portraitId) return S.portraitId;
  try {
    const r = await api("GET", `/api/portraits?uid=${S.uid}`);
    const active = (r.portraits || []).find((p) => p.active);
    S.portraitId = active ? active.id : "";
    S.portraitName = (active && active.name) || "";
  } catch (_) {
    S.portraitId = "";
    S.portraitName = "";
  }
  if (S.portraitId) adoptLegacyTrail(S.portraitId);
  const state = await pullTrail();
  // 老数据迁移：方向原来存在全局 prefs 里，第一次进画像时收进这份画像的 trail
  // 只有成功读到、且远端同时缺 dirs/sel 的旧 schema 才迁移；读失败是未知，空数组是明确选择
  const t = trail();
  if (state.loaded && !state.modern && !t.dirs.length && Array.isArray(S.myDirs) && S.myDirs.length) {
    const dirs = S.myDirs.filter((id) => !window.RG_DIR || RG_DIR.byId[id]);
    if (dirs.length) await saveTrail({ ...t, dirs, sel: dirs });
  }
  if (state.loaded) ensureSelKey();
  return S.portraitId;
}

/* sel 现在是权威状态：空 = 一个都没勾（推荐回落到泛兴趣）。
   老 trail 没有 sel 键的，补成「全选」，别让老用户一进来全灭。 */
function ensureSelKey() {
  try {
    const raw = JSON.parse(localStorage.getItem(trailStorageKey()) || "null");
    if (raw && typeof raw === "object" && !Array.isArray(raw.sel)
      && Array.isArray(raw.dirs) && raw.dirs.length) {
      saveTrail({ ...trail(), sel: trail().dirs });
    }
  } catch (_) { /* 下次再补 */ }
}

function adoptLegacyTrail(pid) {
  const ownerKey = "rg_trail_owner_" + S.uid;
  if (localStorage.getItem(ownerKey)) return;
  const legacy = localStorage.getItem("rg_trail_" + S.uid);
  if (legacy) localStorage.setItem("rg_trail_" + S.uid + "_" + pid, legacy);
  localStorage.setItem(ownerKey, pid);
}

function trail() {
  try {
    const t = JSON.parse(localStorage.getItem(trailStorageKey()) || "null");
    if (t && typeof t === "object") {
      const code = t.code || "";
      const mapped = window.RG_DIR && RG_DIR.legacyDir[code];
      return {
        code,
        dir: t.dir || mapped || "",
        done: Array.isArray(t.done) ? t.done : [],
        tasks: t.tasks && typeof t.tasks === "object" ? t.tasks : {},
        byDir: t.byDir && typeof t.byDir === "object" ? t.byDir : {},
        // dirs = 这份画像添加过的方向（菜单里的选项池）；sel = 勾选中的（推荐按它来）
        dirs: Array.isArray(t.dirs) ? t.dirs.map(String) : [],
        sel: Array.isArray(t.sel) ? t.sel.map(String) : [],
      };
    }
  } catch (_) { /* 坏数据就当没有进度 */ }
  return { code: "", dir: "", done: [], tasks: {}, byDir: {}, dirs: [], sel: [] };
}

function saveTrail(t, force, c) {
  if (c) c.check();
  const prev = trail();
  const code = t.code || "";
  let done = t.done || [];
  const field = FIELD_TREES[code];
  if (!force && prev.code === code && field) {
    const path = pathNodes(field);
    const prevAt = currentOnPath(field, prev.done);
    const nextAt = currentOnPath(field, done);
    const prevI = prevAt ? path.findIndex((n) => n.id === prevAt.id) : path.length;
    const nextI = nextAt ? path.findIndex((n) => n.id === nextAt.id) : path.length;
    if (nextI < prevI) done = prev.done;
  }
  const packed = {
    code,
    dir: t.dir || (force ? "" : prev.dir) || (window.RG_DIR && RG_DIR.legacyDir[code]) || "",
    done,
    tasks: force ? (t.tasks || {}) : Object.assign({}, prev.code === code && prev.dir === (t.dir || prev.dir) ? prev.tasks : {}, t.tasks || {}),
    byDir: t.byDir && typeof t.byDir === "object" ? t.byDir : (prev.byDir || {}),
    dirs: Array.isArray(t.dirs) ? t.dirs.map(String) : prev.dirs,
    sel: Array.isArray(t.sel) ? t.sel.map(String) : prev.sel,
  };
  localStorage.setItem(c ? c.key : trailStorageKey(), JSON.stringify(packed));
  if (c ? c.token : S.token) {
    const pid = c ? c.pid : (S.portraitId || "default");
    const run = () => api("PUT", `/api/projects/${pid}/trail`, { data: packed });
    return (c ? dirAwait(c, run) : run()).catch((e) => { if (e === STALE_DIR) throw e; });
  }
  return Promise.resolve();
}

async function pullTrail() {
  if (!S.token) return { loaded: true, modern: false };
  const pid = S.portraitId || "default";
  try {
    const remote = await api("GET", `/api/projects/${pid}/trail`);
    const modern = Array.isArray(remote?.dirs) || Array.isArray(remote?.sel);
    if (modern || remote?.code) {
      localStorage.setItem(trailStorageKey(), JSON.stringify(remote));
    } else {
      const local = trail();
      if (local.code) await api("PUT", `/api/projects/${pid}/trail`, { data: local });
    }
    ensureSelKey();
    return { loaded: true, modern };
  } catch (_) { return { loaded: false, modern: false }; /* 先用本地，不迁移、不补 sel */ }
}

function inferredDone(code, tasks) {
  const field = FIELD_TREES[code];
  if (!field) return [];
  const path = pathNodes(field);
  let furthest = -1;
  path.forEach((node, i) => {
    const title = node.label.slice(0, 40);
    if ((tasks || []).some((tk) => tk.direction === code && tk.title === title)) furthest = i;
  });
  if (furthest <= 0) return [];
  return path.slice(0, furthest).map((n) => n.id);
}

function mergeTrail(code, tasks) {
  const local = trail();
  const field = FIELD_TREES[code];
  const inferred = inferredDone(code, tasks);
  if (!field) return local;
  if (local.code !== code) {
    return { code, dir: local.dir || (window.RG_DIR && RG_DIR.legacyDir[code]) || "", done: inferred, tasks: {} };
  }
  const path = pathNodes(field);
  const localAt = currentOnPath(field, local.done);
  const inferredAt = currentOnPath(field, inferred);
  const localI = localAt ? path.findIndex((n) => n.id === localAt.id) : path.length;
  const inferredI = inferredAt ? path.findIndex((n) => n.id === inferredAt.id) : path.length;
  if (inferredI > localI) return { code, dir: local.dir, done: inferred, tasks: local.tasks || {} };
  return local;
}

function adoptDirection(facts) {
  // 方向写在事实里（服务端），进度写在本机；本机还没有进度时，从事实里接过方向
  const t = trail();
  if (t.code) return t;
  const dirs = (facts || []).filter((f) => (f.key || "").startsWith("direction:")
    && (f.status === "confirmed" || f.status === "active"));
  const chosen = dirs[dirs.length - 1];
  const code = chosen ? chosen.key.split(":")[1] : "";
  if (!code || !FIELD_TREES[code]) return t;
  return { ...t, code, dir: (window.RG_DIR && RG_DIR.legacyDir[code]) || t.dir || "" };
}

function pathNodes(field) {
  const out = [];
  const walk = (node) => {
    if (!node.virtual) out.push(node);  // 主干的虚拟根只用来挂六步，不算一个节点
    (node.children || []).forEach(walk);
  };
  walk(field.root);
  return out;
}

function currentOnPath(field, done) {
  const seen = new Set(done || []);
  return pathNodes(field).find((node) => !seen.has(node.id)) || null;
}

function drawFieldTree(field, picked, onPick, progress, animate) {
  // 整齐树布局：叶子各占一行，父节点落在子节点的中线上，连线不会交叉
  const { byId } = flattenField(field.root);
  const narrow = window.innerWidth < 920;
  const COL = narrow ? 128 : 168;
  const ROW = narrow ? 42 : 50;
  const NODE_W = narrow ? 104 : 128;
  const MAIN_W = narrow ? 140 : 212;
  const NODE_H = 34;
  const PAD = narrow ? 24 : 30;
  const shift = field.root.virtual ? 1 : 0;
  const colX = (depth) => {
    const d = depth - shift;
    if (!field.chain) return d * COL;
    return d === 0 ? 0 : MAIN_W + (COL - NODE_W) + (d - 1) * COL;
  };
  const wOf = (node) => (node.main ? MAIN_W : NODE_W);
  const pos = {};
  let row = 0;
  let depthMax = 0;
  const place = (node, depth) => {
    depthMax = Math.max(depthMax, depth);
    const kids = node.children || [];
    if (!kids.length) {
      pos[node.id] = { x: colX(depth), y: row * ROW };
      row += 1;
      return;
    }
    kids.forEach((k) => place(k, depth + 1));
    const first = pos[kids[0].id].y;
    const last = pos[kids[kids.length - 1].id].y;
    // 主干步骤放在它那组节点的第一行，六步从上到下排成一条线
    pos[node.id] = { x: colX(depth), y: node.main ? first : (first + last) / 2 };
  };
  place(field.root, 0);
  const width = colX(depthMax) + (depthMax - shift === 0 ? MAIN_W : NODE_W) + PAD * 2;
  const height = (row - 1) * ROW + NODE_H + PAD * 2;

  const box = el("div", "frontier");
  box.style.width = width + "px";
  box.style.height = height + "px";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "frontier-svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));
  const layer = el("div", "frontier-nodes");
  const done = new Set((progress && progress.done) || []);
  const here = progress && progress.here;
  const onPickPath = (id) => picked && (picked === id || picked.startsWith(id + "."));

  if (field.chain) {
    // 主干：六步之间一条粗线，走过的部分用主色
    const mains = Object.values(byId).filter((n) => n.main).sort((m, n) => m.stage - n.stage);
    mains.slice(1).forEach((node, i) => {
      const a = pos[mains[i].id];
      const b = pos[node.id];
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", String(PAD + 18)); line.setAttribute("x2", String(PAD + 18));
      line.setAttribute("y1", String(PAD + a.y + NODE_H)); line.setAttribute("y2", String(PAD + b.y));
      const passed = done.has(mains[i].id) && (done.has(node.id) || here === node.id || (here || "").startsWith(node.id + "."));
      line.setAttribute("class", "frontier-trunk" + (passed ? " is-past" : ""));
      svg.appendChild(line);
    });
  }

  Object.values(byId).forEach((node) => {
    if (!node.parent || byId[node.parent].virtual) return;
    const a = pos[node.parent];
    const b = pos[node.id];
    const x1 = PAD + a.x + wOf(byId[node.parent]);
    const y1 = PAD + a.y + NODE_H / 2;
    const x2 = PAD + b.x;
    const y2 = PAD + b.y + NODE_H / 2;
    const mid = x1 + (x2 - x1) * 0.5;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`);
    const cls = ["frontier-link"];
    if (onPickPath(node.id)) cls.push("is-hot");
    else if (done.has(node.id) || here === node.id) cls.push("is-past");
    if (animate) { cls.push("reveal"); path.style.setProperty("--d", String(node.depth)); }
    path.setAttribute("class", cls.join(" "));
    svg.appendChild(path);
  });

  Object.values(byId).forEach((node) => {
    if (node.virtual) return;
    const flags = [
      "frontier-node",
      node.main ? "is-main" : "",
      node.id === picked ? "is-pick" : "",
      onPickPath(node.id) && node.id !== picked ? "is-path" : "",
      done.has(node.id) ? "is-past" : "",
      here === node.id ? "is-now" : "",
      animate ? "reveal" : "",
    ].filter(Boolean).join(" ");
    const short = node.main ? node.label.split("：")[0] : node.label;
    const btn = el("button", flags, node.main ? `<b class="step-no">${node.stage}</b><span>${esc(short)}</span>` : esc(node.label));
    btn.type = "button";
    btn.title = node.main ? `第 ${node.stage} 步 · ${node.label}` : node.label;
    btn.style.left = PAD + pos[node.id].x + "px";
    btn.style.top = PAD + pos[node.id].y + "px";
    btn.style.width = wOf(node) + "px";
    if (animate) btn.style.setProperty("--d", String(node.depth));
    if (here === node.id) btn.setAttribute("aria-current", "step");
    btn.onclick = () => onPick(byId[node.id]);
    layer.appendChild(btn);
  });
  box.append(svg, layer);
  return box;
}

function openNodeSheet(field, node, ctx) {
  let sheet = document.getElementById("nodeSheet");
  if (!sheet) {
    sheet = el("aside", "node-sheet");
    sheet.id = "nodeSheet";
    document.body.appendChild(sheet);
  }
  // 这里原来写的是 "node-sheet open"，但 styles.css 里没有 `.open` 这条规则：
  // 抽屉的样式是无条件生效的，关掉是把这个元素 remove 掉。
  // 一个不起作用的类名会让人以为存在开/关两态，去掉。
  sheet.className = "node-sheet";
  sheet.innerHTML = "";
  const head = el("div", "sheet-head");
  const x = el("button", "sheet-x", "关闭");
  x.type = "button";
  x.onclick = () => sheet.remove();
  head.append(el("p", "sheet-kicker", esc(field.name)), x);
  sheet.appendChild(head);
  const entry = entryNode(field);
  if (node.main) sheet.appendChild(el("p", "sheet-label", `主干 · 第 ${node.stage} 步（这一学期走到哪）`));
  sheet.appendChild(el("h3", "", esc(node.label)));
  sheet.appendChild(el("p", "sheet-intro", esc(node.intro || "")));
  if (node.main && node.done_when) {
    sheet.appendChild(el("div", "why-box", `<b>做完怎样算过了　</b>${esc(node.done_when)}`));
  }
  if (node.id === entry.id && ctx.why) {
    sheet.appendChild(el("p", "sheet-why", esc(ctx.why)));
  }
  if (node.children && node.children.length) {
    sheet.appendChild(el("p", "sheet-label", "往下"));
    const row = el("div", "sheet-nexts");
    node.children.forEach((child) => {
      const b = el("button", "sheet-next", esc(child.label));
      b.type = "button";
      b.onclick = () => ctx.onPick(child.id);
      row.appendChild(b);
    });
    sheet.appendChild(row);
  }
  if (ctx.here === node.id && ctx.chosen) {
    const go = el("button", "btn", "去做这一步");
    go.type = "button";
    go.onclick = () => setView("workbench");
    sheet.appendChild(go);
  }
  const courses = el("div", "course-block");
  courses.appendChild(el("h4", "", "课程"));
  courses.appendChild(el("div", "course-status", "检索中"));
  sheet.appendChild(courses);
  loadCourses(courses, node.label || field.query);
  if (node.id === entry.id && !ctx.chosen) {
    const choose = el("button", "btn", "确认这个方向");
    choose.type = "button";
    choose.onclick = () => ctx.onChoose();
    sheet.appendChild(choose);
  }
  sheet.querySelector(".sheet-x").focus({ preventScroll: true });
}

function cloneDirNode(node) {
  return {
    id: node.id,
    label: node.label,
    intro: node.blurb || "",
    query: node.query,
    name: node.name,
    children: (node.children || []).map(cloneDirNode),
  };
}

function visibleTaxonomy(branch) {
  const children = ((window.RG_DIR && RG_DIR.tree) || []).map((l1) => {
    const copy = cloneDirNode(l1);
    if (l1.id !== branch) copy.children = [];
    return copy;
  });
  return {
    name: "学科方向",
    query: "学科",
    root: { id: "dir-root", label: "学科", intro: "", virtual: true, children },
  };
}

function recDirIds(recommended) {
  const out = new Set();
  if (!window.RG_DIR) return out;
  recommended.forEach((code) => {
    const id = RG_DIR.legacyDir[code];
    if (id) out.add(id);
    if (id && RG_DIR.l1Of(id)) out.add(RG_DIR.l1Of(id));
  });
  return out;
}

function openDirSheet(node, ctx) {
  let sheet = document.getElementById("nodeSheet");
  if (!sheet) {
    sheet = el("aside", "node-sheet");
    sheet.id = "nodeSheet";
    document.body.appendChild(sheet);
  }
  sheet.className = "node-sheet open";
  sheet.innerHTML = "";
  const head = el("div", "sheet-head");
  const x = el("button", "sheet-x", "关闭");
  x.type = "button";
  x.onclick = () => sheet.remove();
  const meta = window.RG_DIR && RG_DIR.byId[node.id];
  const level = meta ? (["", "一级", "二级", "三级"][meta.depth] || "方向") : "方向";
  head.append(el("p", "sheet-kicker", `${level}${meta && meta.parent ? ` · 上属${esc(dirTitle(meta.parent))}` : ""}`), x);
  sheet.appendChild(head);
  const title = el("h3", "", esc(node.name || node.label));
  // 已在我的方向里：名字后面直接给对勾，别让用户再猜能不能点添加
  const dirAdded = trail().dirs.includes(node.id);
  if (dirAdded) title.appendChild(el("span", "added-tag", "✓ 已添加"));
  sheet.appendChild(title);
  if (node.name && node.label && node.name !== node.label) {
    sheet.appendChild(el("p", "sheet-kicker", esc(node.label)));
  }
  sheet.appendChild(el("p", "sheet-intro", esc(node.intro || (meta && meta.blurb) || "")));
  if (ctx.why) sheet.appendChild(el("p", "sheet-why", esc(ctx.why)));
  if (ctx.reading && ctx.reading.title) {
    const read = el("div", "sheet-reading");
    read.appendChild(el("h4", "", "入门读物"));
    read.appendChild(el("p", "reading-title", esc(ctx.reading.title)));
    if (ctx.reading.why) read.appendChild(el("p", "reading-why", esc(ctx.reading.why)));
    sheet.appendChild(read);
  }
  if (node.children && node.children.length) {
    sheet.appendChild(el("p", "sheet-label", "往下"));
    const row = el("div", "sheet-nexts");
    node.children.forEach((child) => {
      const b = el("button", "sheet-next", esc(child.label));
      b.type = "button";
      b.onclick = () => ctx.onPick(child.id);
      row.appendChild(b);
    });
    sheet.appendChild(row);
  }
  const courses = el("div", "course-block");
  courses.appendChild(el("h4", "", "课程"));
  courses.appendChild(el("div", "course-status", "检索中"));
  sheet.appendChild(courses);
  loadCourses(courses, (meta && meta.query) || node.query || node.label);
  if (!ctx.chosen) {
    const choose = el("button", "btn", "确认这个方向");
    choose.type = "button";
    choose.onclick = () => ctx.onChoose();
    sheet.appendChild(choose);
  } else {
    const go = el("button", "btn", "看这个方向的教程");
    go.type = "button";
    go.onclick = () => { sheet.remove(); S.cardsPane = "tutorial"; render(); };
    sheet.appendChild(go);
  }
  const add = el("button", "btn secondary", dirAdded ? "取消添加" : "添加到我的方向");
  add.type = "button";
  add.onclick = async () => {
    const c = directionContext();
    try {
      if (dirAdded) await removeMyDirection(node.id);
      else await addMyDirection(node.id);
      c.check();
      // 重建浮层：对勾和按钮当场换过来，树也跟着重画
      ctx.onPick(node.id);
    } catch (e) { if (e !== STALE_DIR) throw e; }
  };
  sheet.appendChild(add);
  sheet.querySelector(".sheet-x").focus({ preventScroll: true });
}

async function renderCards() {
  const seq = S.renderSeq;
  await ensurePortrait();
  if (stale(seq)) return;
  if (!Object.keys(TUTORIALS).length) buildTutorials();
  let saved = trail();
  let serverTasks = [];
  try {
    const taskRes = await api("GET", `/api/tasks?uid=${S.uid}`);
    if (stale(seq)) return;
    serverTasks = taskRes.tasks || [];
    if (saved.code) {
      const merged = mergeTrail(saved.code, taskRes.tasks || []);
      if (merged.done.join(",") !== saved.done.join(",")) saveTrail(merged);
    }
  } catch (_) { /* 树先按本地进度画 */ }
  if (stale(seq)) return;
  saved = trail();
  let chosenCode = saved.code && FIELD_TREES[saved.code] ? saved.code : "";
  let chosenDir = saved.dir && window.RG_DIR && RG_DIR.byId[saved.dir]
    ? saved.dir
    : ((window.RG_DIR && RG_DIR.legacyDir[chosenCode]) || "");
  let cards = [];
  let recommended = new Set();
  let focus = (S.dirFocus && window.RG_DIR && RG_DIR.byId[S.dirFocus])
    ? S.dirFocus
    : (chosenDir || "520");
  let branch = (S.dirBranch && window.RG_DIR && RG_DIR.byId[S.dirBranch])
    ? S.dirBranch
    : ((window.RG_DIR && RG_DIR.l1Of(focus)) || "520");
  let pane = S.cardsPane === "tutorial" ? "tutorial" : "direction";
  let picked = S.dirPicked || null;
  let tutPicked = S.tutPicked || null;
  let touched = !!(chosenDir || chosenCode);
  let grew = true;

  $app.innerHTML = "";
  const head = el("header", "ws-head cards-head");
  const titles = el("div");
  titles.appendChild(el("h2", "", pane === "tutorial" ? "教程" : "方向"));
  head.appendChild(titles);
  const tabs = el("nav", "ws-tabs");
  tabs.setAttribute("role", "tablist");
  [["direction", "方向"], ["tutorial", "教程"]].forEach(([id, label]) => {
    const b = el("button", `ws-tab${pane === id ? " on" : ""}`, label);
    b.type = "button";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", pane === id ? "true" : "false");
    b.onclick = () => {
      S.cardsPane = id;
      pane = id;
      document.getElementById("nodeSheet")?.remove();
      grew = true;
      paint();
    };
    tabs.appendChild(b);
  });
  head.appendChild(tabs);
  $app.appendChild(head);
  const recLine = el("div", "rec-line");
  const confirmBar = el("div", "switch-bar");
  const stage = el("div", "forest-stage");
  const legend = el("div", "tree-legend");
  $app.append(recLine, confirmBar, stage, legend);

  const viewName = () => dirTitle(focus) || "未选方向";
  const recCard = () => {
    const backend = window.RG_DIR ? RG_DIR.backendCode(focus) : "";
    return cards.find((c) => c.direction && c.direction.code === backend);
  };
  const recWhy = () => {
    const card = recCard();
    return card && card.why_you;
  };

  const chooseFocus = async () => {
    if (!window.RG_DIR || !RG_DIR.byId[focus]) { toast("先点树上的一个方向"); return; }
    const hadSheet = !!document.getElementById("nodeSheet");
    const c = directionContext();
    try {
      await chooseDirection(focus);
      c.check();
      chosenCode = RG_DIR.backendCode(focus);
      chosenDir = focus;
      picked = null;
      S.dirPicked = null;
      paint();
      // 浮层本来开着就刷它：确认即已添加，对勾和「取消添加」当场可见
      if (hadSheet) showDir(focus);
    } catch (e) { if (e !== STALE_DIR) toast(e.message); }
  };

  const paintRec = () => {
    recLine.innerHTML = "";
    if (pane === "tutorial") return;
    if (!cards.length) {
      recLine.appendChild(el("p", "", "还没有可对照的兴趣。先去聊几句，建议才会标到树上。"));
      return;
    }
    const names = cards.map((c) => {
      const code = c.direction && c.direction.code;
      const id = window.RG_DIR && RG_DIR.legacyDir[code];
      return (id && RG_DIR.displayName(id)) || (FIELD_TREES[code] && FIELD_TREES[code].name) || (c.direction && c.direction.name) || "";
    }).filter(Boolean);
    recLine.appendChild(el("p", "", `按你刚说的，更贴近${names.map(esc).join("、")}。${esc(cards[0].why_you || "")}`));
    if (window.innerWidth < 920) {
      recLine.classList.add("clamp");
      recLine.onclick = () => recLine.classList.remove("clamp");
    }
  };

  const paintBar = (field, here) => {
    confirmBar.innerHTML = "";
    const name = viewName();
    if (pane === "direction") {
      if (!chosenDir || focus !== chosenDir) {
        const note = el("p", "", `正在看「${name}」。确认之后，教程和任务会按它开始。`);
        const ok = el("button", "btn small", "确认这个方向");
        ok.type = "button";
        ok.onclick = () => chooseFocus();
        confirmBar.append(note, ok);
      } else {
        const note = el("p", "", `「${name}」的教程和学习流程在这里。`);
        const go = el("button", "btn small", "看教程");
        go.type = "button";
        go.onclick = () => { S.cardsPane = "tutorial"; pane = "tutorial"; grew = true; paint(); };
        confirmBar.append(note, go);
      }
      return;
    }
    titles.querySelector("h2").textContent = "教程";
    if (!focus || !field) {
      confirmBar.appendChild(el("p", "", "先在方向页点一个方向，再回到这里看它的学习流程。"));
      return;
    }
    const lead = el("p", "", chosenDir === focus
      ? (here ? `你在「${here.label}」，接着往下走。` : `「${name}」的流程已经确认。`)
      : `教程跟着正在看的「${name}」走。确认之后，任务才会按它开始。`);
    confirmBar.appendChild(lead);
    if (chosenDir !== focus) {
      const ok = el("button", "btn small", "确认这个方向");
      ok.type = "button";
      ok.onclick = () => chooseFocus();
      confirmBar.appendChild(ok);
    } else if (here) {
      const go = el("button", "btn small", "去做这一步");
      go.type = "button";
      go.onclick = () => setView("workbench");
      confirmBar.appendChild(go);
    }
  };

  const showDir = (id) => {
    if (!window.RG_DIR || !RG_DIR.byId[id]) return;
    focus = id;
    S.dirFocus = id;
    branch = RG_DIR.l1Of(id) || branch;
    S.dirBranch = branch;
    picked = id;
    S.dirPicked = id;
    touched = true;
    grew = true;
    paint();
    const meta = RG_DIR.byId[id];
    openDirSheet({
      id: meta.id, label: meta.label, name: meta.name, intro: meta.blurb, query: meta.query,
      children: meta.children,
    }, {
      why: recWhy(),
      reading: (recCard() || {}).reading,
      chosen: id === chosenDir,
      onPick: showDir,
      onChoose: chooseFocus,
    });
  };

  const showTut = (id) => {
    const field = tutorialView(focus);
    if (!field) return;
    const node = flattenField(field.root).byId[id];
    if (!node) return;
    tutPicked = id;
    S.tutPicked = id;
    paint();
    const hereTrail = trail();
    const here = hereTrail.dir === focus ? currentOnPath(field, hereTrail.done) : null;
    openNodeSheet(field, node, {
      why: recWhy(),
      chosen: focus === chosenDir,
      here: here && here.id,
      onPick: showTut,
      onChoose: chooseFocus,
    });
  };

  function tutorialView(dirId) {
    const tut = tutorialOf(dirId);
    if (!tut) return null;
    const q = (window.RG_DIR && RG_DIR.byId[dirId] && RG_DIR.byId[dirId].query) || tut.query;
    return { ...tut, name: dirTitle(dirId, tut), query: q, backend: window.RG_DIR ? RG_DIR.backendCode(dirId) : tut.code };
  }

  const paint = () => {
    titles.querySelector("h2").textContent = pane === "tutorial" ? "教程" : "方向";
    if (pane === "tutorial") {
      const name = viewName();
      titles.querySelector("p")?.remove();
      const lead = el("p", "dir-now", name === "未选方向" ? "还没有选方向" : `跟着「${name}」走`);
      titles.appendChild(lead);
    } else {
      titles.querySelector("p")?.remove();
      titles.appendChild(el("p", "", "一棵学科树。点一级展开二级，热门方向有三级。统计在数学下面，医学只做基础医学。"));
    }
    tabs.querySelectorAll(".ws-tab").forEach((b, i) => {
      const on = (i === 0 ? "direction" : "tutorial") === pane;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    paintRec();
    stage.classList.toggle("is-dir", pane === "direction");
    const hereTrail = trail();
    if (pane === "direction") {
      const tax = visibleTaxonomy(branch);
      paintBar(null, null);
      legend.innerHTML = '<span class="now"><i></i>正在看</span><span class="past"><i></i>已确认</span><span><i></i>点一级展开</span><span>点节点看说明和课</span>';
      stage.innerHTML = "";
      stage.appendChild(drawFieldTree(tax, picked || focus, (node) => showDir(node.id), {
        done: chosenDir && chosenDir !== focus ? [chosenDir] : [],
        here: focus,
      }, grew));
    } else {
      const field = tutorialView(focus);
      const here = field && hereTrail.dir === focus ? currentOnPath(field, hereTrail.done) : null;
      paintBar(field, here);
      if (!field) {
        stage.innerHTML = "";
        legend.innerHTML = "";
        stage.appendChild(emptyPanel("先在方向页点一个方向，教程会跟着那个方向走。", "去方向树", "cards"));
        const btn = stage.querySelector("button");
        if (btn) btn.onclick = () => { S.cardsPane = "direction"; pane = "direction"; grew = true; paint(); };
      } else {
        legend.innerHTML = (field.chain ? '<span class="main"><i>1</i>主干：这一学期走到哪</span>' : "")
          + '<span class="now"><i></i>你在这里</span><span class="past"><i></i>已走过</span><span><i></i>还可以去</span>'
          + `<span>${field.chain ? "节点：二十分钟一件事；点主干看过关标准" : "点任一节点看说明"}</span>`;
        stage.innerHTML = "";
        stage.appendChild(drawFieldTree(field, tutPicked, (node) => showTut(node.id), {
          done: hereTrail.dir === focus ? hereTrail.done : [],
          here: here && here.id,
        }, grew));
      }
    }
    grew = false;
  };

  paint();
  api("GET", `/api/onboard/result?uid=${S.uid}`).then((onboard) => {
    if (stale(seq)) return;
    const facts = ((onboard && onboard.facts) || []).filter((f) => f.status !== "deleted" && f.status !== "dismissed");
    const dirs = facts.filter((f) => (f.key || "").startsWith("direction:") && (f.status === "confirmed" || f.status === "active"));
    const chosen = dirs[dirs.length - 1];
    const code = chosen ? chosen.key.split(":")[1] : "";
    if (code && FIELD_TREES[code] && !trail().code) {
      const merged = mergeTrail(code, serverTasks);
      merged.dir = merged.dir || (window.RG_DIR && RG_DIR.legacyDir[code]) || "";
      saveTrail(merged);
      chosenCode = code;
      chosenDir = merged.dir;
      focus = chosenDir || focus;
      branch = (window.RG_DIR && RG_DIR.l1Of(focus)) || branch;
      S.dirFocus = focus;
      S.dirBranch = branch;
      touched = true;
      grew = true;
      paint();
    } else if (trail().code && trail().code !== chosenCode) {
      const t = trail();
      chosenCode = t.code;
      chosenDir = t.dir || chosenDir;
      paint();
    }
  }).catch(() => {});
  api("GET", `/api/directions/recommend?uid=${S.uid}`).then((rec) => {
    if (stale(seq)) return;
    cards = rec.cards || [];
    recommended = new Set(cards.map((c) => c.direction && c.direction.code).filter(Boolean));
    const marks = recDirIds(recommended);
    if (!touched && marks.size && window.RG_DIR) {
      const top = cards[0] && cards[0].direction && cards[0].direction.code;
      const id = RG_DIR.legacyDir[top];
      if (id) {
        focus = id;
        branch = RG_DIR.l1Of(id);
        S.dirFocus = focus;
        S.dirBranch = branch;
        grew = true;
      }
    }
    paint();
  }).catch(() => {});
}

async function loadCourses(box, query) {
  const status = box.querySelector(".course-status");
  try {
    const r = await api("GET", `/api/explore/courses?query=${encodeURIComponent(query)}&limit=6`);
    if (!r.ok) { status.textContent = `检索失败（如实说明）：${r.error || "未知错误"}`; return; }
    status.remove();
    const mark = r.source === "catalog" ? "快照" : "实时";
    const head = box.querySelector("h4");
    if (head) head.dataset.source = mark;
    if (!r.items || !r.items.length) {
      box.appendChild(el("div", "course-status", `「${esc(query)}」没有对上的课。查的是本学期公开课快照，对不上就空着。`));
      return;
    }
    r.items.forEach((it) => {
      const name = it.name || it.courseName || "(未命名课程)";
      const names = (it.teacher_names && it.teacher_names.length)
        ? it.teacher_names
        : String(it.teachers || it.teacher || "").split(/[/／、;；,，|]+/).map((n) => n.trim()).filter((n) => n.length >= 2);
      const dept = it.department || it.dept || "";
      const row = el("div", "course-item", `${esc(name)}<br>`);
      const meta = el("span", "meta");
      names.forEach((n, i) => {
        if (i) meta.appendChild(document.createTextNode(" "));
        const b = el("button", "teacher-link", esc(n));
        b.type = "button";
        b.addEventListener("click", (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          showTeacher(n);
        });
        meta.appendChild(b);
      });
      const extra = [dept, r.term].filter(Boolean).join(" · ");
      if (extra) meta.appendChild(document.createTextNode((names.length ? " · " : "") + extra));
      row.appendChild(meta);
      box.appendChild(row);
    });
  } catch (e) {
    status.textContent = `检索失败（如实说明）：${e.message}`;
  }
}

async function showTeacher(name) {
  name = String(name || "").trim();
  if (!name) return;
  document.getElementById("teacherSheet")?.remove();
  const sheet = el("div", "node-sheet teacher-sheet");
  sheet.id = "teacherSheet";
  const head = el("div", "sheet-head");
  head.appendChild(el("span", "sheet-kicker", "授课老师"));
  const back = el("button", "sheet-x", "返回");
  back.type = "button";
  back.onclick = () => sheet.remove();
  head.appendChild(back);
  sheet.appendChild(head);
  sheet.appendChild(el("h3", "", esc(name)));
  const status = el("p", "sheet-intro", "正在读本学期快照…");
  sheet.appendChild(status);
  document.body.appendChild(sheet);
  back.focus({ preventScroll: true });
  try {
    const r = await api("GET", `/api/explore/teachers?name=${encodeURIComponent(name)}`);
    if (!r.ok) { status.textContent = r.error || "快照里没有这位老师。"; return; }
    status.remove();
    if (r.departments && r.departments.length) {
      sheet.appendChild(el("p", "sheet-kicker", esc(r.departments.join(" · "))));
    }
    if (r.bio) sheet.appendChild(el("p", "sheet-intro", esc(r.bio)));
    else sheet.appendChild(el("p", "sheet-intro", "没有对上北京大学的公开学者简介，这里只列出本学期教的课。"));
    sheet.appendChild(el("p", "sheet-label", `本学期课程 · ${esc(r.term || "")}`));
    (r.courses || []).forEach((c) => {
      sheet.appendChild(el("div", "course-item", `${esc(c.name || "")}<br><span class="meta">${esc([c.department, c.credits].filter(Boolean).join(" · "))}</span>`));
    });
  } catch (e) {
    status.textContent = e.message;
  }
}

/* ---------- ⑤⑥ 工作台 ---------- */

function readDraft(tid) {
  try {
    const key = `rg_draft_${S.uid}_${tid}`;
    const cur = localStorage.getItem(key);
    if (cur !== null) { localStorage.removeItem("rg_draft_" + tid); return cur; }  // 新键在就以它为准，旧键别留着日后冒出来
    // 升级前的草稿存在不带 uid 的键里：能打开这道任务的就是它的主人（任务 id 是这个人的），搬到新键、删掉旧键。
    // 原来升级之后没交的草稿就看不见了（Codex 复现）。搬不过去（存储满了）也照样显示，旧键留着
    const old = localStorage.getItem("rg_draft_" + tid);
    if (old) {
      try { localStorage.setItem(key, old); localStorage.removeItem("rg_draft_" + tid); } catch (_) { /* 存不了：下次再搬 */ }
    }
    return old || "";
  } catch (_) { return ""; }
}
function readDraftMeta(tid) {
  let m = null;
  try { m = JSON.parse(localStorage.getItem(`rg_draft_meta_${S.uid}_${tid}`) || "null"); } catch (_) { m = null; }
  if (m && typeof m === "object" && typeof m.base_rev === "number" && typeof m.edit_seq === "number") {
    return { base_rev: m.base_rev, edit_seq: m.edit_seq, dirty: !!m.dirty, conflict: !!m.conflict };
  }
  // 升级前的本地稿没有 rev：当作未同步的冲突稿先展示，不静默上传（排序只认服务端 rev，客户端时钟不算数）
  const legacy = !!readDraft(tid) || !!(m && typeof m === "object" && m.dirty);  // 旧 dirty 空稿是没同步上去的清空
  return { base_rev: 0, edit_seq: 0, dirty: legacy, conflict: legacy };
}
function writeDraftMeta(tid, meta) {
  try { localStorage.setItem(`rg_draft_meta_${S.uid}_${tid}`, JSON.stringify(meta || {})); } catch (_) { /* ignore */ }
}
/* 本地记录 {text, base_rev, edit_seq, dirty}；每任务最多一个 PUT 在途。
   text 为 undefined 时只把 dirty 的本地稿推上去（hydrate、冲突按钮用）。 */
function writeDraft(tid, text) {
  const sync = (S.draftSync = S.draftSync || {});
  const st = sync[tid] || (sync[tid] = { flight: null, onConflict: null });
  if (text !== undefined) {
    const prev = readDraftMeta(tid);
    try {
      localStorage.setItem(`rg_draft_${S.uid}_${tid}`, text || "");  // 空稿也存：清空也要同步上去
      localStorage.removeItem("rg_draft_" + tid);
    } catch (_) { /* 存不了就算了，不影响提交 */ }
    writeDraftMeta(tid, { base_rev: prev.base_rev, edit_seq: prev.edit_seq + 1, dirty: true, conflict: prev.conflict });
  }
  if (!S.token) return Promise.resolve();
  if (st.flight) return st.flight;  // 在途那笔回来后会接着发最新稿
  const run = async () => {
    for (;;) {
      const sent = readDraftMeta(tid);
      if (!sent.dirty || sent.conflict) return;
      let r;
      try {
        r = await api("PUT", `/api/tasks/${tid}/draft`, { text: readDraft(tid), base_rev: sent.base_rev });
      } catch (e) {
        if (e && e.status === 409) {
          writeDraftMeta(tid, Object.assign(readDraftMeta(tid), { conflict: true }));
          if (st.onConflict) st.onConflict();
        } else if (typeof toast === "function") toast("草稿未同步，联网后重试");
        return;  // 断网：dirty 留着，下次输入或打开再试
      }
      const cur = readDraftMeta(tid);
      const rev = r && typeof r.rev === "number" ? r.rev : sent.base_rev + 1;
      if (rev < cur.base_rev) continue;  // 更新的 GET 已确认过更高 rev：旧 ack 不降版本
      // 旧 ack 只推进 base_rev；这期间又改过就不清 dirty，接着发
      writeDraftMeta(tid, { base_rev: rev, edit_seq: cur.edit_seq, dirty: cur.edit_seq !== sent.edit_seq, conflict: cur.conflict });
      if (cur.edit_seq === sent.edit_seq) return;
    }
  };
  st.flight = run().finally(() => { st.flight = null; });
  return st.flight;
}
async function hydrateDraft(tid, ta) {
  if (!S.token) return;
  const sync = (S.draftSync = S.draftSync || {});
  const st = sync[tid] || (sync[tid] = { flight: null, onConflict: null });
  const url = `/api/tasks/${tid}/draft`;
  if (st.ta !== ta) { st.btn?.remove(); st.btn = null; st.ta = ta; }  // 按钮属于当前表单
  st.onConflict = () => {
    if (!ta || st.btn || !document.createElement) return;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn ghost small";
    b.textContent = "用本地稿覆盖云端";
    b.title = "草稿已在别处更新，本地稿已保留";
    b.onclick = async () => {
      b.disabled = true;
      try {
        const r = await api("GET", url);
        const cur = readDraftMeta(tid);
        writeDraftMeta(tid, { base_rev: r && typeof r.rev === "number" ? r.rev : 0, edit_seq: cur.edit_seq, dirty: true, conflict: false });
        await writeDraft(tid);
      } catch (_) { /* 断网：按钮留着再点 */ }
      const after = readDraftMeta(tid);
      if (after.conflict || after.dirty) { b.disabled = false; return; }
      b.remove();
      st.btn = null;
    };
    st.btn = b;
    if (ta.insertAdjacentElement) ta.insertAdjacentElement("afterend", b);
  };
  const seq0 = readDraftMeta(tid).edit_seq;
  let r;
  try { r = await api("GET", url); } catch (_) { return; }  // 断网先用本地
  const local = readDraftMeta(tid);
  const text = (r && r.text) || "";
  const rev = r && typeof r.rev === "number" ? r.rev : 0;
  if (rev >= local.base_rev && local.edit_seq === seq0) {  // 比本地旧的快照、GET 等待期间有编辑，都不碰输入框
    if (!local.dirty || readDraft(tid) === text) {
      try { localStorage.setItem(`rg_draft_${S.uid}_${tid}`, text); } catch (_) { /* 存不了就只填进框里 */ }
      writeDraftMeta(tid, { base_rev: rev, edit_seq: local.edit_seq, dirty: false, conflict: false });
      if (ta) ta.value = text;
    }
  }
  if (readDraftMeta(tid).conflict) st.onConflict();
  await writeDraft(tid);
}

function renderProjectsSoon() {
  $app.innerHTML = "";
  $app.appendChild(workspaceHead("项目", "这一栏还在做，先用今日和研读。"));
  $app.appendChild(emptyPanel("项目栏还在做。已经选过或交过的项目还在库里，导出账号能带出来。", "回今日", "today"));
}

function renderPositionSoon() {
  $app.innerHTML = "";
  $app.appendChild(workspaceHead("定位", "这一栏还在做。"));
  $app.appendChild(emptyPanel("定位栏还在做。先用今日和研读。", "回今日", "today"));
}

function emptyPanel(text, label, view) {
  const p = el("div", "panel");
  p.appendChild(el("p", "panel-sub", text));
  if (label) {
    const b = el("button", "btn", label);
    b.type = "button";
    b.style.marginTop = "16px";
    b.onclick = () => setView(view);
    p.appendChild(b);
  }
  return p;
}

/* ---------- 任务渲染：对话出的和树上出的用同一套 ---------- */

/* 「填入演示示例」用的样例：演示时手上没有材料，点一下就有内容可提交。
   它存在的唯一目的是别让演示卡在「没东西可写」。 */
const DEMO_ANSWER =
  "10 条弹幕：「太好哭了」「就这？」「编剧封神」「注水严重」「封神」「看不下去了」「细节绝了」「一般」「泪目」「神剧」\n\n" +
  "不一致例子 1：「太好哭了」——规则判积极（含「哭」可能误判消极），模型判积极。原因：规则把「哭」当消极词，但语境是感动。\n" +
  "不一致例子 2：「就这？」——规则因为无情感词判中性，模型判消极。原因：反问语气规则抓不到，模型学了语料里的讽刺用法。\n" +
  "不一致例子 3：「封神」——规则词表里没有，判中性；模型判积极。原因：网络新词，词表更新慢，模型能从上下文推断。\n\n" +
  "总结：模型的错误多来自词表覆盖与语境缺失两类；因为规则的可解释性和模型的表达力正好互补，可以互为校验。所以每次重要判断最好两个方法都跑一遍，不一致的例子就是最有价值的学习样本。";

/* 一个任务必须回答三个问题，否则用户不知道要干什么：
     做什么（步骤）· 交什么（交付物）· 怎样算做到（评分标准）
   以前只画了「步骤」和「怎样算做到」，没有「交什么」——
   所以对话里出现任务时，用户最自然的反应就是「这是要我怎么完成」。

   opts.onDone：完成后的去处（对话任务回对话，树任务进下一节点） */
function taskOriginLine(task) {
  return task.origin === "dialogue" ? "来自对话" : "来自方向";
}

function taskStemBlock(task, opts) {
  const f = task.formal || {};
  const stem = f.stem || opts.intro || task.brief || "";
  const given = Array.isArray(f.given) ? f.given.filter(Boolean) : [];
  const ask = f.ask || "";
  const deliver = task.deliverable || "一段文字：你做了什么、结果是什么、你的判断。";
  const box = el("div", "task-stem");
  if (stem) box.appendChild(el("p", "task-stem-text", esc(stem)));
  box.appendChild(el("p", "section-label", "已知"));
  if (given.length) {
    const ul = el("ul", "task-given");
    given.forEach((g) => ul.appendChild(el("li", "", esc(g))));
    box.appendChild(ul);
  } else {
    box.appendChild(el("p", "form-note", esc(f.notes || "本题不另给数据，用你能核对的一个例子。")));
  }
  box.appendChild(el("p", "section-label", "求"));
  box.appendChild(el("p", "task-ask", esc(ask || "完成本题要求的那一件事。")));
  box.appendChild(el("p", "section-label", "交"));
  const dv = el("p", "deliverable");
  dv.textContent = deliver;
  box.appendChild(dv);
  return box;
}

function taskFolds(items, startOpen) {
  const wrap = el("div", "task-folds");
  let open = startOpen || "";
  const paint = () => {
    wrap.innerHTML = "";
    const bar = el("div", "task-fold-bar");
    items.forEach((it) => {
      const btn = el("button", "btn ghost small task-fold-btn" + (open === it.key ? " on" : ""), it.label);
      btn.type = "button";
      btn.onclick = () => { open = open === it.key ? "" : it.key; paint(); };
      bar.appendChild(btn);
    });
    wrap.appendChild(bar);
    const cur = items.find((it) => it.key === open);
    if (!cur) return;
    const body = el("div", "task-fold");
    body.appendChild(cur.node());
    wrap.appendChild(body);
  };
  paint();
  return wrap;
}

function taskHowNode(task) {
  const box = el("div");
  box.appendChild(el("h3", "section-label", "① 做什么"));
  const steps = el("div", "step-list");
  (task.steps || []).forEach((st) => {
    const row = el("label", "step");
    const cb = el("input"); cb.type = "checkbox";
    row.append(cb, el("span", "", esc(st)));
    steps.appendChild(row);
  });
  box.appendChild(steps);
  box.appendChild(el("h3", "section-label", "② 交什么"));
  const dv = el("p", "deliverable");
  dv.textContent = task.deliverable || "一段文字：你做了什么、结果是什么、你的判断。";
  box.appendChild(dv);
  return box;
}

function taskRubricNode(task) {
  const box = el("div");
  box.appendChild(el("h3", "section-label", "③ 怎样算做到"));
  const crit = el("ul", "criteria");
  (task.rubric || []).forEach((r2) => crit.appendChild(el("li", "", esc(r2.criterion))));
  box.appendChild(crit);
  return box;
}

function taskPanel(task, opts) {
  opts = opts || {};
  const p = el("div", "panel task-panel");
  if (task.status === "done") {
    p.innerHTML = "";
    taskPanelDone(p, task, opts);
    return p;
  }
  const head = el("div", "task-head");
  const hl = el("div");
  hl.appendChild(el("h2", "task-title", esc(opts.title || task.title)));
  head.appendChild(hl);
  head.appendChild(el("span", "task-meta", `约 ${task.time_budget_min} 分钟`));
  p.appendChild(head);
  p.appendChild(el("p", "task-origin", taskOriginLine(task)));
  p.appendChild(taskStemBlock(task, opts));

  const folds = [];
  folds.push({ key: "how", label: "题目说明", node: () => taskHowNode(task) });
  folds.push({ key: "rubric", label: "怎样算做到", node: () => taskRubricNode(task) });
  if (opts.note) {
    folds.push({
      key: "path",
      label: "路径说明",
      node: () => opts.note() || el("p", "form-note", "这一题不挂在路径步骤上。"),
    });
  }
  p.appendChild(taskFolds(folds, ""));

  const box = el("div", "submit-box");
  const ta = el("textarea");
  ta.placeholder = "按「交」里要求的写。没做完也可以交。";
  ta.setAttribute("aria-label", "提交内容");
  ta.value = readDraft(task.id);
  hydrateDraft(task.id, ta).then(() => { if (ta.value) recount(); }).catch(() => {});
  const count = el("span", "hint");
  const recount = () => {
    count.textContent = ta.value.trim()
      ? `已写 ${ta.value.trim().length} 字 · 草稿自动保存在本机`
      : "提交后按三条逐条反馈，并写回你的记录。";
  };
  ta.addEventListener("input", () => { writeDraft(task.id, ta.value); recount(); });
  recount();
  const submit = el("button", "btn", "提交");
  submit.type = "button";
  submit.onclick = async (ev) => {
    ev.preventDefault();
    if (ta.value.trim().length < 10) { toast("至少写一句话再提交"); ta.focus(); return; }
    submit.disabled = true; submit.textContent = "正在逐条看…";
    try {
      const fb = await api("POST", `/api/tasks/${task.id}/submit`, { uid: S.uid, payload: ta.value });
      S.lastFeedback = fb;
      S.lastFeedbackTaskId = task.id;
      sessionStorage.setItem("rg_fb_" + task.id, JSON.stringify(fb));
      writeDraft(task.id, "");
      S.newFactIds = (fb.learned_facts || []).map((f) => f.id);
      task.status = "done";
      p.innerHTML = "";
      taskPanelDone(p, task, opts);
      setTimeout(() => document.getElementById("fbPanel")?.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
    } catch (e) { toast(e.message); submit.disabled = false; submit.textContent = "提交"; }
  };
  const acts = el("div", "submit-actions");
  if (opts.demo) {
    const demo = el("button", "btn ghost small", "填入演示示例");
    demo.type = "button";
    demo.onclick = () => { ta.value = DEMO_ANSWER; writeDraft(task.id, ta.value); recount(); };
    acts.append(submit, demo, count);
  } else {
    acts.append(submit, count);
  }
  box.append(ta, acts);
  p.appendChild(box);
  p.appendChild(taskHintBox(task, () => ta.value));
  return p;
}

function taskHintBox(task, draftOf) {
  const box = el("div", "hint-box");
  const open = el("button", "btn ghost small", "卡住了，要提示");
  open.type = "button";
  const pane = el("div", "hint-pane");
  pane.hidden = true;
  const log = el("div", "hint-log");
  const chips = el("div", "task-fold-bar");
  [["这题在问什么", "这题在问什么"], ["第一步做什么", "第一步做什么"], ["我写的像不像", "我写的像不像"]].forEach(([label, q]) => {
    const b = el("button", "chip", label);
    b.type = "button";
    b.onclick = () => sendHint(q);
    chips.appendChild(b);
  });
  const row = el("div", "chat-input-row");
  const input = el("input");
  input.placeholder = "说你卡在哪";
  input.maxLength = 200;
  const send = el("button", "btn small", "发送");
  send.type = "button";
  row.append(input, send);
  pane.append(log, chips, row);
  let busy = false;
  async function sendHint(q) {
    const question = (q || input.value || "").trim();
    if (question.length < 2 || busy) return;
    busy = true;
    send.disabled = true;
    log.appendChild(el("p", "hint-q", esc(question)));
    const wait = el("p", "hint-a", "…");
    log.appendChild(wait);
    try {
      const r = await api("POST", `/api/tasks/${task.id}/hint`, {
        uid: S.uid, question, draft: draftOf ? draftOf() : "",
      });
      wait.textContent = r.reply || "";
    } catch (e) {
      wait.textContent = e.message || "这次没问到";
    } finally {
      busy = false;
      send.disabled = false;
      input.value = "";
    }
  }
  send.onclick = () => sendHint("");
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) sendHint(""); });
  open.onclick = () => { pane.hidden = !pane.hidden; };
  box.append(open, pane);
  return box;
}

function taskPanelDone(p, task, opts) {
  const head = el("div", "task-head");
  const hl = el("div");
  hl.appendChild(el("h2", "task-title", esc(opts.title || task.title)));
  head.appendChild(hl);
  head.appendChild(el("span", "task-meta done", "已完成"));
  p.appendChild(head);
  p.appendChild(el("p", "task-origin", taskOriginLine(task)));
  p.appendChild(taskStemBlock(task, opts));
  const foldsNode = el("div");
  p.appendChild(foldsNode);
  const hasFb = () => !!(S.lastFeedback && S.lastFeedbackTaskId === task.id);
  const paintFolds = () => {
    const folds = [
      { key: "how", label: "题目说明", node: () => taskHowNode(task) },
      { key: "rubric", label: "怎样算做到", node: () => taskRubricNode(task) },
    ];
    if (hasFb()) {
      folds.push({ key: "fb", label: "评阅", node: () => {
        const box = el("div");
        renderFeedbackInto(box, { hideNext: true });
        return box;
      } });
      if (S.lastFeedback.next_hint) {
        folds.push({ key: "next", label: "下一步", node: () => el("div", "why-box", `<b>下一步　</b>${esc(S.lastFeedback.next_hint)}`) });
      }
    }
    if (opts.note) {
      folds.push({
        key: "path",
        label: "路径说明",
        node: () => opts.note() || el("p", "form-note", "这一题不挂在路径步骤上。"),
      });
    }
    foldsNode.innerHTML = "";
    foldsNode.appendChild(taskFolds(folds, hasFb() ? "fb" : ""));
  };
  paintFolds();
  if (!hasFb()) {
    // 这次会话里没有这条反馈（刷新过、或从别的任务切回来）：从服务器取最近一次提交的反馈，到了再把「评阅」折叠画上。原来就看不到了（Codex 复现）
    api("GET", `/api/tasks/${encodeURIComponent(task.id)}`).then((t) => {
      if (!t || !t.feedback || !foldsNode.isConnected) return;
      S.lastFeedback = t.feedback;
      S.lastFeedbackTaskId = task.id;
      paintFolds();
    }).catch(() => { /* 取不到就不显示，不挡任务页 */ });
  }
  if (opts.onDone) p.appendChild(opts.onDone());
}

/* 对话给你的任务：**和知识树完全无关**，没有选方向也要显示。
   以前这里是整页 return，所以「对话出了任务但任务区是空的」。 */
function dialogueTaskSection(tasks) {
  const box = el("div", "panel dialogue-tasks");
  box.appendChild(el("h3", "section-label", "对话给你的这一步"));
  if (!tasks.length) {
    box.appendChild(el("p", "panel-sub",
      "还没有对话里接下来要做的一步。去对话看右边的方向排序，或去今日读一篇。"));
    return box;
  }
  const open = tasks.filter((tk) => tk.status !== "done");
  const done = tasks.filter((tk) => tk.status === "done");
  const pick = tasks.find((tk) => tk.id === S.openTaskId) || open[open.length - 1] || done[done.length - 1];
  if (pick) S.openTaskId = pick.id;

  if (tasks.length > 1) {
    const row = el("div", "task-chips");
    tasks.forEach((tk) => {
      const b = el("button", `chip${tk.id === S.openTaskId ? " chip-on" : ""}`,
        `${tk.status === "done" ? "✓ " : ""}${esc(tk.title)}`);
      b.type = "button";
      b.onclick = () => { S.openTaskId = tk.id; setView("workbench"); };
      row.appendChild(b);
    });
    box.appendChild(row);
  }
  if (pick) box.appendChild(taskPanel(pick, {}));
  return box;
}

async function renderWorkbench() {
  const seq = S.renderSeq;
  await ensurePortrait();
  if (stale(seq)) return;
  let tasksFailed = false;
  const [taskRes, onboard] = await Promise.all([
    api("GET", `/api/tasks?uid=${S.uid}`).catch(() => { tasksFailed = true; return { tasks: [] }; }),
    api("GET", `/api/onboard/result?uid=${S.uid}`).catch(() => null),
  ]);
  if (stale(seq)) return;
  $app.innerHTML = "";
  const wrap = el("div", "stagger");
  wrap.appendChild(workspaceHead("这一步", "先看题。做完再对。"));
  $app.appendChild(wrap);
  if (tasksFailed) {
    // 没取到不等于没有：原来当成空列表往下走，进度被对成「没做过」、当前节点又生成一份新作业，做完的变回没做（Codex 复现）
    wrap.appendChild(emptyPanel("作业列表这次没取到（网络或服务器忙），已经做过的都还在。", "再试一次", "workbench"));
    return;
  }

  const listed = taskRes.tasks || [];
  wrap.appendChild(roomGuide("workbench", listed.some((tk) => tk.status === "open" || tk.status === "done")));

  /* ① 对话给你的任务：先渲染，且不受「有没有选方向」影响 */
  const fromDialogue = listed.filter((tk) => tk.origin === "dialogue");
  wrap.appendChild(dialogueTaskSection(fromDialogue));

  /* ② 方向路径 */
  const facts = ((onboard && onboard.facts) || []).filter((f) => f.status !== "deleted" && f.status !== "dismissed");
  let t = adoptDirection(facts);
  if (t.code) {
    const merged = mergeTrail(t.code, listed);
    if (merged.done.join(",") !== t.done.join(",")) {
      t = merged;
      saveTrail(t);
    }
  }
  const code = t.code;
  const field = activeField(t);
  if (!code || !field) {
    // 注意：不再整页 return——上面已经画过对话任务了
    if (!fromDialogue.length) {
      wrap.appendChild(emptyPanel("还没有要交的。去对话里说说手头的事，或先选定一个方向。", "去对话", "dialogue"));
    }
    return;
  }
  const node = currentOnPath(field, t.done);
  if (!node) {
    wrap.appendChild(emptyPanel(`「${dirTitle(t.dir, field)}」这条教程上的节点都走完了。`, "回方向区换一个方向", "cards"));
    return;
  }
  const expect = node.label.slice(0, 40);
  const boundId = (t.tasks || {})[node.id];
  let task = boundId ? listed.find((tk) => tk.id === boundId) : null;
  if (task && task.title !== expect) task = null;
  if (!task) task = [...listed].reverse().find((tk) => tk.title === expect && tk.direction === code) || null;
  if (!task) {
    task = await api("POST", "/api/tasks/generate", {
      uid: S.uid, direction: code, level: 1, title: node.label, brief: node.intro,
    });
    if (stale(seq)) return;
  }
  if (!task || task.title !== expect) {
    wrap.appendChild(emptyPanel(`当前节点是「${node.label}」，但任务服务还在用旧题目。请重新启动本地服务后再打开任务区。`));
    return;
  }
  t.tasks = Object.assign({}, t.tasks, { [node.id]: task.id });
  saveTrail(t);
  S.lastTask = task;
  if (task.status === "done" && S.lastFeedbackTaskId !== task.id) {
    try {
      const saved = JSON.parse(sessionStorage.getItem("rg_fb_" + task.id) || "null");
      if (saved) {
        S.lastFeedback = saved;
        S.lastFeedbackTaskId = task.id;
      }
    } catch (_) { /* 没有存过这次反馈 */ }
  }
  const path = pathNodes(field);
  const stepNo = path.findIndex((n) => n.id === node.id) + 1;
  wrap.appendChild(el("div", "ws-status",
    `<span>${esc(dirTitle(t.dir, field))}</span><span>第 ${stepNo} / ${path.length} 个节点</span>`));

  /* 路径步骤说明：主干步骤要交的是学期尺度的东西，二十分钟任务只是入门。
     返回节点而不是直接 append——面板是 taskPanel 画的，插在哪由它决定。 */
  const stepNote = () => {
    const step = stepOf(field, node.id);
    if (!step) return null;
    const note = el("div", "note-box step-note");
    note.innerHTML = `${node.main ? "这是" : "这一节点属于"}路径第 ${step.stage} 步「${esc(step.label)}」。这一步做完要交：${esc(step.done_when)}`;
    return note;
  };

  /* 树上的任务和对话给的任务共用 taskPanel（对话那条路在 chat.js 里也调它）。
     差别只在完成后的去处，所以用 onDone 注入：
     树上任务要「进入下一节点」，教程走完给「回方向区换一个方向」，对话任务回对话。 */
  wrap.appendChild(taskPanel(task, {
    title: node.label,
    intro: node.intro || task.brief,
    demo: true,
    note: stepNote,
    onDone: () => {
      const next = currentOnPath(field, t.done.concat(node.id));
      const acts = el("div", "submit-actions");
      if (next) {
        const go = el("button", "btn", `进入「${esc(next.label)}」`);
        go.type = "button";
        go.onclick = async () => {
          go.disabled = true;
          try {
            const created = await api("POST", "/api/tasks/generate", {
              uid: S.uid, direction: code, level: 1, title: next.label, brief: next.intro,
            });
            if (!created || created.title !== next.label.slice(0, 40)) {
              toast("下一节点的任务没有生成，请重启本地服务后再试");
              go.disabled = false;
              return;
            }
            const tasksMap = Object.assign({}, t.tasks, { [next.id]: created.id });
            saveTrail({ code, done: t.done.concat(node.id), tasks: tasksMap });
            S.lastTask = created;
            S.lastFeedback = null;
            S.lastFeedbackTaskId = "";
            setView("workbench");
          } catch (e) {
            toast(e.message);
            go.disabled = false;
          }
        };
        acts.appendChild(go);
      }
      if (!next) {
        const back = el("button", "btn", "回方向区换一个方向");
        back.type = "button";
        back.onclick = () => setView("cards");
        acts.appendChild(back);
      }
      const me = el("button", "btn ghost", "看它记下了什么");
      me.type = "button";
      me.onclick = () => setView("me");
      acts.appendChild(me);
      return acts;
    },
  }));
}

/* ---------- ⑦ 反馈 ---------- */

function renderFeedbackInto(p, opts) {
  opts = opts || {};
  const fb = S.lastFeedback;
  if (!fb) return;
  const box = el("section", "feedback");
  box.id = "fbPanel";
  const head = el("div", "feedback-head");
  const hl = el("div");
  hl.appendChild(el("h3", "", "反馈"));
  if (fb.encouragement) hl.appendChild(el("p", "", esc(fb.encouragement)));
  head.appendChild(hl);
  const passed = (fb.rubric || []).filter((r) => r.pass).length;
  head.appendChild(el("div", "score-ring", `<span class="num">${passed}</span>/ ${(fb.rubric || []).length} 条做到`));
  box.appendChild(head);
  if ((fb.rubric || []).length) {
    const bar = el("div", "pass-bar");
    (fb.rubric || []).forEach((r) => bar.appendChild(el("i", r.pass ? "ok" : "bad")));
    box.appendChild(bar);
  }

  const rub = el("div", "rubric-list");
  (fb.rubric || []).forEach((r) => {
    rub.appendChild(el("div", `rubric-item ${r.pass ? "pass" : "fail"}`,
      `<span class="rubric-mark" aria-label="${r.pass ? "做到" : "还没做到"}">${r.pass ? "✓" : "!"}</span><div><p class="rubric-crit">${esc(r.criterion)}</p><p class="rubric-comment">${esc(r.comment)}</p></div>`));
  });
  box.appendChild(rub);
  if (fb.next_hint && !opts.hideNext) box.appendChild(el("div", "why-box", `<b>下一步　</b>${esc(fb.next_hint)}`));

  if (fb.learned_facts && fb.learned_facts.length) {
    const fl = el("div", "fact-list");
    fb.learned_facts.forEach((f) => fl.appendChild(factCard(f, false, true)));
    box.appendChild(fl);
  }
  p.appendChild(box);
}

/* ---------- 今日 ---------- */

function loopStrip(steps, nowIdx) {
  const strip = el("nav", "loop-strip");
  strip.setAttribute("aria-label", "科研入门闭环");
  steps.forEach((s, i) => {
    const cls = "loop-step" + (s.done ? " done" : "") + (i === nowIdx ? " now" : "");
    const b = el("button", cls, `<i></i><span>${esc(s.label)}</span>`);
    b.type = "button";
    b.title = s.done ? `${s.label}：已走过` : i === nowIdx ? `${s.label}：当前这一步` : `${s.label}：还没到`;
    if (i === nowIdx) b.setAttribute("aria-current", "step");
    b.onclick = () => setView(s.view);
    strip.appendChild(b);
  });
  return strip;
}

function goFindProjects(keywords) {
  // 进「项目 · 找项目」，方向和阶段交给服务端按记录预选（/api/projects/context）
  S.projectForm = null;
  S.projectResult = null;
  S.projectTab = "find";
  if (keywords) S.projectKeywords = keywords;
  setView("projects");
}

function openProject(pid) {
  S.projectId = pid;
  setView("project");
}

const PROJECT_AFTER_TASKS = 3; // 在一个方向交过几次小任务之后，开始建议找项目练手

async function renderToday() {
  const seq = S.renderSeq;
  await ensurePortrait();
  if (stale(seq)) return;
  $app.innerHTML = "";
  $app.appendChild(skeleton(3));
  const [taskRes, onboard] = await Promise.all([
    api("GET", `/api/tasks?uid=${S.uid}`).catch(() => null),
    api("GET", `/api/onboard/result?uid=${S.uid}`).catch(() => null),
  ]);
  if (stale(seq)) return;
  adoptDirection((onboard && onboard.facts) || []);
  paintChrome();
  let saved = trail();
  if (taskRes && saved.code) {
    const merged = mergeTrail(saved.code, taskRes.tasks || []);
    if (merged.done.join(",") !== saved.done.join(",")) {
      saveTrail(merged);
      saved = trail();
    }
  }
  const field = activeField(saved);
  const node = field ? currentOnPath(field, saved.done) : null;
  const facts = (onboard && onboard.facts) || [];
  const talked = S.entered || (onboard && onboard.state && onboard.state.phase === "done");
  const drafts = facts.filter((f) => f.status === "draft").length;
  const doneTasks = ((taskRes && taskRes.tasks) || []).filter((t) => t.status === "done" && t.direction === saved.code).length;

  // 每条建议：一个主动作 + 至多一个备选，落到一件具体的事
  let title; let why; let primary; let alt = null;
  const goView = (label, view) => ({ label, run: () => setView(view) });
  if (!field && !talked) {
    title = "先聊聊你现在的情况";
    why = "年级、已经会的、想试什么。说不清就说不知道。聊完再看方向。";
    primary = goView("去对话", "dialogue");
  } else if (!field && drafts) {
    title = `核对这些记录（${drafts} 条）`;
    why = "刚才记下的还是草稿。改掉不对的、划掉不属实的，方向才按你来。";
    primary = goView("去我的页面核对", "me");
  } else if (!field) {
    title = "先选定一个方向";
    why = "按你刚说的会标出建议。确认一个，这一步从它的教程起点开始。";
    primary = goView("去选方向", "cards");
  } else if (node) {
    title = `继续「${node.label}」`;
    why = node.intro;
    primary = goView("去做这一步", "workbench");
  } else {
    title = `「${field.name}」这条路已经走到头`;
    why = "回方向区换一棵树，或先去研读。";
    primary = goView("回方向区", "cards");
    alt = goView("去研读", "read");
  }

  $app.innerHTML = "";
  const wrap = el("div", "stagger");
  wrap.appendChild(workspaceHead("今日", "打开就看这一件。"));
  const status = el("div", "status-chips");
  status.appendChild(el("span", "status-chip" + (field ? " on" : ""), field ? esc(dirTitle(saved.dir, field)) : "还没有方向"));
  if (node) status.appendChild(el("span", "status-chip", `正在「${esc(node.label)}」`));
  if (doneTasks) status.appendChild(el("span", "status-chip", `交过 ${doneTasks} 次`));
  wrap.appendChild(status);
  const confirmed = facts.filter((f) => f.status === "confirmed" || f.status === "active").length;
  const steps = [
    { label: "聊过", done: !!talked, view: "dialogue" },
    { label: "我的", done: !!talked && drafts === 0 && confirmed > 0, view: "me" },
    { label: "方向", done: !!field, view: "cards" },
    { label: "这一步", done: doneTasks > 0, view: "workbench" },
  ];
  wrap.appendChild(loopStrip(steps, steps.findIndex((s) => !s.done)));
  const ruleCard = el("div", "nba-card");
  ruleCard.appendChild(el("h3", "nba-title", esc(title)));
  ruleCard.appendChild(el("p", "nba-why", esc(why)));
  const act = el("div", "submit-actions");
  const go = el("button", "btn", esc(primary.label));
  go.type = "button";
  go.onclick = primary.run;
  act.appendChild(go);
  if (alt) {
    const other = el("button", "btn ghost", esc(alt.label));
    other.type = "button";
    other.onclick = alt.run;
    act.appendChild(other);
  }
  ruleCard.appendChild(act);
  const showPapers = !!(talked || field);
  if (!showPapers) wrap.appendChild(ruleCard);
  else {
    const slot = el("div", "today-papers panel daily", '<p class="panel-sub">正在取最近的新论文…</p>');
    wrap.appendChild(slot);
    const next = el("p", "today-next", `继续：${esc(title)} `);
    const more = el("button", "linkish", esc(primary.label));
    more.type = "button";
    more.onclick = primary.run;
    next.appendChild(more);
    wrap.appendChild(next);
    dailyBlock(seq).then((box) => { if (!stale(seq)) slot.replaceWith(box); });
  }
  $app.appendChild(wrap);
}

/* ---------- 研读：工具包 / 阅读卡 / 矩阵（docs/DESIGN_PROPOSAL.md §1C） ---------- */

const DEFAULT_KIT = "llm-eval";
const DIR_LABEL = { up: "↑ 提升", down: "↓ 下降 / 失效", mixed: "~ 有好有坏", none: "? 没报告" };

function readTabs(active) {
  document.body.dataset.readTab = active || "kit";
  const nav = el("nav", "ws-tabs");
  [["kit", "工具包"], ["cards", "阅读卡"], ["matrix", "矩阵"]].forEach(([key, label]) => {
    const b = el("button", `ws-tab${key === active ? " on" : ""}`, label);
    b.type = "button";
    b.onclick = () => { S.readTab = key; setView("read"); };
    nav.appendChild(b);
  });
  return nav;
}

function openCard(arxivId, cardKit) {
  S.cardId = arxivId;
  if (cardKit !== undefined) S.cardKit = cardKit || "";
  setView("card");
}

function useKit(kitId) {
  S.kitId = kitId;
  try { localStorage.setItem("rg_kit", kitId); } catch (_) { /* 存不了就每次用默认 */ }
  if (S.token) api("PUT", "/api/me/prefs", { kit_id: kitId }).catch(() => {});
}

async function renderRead() {
  const seq = S.renderSeq;
  let catalog = [];
  if (S.uid) {
    try {
      const d = await api("GET", `/api/papers/dir?uid=${encodeURIComponent(S.uid)}`);
      catalog = d.items || [];
    } catch (_) { catalog = []; }
  }
  if (!S.kitId && !catalog.length) useKit(DEFAULT_KIT);
  const kitId = S.kitId || "";
  const tab = S.readTab || "kit";
  let kit = { id: kitId, papers: [], goal: "", direction: "" }; let cards = { cards: [] };
  if (kitId) {
    try {
      kit = await api("GET", `/api/kits/${kitId}`);
    } catch (e) {
      if (stale(seq)) return;
      if (await recoverUser(e)) return;
      if (!catalog.length) {
        $app.innerHTML = "";
        $app.appendChild(workspaceHead("研读"));
        $app.appendChild(el("div", "note-box", `工具包加载失败：${esc(e.message)}`));
        return;
      }
    }
  }
  if (!S.uid) {
    if (stale(seq)) return;
    $app.innerHTML = "";
    $app.appendChild(workspaceHead("研读"));
    $app.appendChild(el("div", "note-box", "先进入启研，再看你的阅读卡。工具包本身可以稍后打开。"));
    return;
  }
  if (kitId) {
    try {
      cards = await api("GET", `/api/cards?uid=${S.uid}&kit=${kitId}`);
    } catch (e) {
      if (stale(seq)) return;
      if (await recoverUser(e)) return;
      if (!catalog.length) {
        $app.innerHTML = "";
        $app.appendChild(workspaceHead("研读"));
        $app.appendChild(el("div", "note-box", `阅读卡没取到：${esc(e.message)}`));
        return;
      }
    }
  }
  if (stale(seq)) return;
  const byPaper = Object.fromEntries((cards.cards || []).map((c) => [c.arxiv_id, c]));
  $app.innerHTML = "";
  const passed = Object.values(byPaper).filter((c) => c.status === "pass").length;
  const bar = el("header", "ws-head ws-head-bar");
  const leftH = el("div", "ws-bar-main");
  leftH.appendChild(el("h2", "", "研读"));
  leftH.appendChild(el("p", "ws-lead", esc((catalog.length ? "方向论文目录" : (kit.goal || "")).slice(0, 42))));
  // 控件不塞在标题右侧了：整行工具栏，放不下就换行，不出横向滚动条
  const rightH = el("div", "read-toolbar");
  rightH.appendChild(readTabs(tab));
  const search = el("form", "read-search");
  const q = el("input");
  q.placeholder = "英文关键词，例如 evaluation benchmark";
  q.name = "q";
  const goSearch = el("button", "btn small", "搜索");
  goSearch.type = "submit";
  search.append(q, goSearch);
  const file = el("input");
  file.type = "file";
  file.accept = "application/pdf";
  file.hidden = true;
  const up = el("button", "btn small", "导入 PDF");
  up.type = "button";
  up.onclick = () => file.click();
  file.onchange = async () => {
    const f = file.files && file.files[0];
    if (!f) return;
    try {
      const res = await apiFetch(`/api/papers/upload?uid=${S.uid}`, {
        method: "POST",
        body: f,
        headers: { "Content-Type": "application/pdf", "X-Filename": encodeURIComponent(f.name) },
      });
      if (!res.ok) {
        const t = await res.text();
        throw new Error(t || res.statusText);
      }
      const got = await res.json();
      S.cardFrom = "read";
      openCard(got.pid, "");
    } catch (e) { toast(e.message); }
  };
  rightH.append(search, up, file);
  bar.append(leftH, rightH);
  $app.appendChild(bar);
  if (!catalog.length) {
    const mismatch = kitMismatch(kit.direction, await myDirection());
    if (stale(seq)) return;
    if (mismatch) $app.appendChild(mismatch);
  }

  document.body.dataset.readTab = tab;
  const hasKitPapers = !!(kit.papers || []).length;
  if ((tab === "cards" || tab === "matrix") && !hasKitPapers) {
    const panel = el("div", "panel");
    panel.appendChild(el("p", "panel-sub", "这个方向还没有阅读卡工具包"));
    const useEval = el("button", "btn", "用 llm-eval");
    useEval.type = "button";
    useEval.onclick = () => { useKit("llm-eval"); S.readTab = tab; setView("read"); };
    panel.appendChild(useEval);
    $app.appendChild(panel);
    return;
  }
  if (tab === "cards") { renderCardList(kit, byPaper); return; }
  if (tab === "matrix") { await renderMatrix(seq, kit); return; }

  const grid = el("div", "read-grid");
  const leftPane = el("section", "read-pane");
  const leftHead = el("div", "read-pane-head");
  leftHead.appendChild(el("h3", "panel-title", "经典文献推荐"));
  leftHead.appendChild(el("small", "", `过线 ${passed} 张`));
  const leftBody = el("div", "read-pane-body");
  const recPapers = catalog.length ? catalog.map((p, i) => ({
    arxiv_id: p.paper_id || p.id,
    title: p.title,
    title_zh: p.title_zh || "",
    keywords: p.keywords || [],
    order: p.rank || i + 1,
    why: p.why || "",
    card_kit: p.card_kit || "",
    lib: p.lib || "",
  })) : (kit.papers || []);
  const paintPapers = (papers, fromSearch) => {
    leftBody.innerHTML = "";
    if (fromSearch) {
      const back = el("button", "linkish", "← 回到推荐");
      back.type = "button";
      back.onclick = () => paintPapers(recPapers, false);
      leftBody.appendChild(back);
    }
    if (!papers.length) {
      leftBody.appendChild(el("p", "panel-sub", "没有结果。arXiv 只认英文关键词。"));
      return;
    }
    const list = el("ol", "kit-papers");
    let firstUnread = true;
    papers.forEach((p, i) => {
      const aid = p.arxiv_id || p.id;
      const c = byPaper[aid];
      const unread = !c;
      const primary = !fromSearch && unread && firstUnread;
      if (unread && !fromSearch) firstUnread = false;
      const li = el("li", "kit-paper");
      const main = el("div", "kp-main");
      const kitForCard = fromSearch ? (p.card_kit || "") : (p.card_kit || (!catalog.length && kit.id) || "");
      const title = el("button", "linkish", esc(p.title));
      title.type = "button";
      title.onclick = () => { S.cardFrom = "read"; openCard(aid, kitForCard); };
      main.appendChild(el("span", "kp-n", String(p.order || i + 1)));
      main.appendChild(title);
      if (p.title_zh) {
        const zh = el("p", "kp-zh");
        zh.textContent = p.title_zh;
        main.appendChild(zh);
      }
      if ((p.keywords || []).length) {
        const kws = el("p", "kp-kw");
        (p.keywords || []).slice(0, 4).forEach((k) => {
          const s = el("span");
          s.textContent = k.zh || k.en || k;
          kws.appendChild(s);
        });
        main.appendChild(kws);
      }
      if (p.lib === "html" || p.lib === "abstract") {
        const badge = el("i", "kp-lib", p.lib === "html" ? "库内" : "只有摘要");
        main.appendChild(badge);
      }
      if (c) {
        main.appendChild(el("span", c.status === "pass" ? "kp-state pass" : "kp-state revise",
          c.status === "pass" ? `✓ 过线 v${c.version}` : `待改 v${c.version}`));
      }
      if (p.why) main.appendChild(el("p", "form-note", esc(p.why)));
      li.appendChild(main);
      const goLabel = fromSearch ? "打开" : (c ? (c.status === "pass" ? "看卡" : "改这张卡") : (kitForCard ? "写阅读卡" : "打开"));
      const go = el("button", primary ? "btn small" : "btn small secondary", goLabel);
      go.type = "button";
      go.onclick = () => { S.cardFrom = "read"; openCard(aid, kitForCard); };
      li.appendChild(go);
      list.appendChild(li);
    });
    leftBody.appendChild(list);
  };
  paintPapers(recPapers, false);
  const mineBox = el("div", "read-mine");
  try {
    const mine = await api("GET", `/api/papers/mine?uid=${S.uid}`);
    if ((mine.items || []).length) {
      mineBox.appendChild(el("p", "section-label", "我导入的"));
      (mine.items || []).forEach((it) => {
        const row = el("div", "my-dir-row");
        const open = el("button", "linkish", esc(it.title || it.pid));
        open.type = "button";
        open.onclick = () => { S.cardFrom = "read"; openCard(it.pid, ""); };
        const del = el("button", "btn small ghost", "删除");
        del.type = "button";
        del.onclick = async () => {
          try {
            await api("DELETE", `/api/papers/${encodeURIComponent(it.pid)}?uid=${S.uid}`);
            row.remove();
          } catch (e) { toast(e.message); }
        };
        row.append(open, del);
        mineBox.appendChild(row);
      });
    }
  } catch (_) { /* 没有导入就不画 */ }
  search.onsubmit = async (ev) => {
    ev.preventDefault();
    const term = q.value.trim();
    if (!term) return;
    if (/[\u4e00-\u9fff]/.test(term)) { toast("arXiv 只认英文关键词"); return; }
    try {
      const r = await api("GET", `/api/papers/search?q=${encodeURIComponent(term)}&uid=${S.uid}`);
      paintPapers(r.items || [], true);
    } catch (e) { toast(e.message); }
  };
  leftPane.append(leftHead, leftBody, mineBox);
  const side = el("div", "read-side");
  const open = el("section", "read-pane");
  open.appendChild(el("div", "read-pane-head", "<h3 class=\"panel-title\">作者自己写下的开放问题</h3>"));
  const openBody = el("div", "read-pane-body");
  (kit.open_problems || []).forEach((o) => {
    const li = el("div", "open-item");
    li.innerHTML = `<blockquote>「${esc(o.quote)}」</blockquote><p>${esc(o.note)}</p>`;
    const meta = el("small");
    const jump = el("button", "linkish", `《${esc(o.paper)}》`);
    jump.type = "button";
    jump.onclick = () => openCard(o.arxiv_id);
    meta.append(jump, document.createTextNode(` · ${o.section_label || o.section} · `));
    const ext = el("a", "", "arXiv ↗");
    ext.href = `https://arxiv.org/abs/${o.arxiv_id}`; ext.target = "_blank"; ext.rel = "noopener";
    meta.appendChild(ext);
    li.appendChild(meta);
    openBody.appendChild(li);
  });
  open.appendChild(openBody);
  const data = el("section", "read-pane");
  data.appendChild(el("div", "read-pane-head", "<h3 class=\"panel-title\">数据集与代码</h3>"));
  const dataBody = el("div", "read-pane-body");
  const dl = el("ul", "criteria");
  (kit.datasets || []).forEach((d) => dl.appendChild(el("li", "", `<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.name)} ↗</a>　${esc(d.note)}`)));
  dataBody.appendChild(dl);
  data.appendChild(dataBody);
  if (!catalog.length) side.append(open, data);
  grid.append(leftPane, side);
  $app.appendChild(grid);
}

function renderCardList(kit, byPaper) {
  const panel = el("div", "panel");
  const cards = Object.values(byPaper);
  const first = (kit.papers || [])[0];
  if (!cards.length) {
    panel.appendChild(el("p", "panel-sub", "还没有阅读卡。从「经典文献推荐」挑一篇开始。"));
    if (first) {
      panel.appendChild(el("p", "panel-sub", esc(first.title)));
      const go = el("button", "btn", "写第一张阅读卡");
      go.type = "button";
      go.style.marginTop = "16px";
      go.onclick = () => openCard(first.arxiv_id);
      panel.appendChild(go);
    }
    $app.appendChild(panel);
    return;
  }
  const rows = el("div", "fact-list");
  const order = Object.fromEntries((kit.papers || []).map((p, i) => [p.arxiv_id, i]));
  cards.sort((a, b) => (order[a.arxiv_id] ?? 99) - (order[b.arxiv_id] ?? 99)).forEach((c) => {
    const row = el("button", "mine-row");
    row.type = "button";
    const badge = c.status === "pass" ? '<span class="kp-state pass">过线</span>' : '<span class="kp-state revise">待改</span>';
    row.innerHTML = `<span class="mine-name">${esc(c.title)} ${badge}</span><span class="mine-meta">v${c.version}${c.status === "pass" ? "" : ` · ${esc(c.review.next_step || "")}`}</span>`;
    row.onclick = () => openCard(c.arxiv_id);
    rows.appendChild(row);
  });
  panel.appendChild(rows);
  $app.appendChild(panel);
}

async function renderMatrix(seq, kit) {
  const m = await api("GET", `/api/matrix?uid=${S.uid}&kit=${kit.id}`).catch((e) => ({ error: e.message }));
  if (stale(seq)) return;
  const panel = el("div", "panel");
  if (m.error) { panel.appendChild(el("div", "note-box", esc(m.error))); $app.appendChild(panel); return; }
  if (!m.ready) {
    panel.appendChild(el("p", "panel-sub", "先交一张过线的阅读卡。格子只来自你自己的过线卡。卡越多，空格和冲突才有意义。"));
    if (kit && kit.papers && kit.papers[0]) {
      panel.appendChild(el("p", "panel-sub", esc(kit.papers[0].title)));
      const go = el("button", "btn", "写第一张阅读卡");
      go.type = "button";
      go.onclick = () => openCard(kit.papers[0].arxiv_id);
      panel.appendChild(go);
    }
    $app.appendChild(panel);
    return;
  }
  const f = m.flags;
  const emptyCols = new Set(f.empty_columns);
  const emptyCells = new Set(f.empty_cells.map(([a, k]) => `${a}|${k}`));
  const conflict = new Set(m.rows.filter((r) => r.conflict).map((r) => r.arxiv_id));  // 完整成员；c.up / c.down 只是预览
  const titleOf = Object.fromEntries(m.rows.map((r) => [r.arxiv_id, clip(r.title, 28)]));
  const names = (ids, total) => ids.map((id) => `《${esc(titleOf[id] || id)}》`).join("") + (total > ids.length ? ` 等 ${total} 篇` : "");
  panel.appendChild(el("p", "panel-sub", "格子只来自你自己的过线卡。卡越多，空格和冲突才有意义。"));
  const wrap = el("div", "matrix-wrap");
  const table = el("table", "matrix");
  table.innerHTML = `<thead><tr><th>论文</th>${m.dimensions.map((d) => `<th class="${emptyCols.has(d.key) ? "col-empty" : ""}" title="${esc(d.hint || "")}">${esc(d.label)}${emptyCols.has(d.key) ? " ·空列" : ""}</th>`).join("")}</tr></thead>`;
  const tb = el("tbody");
  m.rows.forEach((r) => {
    const tr = el("tr", conflict.has(r.arxiv_id) ? "row-conflict" : "");
    tr.innerHTML = `<th><button class="linkish" type="button" title="${esc(r.title)}">${esc(clip(r.title, 40))}</button>${conflict.has(r.arxiv_id) ? " ⚡" : ""}</th>`
      + m.dimensions.map((d) => {
        const v = r.cells[d.key];
        const shown = d.key === "direction" ? (DIR_LABEL[v] || v) : v;
        return `<td class="${emptyCells.has(`${r.arxiv_id}|${d.key}`) ? "cell-empty" : ""}">${shown ? esc(shown) : "·"}</td>`;
      }).join("");
    tr.querySelector("button").onclick = () => openCard(r.arxiv_id);
    tb.appendChild(tr);
  });
  table.appendChild(tb);
  wrap.appendChild(table);
  panel.appendChild(wrap);
  const notes = el("ul", "matrix-flags");
  // 每个指标一条：哪些说提升、哪些说下降（各列前几篇），不逐对展开
  f.conflicts.forEach((c) => notes.appendChild(el("li", "", `⚡ 同一指标「${esc(c.metric)}」上方向相反：提升 ${c.up_total} 篇 ${names(c.up, c.up_total)}；下降 ${c.down_total} 篇 ${names(c.down, c.down_total)}——真的冲突，还是数据或设置不同？`)));
  if (f.empty_columns.length) notes.appendChild(el("li", "", `整列多半空着：${f.empty_columns.map((k) => esc((m.dimensions.find((d) => d.key === k) || {}).label || k)).join("、")}——大家都没检验，常常就是隐藏的假设。`));
  notes.appendChild(el("li", "", `空格 ${f.empty_cells.length} 个：是没人做过，还是你没读到？`));
  panel.appendChild(notes);
  $app.appendChild(panel);
}

function renderPaperTable(tb) {
  const wrap = el("div", "paper-table-wrap");
  if (tb.caption) wrap.appendChild(el("p", "paper-table-cap", esc(tb.caption)));
  const table = el("table", "paper-table");
  if (tb.headers && tb.headers.length) {
    const thead = el("thead");
    const tr = el("tr");
    tb.headers.forEach((h) => { const th = el("th"); th.textContent = h; tr.appendChild(th); });
    thead.appendChild(tr);
    table.appendChild(thead);
  }
  const body = el("tbody");
  (tb.rows || []).forEach((row) => {
    const tr = el("tr");
    row.forEach((c) => { const td = el("td"); td.textContent = c; tr.appendChild(td); });
    body.appendChild(tr);
  });
  table.appendChild(body);
  wrap.appendChild(table);
  return wrap;
}

/* ---------- 阅读卡工作台 ---------- */

function applyTutorPrefill(fields, prefill) {
  const alts = [];
  Object.entries(prefill || {}).forEach(([k, v]) => {
    if (!(String(fields[k] || "").trim())) fields[k] = v;
    else if (fields[k] !== v) alts.push({ key: k, text: v });
  });
  return alts;
}

function pickBenchMode({ saved, session, hasDraft, hasCard, passedCount, canGrade, inKit }) {
  if (saved === "tutor" || saved === "card") return saved;
  const st = session && session.status;
  if (st === "active" || st === "read") return "tutor";
  if (hasDraft || hasCard || (passedCount || 0) >= 2) return "card";
  if (!canGrade || inKit === false) return "card";
  return "tutor";
}

function tutorPanel({ aid, kitId, scrollTo, selection, onPrefill, session }) {
  const box = el("section", "tutor-panel");
  box.hidden = false;
  let sess = session || null;
  const draft = {};
  let focusQid = null;
  const paint = () => {
    box.innerHTML = "";
    if (!sess) {
      const empty = el("p", "form-note");
      empty.textContent = "按节出题；你写的作答会和论文节选一起发给大模型、按标准判对错。";
      const go = el("button", "btn small tutor-launch", "开始带读");
      go.type = "button";
      go.onclick = () => startTutor(false);
      box.append(empty, go);
      return;
    }
    const head = el("div", "tutor-head");
    const voice = el("span", "tutor-voice");
    voice.textContent = sess.voice === "llm" ? "模型出题" : sess.voice === "mixed" ? "模型出题 / 规则版" : "规则版";
    head.appendChild(voice);
    const prog = el("span", "tutor-prog");
    prog.textContent = `${sess.cur || 0}/${sess.total || 0}`;
    head.appendChild(prog);
    box.appendChild(head);
    if (sess.notice) {
      const n = el("p", "tutor-notice");
      n.textContent = sess.notice;
      box.appendChild(n);
    }
    const slots = sess.slots || [];
    const cur = sess.cur || 0;
    const sl = (focusQid && slots.find((s) => s.qid === focusQid)) || slots[cur];
    const followups = slots.filter((s) => s.status === "pending" || s.status === "retry");
    const paintFollowups = () => {
      followups.forEach((s) => {
        if (sl && s.qid === sl.qid) return;
        const row = el("p", "tutor-pending");
        row.textContent = s.status === "pending" ? `这题稍后再判：${s.question || s.qid}` : `再改一改：${s.question || s.qid}`;
        if (s.status === "pending") {
          const rg = el("button", "linkish", "重判");
          rg.type = "button";
          rg.onclick = () => turnTutor(s.qid, s.last || "", false, true);
          row.appendChild(rg);
        }
        const edit = el("button", "linkish", "改答案");
        edit.type = "button";
        edit.onclick = () => { focusQid = s.qid; paint(); };
        row.appendChild(edit);
        box.appendChild(row);
      });
    };
    if (sess.status === "stale") {
      const tip = el("p", "tutor-miss");
      tip.textContent = "原文变了，请重开。";
      const again = el("button", "btn small", "重开带读");
      again.type = "button";
      again.onclick = () => startTutor(true);
      box.append(tip, again);
      return;
    }
    if ((sess.status === "read" || sess.status === "done" || !sl) && !focusQid) {
      const tip = el("p", "form-note");
      tip.textContent = "读完了。把你写过的答案预填进阅读卡，改完再交。";
      const fin = el("button", "btn small", "预填进阅读卡");
      fin.type = "button";
      fin.onclick = finishTutor;
      box.append(tip, fin);
      const again = el("button", "linkish tutor-launch", "再带读一遍");
      again.type = "button";
      again.onclick = () => startTutor(true);
      box.appendChild(again);
      paintFollowups();
      return;
    }
    if (!sl) { paintFollowups(); return; }
    const q = el("p", "tutor-q");
    q.textContent = sl.question || "";
    box.appendChild(q);
    if (sl.hint) {
      const hint = el("details", "tutor-hint");
      const sum = el("summary");
      sum.textContent = "提示";
      const hp = el("p");
      hp.textContent = sl.hint;
      hint.append(sum, hp);
      box.appendChild(hint);
    }
    if (sl.type === "number") {
      const warn = el("p", "tutor-miss");
      warn.textContent = "表格里的字核对不了，选正文句子。";
      box.appendChild(warn);
    }
    const ta = el("textarea", "tutor-answer");
    ta.maxLength = 600;
    ta.rows = sl.type === "own" ? 3 : 4;
    ta.placeholder = sl.type === "own"
      ? "用你自己的话写 10–160 字，可以粘贴"
      : "从左边原文复制一句粘贴进来，或选中后点「填进作答框」";
    ta.value = draft[sl.qid] ?? sl.last ?? sl.answer ?? "";
    ta.addEventListener("input", () => { draft[sl.qid] = ta.value; send.disabled = !ta.value.trim(); });
    box.appendChild(ta);
    const acts = el("div", "tutor-acts");
    if (sl.type === "find_quote" || sl.type === "number" || sl.type === "limitation") {
      const useSel = el("button", "linkish", "填进作答框");
      useSel.type = "button";
      useSel.onclick = () => {
        const got = selection ? selection() : "";
        if (!got) { toast("先在左边原文里选中一句"); return; }
        box.fillAnswer(got);
      };
      acts.appendChild(useSel);
    }
    const send = el("button", "btn small", "作答");
    send.type = "button";
    send.disabled = !ta.value.trim();
    send.onclick = async () => {
      send.disabled = true;
      send.textContent = "正在判…";
      try { await turnTutor(sl.qid, ta.value, false); }
      finally { send.disabled = !ta.value.trim(); send.textContent = "作答"; }
    };
    acts.appendChild(send);
    if (sl.skippable || (sl.attempts || 0) >= 1 || sl.status === "skipped") {
      const sk = el("button", "linkish", "跳过");
      sk.type = "button";
      sk.onclick = () => turnTutor(sl.qid, "", true);
      acts.appendChild(sk);
    }
    if (sl.status === "pending") {
      const rg = el("button", "linkish", "重判");
      rg.type = "button";
      rg.onclick = () => turnTutor(sl.qid, sl.last || ta.value, false, true);
      acts.appendChild(rg);
    }
    box.fillAnswer = (text) => { ta.value = text || ""; draft[sl.qid] = ta.value; send.disabled = !ta.value.trim(); };
    box.appendChild(acts);
    if (sl.note) {
      const note = el("p", "form-note");
      note.textContent = sl.note;
      box.appendChild(note);
    }
    const st = (sess.stations || []).find((x) => x.key === sl.station);
    if (st && scrollTo) {
      const jump = el("button", "linkish", `跳到本节`);
      jump.type = "button";
      jump.onclick = () => scrollTo(st.heading || st.label, st.start);
      box.appendChild(jump);
    }
    if (sl.status === "pending") {
      const pend = el("p", "tutor-pending");
      pend.textContent = "这题稍后再判";
      box.appendChild(pend);
    }
    if (sl.feedback || sl.note) {
      const kind = sl.status === "pass" ? "tutor-ok" : sl.status === "pending" ? "tutor-pending" : "tutor-bad";
      const fb = el("p", `tutor-verdict ${kind}`);
      fb.textContent = sl.feedback || sl.note;
      box.appendChild(fb);
    }
    (sl.missing || []).forEach((m) => {
      const miss = el("p", "tutor-miss");
      miss.textContent = m;
      box.appendChild(miss);
    });
    if ((sl.reasons || []).length) {
      const why = el("details", "tutor-why");
      why.appendChild(el("summary", "", "判卷依据"));
      sl.reasons.forEach((r) => {
        const p = el("p");
        p.textContent = r;
        why.appendChild(p);
      });
      box.appendChild(why);
    }
    paintFollowups();
  };
  async function startTutor(restart) {
    try {
      sess = await api("POST", `/api/tutor/${encodeURIComponent(aid)}/start`, { uid: S.uid, kit: kitId, restart: !!restart });
      focusQid = null;
      box.hidden = false;
      paint();
    } catch (e) { toast(e.message); }
  }
  async function turnTutor(qid, answer, skip, regrade) {
    try {
      const out = await api("POST", `/api/tutor/${encodeURIComponent(aid)}/turn`, {
        uid: S.uid, kit: kitId, qid, answer, skip: !!skip, rev: sess ? sess.rev : 0, regrade: !!regrade,
      });
      sess = out.session;
      if (focusQid) {
        const focused = (sess.slots || []).find((s) => s.qid === focusQid);
        if (!focused || !["open", "pending", "retry"].includes(focused.status)) focusQid = null;
      }
      paint();
    } catch (e) {
      if (/已经答过|刷新/.test(e.message || "")) toast("这题已经答过了，刷新");
      else toast(e.message);
    }
  }
  async function finishTutor() {
    try {
      const out = await api("POST", `/api/tutor/${encodeURIComponent(aid)}/finish`, { uid: S.uid, kit: kitId });
      sess = out.session;
      const alts = onPrefill ? onPrefill(out.prefill || {}, out.missing || []) : [];
      paint();
      (alts || []).forEach((a) => {
        const row = el("p", "tutor-alt");
        const label = el("span");
        label.textContent = `带读里你写的是 ${a.text} `;
        const swap = el("button", "linkish", "换成这句");
        swap.type = "button";
        swap.onclick = () => { if (onPrefill) onPrefill({ [a.key]: a.text }, []); };
        row.append(label, swap);
        box.appendChild(row);
      });
      (out.missing || []).forEach((m) => {
        const miss = el("p", "tutor-miss");
        miss.textContent = m;
        box.appendChild(miss);
      });
    } catch (e) { toast(e.message); }
  }
  box.startTutor = startTutor;
  box.loadSession = (s) => { sess = s; box.hidden = false; paint(); };
  paint();
  return box;
}

async function renderCard() {
  const seq = S.renderSeq;
  const kitId = S.cardKit || "";
  const aid = S.cardId;
  if (!aid) { setView("read"); return; }
  $app.innerHTML = "";
  const isLocal = String(aid).startsWith("local:");
  const back = el("button", "linkish back-link", S.cardFrom === "today" ? "← 今日" : "← 研读");
  back.type = "button";
  back.onclick = () => { const to = S.cardFrom === "today" ? "today" : "read"; S.cardFrom = ""; setView(to); };
  const loading = el("div", "note-box", "正在取原文（arXiv HTML 版，第一次稍慢）…");
  $app.appendChild(loading);
  let kit = { papers: [], dimensions: [], id: kitId };
  let paper; let cards = { cards: [], fields: [] };
  try {
    const jobs = [api("GET", `/api/papers/${encodeURIComponent(aid)}`)];
    if (!isLocal && kitId) jobs.unshift(api("GET", `/api/kits/${kitId}`));
    const got = await Promise.all(jobs);
    if (stale(seq)) return;
    if (!isLocal && kitId) { kit = got[0]; paper = got[1]; }
    else paper = got[0];
    const inKit = !!(kit.papers || []).some((p) => p.arxiv_id === aid);
    const showCards = !isLocal && inKit;
    if (S.uid && showCards) cards = await api("GET", `/api/cards?uid=${S.uid}&kit=${kitId}`);
    if (stale(seq)) return;
  } catch (e) {
    if (stale(seq)) return;
    if (await recoverUser(e)) return;
    const why = e.message || "";
    if (/503|排队|忙/.test(why)) {
      loading.textContent = `现在人多，原文在排队（${why}）。稍后重试：https://arxiv.org/abs/${aid}`;
    } else {
      loading.textContent = `原文取不到（${why}）。`;
    }
    return;
  }
  if (stale(seq)) return;
  loading.remove();
  const showCard = !isLocal && !!(kit.papers || []).some((p) => p.arxiv_id === aid);
  const prev = (cards.cards || []).find((c) => c.arxiv_id === aid);
  const draftKey = `rg_card_${S.uid || "anon"}_${kitId}_${aid}`;
  let draft = null;
  try { draft = JSON.parse(localStorage.getItem(draftKey) || "null"); } catch (_) { /* 坏草稿就丢掉 */ }
  if (!draft) {
    try { draft = JSON.parse(localStorage.getItem(`rg_card_${kitId}_${aid}`) || "null"); } catch (_) { /* 旧键没有就空着 */ }
  }
  const state = draft || { fields: { ...((prev && prev.fields) || {}) }, dims: { ...((prev && prev.dims) || {}) }, log: (prev && prev.decision_log) || [] };
  const save = () => { try { localStorage.setItem(draftKey, JSON.stringify(state)); } catch (_) { /* 存不了不影响提交 */ } };

  const head = el("div", "card-head");
  head.appendChild(back);
  head.appendChild(el("h2", "", esc(paper.title)));
  const kitP = (kit.papers || []).find((p) => p.arxiv_id === aid);
  const titleZh = paper.title_zh || (kitP && kitP.title_zh) || "";
  const kws = (paper.keywords && paper.keywords.length) ? paper.keywords : ((kitP && kitP.keywords) || []);
  let absNote = "正文已取到，引文可逐字核对";
  if (paper.source === "pdf") absNote = "你导入的 PDF · 只有你能看到";
  else if (paper.source !== "html") absNote = "只取到摘要：正文引文核对不了，请先读 arXiv 原文";
  const status = el("div", "ws-status");
  if (!isLocal && paper.url) {
    status.appendChild(el("span", "", `<a href="${esc(paper.url)}" target="_blank" rel="noopener">arXiv:${esc(aid)} ↗</a>`));
  }
  status.appendChild(el("span", "", absNote));
  if (prev) status.appendChild(el("span", "", `上一版 v${prev.version} · ${prev.status === "pass" ? "过线" : "待改"}`));
  if (paper.source !== "html" && paper.source !== "pdf") {
    const retry = el("button", "linkish", "重新取正文");
    retry.type = "button";
    const until = Number(paper.retry_after || 0);
    if (until && until * 1000 > Date.now()) {
      const hh = new Date(until * 1000);
      retry.textContent = `arXiv 在 ${String(hh.getHours()).padStart(2, "0")}:${String(hh.getMinutes()).padStart(2, "0")} 前不会再试`;
      retry.disabled = true;
    } else {
      retry.onclick = () => renderCard();
    }
    status.appendChild(retry);
  }
  head.appendChild(status);
  $app.appendChild(head);
  if (titleZh) {
    const zh = el("p", "paper-zh");
    zh.textContent = titleZh;
    $app.appendChild(zh);
  }
  if (kws.length) {
    const row = el("p", "paper-kw");
    kws.slice(0, 5).forEach((k) => {
      const s = el("span");
      s.textContent = k.zh || k.en || k;
      row.appendChild(s);
    });
    $app.appendChild(row);
  }

  let tutorMeta = { session: null, can_grade: false, in_kit: true };
  if (S.uid) {
    try { tutorMeta = await api("GET", `/api/tutor/${encodeURIComponent(aid)}?uid=${S.uid}&kit=${kitId}`); }
    catch (_) { /* 没有会话就当没开始 */ }
    if (stale(seq)) return;
  }
  const canGrade = !!(tutorMeta && tutorMeta.can_grade);
  const inKit = !tutorMeta || tutorMeta.in_kit !== false;
  const modeKey = `rg_mode_${S.uid || "anon"}_${kitId}_${aid}`;
  let savedMode = "";
  try { savedMode = localStorage.getItem(modeKey) || ""; } catch (_) { /* 记不住就按规则选 */ }
  const hasDraft = Object.values((state.fields || {})).some((v) => String(v || "").trim());
  const passedCount = (cards.cards || []).filter((c) => c.status === "pass").length;
  let mode = pickBenchMode({
    saved: savedMode, session: tutorMeta && tutorMeta.session, hasDraft, hasCard: !!prev,
    passedCount, canGrade, inKit: showCard && inKit,
  });

  const tabs = el("nav", "ws-tabs bench-modes");
  const tabTutor = el("button", "ws-tab", "AI 带读");
  tabTutor.type = "button";
  tabTutor.dataset.mode = "tutor";
  const tabCard = el("button", "ws-tab", "写阅读卡");
  tabCard.type = "button";
  tabCard.dataset.mode = "card";
  if (!canGrade) {
    tabTutor.disabled = true;
    tabTutor.title = "现在用不了模型（没配 / 没同意隐私说明 / 今天额度用完），先写阅读卡";
    if (mode === "tutor") mode = "card";
  }
  if (!showCard) {
    tabCard.disabled = true;
    tabCard.title = "这篇不能交阅读卡";
    if (mode === "card") mode = canGrade ? "tutor" : "card";
  }
  tabs.append(tabTutor, tabCard);
  $app.appendChild(tabs);

  const bench = el("div", "bench");
  bench.dataset.mode = mode;
  // 左：原文
  const left = el("section", "bench-paper");
  const tools = el("div", "quote-tools");
  const tutorQuote = el("div", "quote-tutor-tools");
  tutorQuote.appendChild(el("span", "", "选中原文里的一句，再点："));
  const fillChip = el("button", "chip", "填进作答框");
  fillChip.type = "button";
  const cardQuote = el("div", "quote-card-tools");
  cardQuote.appendChild(el("span", "", "选中原文里的一句，再点："));
  tools.append(tutorQuote, cardQuote);
  const quoteFields = (cards.fields || []).filter((f) => f.quote);
  const pane = el("div", "paper-text");
  const jump = el("div", "sec-jump", "<span>跳到</span>");
  const starts = new Map((paper.sections || []).map((sec) => [sec.at, sec]));
  const jumped = new Set(["参考文献", "附录", "致谢"]);
  const byTable = Object.fromEntries((paper.tables || []).map((tb) => [tb.id, tb]));
  const htmlUrl = `https://arxiv.org/html/${aid}`;
  const absUrl = paper.url || `https://arxiv.org/abs/${aid}`;
  const tableMiss = () => {
    const node = el("p", "table-miss");
    node.innerHTML = `这张表没能排版，请看原文 <a href="${esc(htmlUrl)}" target="_blank" rel="noopener">HTML ↗</a> · <a href="${esc(absUrl)}" target="_blank" rel="noopener">摘要 ↗</a>`;
    return node;
  };
  const lineIndex = [];
  const jumpTargets = [];   // 有「跳到」按钮的章节标题，滚动联动用
  let off = 0;
  let currentJump = null;
  let sawSec = false;
  let frontWrap = null;
  let frontCount = 0;
  const fillPaperLine = (node, line) => {
    line.split(/(\s+)/).forEach((part) => {
      if (/\\[a-zA-Z]+|[_^]\{/.test(part)) {
        const s = el("span", "tex");
        s.textContent = part;
        node.appendChild(s);
      } else {
        node.appendChild(document.createTextNode(part));
      }
    });
  };
  paper.text.split("\n").forEach((line) => {
    const sec = starts.get(off);
    let node;
    const fail = /^\[\[table:fail:([^\]]+)\]\]$/.exec(line);
    const hit = /^\[\[table:([^\]]+)\]\]$/.exec(line);
    const asHead = !!(sec && line.length <= 80 && !/^\d+$/.test(line.trim()));
    if (fail || (hit && String(hit[1]).startsWith("fail:"))) node = tableMiss();
    else if (hit && byTable[hit[1]]) node = renderPaperTable(byTable[hit[1]]);
    else if (hit) node = tableMiss();
    else {
      node = el(asHead ? "h4" : "p");
      fillPaperLine(node, line);
    }
    if (!sawSec && !asHead && !hit && !fail) {
      if (!frontWrap) {
        frontWrap = el("details", "paper-front");
        frontWrap.appendChild(el("summary", "", "作者与单位"));
        pane.appendChild(frontWrap);
      }
      frontWrap.appendChild(node);
      frontCount += 1;
    } else {
      if (asHead) sawSec = true;
      pane.appendChild(node);
    }
    const offBefore = off;
    off += line.length + 1;
    lineIndex.push({ start: offBefore, end: off, node });
    if (!asHead || jumped.has(sec.label)) return;
    jumped.add(sec.label);
    const b = el("button", "sec-chip", esc(sec.label));
    b.type = "button";
    b.onclick = () => {
      if (currentJump) currentJump.classList.remove("on");
      b.classList.add("on");
      currentJump = b;
      pane.scrollTo({ top: node.offsetTop - 8, behavior: "smooth" });
    };
    jump.appendChild(b);
    jumpTargets.push({ node, btn: b });
  });
  if (frontWrap) {
    const sum = frontWrap.querySelector("summary");
    if (sum) sum.textContent = `作者与单位（${frontCount} 行）`;
  }
  quoteFields.forEach((f) => {
    const b = el("button", "chip", `引为「${esc(f.label.replace(" · 原句", ""))}」`);
    b.type = "button";
    b.onclick = () => {
      const sel = (window.getSelection() || "").toString().replace(/\s+/g, " ").trim();
      if (!sel || !pane.contains(window.getSelection().anchorNode)) { toast("先在左边原文里选中一句"); return; }
      state.fields[f.key] = sel;
      save();
      paintForm();
    };
    cardQuote.appendChild(b);
  });
  // 「跳到」滚动联动：读到哪一节，对应按钮就亮哪一节。
  // 判定标准是「最后一个已经滚过视口上沿的标题」；还在最前面的作者区时都不亮。
  const spy = () => {
    const base = pane.getBoundingClientRect().top;
    let hit = null;
    for (const t of jumpTargets) {
      if (t.node.getBoundingClientRect().top - base <= 28) hit = t;
      else break;
    }
    const next = hit ? hit.btn : null;
    if (next === currentJump) return;
    if (currentJump) currentJump.classList.remove("on");
    if (next) next.classList.add("on");
    currentJump = next;
  };
  let spyTick = false;
  pane.addEventListener("scroll", () => {
    if (spyTick) return;
    spyTick = true;
    window.requestAnimationFrame(() => { spyTick = false; spy(); });
  }, { passive: true });
  // 初次判定要等面板挂进页面之后再做：挂载前坐标全是 0，会把最后一节误判成当前节
  window.requestAnimationFrame(() => window.requestAnimationFrame(spy));

  // 「选中原文」和「跳到章节」的按钮收进同一个工具栏，放不下就换行，不再横向滚动
  const toolBar = el("div", "paper-tools");
  toolBar.append(tools, jump);
  left.append(toolBar, pane);

  // 右：卡
  const right = el("section", "bench-card");
  const form = el("div", "card-form");
  const result = el("div", "card-result");
  const scrollToStation = (heading, start) => {
    pane.querySelectorAll(".tutor-hit").forEach((n) => n.classList.remove("tutor-hit"));
    const text = String(heading || "").trim();
    let hit = text ? lineIndex.find((x) => (x.node.textContent || "").trim() === text) : null;
    if (!hit && start != null) hit = lineIndex.find((x) => x.start <= start && start < x.end);
    if (hit) {
      hit.node.classList.add("tutor-hit");
      pane.scrollTo({ top: hit.node.offsetTop - 8, behavior: "smooth" });
    }
  };
  const readSelection = () => {
    const sel = (window.getSelection() || "").toString().replace(/\s+/g, " ").trim();
    if (!sel || !pane.contains(window.getSelection().anchorNode)) return "";
    return sel;
  };
  const panel = tutorPanel({
    aid, kitId,
    scrollTo: scrollToStation,
    selection: readSelection,
    onPrefill: (prefill, missing) => {
      const alts = applyTutorPrefill(state.fields, prefill);
      save();
      paintForm();
      if (missing && missing.length) toast(missing.join("；"));
      return alts;
    },
    session: tutorMeta && tutorMeta.session,
  });
  fillChip.onclick = () => {
    const got = readSelection();
    if (!got) { toast("先在左边原文里选中一句"); return; }
    if (panel.fillAnswer) panel.fillAnswer(got);
  };
  tutorQuote.appendChild(fillChip);
  const applyMode = (next) => {
    mode = next;
    bench.dataset.mode = next;
    tabTutor.classList.toggle("on", next === "tutor");
    tabCard.classList.toggle("on", next === "card");
    panel.hidden = next !== "tutor";
    form.hidden = next !== "card";
    result.hidden = next !== "card";
    tutorQuote.hidden = next !== "tutor";
    cardQuote.hidden = next !== "card";
    try { localStorage.setItem(modeKey, next); } catch (_) { /* 记不住也不挡用 */ }
  };
  tabTutor.onclick = () => { if (!tabTutor.disabled) applyMode("tutor"); };
  tabCard.onclick = () => applyMode("card");
  right.append(panel, form, result);
  bench.append(left, right);
  $app.appendChild(bench);
  applyMode(mode);

  function paintForm() {
    form.innerHTML = "";
    cards.fields.forEach((f) => {
      const box = el("label", `cf${f.quote ? " cf-quote" : ""}${f.own ? " cf-own" : ""}`);
      box.appendChild(el("span", "cf-label", `${esc(f.label)}${f.own ? '<i>只能你自己写</i>' : ""}`));
      const ta = el("textarea");
      ta.rows = f.quote ? 3 : 2;
      ta.placeholder = f.hint;
      ta.value = state.fields[f.key] || "";
      ta.addEventListener("input", () => { state.fields[f.key] = ta.value; save(); });
      box.appendChild(ta);
      form.appendChild(box);
    });
    form.appendChild(el("p", "section-label", "矩阵维度（和别的论文对比用）"));
    const grid = el("div", "dims-grid");
    kit.dimensions.forEach((d) => {
      const box = el("label", "cf");
      box.appendChild(el("span", "cf-label", esc(d.label)));
      let input;
      if (d.options) {
        input = el("select");
        input.innerHTML = `<option value="">—</option>${d.options.map((o) => `<option value="${o}">${esc(DIR_LABEL[o] || o)}</option>`).join("")}`;
      } else {
        input = el("input");
        input.placeholder = d.hint;
      }
      input.value = state.dims[d.key] || "";
      input.addEventListener("input", () => { state.dims[d.key] = input.value; save(); });
      input.addEventListener("change", () => { state.dims[d.key] = input.value; save(); });
      box.appendChild(input);
      grid.appendChild(box);
    });
    form.appendChild(grid);

    // 双窗口：交给你的 Agent + 决策日志
    const agent = el("details", "agent-box");
    agent.open = state.log.length > 0;
    agent.appendChild(el("summary", "", `你的 Agent（可选）· 决策日志 ${state.log.length} 条`));
    agent.appendChild(el("p", "form-note", "下载简报放进你的 Agent（Claude Code / Codex 放项目目录；DeepSeek 等网页版就复制内容）。它只能挑错和提问，不能替你写「主张」「假设」「我会改什么」。它的每条建议在这里登记，并逐条决定采纳、修改还是拒绝——拒绝要写理由。"));
    const dl = el("a", "btn small secondary", "交给你的 Agent ↓ AGENTS.md");
    // 走接口地址：页面在 GitHub Pages 时，相对路径会去 Pages 上找，下载不到（Codex 复现）
    dl.href = apiUrl(`/api/brief?kit=${encodeURIComponent(kitId)}&arxiv_id=${encodeURIComponent(aid)}`);
    agent.appendChild(dl);
    const table = el("div", "log-rows");
    const paintLog = () => {
      table.innerHTML = "";
      state.log.forEach((row, i) => {
        const r = el("div", "log-row");
        r.innerHTML = `<b>A${i + 1}</b>`;
        const s = el("input"); s.placeholder = "Agent 建议了什么"; s.value = row.suggestion || "";
        s.addEventListener("input", () => { row.suggestion = s.value; save(); });
        const a = el("select");
        a.innerHTML = '<option value="">处理…</option><option value="adopt">采纳</option><option value="modify">修改后采纳</option><option value="reject">拒绝</option>';
        a.value = row.action || "";
        a.addEventListener("change", () => { row.action = a.value; save(); });
        const why = el("input"); why.placeholder = "为什么（拒绝必填）"; why.value = row.reason || "";
        why.addEventListener("input", () => { row.reason = why.value; save(); });
        const x = el("button", "linkish danger", "删");
        x.type = "button";
        x.onclick = () => { state.log.splice(i, 1); save(); paintLog(); };
        r.append(s, a, why, x);
        table.appendChild(r);
      });
    };
    paintLog();
    const add = el("button", "linkish", "+ 登记一条 Agent 建议");
    add.type = "button";
    add.onclick = () => { state.log.push({ suggestion: "", action: "", reason: "" }); save(); paintLog(); agent.open = true; };
    agent.append(table, add);
    form.appendChild(agent);

    const acts = el("div", "submit-actions");
    const submit = el("button", "btn", prev ? "交新一版" : "交这张卡");
    submit.type = "button";
    submit.onclick = async () => {
      submit.disabled = true; submit.textContent = "正在逐字核对引文…";
      try {
        const card = await api("POST", "/api/cards", {
          uid: S.uid, kit: kitId, arxiv_id: aid, fields: state.fields, dims: state.dims,
          decision_log: state.log.filter((r) => (r.suggestion || "").trim()),
        });
        paintReview(card);
        if (card.status === "pass") { try { localStorage.removeItem(draftKey); } catch (_) { /* ignore */ } }
        result.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (e) { toast(e.message); }
      submit.disabled = false; submit.textContent = "交新一版";
    };
    acts.appendChild(submit);
    acts.appendChild(el("span", "hint", "草稿自动存在本机。没过线也会记一版，改了再交。"));
    form.appendChild(acts);
  }

  function paintReview(card) {
    result.innerHTML = "";
    const r = card.review;
    const sec = el("section", "feedback");
    if (card.prev_passed !== null && card.prev_passed !== undefined) {
      const d = r.passed - card.prev_passed;
      sec.appendChild(el("p", `review-delta ${d > 0 ? "up" : d < 0 ? "down" : ""}`, d > 0 ? `比上一版多过 ${d} 条（${card.prev_passed} → ${r.passed}）` : d < 0 ? `比上一版少了 ${-d} 条` : `和上一版一样是 ${r.passed} 条`));
    }
    const head = el("div", "feedback-head");
    head.appendChild(el("div", "", `<h3>${card.status === "pass" ? "过线" : "还差一点"} · v${card.version}</h3><p>${card.status === "pass" ? "这张卡记进了你的账本，会出现在矩阵里。" : esc(r.next_step)}</p>`));
    head.appendChild(el("div", "score-ring", `<span class="num">${r.passed}</span>/ ${r.total} 条`));
    sec.appendChild(head);
    const list = el("div", "rubric-list");
    r.checks.forEach((c) => list.appendChild(el("div", `rubric-item ${c.pass ? "pass" : "fail"}`,
      `<span class="rubric-mark">${c.pass ? "✓" : "!"}</span><div><p class="rubric-crit">${esc(c.label)}${c.where ? `<em>${esc(c.where)}</em>` : ""}</p><p class="rubric-comment">${esc(c.note)}</p></div>`)));
    sec.appendChild(list);
    if (card.fact) sec.appendChild(el("div", "why-box", `<b>写进账本　</b>${esc(card.fact.value)}`));
    result.appendChild(sec);
    loadCardHistory();
  }

  async function loadCardHistory() {
    if (!S.uid) return;
    try {
      const h = await api("GET", `/api/cards/${aid}/history?uid=${S.uid}&kit=${kitId}`);
      const vers = h.versions || [];
      if (!vers.length) return;
      result.querySelector(".card-hist")?.remove();
      const box = el("details", "card-hist");
      box.appendChild(el("summary", "", `历次版本 ${vers.length}`));
      vers.forEach((v) => {
        const r = v.review || {};
        box.appendChild(el("p", "form-note",
          `v${v.version} · ${r.passed ?? "—"}/${r.total ?? "—"} · ${esc(v.created_at || "")} · ${esc(r.next_step || "")}`));
      });
      result.appendChild(box);
    } catch (_) { /* 没有历史就不画 */ }
  }

  if (showCard) {
    paintForm();
    if (prev) paintReview(prev);
    else loadCardHistory();
  } else {
    form.hidden = true;
    result.hidden = true;
  }
}

/* ---------- 今日 · 每日情报（≤10 分钟） ---------- */

function clipAbs(text) {
  const t = String(text || "");
  const parts = t.split(/(?<=(?<!e\.g|i\.e|et al)\.)\s+/);
  return parts.slice(0, 2).join(" ");
}

function highlightHits(text, hits) {
  const node = el("p", "triage-abs");
  const terms = (hits || []).map((h) => h.term_en).filter(Boolean).sort((a, b) => b.length - a.length);
  if (!terms.length) { node.textContent = text; return node; }
  const re = new RegExp("(" + terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
  let last = 0;
  const src = String(text || "");
  src.replace(re, (m, _g, i) => {
    if (i > last) node.appendChild(document.createTextNode(src.slice(last, i)));
    const mark = el("span", "triage-hit");
    mark.textContent = m;
    node.appendChild(mark);
    last = i + m.length;
    return m;
  });
  if (last < src.length) node.appendChild(document.createTextNode(src.slice(last)));
  if (!node.childNodes.length) node.textContent = src;
  return node;
}

async function dailyBlock(seq) {
  const box = el("section", "panel daily today-papers");
  let d;
  try {
    d = await api("GET", `/api/daily?uid=${S.uid}`);
  } catch (e) {
    box.appendChild(el("p", "panel-sub", `今日情报暂时取不到（如实说明）：${esc(e.message)}`));
    return box;
  }
  if (stale(seq)) return box;
  if (d.day) box.dataset.day = d.day;
  const dirName = d.direction && d.direction.name ? `按你的方向「${d.direction.name}」` : "按你的兴趣";
  const head = el("div", "daily-head");
  head.innerHTML = `<div><p class="section-label">最近的新论文 · 约 5 分钟 · 可跳过</p><h3 class="panel-title">分拣最近的新论文</h3><p class="panel-sub">${esc(dirName)}的新论文。只看标题和摘要原文，决定留还是过。</p></div><div class="score-ring"><span class="num">${d.done_today}</span>/ ${d.goal} 今天</div>`;
  box.appendChild(head);
  if (d.error) box.appendChild(el("div", "note-box", `arXiv 暂时连不上（如实说明）：${esc(d.error)}`));
  if (!d.items.length) {
    const reason = d.empty_reason;
    if (reason === "thin") {
      box.appendChild(el("p", "panel-sub", "这个方向 arXiv 上很少。下面是替代来源（例如 PsyArXiv 预印本）。"));
      (d.channels || []).forEach((c) => {
        box.appendChild(el("p", "", `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.name)} ↗</a>`));
      });
    } else if (reason === "exhausted") box.appendChild(el("p", "panel-sub", "这个话题最近的都分拣完了。"));
    else if (reason === "upstream" || d.stale) box.appendChild(el("p", "panel-sub", `连不上来源，这是已缓存的候选。`));
    else if (reason === "no_topics") {
      const tip = el("p", "panel-sub", "还没有能用来检索的兴趣话题。先聊几句或选定方向。");
      const go = el("button", "btn small", "去选方向");
      go.type = "button";
      go.onclick = () => setView("dialogue");
      box.append(tip, go);
    } else box.appendChild(el("p", "panel-sub", "最近没有待分拣论文。"));
  }
  d.items.forEach((it) => {
    const row = el("article", "triage");
    const title = el("a", "triage-title");
    title.href = it.url; title.target = "_blank"; title.rel = "noopener";
    title.textContent = it.title;
    row.appendChild(title);
    if (it.lib === "html" || it.lib === "abstract") {
      row.appendChild(el("i", "kp-lib", it.lib === "html" ? "库内" : "只有摘要"));
    }
    if (it.title_zh) {
      const zh = el("p", "triage-zh");
      zh.textContent = `${it.title_zh} · 机器翻译`;
      row.appendChild(zh);
    }
    if ((it.keywords || []).length) {
      const kws = el("p", "triage-kw");
      (it.keywords || []).slice(0, 5).forEach((k) => {
        const s = el("span");
        s.textContent = k;
        kws.appendChild(s);
      });
      row.appendChild(kws);
    }
    row.appendChild(highlightHits(clipAbs(it.abstract), it.hits));
    row.appendChild(el("small", "", `${esc((it.authors || []).join(", "))} · ${esc(it.published || "")}`));
    const ctl = el("div", "triage-ctl");
    const why = el("input");
    why.placeholder = "留下／跳过的理由（4–80 字）：碰到了什么 / 离你的问题多远";
    why.maxLength = 80;
    let sending = false;
    const send = async (verdict) => {
      // 请求回来之前这一行只算一次；今天分拣了几篇用服务器回的数，不自己加。
      // 原来慢的时候双击「留」显示完成 2 篇，库里只有 1 篇（Codex 复现）
      if (sending || row.classList.contains("done")) return;
      sending = true;
      keep.disabled = true; skip.disabled = true;
      try {
        const r = await api("POST", "/api/daily/triage", { uid: S.uid, kit: "", arxiv_id: it.arxiv_id, verdict, why: why.value, title: it.title });
        row.classList.add("done", verdict);
        ctl.innerHTML = `<span class="triage-done">${verdict === "keep" ? "已留下" : "已跳过"}：${esc(why.value)}</span>`;
        const n = box.querySelector(".daily-head .num");
        // 几行同时交时回来的顺序不定：同一天里只往大里改，旧的那个数不能把新的盖小；
        // 过了零点就照新一天的数来（原来一律取大，跨天还显示昨天的 5/3，Codex 复现）
        // 跨零点倒着回来的那种：比现在显示的日期早的回包不用（原来昨天的 6 盖掉了今天的 1，Codex 复现）
        const shownDay = box.dataset.day || "";
        if (n && r && typeof r.done_today === "number" && !(r.day && shownDay && r.day < shownDay)) {
          const sameDay = r.day && r.day === shownDay;
          n.textContent = String(sameDay ? Math.max(Number(n.textContent) || 0, r.done_today) : r.done_today);
          if (r.day) box.dataset.day = r.day;
        }
      } catch (e) { toast(e.message); keep.disabled = false; skip.disabled = false; why.focus(); }
      sending = false;
    };
    const keep = el("button", "btn small", "留");
    keep.type = "button";
    keep.onclick = () => send("keep");
    const skip = el("button", "btn small secondary", "过");
    skip.type = "button";
    skip.onclick = () => send("skip");
    ctl.append(why, keep, skip);
    if (it.card_kit) {
      const card = el("button", "btn small ghost", "读成一张卡");
      card.type = "button";
      card.onclick = () => { S.kitId = it.card_kit; S.cardFrom = "today"; openCard(it.arxiv_id, it.card_kit); };
      ctl.appendChild(card);
    } else {
      const ext = el("a", "linkish", /arxiv/i.test(it.source || it.url || "") ? "在 arXiv 读" : "打开原文");
      ext.href = it.url || "#"; ext.target = "_blank"; ext.rel = "noopener";
      ctl.appendChild(ext);
    }
    row.appendChild(ctl);
    box.appendChild(row);
  });
  if (d.recent_keeps.length) {
    const kept = el("details", "inventory");
    kept.appendChild(el("summary", "", `你最近留下的 ${d.recent_keeps.length} 篇（共分拣 ${d.triaged_total} 篇）`));
    const ul = el("ul");
    d.recent_keeps.forEach((k) => {
      const href = k.url || (/^(psyarxiv|socarxiv):/i.test(k.arxiv_id || "") ? "#" : `https://arxiv.org/abs/${esc(k.arxiv_id)}`);
      ul.appendChild(el("li", "", `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(k.title || k.arxiv_id)}</a><span>${esc(k.why)}</span>`));
    });
    kept.appendChild(ul);
    box.appendChild(kept);
  }
  return box;
}

/* ---------- 定位：边清单 / 竞争地图 / 定位陈述 / 下注组合（docs/DESIGN_PROPOSAL.md §1D） ---------- */

const TREND = { up: "↑ 涨得比分类快", down: "↓ 涨得比分类慢", flat: "→ 和分类差不多" };
const TIER_HINT = {
  reach: "拥挤或门槛高，你的边只部分匹配",
  match: "你的边匹配，拥挤中等",
  safety: "不拥挤或有制度化通道（本研公开题目、助教项目）",
};

function positionTabs(active) {
  const nav = el("nav", "ws-tabs pos-tabs");
  [
    ["edges", "1 我有什么", "别人不容易有、又能核对的"],
    ["sources", "2 去哪看", "这类人在哪说话"],
    ["map", "3 能站哪", "作者写下的开放问题"],
    ["statement", "4 一句话", "我是能做 X 的人，因为 Y"],
    ["bets", "5 这学期压哪", "冲 / 稳 / 保"],
  ].forEach(([key, label, sub]) => {
    const b = el("button", `ws-tab pos-tab${key === active ? " on" : ""}`, label);
    b.type = "button";
    b.title = sub;
    b.onclick = () => { S.posTab = key; setView("position"); };
    nav.appendChild(b);
  });
  return nav;
}

function posLead(text) {
  return el("p", "pos-lead", esc(text));
}

async function _deadRenderPosition() {
  const seq = S.renderSeq;
  if (!S.kitId) useKit(DEFAULT_KIT);
  const tab = S.posTab || "edges";
  let hasEdge = false;
  if (S.uid) {
    try { hasEdge = ((await api("GET", `/api/edges?uid=${S.uid}`)).edges || []).length > 0; } catch (_) { /* 引导按新用户算 */ }
    if (stale(seq)) return;
  }
  $app.innerHTML = "";
  $app.appendChild(workspaceHead("定位", "先写下别人不容易有的几条。再决定站哪。"));
  $app.appendChild(roomGuide("position", hasEdge));
  $app.appendChild(positionTabs(tab));
  const body = el("div");
  $app.appendChild(body);
  try {
    if (tab === "sources") await paintSources(seq, body);
    else if (tab === "map") await paintMap(seq, body);
    else if (tab === "statement") await paintStatement(seq, body);
    else if (tab === "bets") await paintBets(seq, body);
    else await paintEdges(seq, body);
  } catch (e) {
    if (stale(seq)) return;
    if (await recoverUser(e)) return;
    body.appendChild(el("div", "note-box", `加载失败：${esc(e.message)}`));
  }
}

/* 学生的方向：本机轨迹优先，没有就问服务端（直接打开定位 / 研读时本机可能还没记）。
   问到的按画像记：问的时候切了画像，这个结果不记；没问到也不记，下次再问。 */
async function myDirection() {
  if (trail().code) return trail().code;
  if (S.myDir !== undefined) return S.myDir;
  const portrait = S.portraitId;
  let dir;
  try { dir = (await api("GET", `/api/projects/context?uid=${S.uid}`)).direction || ""; } catch (_) { return ""; }
  if (S.portraitId === portrait) S.myDir = dir;
  return dir;
}

/* 学生的方向还没有工具包时如实说，并指向不依赖工具包的「信息源」 */
function kitMismatch(kitDirection, code) {
  if (!code || !kitDirection || code === kitDirection || !FIELD_TREES[code]) return null;
  const box = el("div", "note-box", `这个工具包属于「${esc((FIELD_TREES[kitDirection] || {}).name || kitDirection)}」。你的方向「${esc(FIELD_TREES[code].name)}」还没有工具包——可以先看这个方向的人在哪说话。`);
  const go = el("button", "linkish", "去换方向 →");
  go.type = "button";
  go.onclick = () => setView("cards");
  box.appendChild(go);
  return box;
}

function edgeChip(e) {
  const tag = e.status === "proven" ? '<b class="edge-st proven">已证明</b>' : '<b class="edge-st">自述</b>';
  return `${tag}${esc(e.text)}${e.generic ? '<em class="edge-common">谁都会写</em>' : ""}`;
}

async function paintEdges(seq, body) {
  const d = await api("GET", `/api/edges?uid=${S.uid}`);
  if (stale(seq)) return;
  const intro = el("div", "panel");
  intro.appendChild(posLead("边 = 别人不容易抄、又能打开核对的东西。例：粤语母语；同时修过能在成绩单上看到的两门课。做完去：去哪看。"));
  const what = el("details");
  what.appendChild(el("summary", "", "什么算边"));
  what.appendChild(el("p", "form-note", "会的语言或方言、跨院系的课程组合、能接触到的人群 / 数据 / 设备、你常去而同学不去的信息源、整块的时间。「热爱科研」「学习能力强」谁都会写，不算边。"));
  intro.appendChild(what);
  const proven = d.edges.filter((e) => e.status === "proven");
  const mine = d.edges.filter((e) => e.status !== "proven");
  intro.appendChild(el("p", "section-label", `自述 · ${mine.length}`));
  const ml = el("ul", "edge-list");
  mine.forEach((e) => {
    const li = el("li", "", `<span>${edgeChip(e)}<small>${esc(d.kinds[e.kind] || e.kind)}${e.evidence_url ? ` · <a href="${esc(e.evidence_url)}" target="_blank" rel="noopener">凭据 ↗</a>` : ""}</small></span>`);
    const x = el("button", "linkish danger", "删");
    x.type = "button";
    x.onclick = once(x, async () => { try { await api("DELETE", `/api/edges/${e.id}?uid=${S.uid}`); backToPosition(seq); } catch (err) { toast(err.message); } });
    li.appendChild(x);
    ml.appendChild(li);
  });
  intro.appendChild(ml);
  intro.appendChild(el("p", "section-label", `已证明 · ${proven.length}（来自账本，过线的作品自动进来，不能手改）`));
  if (!proven.length) intro.appendChild(el("p", "form-note", "还没有。在「研读」交一张过线的阅读卡，这里就有第一条。"));
  const pl = el("ul", "edge-list");
  proven.forEach((e) => pl.appendChild(el("li", "", `<span>${edgeChip(e)}</span>`)));
  intro.appendChild(pl);

  const form = el("div", "edge-form");
  const kind = el("select");
  kind.innerHTML = Object.entries(d.kinds).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  kind.value = "language";
  const text = el("input");
  text.placeholder = "具体到别人能核对：粤语母语 / 修过《数理统计》和《认知心理学》/ 常逛 r/MachineLearning";
  text.maxLength = 60;
  const url = el("input");
  url.placeholder = "凭据链接（可选）";
  const add = el("button", "btn small", "加一条");
  add.type = "button";
  const submit = async (k, t, u) => {
    try { await api("POST", "/api/edges", { uid: S.uid, kind: k, text: t, evidence_url: u || "" }); backToPosition(seq); } catch (err) { toast(err.message); }
  };
  add.onclick = once(add, () => submit(kind.value, text.value, url.value));
  text.addEventListener("keydown", (ev) => { if (ev.key === "Enter") add.click(); });
  form.append(kind, text, url, add);
  intro.appendChild(form);
  if (d.suggest.length) {
    const sug = el("div", "edge-suggest", "<span>画像里记着，可以加成自述：</span>");
    d.suggest.forEach((s) => {
      const b = el("button", "chip", `+ ${esc(s.text)}`);
      b.type = "button";
      b.onclick = () => submit(s.kind, s.text.slice(0, 60), "");
      sug.appendChild(b);
    });
    intro.appendChild(sug);
  }
  body.appendChild(intro);
}

const CADENCE = { daily: "每天", weekly: "每周", "when-needed": "用时再看" };

async function paintSources(seq, body) {
  const dir = S.srcDir || (await myDirection()) || "ai";
  const m = await api("GET", `/api/channels?uid=${S.uid}&direction=${dir}`);
  if (stale(seq)) return;
  const panel = el("div", "panel");
  panel.appendChild(posLead("标出你常看的地方。你看、同学少看，就是一条边。做完去：能站哪。"));
  const bar = el("div", "src-bar");
  const sel = el("select");
  sel.innerHTML = Object.entries(FIELD_TREES).map(([code, f]) => `<option value="${code}">${esc(f.name)}</option>`).join("");
  sel.value = dir;
  sel.onchange = () => { S.srcDir = sel.value; setView("position"); };
  bar.appendChild(sel);
  const sum = m.summary;
  bar.appendChild(el("span", "", `你常看：中文圈 ${sum["中文圈"].read}/${sum["中文圈"].total} · 英文圈 ${sum["英文圈"].read}/${sum["英文圈"].total}${m.checked_at ? ` · 清单核对于 ${esc(m.checked_at)}` : ""}`));
  panel.appendChild(bar);
  if (!m.channels.length) {
    panel.appendChild(el("div", "note-box", "这个方向的信息源清单还没有整理好。"));
    body.appendChild(panel);
    return;
  }
  if (m.blind_spot) panel.appendChild(el("div", "why-box", `<b>常见盲区　</b>${esc(m.blind_spot)}`));
  if (m.other_circle_unread.length) panel.appendChild(el("p", "form-note", `你读得多的那一圈之外，这个方向的人还在：${m.other_circle_unread.map(esc).join("、")}`));
  const groups = [[dir, `${(FIELD_TREES[dir] || {}).name || dir}`], ["career", "进组、夏令营与机会（各方向通用）"]];
  groups.forEach(([g, title]) => {
    const rows = m.channels.filter((c) => c.group === g);
    if (!rows.length) return;
    // 方向本身的信息源直接摊开；各方向通用的进组信息收起来，免得一页太长
    const host = g === dir ? panel : el("details", "src-more");
    if (g === dir) panel.appendChild(el("p", "section-label", `${esc(title)} · ${rows.length}`));
    else { host.appendChild(el("summary", "", `${esc(title)} · ${rows.length}（你常看 ${rows.filter((c) => c.read).length}）`)); panel.appendChild(host); }
    ["中文圈", "英文圈"].forEach((circle) => {
      const cs = rows.filter((c) => c.circle === circle);
      if (!cs.length) return;
      const list = el("div", "src-list");
      list.appendChild(el("p", "src-circle", circle));
      cs.forEach((c) => {
        const card = el("article", `src-card${c.read ? " on" : ""}`);
        const where = c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.name)} ↗</a>` : `<b>${esc(c.name)}</b>`;
        const sig = (c.signals || []).map((k) => `<span class="src-sig">${esc(m.signal_kinds[k] || k)}</span>`).join("");
        card.innerHTML = `<div class="src-top">${where}<small>${esc(c.kind)}</small></div>`
          + `<p>${esc(c.good_for)}</p>`;
        const extra = el("details");
        extra.innerHTML = `<summary>偏差 / 搜词 / 班次</summary><div class="src-sigs">${sig}</div><p class="src-caveat">偏差：${esc(c.caveat)}</p>`
          + (c.search_hint ? `<p class="src-hint">搜：「${esc(c.search_hint)}」</p>` : "")
          + `<p class="src-band">在用启研的同学里：${esc(c.band)} · ${esc(CADENCE[c.cadence] || c.cadence)} · ${esc(c.access)}${c.verified ? "" : " · 未核实"}</p>`;
        card.appendChild(extra);
        const t = el("button", c.read ? "btn small" : "btn small secondary", c.read ? "✓ 我常看" : "我常看");
        t.type = "button";
        t.onclick = async () => {
          try { await api("POST", "/api/channels/toggle", { uid: S.uid, id: c.id, on: !c.read, direction: dir }); backToPosition(seq); } catch (e) { toast(e.message); }
        };
        card.appendChild(t);
        list.appendChild(card);
      });
      host.appendChild(list);
    });
  });
  if (m.career_blind_spot) panel.appendChild(el("p", "map-foot", `进组信息的盲区：${esc(m.career_blind_spot)}`));
  if (m.access_note) panel.appendChild(el("p", "map-foot", esc(m.access_note)));
  body.appendChild(panel);
}

async function paintMap(seq, body) {
  const m = await api("GET", `/api/map?uid=${S.uid}&kit=${S.kitId}`);
  if (stale(seq)) return;
  const mismatch = kitMismatch(m.kit.direction, await myDirection());
  if (stale(seq)) return;
  if (mismatch) body.appendChild(mismatch);
  const panel = el("div", "panel");
  panel.appendChild(posLead("每个位置都是论文里作者自己写的局限或开放问题，不是排行榜。做完去：一句话。"));
  const sortBar = el("div", "map-sort", "<span>排序</span>");
  const sorts = { order: "工具包顺序", mine: "和我的边相关的在前", momentum: "势头" };
  const sortKey = S.mapSort || "order";
  Object.entries(sorts).forEach(([k, label]) => {
    const b = el("button", `linkish${k === sortKey ? " on" : ""}`, label);
    b.type = "button";
    b.onclick = () => { S.mapSort = k; setView("position"); };
    sortBar.appendChild(b);
  });
  panel.appendChild(sortBar);
  const rows = [...m.rows];
  if (sortKey === "mine") rows.sort((a, b) => b.my_edges.length - a.my_edges.length || a.order - b.order);
  if (sortKey === "momentum") rows.sort((a, b) => (b.momentum.relative || 0) - (a.momentum.relative || 0));
  rows.forEach((r) => {
    const card = el("article", "map-card");
    const mo = r.momentum;
    card.appendChild(el("h3", "", esc(r.niche)));
    card.appendChild(el("p", "map-card-meta", `需求 ${esc(r.demand.band)} · 势头 ${mo.trend ? esc(TREND[mo.trend]) : "—"} · 相关边 ${r.my_edges.length} 条`));
    const more = el("details");
    more.innerHTML = `<summary>原句、供给、你的边</summary><blockquote>「${esc(r.quote)}」——《${esc(r.paper)}》${esc(arxivSection(r.section))}</blockquote>`
      + `<p class="form-note">${esc(r.note)}</p>`
      + `<p class="form-note">已知供给：${esc(r.supply.status)}（去问）</p>`
      + `<p class="form-note">你的边：${r.my_edges.length ? r.my_edges.map((e) => esc(clip(e.text, 24))).join("、") : "还没有"}${r.read ? "" : " · 出处还没读"}</p>`;
    const use = el("button", "btn ghost small", "用它写定位");
    use.type = "button";
    use.onclick = () => { S.posDraft = { ...(S.posDraft || {}), x_ref: r.id }; S.posTab = "statement"; setView("position"); };
    more.appendChild(use);
    card.appendChild(more);
    panel.appendChild(card);
  });
  const foot = el("details", "map-foot-box");
  foot.appendChild(el("summary", "", "少于 k 人不报数等怎么算"));
  const base = m.base && m.base.growth ? `；cs.CL / cs.LG / cs.AI 整体近 12 个月是前 12 个月的 ×${m.base.growth}（核对于 ${esc(m.base.checked_at)}）` : "";
  foot.appendChild(el("p", "map-foot", `依据：${esc(m.formula)}${base}。供给要靠人：问学长学姐哪些组在做、收不收本科生。需求按周更新、滞后 ${m.lag_days} 天、少于 ${m.k_min} 人不报数。`));
  panel.appendChild(foot);
  if (m.rarity) {
    const r = m.rarity;
    const more = r.status === "ok" && r.total > r.pairs.length ? `（共 ${r.total} 组，只列最少见的 ${r.pairs.length} 组）` : "";
    panel.appendChild(el("p", "map-foot", `你的边两两组合有多稀有${more}：${r.status === "ok" ? r.pairs.map((p) => `${esc(p.a)} × ${esc(p.b)}：${esc(p.band)}`).join("；") || "边不到两条" : esc(r.why)}`));
  }
  body.appendChild(panel);
}

function clip(s, n) {
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

function arxivSection(s) {
  const cn = { conclusion: "结论", limitations: "局限", discussion: "讨论" };
  return s ? ` · ${cn[s] || s}` : "";
}

async function paintStatement(seq, body) {
  const [st, map, eg] = await Promise.all([
    api("GET", `/api/statement?uid=${S.uid}&kit=${S.kitId}`),
    api("GET", `/api/map?uid=${S.uid}&kit=${S.kitId}`),
    api("GET", `/api/edges?uid=${S.uid}`),
  ]);
  if (stale(seq)) return;
  const cur = st.statement;
  const draft = S.posDraft || {};
  const state = {
    x_ref: draft.x_ref || (cur && cur.x_ref) || "",
    x_text: draft.x_text ?? ((cur && cur.x_text) || ""),
    y: draft.y || (cur ? [...cur.y_ids] : []),
  };
  const keep = () => { S.posDraft = { ...state }; };

  if (cur) {
    const now = el("div", "panel stmt-now");
    now.appendChild(el("p", "section-label", `现在的定位 · 第 ${cur.version} 版 · ${cur.portfolio_ready ? "可以放进作品集" : "待证明"}`));
    now.appendChild(el("p", "stmt-sentence", esc(cur.sentence)));
    if (cur.niche) now.appendChild(el("p", "form-note", `指向：开放问题「${esc(cur.niche.niche)}」——《${esc(cur.niche.paper)}》${esc(arxivSection(cur.niche.section))}`));
    body.appendChild(now);
  }

  const panel = el("div", "panel");
  panel.appendChild(el("h3", "panel-title", cur ? "改一版" : "写第一版"));
  panel.appendChild(posLead("只能你写。启研只核对 X 是否具体、Y 有没有证据。做完去：这学期压哪。"));
  const form = el("div", "stmt-form");
  const line1 = el("div", "stmt-line", "<span>我是能做</span>");
  const x = el("input");
  x.placeholder = "点名对象、方法或数据：如「中文数学题基准上的单次污染检测」";
  x.value = state.x_text;
  x.maxLength = 60;
  line1.appendChild(x);
  line1.appendChild(el("span", "", "的人，"));
  const pick = el("label", "cf");
  pick.appendChild(el("span", "cf-label", "X 指向哪个开放问题"));
  const sel = el("select");
  sel.innerHTML = '<option value="">选一个…</option>' + map.rows.map((r) => `<option value="${r.id}">${esc(r.niche)}${r.my_edges.length ? `（你有 ${r.my_edges.length} 条相关边）` : ""}</option>`).join("");
  sel.value = state.x_ref;
  pick.appendChild(sel);
  const quote = el("p", "form-note stmt-quote");
  const paintQuote = () => {
    const r = map.rows.find((row) => row.id === sel.value);
    quote.innerHTML = r ? `原句：「${esc(r.quote)}」——《${esc(r.paper)}》${esc(arxivSection(r.section))}` : "";
  };
  paintQuote();
  const ylab = el("p", "cf-label", "因为（从「我有什么」里选，至少一条已证明的才能进作品集）");
  const ys = el("div", "edge-pick");
  eg.edges.forEach((e) => {
    const b = el("button", `edge-toggle${state.y.includes(e.id) ? " on" : ""}`, edgeChip(e));
    b.type = "button";
    b.onclick = () => {
      state.y = state.y.includes(e.id) ? state.y.filter((i) => i !== e.id) : [...state.y, e.id];
      b.classList.toggle("on");
      keep(); check();
    };
    ys.appendChild(b);
  });
  if (!eg.edges.length) ys.appendChild(el("p", "form-note", "还没有边——先去「我有什么」加几条。"));
  const checks = el("div", "rubric-list stmt-checks");
  const acts = el("div", "submit-actions");
  const save = el("button", "btn", cur ? `保存为第 ${cur.version + 1} 版` : "保存第一版");
  save.type = "button";
  acts.appendChild(save);
  acts.appendChild(el("span", "hint", "旧版本都留着；新证明了一条边，回来改一版。"));
  form.append(line1, pick, quote, ylab, ys, checks, acts);
  panel.appendChild(form);
  body.appendChild(panel);

  let timer = null;
  let checkGen = 0;  // 每次编辑加一；回来的检查结果编号不是最新的就丢掉
  async function check() {
    clearTimeout(timer);
    const gen = ++checkGen;
    timer = setTimeout(async () => {
      let r;
      try { r = await api("POST", "/api/statement", { uid: S.uid, kit: S.kitId, x_ref: state.x_ref, x_text: state.x_text, y: state.y, dry_run: true }); } catch (_) { return; }
      // 旧的检查可能比新的晚回来（服务器按到达先后排队），原来会用旧结果把「保存」又禁掉（Codex 复现）
      if (stale(seq) || gen !== checkGen) return;
      checks.innerHTML = "";
      r.checks.forEach((c) => {
        const mark = c.pass ? "✓" : c.level === "hint" ? "·" : "!";
        checks.appendChild(el("div", `rubric-item ${c.pass ? "pass" : c.level === "hint" ? "is-partial" : "fail"}`,
          `<span class="rubric-mark">${mark}</span><div><p class="rubric-crit">${esc(c.label)}${c.level === "portfolio" && !c.pass ? "<em>进作品集前要改</em>" : ""}</p><p class="rubric-comment">${esc(c.note)}</p></div>`));
      });
      save.disabled = !r.can_save;
    }, 250);
  }
  x.addEventListener("input", () => { state.x_text = x.value; keep(); check(); });
  sel.addEventListener("change", () => { state.x_ref = sel.value; keep(); paintQuote(); check(); });
  save.onclick = async () => {
    save.disabled = true;
    const sent = JSON.stringify(state);  // 这次交的是哪一版
    try {
      await api("POST", "/api/statement", { uid: S.uid, kit: S.kitId, x_ref: state.x_ref, x_text: state.x_text, y: state.y });
      // 保存期间离开再回来接着改了：只清掉这次交的那一版，后来写的留着；人已经去了别的页就别拉回来（原来两样都直接做，Codex 复现）
      const later = S.posDraft && JSON.stringify(S.posDraft) !== sent;
      if (!later) S.posDraft = null;
      toast(later ? "已保存；之后改的还在草稿里" : "已保存");
      if (!stale(seq)) setView("position");
    } catch (e) { toast(e.message); if (!stale(seq)) save.disabled = false; }
  };
  check();
}

/* 定位页里的写操作做完重画定位页——只在还是发起它的那一次渲染时。人去了别的页、或者在定位页里换了一栏
   （比如去「下注组合」正在填），就不重画：原来一律 setView，刚输入的内容被清掉（Codex 复现） */
function backToPosition(seq) { if (!stale(seq)) setView("position"); }

/* 按钮的点击在请求回来之前只算一次：原来双击「加上」会建两个同名目标，三个名额一下占满（Codex 复现） */
function once(btn, run) {
  return async (...args) => {
    if (btn.disabled) return;
    btn.disabled = true;
    try { await run(...args); } finally { btn.disabled = false; }
  };
}

async function paintBets(seq, body) {
  const kit = await api("GET", `/api/kits/${S.kitId}`);
  const [b, eg] = await Promise.all([
    api("GET", `/api/bets?uid=${S.uid}`),
    api("GET", `/api/edges?uid=${S.uid}`).catch(() => ({ edges: [] })),
  ]);
  if (stale(seq)) return;
  const panel = el("div", "panel");
  panel.appendChild(posLead("冲 / 稳 / 保是给这学期的，不是人生规划。没有边就先回『我有什么』。"));
  if (!(eg.edges || []).length && !(b.active || []).length) {
    const go = el("button", "btn", "去写边");
    go.type = "button";
    go.onclick = () => { S.posTab = "edges"; setView("position"); };
    panel.appendChild(el("p", "form-note", "没有边就先回「我有什么」。"));
    panel.appendChild(go);
    body.appendChild(panel);
    return;
  }
  panel.appendChild(el("p", "form-note", `同时最多 ${b.max} 个。冲：值得一试但不确定。稳：你有边能对上。保：不拥挤或有制度化通道。`));
  const list = el("div", "bet-list");
  b.active.forEach((t) => {
    const row = el("div", "bet-row");
    const niche = t.niche ? (kit.open_problems.find((o) => o.id === t.niche) || {}).niche : "";
    row.innerHTML = `<span class="tier tier-${t.tier}">${esc(b.tiers[t.tier])}</span><div class="bet-main"><b>${esc(t.name)}</b><small>${esc(b.kinds[t.kind])}${niche ? ` · ${esc(niche)}` : ""}</small></div>`;
    const end = el("button", "linkish", "结束");
    end.type = "button";
    end.onclick = () => {
      if (row.querySelector(".bet-close")) return;
      const f = el("div", "bet-close");
      const out = el("select");
      out.innerHTML = Object.entries(b.outcomes).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
      const why = el("input");
      why.placeholder = "一句原因：以后的你和后来的同学都用得上";
      const ok = el("button", "btn small", "记下");
      ok.type = "button";
      ok.onclick = once(ok, async () => { try { await api("POST", `/api/bets/${t.id}/close`, { uid: S.uid, outcome: out.value, reason: why.value }); backToPosition(seq); } catch (e) { toast(e.message); } });
      f.append(out, why, ok);
      row.appendChild(f);
    };
    row.querySelector(".bet-main").appendChild(end);
    list.appendChild(row);
  });
  if (!b.active.length) list.appendChild(el("p", "form-note", "还没有目标。"));
  panel.appendChild(list);
  if (b.checks.length) {
    const c = el("ul", "bet-checks");
    b.checks.forEach((x) => c.appendChild(el("li", x.level, esc(x.note))));
    panel.appendChild(c);
  }
  if (b.active.length < b.max) {
    panel.appendChild(el("p", "section-label", "加一个目标"));
    const f = el("div", "bet-form");
    const name = el("input");
    name.placeholder = "哪个组 / 计划 / 竞赛（写名字）";
    name.maxLength = 40;
    const kind = el("select");
    kind.innerHTML = Object.entries(b.kinds).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    const tier = el("select");
    tier.innerHTML = Object.entries(b.tiers).map(([k, v]) => `<option value="${k}">${esc(v)} · ${esc(TIER_HINT[k])}</option>`).join("");
    tier.value = "safety";
    const niche = el("select");
    niche.innerHTML = '<option value="">子方向（可选）</option>' + kit.open_problems.map((o) => `<option value="${o.id}">${esc(o.niche)}</option>`).join("");
    const add = el("button", "btn small", "加上");
    add.type = "button";
    add.onclick = once(add, async () => { try { await api("POST", "/api/bets", { uid: S.uid, name: name.value, kind: kind.value, tier: tier.value, kit: S.kitId, niche: niche.value }); backToPosition(seq); } catch (e) { toast(e.message); } });
    f.append(name, kind, tier, niche, add);
    panel.appendChild(f);
  }
  if (b.closed.length) {
    const h = el("details", "inventory");
    h.appendChild(el("summary", "", `结束的目标 ${b.closed.length} 个`));
    const ul = el("ul");
    b.closed.forEach((t) => ul.appendChild(el("li", "", `${esc(t.name)}（${esc(b.tiers[t.tier])}）<span>${esc(b.outcomes[t.outcome])}：${esc(t.reason)}</span>`)));
    h.appendChild(ul);
    panel.appendChild(h);
  }
  body.appendChild(panel);
}

/* ---------- ⑧ me 页 ---------- */

function projectFolders(projects) {
  const box = el("div", "mem-layer mem-layer-practice");
  box.appendChild(el("p", "mem-layer-head",
    `在做的项目<span class="mem-layer-n">${projects.length}</span>`));
  box.appendChild(el("p", "mem-layer-hint", "一个项目一张卡。里面每条都能单独改或删。"));
  projects.forEach((pj) => {
    const wrap = el("div", "proj");
    const head = el("div", "proj-head");
    head.appendChild(el("span", "proj-name", esc(pj.name)));
    head.appendChild(el("span", "proj-n", `${pj.facts.length} 条`));
    const body = el("div", "proj-body");
    pj.facts.forEach((f) => body.appendChild(memoryCard(f)));
    wrap.append(head, body);
    box.appendChild(wrap);
  });
  return box;
}

function layerBlock(L) {
  const box = el("section", `mem-layer mem-layer-${L.layer}`);
  const labels = {identity:"身份与专业", interest:"兴趣与交流偏好", capability:"你描述的学习基础", practice:"近期学习与经历", experience:"实际经历", other:"其他背景"};
  box.appendChild(el("h4", "mem-layer-head", `${esc(labels[L.layer] || L.label)}<span class="mem-layer-n">${L.facts.length}</span>`));
  const list = el("div", "mem-layer-list");
  L.facts.forEach((f) => list.appendChild(memoryCard(f)));
  box.appendChild(list);
  return box;
}

function memoryCard(f) {
  const derived = f.source === "derived";
  const card = el("div", `mem-card${derived ? " mem-derived" : ""}`);
  const match = derived && String(f.value).match(/^修过\s*(\d+)\s*门(.+?)类课程/);
  const value = el("div", "mem-value", esc(match ? `修过 ${match[1]} 门${match[2]}相关课程` : f.value));
  const meta = el("div", "mem-meta");
  const srcCn = {declared:"你的自述", inferred:"待确认的理解", behavior:"实际记录", derived:"来自课程记录", user_edit:"你的修改"};
  meta.appendChild(el("span", `badge src-${f.source}`, srcCn[f.source] || esc(f.source)));
  if (f.valid_until) meta.appendChild(el("span", "badge plain", `有效至 ${esc(String(f.valid_until).slice(0, 10))}`));
  card.append(value, meta);
  const quotes = (f.evidence || []).map((e) => e.quote).filter(Boolean);
  const ev = derived ? (f.evidence || []).filter((e) => e.type === "transcript") : [];
  if (quotes.length || ev.length || derived) {
    const details = el("details", "mem-evidence");
    details.appendChild(el("summary", "", derived ? "课程依据与统计" : "查看来源"));
    if (derived) details.appendChild(el("p", "mem-basis", esc(f.value)));
    const basis = quotes.length ? quotes.map((q) => `「${q}」`).join("；") : ev.map((e) => `${e.course} ${e.grade}`).join("、");
    if (basis) details.appendChild(el("p", "mem-basis", esc(basis)));
    card.appendChild(details);
  }
  const acts = el("div", "mem-acts");
  if (!derived) {
    const edit = el("button", "linkish", "修改");
    edit.type = "button";
    edit.onclick = () => {
      if (value.querySelector("input")) return;
      const input = el("input");
      input.type = "text";
      input.value = f.value;
      input.setAttribute("aria-label", "修改这条记忆");
      value.innerHTML = "";
      value.appendChild(input);
      input.focus();
      let done = false;
      const restore = (v) => { value.textContent = v; };
      const save = async () => {
        if (done) return;
        done = true;
        const v = input.value.trim();
        if (!v || v === f.value) { restore(f.value); return; }
        try {
          await api("PATCH", `/api/me/facts/${f.id}`, { uid: S.uid, value: v });
          restore(v);
          toast("改好了。这条现在算你亲口说的。");
        } catch (e) { toast(e.message); restore(f.value); }
      };
      input.addEventListener("blur", save);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.isComposing) input.blur();
        if (e.key === "Escape") { done = true; restore(f.value); }
      });
    };
    acts.appendChild(edit);
  }
  const del = el("button", "linkish danger", derived ? "暂不采用" : "删除");
  del.type = "button";
  del.onclick = async () => {
    del.disabled = true;
    try {
      await api("DELETE", `/api/me/facts/${f.id}?uid=${encodeURIComponent(S.uid)}`);
      card.classList.add("is-dismissed");
      del.textContent = "已移除";
      toast(derived ? "已移除这条理解，原始课程记录保留。" : "已移除这条记录。");
    } catch (e) { toast(e.message); del.disabled = false; }
  };
  acts.appendChild(del);
  card.appendChild(acts);
  return card;
}

/* ---------- 意见反馈 ---------- */

async function renderFeedback() {
  const seq = S.renderSeq;
  if (stale(seq)) return;
  $app.innerHTML = "";
  // 致谢三行 + 署名：这页的开场是一封短信，不是功能说明
  const head = el("header", "ws-head fb-head");
  head.appendChild(el("h2", "", "意见反馈"));
  const lead = el("div", "fb-lead");
  lead.appendChild(el("p", "", "感谢您体验“启研”项目！"));
  lead.appendChild(el("p", "", "目前项目仍处于开发阶段，部分功能尚不完善。我们真诚期待您畅所欲言，您的每一条反馈，都对项目的改进至关重要！"));
  lead.appendChild(el("p", "fb-sign", "“启研”项目团队"));
  head.appendChild(lead);
  $app.appendChild(head);

  const box = el("section", "panel fb-form-panel");
  const form = el("form", "fb-form");
  const ta = el("textarea");
  ta.maxLength = 4000;
  ta.placeholder = "网页bug，使用体验，新功能建议...什么都可以提~";
  ta.setAttribute("aria-label", "反馈内容");
  const counter = el("p", "fb-count", "0 / 4000");
  counter.setAttribute("aria-live", "polite");
  ta.addEventListener("input", () => { counter.textContent = `${ta.value.length} / 4000`; });
  const actions = el("div", "submit-actions");
  const ok = el("button", "btn", "提交反馈");
  ok.type = "submit";
  actions.appendChild(ok);
  form.append(ta, counter, actions);
  form.onsubmit = async (e) => {
    e.preventDefault();
    const text = ta.value.trim();
    if (text.length < 10) { toast("至少写 10 个字，说清哪里不对"); ta.focus(); return; }
    ok.disabled = true; ok.textContent = "提交中…";
    try {
      await api("POST", "/api/feedback", { uid: S.uid, text });
      toast("您的反馈已提交，非常感谢您对“启研”项目的支持！");
      setView("feedback");
    } catch (err) {
      toast(err.message);
      ok.disabled = false; ok.textContent = "提交反馈";
    }
  };
  box.appendChild(form);
  $app.appendChild(box);
}

function meFold(title, content, description = "") {
  const fold = el("details", "panel me-settings-fold");
  fold.appendChild(el("summary", "", title));
  if (description) fold.appendChild(el("p", "form-note", description));
  fold.appendChild(content);
  return fold;
}

function transcriptPanel(data, derived) {
  const panel = el("section", "panel me-transcript-panel");
  const courses = data.courses || [];
  panel.appendChild(el("h3", "me-section-title", "课程与学习基础"));
  if (data.error) panel.appendChild(el("p", "cov-bad", "课程记录暂时未能加载，请稍后刷新。"));
  else if (!courses.length) panel.appendChild(el("p", "panel-sub", "还没有课程记录。不导入成绩单也能开始阅读，想补充时再来。"));
  else {
    const summary = data.summary || {};
    const stats = el("div", "me-course-stats");
    [["课程记录", `${courses.length} 门`], ["通过学分", String(summary.passed_credits ?? "—")], ["在修课程", `${(summary.in_progress || []).length} 门`]].forEach(([label, val]) => {
      const cell = el("div", "me-course-stat");
      cell.append(el("span", "", label), el("strong", "", val));
      stats.appendChild(cell);
    });
    panel.appendChild(stats);
    panel.appendChild(el("p", "me-basis-note", "这些记录说明你接触过哪些课程，不等同于熟练掌握；回答问题时也会参考你的自述。"));
    if (derived.length) {
      const list = el("div", "me-derived-list");
      derived.forEach((f) => list.appendChild(memoryCard(f)));
      panel.appendChild(list);
    }
    const details = el("details", "me-course-details");
    details.appendChild(el("summary", "", `查看 ${courses.length} 门课程与成绩`));
    if (summary.gpa != null) details.appendChild(el("p", "form-note", `百分制课程参考 GPA ${summary.gpa.toFixed(4)}（计入 ${summary.gpa_credits} 学分）。字母等级未转换计入。`));
    const scroll = el("div", "me-table-scroll");
    const table = el("table", "me-course-table");
    const thead = el("thead"); const head = el("tr");
    ["课程", "成绩", "学分", "学期"].forEach((text) => { const th = el("th", "", text); th.scope = "col"; head.appendChild(th); });
    thead.appendChild(head); table.appendChild(thead);
    const body = el("tbody");
    courses.forEach((c) => {
      const row = el("tr");
      [c.course, c.grade || "未出成绩", String(c.credits ?? "—"), c.term || "—"].forEach((text) => row.appendChild(el("td", "", esc(text))));
      body.appendChild(row);
    });
    table.appendChild(body); scroll.appendChild(table); details.appendChild(scroll); panel.appendChild(details);
  }
  panel.appendChild(transcriptImportPanel());
  return panel;
}

async function renderMe() {
  const seq = S.renderSeq;
  const [mem, mine, portraits, transcript] = await Promise.all([
    api("GET", `/api/memory?uid=${encodeURIComponent(S.uid)}`).catch(() => ({layers:[], projects:[], error:true})),
    api("GET", `/api/me/facts?uid=${encodeURIComponent(S.uid)}`).catch(() => ({facts:[], error:true})),
    portraitSection(),
    api("GET", `/api/me/transcript?uid=${encodeURIComponent(S.uid)}`).catch(() => ({courses:[], error:true})),
  ]);
  if (stale(seq)) return;
  const facts = (mine.facts || []).filter((f) => ["draft","confirmed","active"].includes(f.status));
  const expired = facts.filter((f) => f.valid_until && new Date(f.valid_until).getTime() < Date.now());
  const layers = (mem.layers || []).filter((L) => (L.facts || []).length);
  const derived = layers.flatMap((L) => L.facts).filter((f) => f.source === "derived" && String(f.key).startsWith("transcript:"));
  $app.innerHTML = "";
  const page = el("div", "me-page");
  const head = workspaceHead("我的", "看看启研对你的了解。信息可以慢慢补充，不必一次填完整。");
  const back = el("button", "btn small secondary", "继续对话"); back.type = "button";
  back.onclick = () => setView("dialogue"); head.appendChild(back); page.appendChild(head);
  const overview = el("section", "me-overview");
  const intro = el("div");
  intro.append(el("p", "me-kicker", "当前学习画像"), el("h3", "", esc(portraits.dataset.activeLabel || S.portraitName || "当前画像")));
  const identity = ["grade","department","major"].map((key) => facts.find((f) => f.key === key)?.value).filter(Boolean);
  intro.appendChild(el("p", "panel-sub", esc(identity.join(" · ") || "可以在对话中认识彼此，也可以直接问问题。")));
  const read = el("button", "btn small", "去研读"); read.type = "button";
  read.onclick = () => S.entered ? setView("read") : enterApp("read");
  overview.append(intro, read); page.appendChild(overview);
  const layout = el("div", "me-layout");
  const main = el("div", "me-main"); const aside = el("aside", "me-aside");
  main.appendChild(transcriptPanel(transcript, derived));
  const board = el("section", "panel me-memory-panel");
  board.appendChild(el("h3", "me-section-title", "对你的了解"));
  board.appendChild(el("p", "panel-sub", "来源可以展开查看，说得不准确的地方随时修改。"));
  if (mem.error || mine.error) board.appendChild(el("p", "cov-bad", "部分记忆未能加载，请稍后刷新；原有记录没有删除。"));
  let visible = 0;
  layers.forEach((L) => {
    const items = L.facts.filter((f) => !derived.some((d) => d.id === f.id));
    if (items.length) { board.appendChild(layerBlock({...L, facts:items})); visible += items.length; }
  });
  if ((mem.projects || []).length) board.appendChild(projectFolders(mem.projects));
  if (!visible && !(mem.projects || []).length && !mem.error) board.appendChild(el("p", "me-empty", "暂时没有其他背景记录。你可以继续聊，也可以直接开始阅读。"));
  main.appendChild(board);
  if (facts.some((f) => f.status === "draft")) main.appendChild(confirmDrafts(facts));
  main.appendChild(meFold("更多背景（选填）", coverageBoard(), "保留原有信息类型，按需要补充即可。"));
  if (expired.length) {
    const content = el("div"); expired.forEach((f) => content.appendChild(el("p", "form-note", esc(f.value))));
    main.appendChild(meFold(`已过期的记录 · ${expired.length} 条`, content));
  }
  aside.appendChild(meFold("画像与方向设置", portraits, "切换画像、新建画像或管理已选方向。"));
  aside.appendChild(meFold("账号与数据", accountPanel(), "模型配置、数据导出和账号管理。"));
  layout.append(main, aside); page.appendChild(layout); $app.appendChild(page);
  if (S.scrollToConfirm) {
    S.scrollToConfirm = false;
    (document.getElementById("me-confirm") || board).scrollIntoView({block:"start"});
  }
}

/* ---------- 边学边练：项目 ---------- */

const STAGES = [
  { value: 0, label: "只学了概念", hint: "刚看过定义和例子，还没动手" },
  { value: 1, label: "做过小任务", hint: "在树上交过一两次二十分钟任务" },
  { value: 2, label: "学完一块", hint: "走完一个分支，或学过一门相关课" },
  { value: 3, label: "做过项目", hint: "交过一个完整的练手项目" },
];

const SRC_KIND_CN = {
  competition: "竞赛", open_source: "开源", open_problem: "公开题", dataset: "公开数据",
  course_project: "课程大作业", innovation_program: "创新项目",
};

const STATUS_CN = { pass: "做到", partial: "部分做到", fail: "还没做到" };
const STATUS_MARK = { pass: "✓", partial: "~", fail: "!" };

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function downloadBlob(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 800);
}

function projectTabs(active, mineCount) {
  const nav = el("nav", "ws-tabs");
  [["find", "找项目"], ["sources", "来源"], ["mine", mineCount ? `我的项目 · ${mineCount}` : "我的项目"]].forEach(([key, label]) => {
    const b = el("button", `ws-tab${key === active ? " on" : ""}`, label);
    b.type = "button";
    b.onclick = () => { S.projectTab = key; setView("projects"); };
    nav.appendChild(b);
  });
  return nav;
}

async function _deadRenderProjects() {
  const seq = S.renderSeq;
  await ensurePortrait();
  if (stale(seq)) return;
  let mineFailed = false;
  const [ctx, mine] = await Promise.all([
    api("GET", `/api/projects/context?uid=${S.uid}`).catch(() => null),
    api("GET", `/api/projects/mine?uid=${S.uid}`).catch(() => { mineFailed = true; return { projects: [] }; }),
  ]);
  if (stale(seq)) return;
  const tab = S.projectTab || "find";
  $app.innerHTML = "";
  $app.appendChild(workspaceHead("项目", "先说你想碰哪一类，再找公开真题。"));
  $app.appendChild(roomGuide("projects", (mine.projects || []).length > 0));
  $app.appendChild(projectTabs(tab, (mine.projects || []).length));
  if (tab === "mine" && mineFailed) {
    // 没取到不是没有：原来显示成「还没选项目」，选过的项目像是丢了（Codex 复现）
    $app.appendChild(emptyPanel("「我的项目」这次没取到（网络或服务器忙），选过的都还在。", "再试一次", "projects"));
    return;
  }
  if (tab === "mine") { renderMine(mine.projects || []); return; }
  if (tab === "sources") { renderSources(); return; }

  const t = trail();
  const field = activeField(t);
  const node = field ? currentOnPath(field, t.done) : null;
  const pathsBy = (ctx && ctx.paths) || {};
  const form = S.projectForm || {
    direction: (ctx && ctx.direction) || t.code || "ai",
    stage: ctx ? ctx.stage : 0,
    keywords: S.projectKeywords || "",
  };
  S.projectKeywords = "";
  // 任务 4 的路径：有路径的方向按「第几步」选，没有的按四个阶段选
  const defaultStep = (stage) => ({ 0: 1, 1: 2, 2: 3, 3: 5 }[stage] || 1);
  // 优先按方向树上的位置（当前节点属于路径第几步），其次按服务端记录推
  const treeStep = field && field.chain && node && t.code === form.direction ? stepOf(field, node.id) : null;
  if (!form.pathStep) {
    form.pathStep = treeStep ? treeStep.stage : ((ctx && ctx.path_step) || defaultStep(form.stage));
    form.fromTree = !!treeStep;
  }
  S.projectForm = form;

  const ivBox = el("div", "panel interview-box");
  const qEl = el("p", "panel-title", "你现在想先碰哪一类？");
  const chainEl = el("p", "form-note");
  const optsEl = el("div", "chip-row");
  const freeRow = el("div", "chat-input-row");
  const free = el("input");
  free.placeholder = "自己写一句也可以";
  free.maxLength = 40;
  const sendFree = el("button", "btn ghost small", "按这句");
  sendFree.type = "button";
  freeRow.append(free, sendFree);
  const force = el("button", "btn", "就按这些找");
  force.type = "button";
  force.hidden = true;
  ivBox.append(qEl, chainEl, optsEl, freeRow, force);
  $app.appendChild(ivBox);

  const extra = el("details", "project-extra");
  extra.appendChild(el("summary", "", "改条件"));
  extra.appendChild(el("h3", "section-label", "方向"));
  const dirs = el("div", "field-switch");
  const paintDirs = () => {
    dirs.innerHTML = "";
    BACKEND_CODES.forEach((code) => {
      const f = FIELD_TREES[code];
      if (!f) return;
      const b = el("button", "field-chip" + (code === form.direction ? " on" : ""), `<span>${esc(f.name)}</span>`);
      b.type = "button";
      b.onclick = () => { form.direction = code; paintDirs(); paintStages(); };
      dirs.appendChild(b);
    });
  };
  paintDirs();
  extra.appendChild(dirs);

  const stageLabel = el("h3", "section-label");
  const stages = el("div", "stage-pick");
  stages.setAttribute("role", "radiogroup");
  const stageNote = el("p", "form-note");
  const radio = (b, on) => { b.type = "button"; b.setAttribute("role", "radio"); b.setAttribute("aria-checked", on ? "true" : "false"); };
  function paintStages() {
    stages.innerHTML = "";
    const path = pathsBy[form.direction];
    stages.classList.toggle("path", !!path);
    if (path) {
      stageLabel.textContent = "你在路径的哪一步";
      path.steps.forEach((st) => {
        const on = st.step === form.pathStep;
        const b = el("button", "stage-opt" + (on ? " on" : ""), `<i>第 ${st.step} 步</i><b>${esc(st.name)}</b><small>过关：${esc(st.done_when)}</small>`);
        radio(b, on);
        b.onclick = () => { form.pathStep = st.step; form.stage = st.project_stage; paintStages(); };
        stages.appendChild(b);
      });
      const cur = path.steps.find((st) => st.step === form.pathStep);
      if (cur) form.stage = cur.project_stage;
      const why = form.fromTree && form.pathStep === (treeStep && treeStep.stage)
        ? `按你在方向树上的位置预选：第 ${form.pathStep} 步，不对就改。`
        : (ctx && ctx.reason ? `按你的记录预选：${ctx.reason}，不对就改。` : "");
      stageNote.textContent = `六步来自任务 4 的「${path.name}」路径（${path.source_doc}）。按你选的这一步找能交出它过关材料的项目。${why}`;
    } else {
      stageLabel.textContent = "你现在走到哪";
      STAGES.forEach((st) => {
        const on = st.value === form.stage;
        const b = el("button", "stage-opt" + (on ? " on" : ""), `<b>${st.label}</b><small>${st.hint}</small>`);
        radio(b, on);
        b.onclick = () => { form.stage = st.value; form.pathStep = defaultStep(st.value); paintStages(); };
        stages.appendChild(b);
      });
      stageNote.textContent = ctx && ctx.reason ? `按你的记录预选：${ctx.reason}。不对就改。` : "";
    }
  }
  paintStages();
  extra.append(stageLabel, stages, stageNote);
  extra.appendChild(el("h3", "section-label", "想练的关键词（可选）"));
  const row = el("div", "chat-input-row");
  const kw = el("input");
  kw.value = form.keywords;
  kw.placeholder = node ? `例如：${node.label}` : "例如：数据可视化、问卷、证明";
  kw.maxLength = 40;
  kw.setAttribute("aria-label", "关键词");
  kw.addEventListener("input", () => { form.keywords = kw.value; });
  const go = el("button", "btn", "按这些条件找");
  go.type = "button";
  row.append(kw, go);
  extra.appendChild(row);
  $app.appendChild(extra);

  const out = el("div", "project-results");
  $app.appendChild(out);

  let busy = false;
  let ivState = S.projectInterview || { session_id: "", chain: [], done: false };

  const run = async (payload) => {
    if (busy) return;
    busy = true;
    go.disabled = true; go.textContent = "正在查…";
    force.disabled = true;
    out.innerHTML = "";
    const wait = el("div", "panel");
    wait.appendChild(el("p", "panel-sub", "正在逐个来源检索公开项目，第一次大约十几秒。查到什么就给什么，查不到会如实写。"));
    out.appendChild(wait);
    try {
      const r = await api("POST", "/api/projects/search", payload);
      if (stale(seq)) return;
      S.projectResult = r;
      paintResults(out, r);
    } catch (e) {
      out.innerHTML = "";
      out.appendChild(el("div", "note-box", `检索失败（如实说明）：${esc(e.message)}`));
    } finally {
      busy = false;
      go.disabled = false; go.textContent = "按这些条件找";
      force.disabled = false;
    }
  };

  const runFromForm = () => run({
    uid: S.uid, direction: form.direction, stage: form.stage, keywords: form.keywords.trim(),
    path_step: pathsBy[form.direction] ? form.pathStep : 0,
    chain: (ivState.chain || []).slice(),
  });

  const runFromIv = (r) => {
    const q = r.query || {};
    ivBox.hidden = true;
    return run({
      uid: S.uid,
      direction: q.direction || "",
      stage: q.stage ?? 0,
      keywords: q.keywords || "",
      path_step: 0,
      chain: r.chain || [],
    });
  };

  const paintIv = () => {
    if (ivState.done) { ivBox.hidden = true; return; }
    ivBox.hidden = false;
    qEl.textContent = ivState.question || "你现在想先碰哪一类？";
    chainEl.textContent = (ivState.chain || []).length ? `已经记下：${ivState.chain.join(" → ")}` : "";
    optsEl.innerHTML = "";
    (ivState.options || []).forEach((o) => {
      const b = el("button", "chip", esc(o.label));
      b.type = "button";
      b.onclick = () => ask({ option_id: o.id });
      optsEl.appendChild(b);
    });
    if (ivState.unknown_ok && !(ivState.options || []).some((o) => o.id === "unknown")) {
      const b = el("button", "chip", "还不知道");
      b.type = "button";
      b.onclick = () => ask({ option_id: "unknown" });
      optsEl.appendChild(b);
    }
    force.hidden = !((ivState.chain || []).length);
  };

  const ask = async (payload) => {
    if (busy) return;
    busy = true;
    optsEl.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    sendFree.disabled = true; force.disabled = true;
    try {
      const r = await api("POST", "/api/projects/interview", {
        uid: S.uid, session_id: ivState.session_id || "", answer: payload.answer || "", option_id: payload.option_id || "",
      });
      if (stale(seq)) return;
      ivState = r;
      S.projectInterview = r;
      paintIv();
      if (r.done) {
        busy = false;
        await runFromIv(r);
      }
    } catch (e) {
      toast(e.message);
    } finally {
      sendFree.disabled = false; force.disabled = false;
      if (!ivState.done) {
        busy = false;
        paintIv();
      }
    }
  };

  sendFree.onclick = () => {
    const v = free.value.trim();
    if (v) ask({ answer: v });
  };
  free.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) sendFree.click(); });
  force.onclick = () => ask({ option_id: "done" });
  go.onclick = runFromForm;
  kw.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) runFromForm(); });

  if (ivState.done && S.projectResult) {
    ivBox.hidden = true;
    paintResults(out, S.projectResult);
  } else if (ivState.session_id && !ivState.done) {
    paintIv();
  } else {
    ask({});
  }
}

function paintResults(out, r) {
  out.innerHTML = "";
  const list = r.sources || [];
  const live = list.filter((s) => s.ok && s.live).length;
  const snap = list.filter((s) => s.ok && s.snapshot).length;
  const bad = list.filter((s) => !s.ok).length;
  const sources = el("details", "source-status");
  sources.appendChild(el("summary", "", `查了 ${list.length} 个来源：实时 ${live} 个 · 快照 ${snap} 个${bad ? ` · 没查到 ${bad} 个` : ""}`));
  const chips = el("div", "src-chips");
  list.forEach((s) => {
    const ok = s.ok;
    chips.appendChild(el("span", `src-chip ${ok ? "ok" : "bad"}`,
      `${ok ? "✓" : "✕"} ${esc(s.name)}<i>${ok ? `${s.count} 条${s.snapshot ? " · 快照" : ""}` : esc(s.error || "没查到")}</i>`));
  });
  sources.appendChild(chips);
  const head = el("div", "results-head");
  head.appendChild(el("h3", "panel-title", r.items && r.items.length ? `找到 ${r.items.length} 个可以做的项目` : "这次没有找到合适的项目"));
  head.appendChild(el("p", "panel-sub", `${esc(r.query.direction_name)} · ${r.query.path_step ? `路径第 ${r.query.path_step} 步「${esc(r.query.path_step_name)}」` : esc(r.query.stage_label)}${r.query.keywords ? ` · 「${esc(r.query.keywords)}」` : ""} · 检索于 ${fmtTime(r.retrieved_at)}${r.voice === "llm" ? "" : " · 规则版挑选"}`));
  out.append(head, sources);

  if (!r.items || !r.items.length) {
    out.appendChild(el("div", "note-box", esc(r.empty_reason || "来源里没有和这个方向、这个阶段对得上的公开项目。我们不补一个假的；可以换个关键词，或照下面的路线自己去看。")));
  }
  (r.items || []).forEach((p) => out.appendChild(projectCard(p)));

  if (r.routes && r.routes.length) {
    const routes = el("details", "panel routes");
    routes.open = !(r.items && r.items.length);
    routes.appendChild(el("summary", "", `去哪找更多 · ${r.routes.length} 个来源`));
    routes.appendChild(el("p", "panel-sub", "这些来源要你自己去看（需要登录、按届发布，或没有公开接口）。每条写了点哪里、搜什么。"));
    r.routes.forEach((rt) => {
      const item = el("div", "route");
      item.innerHTML = `<p class="route-name"><a href="${esc(rt.url)}" target="_blank" rel="noopener">${esc(rt.name)} ↗</a><span>${esc(rt.kind_label || "")}${rt.cadence ? " · " + esc(rt.cadence) : ""}</span></p><p class="route-how">${esc(rt.manual_route)}</p>${rt.search_terms && rt.search_terms.length ? `<p class="route-terms">搜：${rt.search_terms.map((t) => `<code>${esc(t)}</code>`).join(" ")}</p>` : ""}`;
      routes.appendChild(item);
    });
    out.appendChild(routes);
  }
}

function projectCard(p) {
  const card = el("article", "panel project-card");
  const top = el("div", "project-top");
  top.appendChild(el("h3", "project-name", `<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.name)}</a>`));
  if (p.difficulty) top.appendChild(el("span", "badge plain", esc(p.difficulty)));
  card.appendChild(top);
  card.appendChild(el("p", "project-src", `${p.path_step ? `对应路径第 ${p.path_step} 步 · ` : ""}${esc(p.source_name)} · 检索于 ${fmtTime(p.retrieved_at)}${p.snapshot ? " · 快照" : ""}${p.deadline ? ` · ${p.closed ? `已截止（${esc(p.deadline)}），可当练习` : `截止 ${esc(p.deadline)}`}` : ""}`));
  const dl = el("dl", "project-facts");
  dl.innerHTML = `<dt>在练什么</dt><dd>${esc(p.practices || "来源里没写明。")}</dd><dt>大概要做什么</dt><dd>${esc(p.todo || "来源里没写明，打开链接看原题。")}</dd>${p.why_fit ? `<dt>为什么是现在</dt><dd>${esc(p.why_fit)}</dd>` : ""}`;
  card.appendChild(dl);
  if (p.evidence_quote) card.appendChild(el("p", "project-quote", `原文：「${esc(p.evidence_quote)}」`));
  const acts = el("div", "submit-actions");
  const pick = el("button", "btn small", p.picked ? "已在我的项目里" : "就练这个");
  pick.type = "button";
  pick.disabled = !!p.picked;
  pick.onclick = async () => {
    pick.disabled = true;
    try {
      const saved = await api("POST", "/api/projects/pick", { uid: S.uid, id: p.id });
      p.picked = true;
      S.projectId = saved.id;
      setView("project");
    } catch (e) { toast(e.message); pick.disabled = false; }
  };
  const open = el("a", "btn small secondary", "打开来源 ↗");
  open.href = p.url; open.target = "_blank"; open.rel = "noopener";
  acts.append(pick, open);
  card.appendChild(acts);
  return card;
}

async function renderSources() {
  const seq = S.renderSeq;
  let reg = null;
  try {
    reg = await api("GET", "/api/projects/sources");
  } catch (e) {
    if (stale(seq)) return;
    $app.appendChild(el("div", "note-box", `来源清单读取失败（如实说明）：${esc(e.message)}`));
    return;
  }
  if (stale(seq)) return;
  const trailCode = trail().code;
  const list = reg.sources || [];
  const dirName = (code) => (FIELD_TREES[code] && FIELD_TREES[code].name) || code;
  const stageLabel = (v) => { const st = STAGES.find((s) => s.value === v); return st ? st.label : ""; };
  const inCurrent = (s) => (s.directions || []).includes(trailCode);
  const sorted = [...list].sort((a, b) => Number(inCurrent(b)) - Number(inCurrent(a)));
  const intro = el("p", "panel-sub sources-intro",
    `「找项目」背后的全部来源，共 ${list.length} 个，每条都由人工实际访问核对过（${fmtTime(reg.generated_at)}）。` +
    `其中一部分能实时检索，在「找项目」里直接出结果；其余按「怎么找」的路线自己去看。查不到就如实说查不到。`);
  $app.appendChild(intro);
  const grid = el("div", "src-grid");
  sorted.forEach((s) => {
    const card = el("article", "panel src-card");
    const top = el("div", "src-top");
    top.appendChild(el("h3", "src-name",
      s.home_url ? `<a href="${esc(s.home_url)}" target="_blank" rel="noopener">${esc(s.name)} ↗</a>` : esc(s.name)));
    if (s.kind && SRC_KIND_CN[s.kind]) top.appendChild(el("span", "kind-badge", SRC_KIND_CN[s.kind]));
    card.appendChild(top);
    if ((s.directions || []).length) {
      const chips = el("div", "src-chips-row");
      s.directions.forEach((c) => chips.appendChild(el("span", "mini-chip" + (c === trailCode ? " on" : ""), esc(dirName(c)))));
      card.appendChild(chips);
    }
    if ((s.stage_fit || []).length) {
      card.appendChild(el("p", "src-meta", `适合：${s.stage_fit.map((v) => stageLabel(v)).filter(Boolean).join(" / ")}`));
    }
    if (s.cadence) card.appendChild(el("p", "src-cadence", esc(s.cadence)));
    if (s.manual_route) card.appendChild(el("p", "route-how clamp", `怎么找：${esc(s.manual_route)}`));
    if ((s.search_terms || []).length) {
      card.appendChild(el("p", "route-terms", "搜：" + s.search_terms.slice(0, 4).map((t) => `<code>${esc(t)}</code>`).join(" ")));
    }
    grid.appendChild(card);
  });
  $app.appendChild(grid);
}

function renderMine(list) {
  const panel = el("div", "panel");
  if (!list.length) {
    panel.appendChild(el("p", "panel-sub", "还没有选定项目。在「找项目」里挑一个「就练这个」。"));
    const go = el("button", "btn ghost", "去找项目");
    go.type = "button";
    go.style.marginTop = "16px";
    go.onclick = () => { S.projectTab = "find"; setView("projects"); };
    panel.appendChild(go);
    $app.appendChild(panel);
    return;
  }
  const rows = el("div", "fact-list");
  list.forEach((p) => {
    const last = (p.reviews || [])[0];
    const row = el("button", "mine-row");
    row.type = "button";
    const state = p.status === "done" ? `做完了 · ${last.passed}/${last.total} 条做到` : (last ? `最近一次 ${last.passed}/${last.total} 条做到，还可以再改` : "还没交");
    row.innerHTML = `<span class="mine-name">${esc(p.name)}</span><span class="mine-meta">${esc(p.source_name || "")} · ${state}</span>`;
    row.onclick = () => { S.projectId = p.id; setView("project"); };
    rows.appendChild(row);
  });
  panel.appendChild(rows);
  $app.appendChild(panel);
}

async function renderProject() {
  const seq = S.renderSeq;
  if (!S.projectId) { S.projectTab = "mine"; setView("projects"); return; }
  let p;
  try {
    p = await api("GET", `/api/projects/${S.projectId}?uid=${S.uid}`);
  } catch (e) {
    if (stale(seq)) return;
    S.projectTab = "mine"; setView("projects"); toast(e.message); return;
  }
  if (stale(seq)) return;
  $app.innerHTML = "";
  const back = el("button", "linkish back-link", "← 我的项目");
  back.type = "button";
  back.onclick = () => { S.projectTab = "mine"; setView("projects"); };
  $app.appendChild(back);
  $app.appendChild(workspaceHead(esc(p.name)));
  $app.appendChild(el("div", "ws-status", `<span><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.source_name)} ↗</a></span><span>检索于 ${fmtTime(p.retrieved_at)}</span>`));

  const spec = el("div", "panel");
  const dl = el("dl", "project-facts");
  dl.innerHTML = `<dt>在练什么</dt><dd>${esc(p.practices || "来源里没写明。")}</dd><dt>大概要做什么</dt><dd>${esc(p.todo || "来源里没写明，打开链接看原题。")}</dd>`;
  spec.appendChild(dl);
  spec.appendChild(el("h3", "section-label", "压缩包里至少要有"));
  const need = el("ul", "criteria");
  [
    "README.md（放在最外层）：题目和来源链接、我做了什么、结果在哪、怎么复现、还没做完的",
    "results/：你自己做出来的图、表、输出或报告，每个文件在 README 里有一句说明",
    "代码类项目放 src/ 或 .ipynb，并写清怎么运行；调查、写作类项目写清数据来源和方法",
    "只交 .zip，不超过 20 MB；大数据集只放样例，写下载链接",
  ].forEach((t) => need.appendChild(el("li", "", esc(t))));
  spec.appendChild(need);
  spec.appendChild(el("h3", "section-label", "怎么评"));
  const how = el("ul", "criteria");
  ["说清了要解决什么问题", "有自己做出来的结果", "和项目要求对得上", "别人能照着核对或复现", "说清了没做完的和下一步"]
    .forEach((t) => how.appendChild(el("li", "", t)));
  spec.appendChild(how);
  spec.appendChild(el("p", "form-note", "只看压缩包里的文件，不评价你这个人，也不猜你没写出来的东西。"));
  const acts = el("div", "submit-actions");
  const tpl = el("button", "btn small secondary", "下载 README 模板");
  tpl.type = "button";
  tpl.onclick = async () => {
    const res = await apiFetch(`/api/projects/${p.id}/readme?uid=${S.uid}`);
    if (!res.ok) { toast("模板下载失败"); return; }
    downloadBlob(await res.blob(), "README.md");
  };
  const sample = el("button", "btn small ghost", "看一份示例压缩包");
  sample.type = "button";
  sample.onclick = async () => {
    const res = await apiFetch(`/api/projects/${p.id}/sample.zip?uid=${S.uid}`);
    if (!res.ok) { toast("示例生成失败"); return; }
    downloadBlob(await res.blob(), "示例成果.zip");
  };
  acts.append(tpl, sample);
  spec.appendChild(acts);
  $app.appendChild(spec);

  const up = el("div", "panel");
  up.appendChild(el("h3", "panel-title", "交成果"));
  const drop = el("label", "dropzone");
  const file = el("input");
  file.type = "file"; file.accept = ".zip,application/zip"; file.hidden = true;
  drop.append(file, el("span", "", "把 .zip 拖到这里，或点这里选文件"));
  up.appendChild(drop);
  const result = el("div", "review-out");
  up.appendChild(result);
  $app.appendChild(up);

  const shownIn = S.portraitId;  // 这个项目页属于哪份画像：交的时候带上，期间切走了服务器就不收
  const send = async (f) => {
    if (!f) return;
    if (!/\.zip$/i.test(f.name)) { toast("只收 .zip 文件"); return; }
    if (f.size > 20 * 1024 * 1024) { toast("压缩包超过 20 MB"); return; }
    drop.classList.add("busy");
    drop.querySelector("span").textContent = `正在看「${f.name}」…`;
    try {
      const at = shownIn ? `&portrait=${encodeURIComponent(shownIn)}` : "";
      const res = await apiFetch(`/api/projects/${p.id}/submit?uid=${S.uid}${at}`, {
        method: "POST", headers: { "Content-Type": "application/zip" }, body: f,
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) throw new Error((data && data.detail) || `提交失败 (${res.status})`);
      paintReview(result, data, false, (p.reviews || [])[0]);
      if (data.recorded === false) {
        // 评阅做完了但没记进项目（比如评阅期间在别的页面切了画像）：照样给看结果，但说清楚没保存
        result.prepend(el("div", "note-box", esc(data.note || "这次评阅没有保存。") + "要保存的话，切回原来的画像再交一次。"));
      } else {
        p.reviews = [data].concat(p.reviews || []);
      }
      result.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      result.innerHTML = "";
      result.appendChild(el("div", "note-box", esc(e.message)));
    }
    drop.classList.remove("busy");
    drop.querySelector("span").textContent = "再交一版：把 .zip 拖到这里，或点这里选文件";
    file.value = "";
  };
  file.onchange = () => send(file.files[0]);
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); send(e.dataTransfer.files[0]); });

  if (p.reviews && p.reviews.length) paintReview(result, p.reviews[0], true, p.reviews[1]);
}

function paintReview(box, r, old, prev) {
  box.innerHTML = "";
  const sec = el("section", "feedback");
  if (prev && typeof prev.passed === "number") {
    const d = r.passed - prev.passed;
    sec.appendChild(el("p", `review-delta ${d > 0 ? "up" : d < 0 ? "down" : ""}`,
      d > 0 ? `比上一版多做到 ${d} 条（${prev.passed} → ${r.passed}）` : d < 0 ? `比上一版少了 ${-d} 条（${prev.passed} → ${r.passed}），看看是不是漏交了文件` : `和上一版一样是 ${r.passed} 条，看下面「下一步」改的那一处有没有落到文件里`));
  }
  const head = el("div", "feedback-head");
  const hl = el("div");
  hl.appendChild(el("h3", "", old ? "上一次的评阅" : "评阅"));
  hl.appendChild(el("p", "", `${esc(r.summary || "")}${r.voice === "llm" ? "" : "（规则版）"}`));
  head.appendChild(hl);
  head.appendChild(el("div", "score-ring", `<span class="num">${r.passed}</span>/ ${r.total} 条做到`));
  sec.appendChild(head);
  if ((r.criteria || []).length) {
    const bar = el("div", "pass-bar");
    (r.criteria || []).forEach((c) => {
      bar.appendChild(el("i", c.status === "pass" ? "ok" : c.status === "partial" ? "part" : "bad"));
    });
    sec.appendChild(bar);
  }
  const list = el("div", "rubric-list");
  (r.criteria || []).forEach((c) => {
    const ev = (c.evidence || []).filter((e) => e.file)
      .map((e) => `<span class="ev"><code>${esc(e.file)}</code>${e.quote ? `「${esc(e.quote)}」` : ""}</span>`).join("");
    list.appendChild(el("div", `rubric-item ${c.status === "pass" ? "pass" : "fail"} is-${c.status}`,
      `<span class="rubric-mark" aria-label="${STATUS_CN[c.status]}">${STATUS_MARK[c.status]}</span><div><p class="rubric-crit">${esc(c.criterion)}<em>${STATUS_CN[c.status]}</em></p><p class="rubric-comment">${esc(c.comment)}</p>${ev ? `<p class="rubric-ev">${ev}</p>` : ""}${c.fix ? `<p class="rubric-fix">改：${esc(c.fix)}</p>` : ""}</div>`));
  });
  sec.appendChild(list);
  if (r.next_step) sec.appendChild(el("div", "why-box", `<b>下一步　</b>${esc(r.next_step)}`));
  if (r.passed === r.total) {
    const acts = el("div", "submit-actions");
    const nextOne = el("button", "btn", "找下一个项目（难一档）");
    nextOne.type = "button";
    nextOne.onclick = () => {
      const f = S.projectForm || {};
      S.projectForm = f.direction ? { ...f, stage: Math.min(3, (f.stage || 0) + 1), pathStep: Math.min(6, (f.pathStep || 1) + 1) } : null;
      S.projectResult = null; S.projectTab = "find"; setView("projects");
    };
    const today = el("button", "btn ghost", "回今日");
    today.type = "button";
    today.onclick = () => setView("today");
    acts.append(nextOne, today);
    sec.appendChild(acts);
  }
  const files = el("details", "inventory");
  if (!(r.inventory || []).length) files.hidden = true;
  files.appendChild(el("summary", "", `压缩包里的 ${(r.inventory || []).length} 个文件`));
  const ul = el("ul");
  (r.inventory || []).forEach((i) => ul.appendChild(el("li", "", `<code>${esc(i.path)}</code><span>${esc(i.kind)} · ${Math.max(1, Math.round(i.size / 1024))} KB</span>`)));
  files.appendChild(ul);
  (r.notes || []).forEach((n) => files.appendChild(el("p", "form-note", esc(n))));
  sec.appendChild(files);
  if (r.fact) {
    const fl = el("div", "fact-list");
    fl.appendChild(factCard(r.fact, false, !old));
    sec.appendChild(fl);
  }
  box.appendChild(sec);
}

/* ---------- 事实卡组件 ---------- */

function factCard(f, editable = false, isNew = false) {
  const card = el("div", `fact-card${isNew ? " new-fact" : ""}`);
  const valueHtml = editable
    ? `<input type="text" value="${esc(f.value)}" />`
    : `<div class="fact-value">${esc(f.value)}</div>`;
  const evidence = (f.evidence || []).map((e) => esc(e.quote ? `「${e.quote}」` : (e.type === "submission" ? `任务提交《${e.task_title || ""}》` : e.type === "project_submission" ? `项目成果《${e.task_title || ""}》` : e.type))).join("；");
  const when = f.source === "behavior" && f.created_at ? ` · ${esc(f.created_at.slice(5, 16).replace("T", " "))}` : "";
  card.innerHTML = `
    ${valueHtml}
    <div class="fact-meta">
      <span class="badge cat-${esc(f.category)}">${CAT_CN[f.category] || esc(f.category)}</span>
      <span class="badge plain">${SRC_CN[f.source] || esc(f.source)}${when}</span>
      ${f.status === "draft" ? '<span class="badge draft">待核对</span>' : ""}
    </div>
    ${evidence ? `<div class="fact-evidence">依据：${evidence}</div>` : ""}
    <div class="fact-actions"></div>`;
  if (!editable) {
    const acts = card.querySelector(".fact-actions");
    const edit = el("button", "linkish", "修改");
    edit.type = "button";
    const valueNode = card.querySelector(".fact-value");
    edit.onclick = async () => {
      if (valueNode.querySelector("input")) return;
      const input = el("input"); input.type = "text"; input.value = f.value;
      valueNode.innerHTML = ""; valueNode.appendChild(input); input.focus();
      const save = async () => {
        const v = input.value.trim();
        if (!v || v === f.value) { valueNode.textContent = f.value; return; }
        try {
          await api("PATCH", `/api/me/facts/${f.id}`, { uid: S.uid, value: v });
          f.value = v; valueNode.textContent = v;
          toast("已修改，之后读到的是新版本");
        } catch (e) { toast(e.message); valueNode.textContent = f.value; }
      };
      input.addEventListener("blur", save);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.isComposing) input.blur();
        if (e.key === "Escape") { input.value = f.value; input.blur(); }
      });
    };
    const del = el("button", "linkish danger", "删除");
    del.type = "button";
    del.onclick = async () => {
      try {
        await api("DELETE", `/api/me/facts/${f.id}?uid=${S.uid}`);
        card.classList.add("is-dismissed");
        del.disabled = true; del.textContent = "已删除"; edit.disabled = true;
        toast("已删除，之后不再读取这一条");
      } catch (e) { toast(e.message); }
    };
    acts.append(edit, del);
  }
  return card;
}

/* 同一个浏览器的另一个标签页退出或换了人：这一页手里的会话和画面都已经不对，跟着重载 */
window.addEventListener("storage", (e) => {
  if (e.key !== null && e.key !== "rg_token") return;
  let now = "";
  try { now = localStorage.getItem("rg_token") || ""; } catch (_) { return; }
  if (now !== S.token) location.reload();
});

/* ---------- 导航 & 启动 ---------- */

document.querySelectorAll(".nav-btn").forEach((b) => {
  b.addEventListener("click", () => {
    if (!S.uid) return;
    setView(b.dataset.view);
  });
});
document.getElementById("brandHome").addEventListener("click", () => setView("home"));
document.getElementById("feedbackBtn").addEventListener("click", () => {
  if (!S.uid) return;
  setView("feedback");
});
function closeDirMenu() {
  document.getElementById("dirMenu")?.remove();
}

/* 右上角「画像/方向」下拉：方向是多选。添加过的方向都列在这里，
   勾上的进推荐，取消勾选就退出去（选项保留，随时勾回来）。
   按钮上显示的是画像名，具体勾了哪些在这里看 ✓。 */
function openDirMenu() {
  const chip = document.getElementById("dirChip");
  if (!chip || !S.uid) return;
  if (document.getElementById("dirMenu")) { closeDirMenu(); return; }
  const t = trail();
  // 老数据只有方向没有 dirs：把它当作选项池
  const dirs = t.dirs.length ? t.dirs : (t.dir && (!window.RG_DIR || RG_DIR.byId[t.dir]) ? [t.dir] : []);
  if (!dirs.length) { setView("cards"); return; }
  const menu = el("div", "dir-menu");
  menu.id = "dirMenu";
  menu.appendChild(el("p", "dir-menu-head", "我的方向 · 可多选，推荐按勾选的组合来"));
  const sel = new Set(selectedDirs(t));
  dirs.forEach((id) => {
    const on = sel.has(id);
    const b = el("button", "dir-menu-item" + (on ? " on" : ""),
      `${on ? "✓ " : ""}${esc(dirTitle(id) || id)}`);
    b.type = "button";
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.onclick = async () => {
      closeDirMenu();
      await toggleDirSel(id);
      setTimeout(() => { if (document.getElementById("dirChip")) openDirMenu(); }, 0);
    };
    menu.appendChild(b);
  });
  const tree = el("button", "dir-menu-item", "查看方向树");
  tree.type = "button";
  tree.onclick = () => { closeDirMenu(); S.entered ? setView("cards") : enterApp("cards"); };
  const mine = el("button", "dir-menu-item", "管理画像与方向");
  mine.type = "button";
  mine.onclick = () => { closeDirMenu(); setView("me"); };
  menu.append(tree, mine);
  chip.parentNode.appendChild(menu);
}

document.getElementById("dirChip").addEventListener("click", (e) => {
  e.stopPropagation();
  if (!S.uid) return;
  openDirMenu();
});
document.getElementById("userMenuBtn").addEventListener("click", (e) => {
  e.stopPropagation();
  if (!S.uid) return;
  openUserMenu();
});
const navToggle = document.getElementById("navToggle");
if (navToggle) navToggle.addEventListener("click", () => setNavCollapsed(!navCollapsed()));
const navExpand = document.getElementById("navExpand");
if (navExpand) navExpand.addEventListener("click", () => setNavCollapsed(false));
document.addEventListener("click", (e) => {
  const menu = document.getElementById("userMenu");
  if (menu && !menu.contains(e.target) && !e.target.closest("#userMenuBtn")) closeUserMenu();
  const dirm = document.getElementById("dirMenu");
  if (dirm && !dirm.contains(e.target) && !e.target.closest("#dirChip")) closeDirMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeUserMenu(); closeDirMenu(); }
});

(async function boot() {
  const t0 = performance.now();
  try {
    const [h, paths] = await Promise.all([api("GET", "/api/health"), api("GET", "/api/paths").catch(() => null)]);
    S.llmInfo = h.llm || null;  // 服务器那把兜底密钥的状态，登录、退出时重画侧栏的模型钮要用
    applyLlmPill(h.llm);
    S.auth = h.auth || null;
    if (paths) applyPaths(paths.paths);
  } catch (_) { /* 健康检查失败不挡页面 */ }
  buildTutorials();
  let needConsent = false;
  if (S.uid && !S.token) {
    // 有账号之前，浏览器里只存了 uid：凭它认领一次，换成会话
    try {
      setSession(await api("POST", "/api/auth/legacy", { uid: S.uid }));
    } catch (e) {
      if (e.status === 401) clearSession();  // 认领过或不存在；网络错误时 uid 是这个人唯一的凭证，不能扔
    }
  }
  if (S.token) {
    try {
      const me = await api("GET", "/api/auth/me");
      setSession(me);
      needConsent = !me.consent_ok;
      await loadPrefs();  // 工具包与收藏方向一起恢复；读不到就用本地的工具包、方向留空
      await ensurePortrait();
    } catch (e) {
      if ([401, 404, 410].includes(e.status)) {  // 会话确实没了：清身份（草稿不动），落到登录页并说清为什么
        clearSession();
        reloadInto("login", sessionGoneMsg());
        return;
      } else {
        // 网络断了、服务器在重启：会话和草稿都留着，原来这里一律清掉，草稿就永久没了（Codex 复现）
        setTimeout(() => toast("暂时连不上服务器，稍后刷新一下"), 800);
      }
    }
  } else if (S.uid) {
    // 只有 uid、认领没成（网络错误）：先去登录页，但 uid 留着，下次打开再认领——它是这个人唯一的凭证
    setTimeout(() => toast("暂时连不上服务器，稍后刷新一下"), 800);
  }
  let next = "home", note = "";
  try {
    next = sessionStorage.getItem("rg_next_view") || sessionStorage.getItem("rg_last_view") || "home";
    note = sessionStorage.getItem("rg_toast") || "";
    sessionStorage.removeItem("rg_next_view");
    sessionStorage.removeItem("rg_toast");
  } catch (_) { /* 无痕模式等 */ }
  let landing = next === "login" && !S.uid ? "login" : next !== "home" && S.uid ? next : "home";
  if (S.uid && !S.entered && !["home", "me", "login", "dialogue"].includes(landing)) landing = "dialogue";  // 还没进入：先把开场聊完或点「进入启研」
  setView(landing);
  if (note) toast(note);
  if (needConsent) openPrivacy(true);
  // 开场最多停 0.7 秒：数据到了就走，不再固定等 1.4 秒
  const splash = document.getElementById("boot");
  setTimeout(() => {
    if (!splash) return;
    splash.classList.add("out");
    setTimeout(() => splash.remove(), 600);
  }, Math.max(0, 700 - (performance.now() - t0)));
})();

function transcriptImportPanel() {
    // 成绩单单独给一个粘贴入口：它是一条记录变多条，聊天里说不清楚
    const tcol = el("details", "me-import-fold");
    tcol.appendChild(el("summary", "", "导入或更新成绩单（选填）"));
    tcol.appendChild(el("p", "cov-why", "从树洞成绩单页全选、复制、粘贴。课程只作为学习背景；确认保存后会替换当前画像的课程记录。"));
    const ta = el("textarea", "cov-paste");
    ta.rows = 5;
    ta.setAttribute("aria-label", "粘贴成绩单");
    ta.placeholder = "粘贴整份成绩单，保留学期标题和换行。先预览，确认后再保存。";
    const act = el("div", "cov-actions");
    const btn = el("button", "btn small", "预览识别结果");
    btn.type = "button";
    const report = el("div", "cov-report");
    btn.onclick = async () => {
      const text = ta.value.trim();
      if (!text) { toast("先把成绩单粘进来"); return; }
      act.innerHTML = ""; act.appendChild(btn);
      btn.disabled = true;
      try {
        const r = await api("POST", "/api/me/transcript/parse", { uid: S.uid, text });
        report.innerHTML = "";
        if (!r.courses || !r.courses.length) {
          report.appendChild(el("p", "cov-bad",
            "没识别到课程。" + ((r.warnings || [])[0] || "请检查格式。")));
          btn.disabled = false;
          return;
        }
        const s = r.summary || {};
        report.appendChild(el("p", "cov-ok",
          `识别到 ${r.courses.length} 门课，涉及 ${(r.terms || []).length} 个学期` +
          (s.gpa != null ? `；参考 GPA ${s.gpa.toFixed(3)}，均分 ${s.avg_score.toFixed(1)}` : "") +
          `；通过学分 ${s.passed_credits}，其中计 GPA ${s.gpa_credits}。`));
        // 少算了什么必须自己说出来。字母等级不进 GPA 是我们的选择，
        // 用户看到的总绩点是少算过的——不说就成了一个「看起来完整其实是错的数」。
        if ((s.ungraded || []).length) {
          report.appendChild(el("p", "cov-warn",
            `有 ${s.ungraded.length} 门是字母等级/五级制（`
            + s.ungraded.map((u) => `${esc(u.course)} ${esc(u.grade)}`).join("、")
            + `，共 ${s.ungraded_credits} 学分），学分已计入，但**没有算进绩点**——`
            + "教务的换算口径我不敢替你定，目前没有转换计入。"));
        }
        if ((s.in_progress || []).length) {
          report.appendChild(el("p", "cov-warn",
            `有 ${s.in_progress.length} 门成绩还没出（`
            + s.in_progress.map((u) => esc(u.course)).join("、")
            + `），我按「在修」记下来了，不计入已修学分。`));
        }
        // 把「我从成绩单读出了什么」先给用户看。落库前就能判断我们有没有读错。
        (r.preview || []).forEach((d) => {
          report.appendChild(el("p", "cov-derived", `会记下：${esc(d.value)}`));
        });
        (r.warnings || []).forEach((w) => report.appendChild(el("p", "cov-warn", esc(w))));
        const ok = el("button", "btn small", "确认并更新课程");
        ok.type = "button";
        ok.onclick = async () => {
          ok.disabled = true;
          try {
            const w = await api("POST", "/api/me/transcript",
              { uid: S.uid, text, mode: "replace" });
            toast(`已记下 ${w.written} 门课`);
            setView("me");
          } catch (e) { toast(e.message); ok.disabled = false; }
        };
        act.appendChild(ok);
        btn.disabled = false;
      } catch (e) { toast(e.message); btn.disabled = false; }
    };
    act.appendChild(btn);
    tcol.appendChild(ta);
    tcol.appendChild(act);
    tcol.appendChild(report);
    return tcol;
}
