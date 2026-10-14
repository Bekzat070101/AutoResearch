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

if (fails) {
  console.log("失败 " + fails + " 项");
  process.exit(1);
}
console.log("全部通过");
