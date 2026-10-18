/* 20-R02 草稿按服务端 rev 同步；20-R03 prefs 读失败不全量覆盖收藏、boot 恢复方向。
   用法：node tools/verify/verify_frontend_state.js drafts|prefs|boot */
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "..", "..");
const appSrc = fs.readFileSync(path.join(root, "web", "js", "app.js"), "utf8");

let fails = 0;
const check = (c, l, e = "") => { console.log(`  [${c ? "OK " : "!! "}] ${l} ${e}`); if (!c) fails++; };
const tick = () => new Promise((r) => setTimeout(r, 5));

function extract(src, name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(src);
  if (!m) throw new Error("找不到函数: " + name);
  let i = src.indexOf("{", m.index + m[0].length - 1), d = 0, j = i;
  for (; j < src.length; j++) {
    if (src[j] === "{") d++;
    else if (src[j] === "}") { d--; if (d === 0) break; }
  }
  return src.slice(m.index, j + 1);
}

function memStorage() {
  const mem = {};
  return {
    mem,
    getItem: (k) => (k in mem ? mem[k] : null),
    setItem: (k, v) => { mem[k] = String(v); },
    removeItem: (k) => { delete mem[k]; },
  };
}

/* ---------- R02 ---------- */
function draftServer() {
  // 服务端按 rev 做 CAS；updated_at 带可调的时钟差，只展示
  const srv = { text: "", rev: 0, skewMin: 0, puts: [], getHold: null, putHold: null, putFail: false };
  srv.stamp = () => new Date(Date.now() + srv.skewMin * 60000).toISOString();
  srv.api = async (m, url, body) => {
    if (!url.endsWith("/draft")) return {};
    if (m === "GET") {
      const snap = { text: srv.text, rev: srv.rev, updated_at: srv.stamp() };
      if (srv.getHold) await srv.getHold;
      return snap;
    }
    if (m === "PUT") {
      srv.puts.push(body);
      if (srv.putHold) { const h = srv.putHold; srv.putHold = null; await h; }
      if (srv.putFail) throw new Error("offline");
      if (body.base_rev !== srv.rev) { const e = new Error("草稿已在别处更新，本地稿已保留"); e.status = 409; throw e; }
      srv.text = body.text; srv.rev += 1;
      return { text: srv.text, rev: srv.rev, updated_at: srv.stamp() };
    }
    return {};
  };
  return srv;
}

function draftClient(srv, ls) {
  const S = { uid: "u1", token: "t" };
  const localStorage = ls || memStorage();
  const fns = ["readDraft", "readDraftMeta", "writeDraftMeta", "writeDraft", "hydrateDraft"]
    .map((n) => extract(appSrc, n)).join("\n");
  const toasts = [];
  const toast = (t) => toasts.push(t);
  const document = { activeElement: null, createElement: () => ({ removed: false, remove() { this.removed = true; }, set textContent(v) { this.text = v; } }) };
  const f = new Function("S", "api", "localStorage", "document", "toast",
    fns + "; return {readDraft, readDraftMeta, writeDraft, hydrateDraft};");
  return Object.assign({ S, localStorage, toasts }, f(S, (m, u, b) => srv.api(m, u, b), localStorage, document, toast));
}

function hold() { let open; const p = new Promise((r) => { open = r; }); return { p, open }; }

async function drafts() {
  for (const skew of [-10, 10]) {
    console.log(`=== R02 时钟差 ${skew} 分钟 ===`);
    // 1. 本地新稿 PUT 失败，服务端是旧稿（时间戳更「新」也不算）：hydrate 不覆盖、dirty 仍在
    {
      const srv = draftServer(); srv.skewMin = skew;
      await srv.api("PUT", "/api/tasks/t1/draft", { text: "旧稿", base_rev: 0 });
      const c = draftClient(srv);
      await c.hydrateDraft("t1", { value: "" });
      srv.putFail = true;
      c.writeDraft("t1", "新稿"); await tick();
      const ta = { value: "新稿" };
      await c.hydrateDraft("t1", ta);
      check(c.readDraft("t1") === "新稿" && ta.value === "新稿", "PUT 失败后 GET 旧稿不覆盖本地新稿", c.readDraft("t1") + "/" + ta.value);
      check(c.readDraftMeta("t1").dirty === true, "dirty 仍为 true");
      srv.putFail = false;
      await c.hydrateDraft("t1", ta);
      check(srv.text === "新稿" && c.readDraftMeta("t1").dirty === false, "恢复联网后新稿上传、dirty 清掉", srv.text);
    }
    // 2. GET 等待期间继续输入：旧快照不盖输入框
    {
      const srv = draftServer(); srv.skewMin = skew;
      await srv.api("PUT", "/api/tasks/t1/draft", { text: "云端", base_rev: 0 });
      const c = draftClient(srv);
      const g = hold(); srv.getHold = g.p;
      const ta = { value: "" };
      const h = c.hydrateDraft("t1", ta);
      await tick();
      srv.putFail = true;
      ta.value = "打字中"; c.writeDraft("t1", "打字中");
      srv.getHold = null; g.open(); await h; await tick();
      check(ta.value === "打字中" && c.readDraft("t1") === "打字中", "GET 期间的输入不被旧快照覆盖", ta.value);
      check(c.readDraftMeta("t1").dirty === true, "dirty 正确（未上传）");
    }
    // 3. 旧 PUT 的 ack 不清掉更新稿的 dirty；同一任务最多一个 PUT 在途
    {
      const srv = draftServer(); srv.skewMin = skew;
      const p1 = hold(); srv.putHold = p1.p;
      let count = 0;
      const inner = srv.api;
      srv.api = async (m, u, b) => (m === "PUT" && ++count >= 2 ? Promise.reject(new Error("offline")) : inner(m, u, b));
      const c = draftClient(srv);
      await c.hydrateDraft("t1", { value: "" });
      c.writeDraft("t1", "A"); await tick();
      c.writeDraft("t1", "AB"); await tick();
      check(count === 1, "第一笔在途时不并发第二笔 PUT", String(count));
      p1.open(); await tick(); await tick();
      const m = c.readDraftMeta("t1");
      check(srv.text === "A" && m.base_rev === 1, "A 的 ack 只推进 base_rev", JSON.stringify(m));
      check(m.dirty === true && c.readDraft("t1") === "AB", "AB 失败后 dirty 仍为 true", JSON.stringify(m));
    }
    // 4. 清空稿失败后会重试
    {
      const srv = draftServer(); srv.skewMin = skew;
      const c = draftClient(srv);
      await c.hydrateDraft("t1", { value: "" });
      c.writeDraft("t1", "要清掉的"); await tick();
      srv.putFail = true;
      c.writeDraft("t1", ""); await tick();
      check(srv.text === "要清掉的" && c.readDraftMeta("t1").dirty === true, "清空失败：本地空稿 dirty");
      srv.putFail = false;
      const ta = { value: "" };
      await c.hydrateDraft("t1", ta);
      check(srv.text === "" && ta.value === "" && c.readDraftMeta("t1").dirty === false, "重开后空稿重试成功，云端清空", JSON.stringify(srv.text));
    }
    // 5. 服务端先收新写、再收旧写：后到的 409，本地保留并显示冲突；点按钮先 GET 再 CAS
    {
      const srv = draftServer(); srv.skewMin = skew;
      const a = draftClient(srv), b = draftClient(srv);
      const inserted = [];
      const taB = { value: "", insertAdjacentElement: (_, el) => { inserted.push(el); return el; } };
      await a.hydrateDraft("t1", { value: "" });
      await b.hydrateDraft("t1", taB);
      await a.writeDraft("t1", "新写");
      taB.value = "旧写";
      await b.writeDraft("t1", "旧写");
      check(srv.text === "新写", "后到的旧写被 CAS 拒绝，云端仍是新写", srv.text);
      check(b.readDraft("t1") === "旧写" && b.readDraftMeta("t1").dirty === true, "409 后本地稿保留、dirty");
      check(inserted.length === 1 && inserted[0].text === "用本地稿覆盖云端", "显示「用本地稿覆盖云端」");
      if (inserted[0] && inserted[0].onclick) await inserted[0].onclick();
      check(srv.text === "旧写" && b.readDraftMeta("t1").dirty === false, "点按钮后 GET 最新 rev 再 CAS 覆盖", srv.text);
    }
    // 6. 升级前没有 rev 的本地稿：按 dirty + 冲突展示，不静默上传
    {
      const srv = draftServer(); srv.skewMin = skew;
      await srv.api("PUT", "/api/tasks/t1/draft", { text: "云端稿", base_rev: 0 });
      const ls = memStorage();
      ls.setItem("rg_draft_u1_t1", "老本地稿");
      ls.setItem("rg_draft_meta_u1_t1", JSON.stringify({ updated_at: "2030-01-01T00:00:00Z", dirty: false }));
      const c = draftClient(srv, ls);
      const n0 = srv.puts.length;
      const inserted = [];
      const ta = { value: "老本地稿", insertAdjacentElement: (_, el) => { inserted.push(el); return el; } };
      await c.hydrateDraft("t1", ta);
      check(srv.puts.length === n0 && srv.text === "云端稿", "无 rev 的老稿不静默上传");
      check(ta.value === "老本地稿" && c.readDraftMeta("t1").dirty === true && inserted.length === 1, "老稿按 dirty 冲突展示");
    }
  }
}

/* ---------- R03 ---------- */
function prefsEnv(remoteDirs) {
  const store = { prefs: { directions: remoteDirs.slice() }, trail: null };
  const calls = { puts: [], trailPuts: [] };
  const net = { getFail: false };
  const S = { myDirs: undefined, uid: "u1", token: "t", portraitId: "p1", view: "today" };
  const api = async (m, url, body) => {
    if (url === "/api/me/prefs" && m === "GET") {
      if (net.getFail) { const e = new Error("服务器出错"); e.status = 500; throw e; }
      return JSON.parse(JSON.stringify(store.prefs));
    }
    if (url === "/api/me/prefs" && m === "PUT") {
      calls.puts.push(body);
      if (body.directions) store.prefs.directions = body.directions.slice();
      return JSON.parse(JSON.stringify(store.prefs));
    }
    if (url.startsWith("/api/projects/") && m === "PUT") { calls.trailPuts.push(body.data); store.trail = body.data; return {}; }
    if (url === "/api/directions/choose") return {};
    return {};
  };
  const toasts = [];
  const toast = (t) => toasts.push(t);
  const ls = memStorage();
  const RG_DIR = { byId: { A: {}, B: {}, C: {} }, legacyDir: {}, backendCode: (x) => "code-" + x };
  const window = { RG_DIR };
  const names = ["loadPrefs", "ensureMyDirs", "putPrefs", "trailStorageKey", "trail", "saveTrail",
    "selectedDirs", "portraitDirsAdd", "addMyDirection", "removeMyDirection", "chooseDirection"];
  const src = names.map((n) => extract(appSrc, n)).join("\n");
  const f = new Function("S", "api", "toast", "paintChrome", "useKit", "window", "RG_DIR", "FIELD_TREES",
    "localStorage", "setView", "dirTitle",
    src + "; return {loadPrefs, trail, saveTrail, addMyDirection, removeMyDirection, chooseDirection};");
  const fns = f(S, api, toast, () => {}, () => {}, window, RG_DIR, {}, ls, () => {}, (x) => x);
  return Object.assign({ store, calls, net, S, toasts, ls }, fns);
}

async function prefs() {
  for (const cold of [false, true]) {
    console.log(`=== R03 prefs 中途 500（${cold ? "冷启动" : "已加载过"}）===`);
    for (const op of ["add", "choose", "remove"]) {
      const env = prefsEnv(["A"]);
      await env.saveTrail({ code: "", dirs: ["A"], sel: ["A"] });
      if (!cold) await env.loadPrefs();
      env.store.prefs.directions = ["A", "B"];        // 别的设备加了 B
      env.net.getFail = true;
      const loaded = await env.loadPrefs();
      check(loaded === false && env.S.myDirs === undefined, `${op}: loadPrefs 失败把 S.myDirs 置回 undefined`);
      const trailBefore = JSON.stringify(env.trail());
      const tp0 = env.calls.trailPuts.length;
      if (op === "add") await env.addMyDirection("C");
      if (op === "choose") await env.chooseDirection("C");
      if (op === "remove") await env.removeMyDirection("A");
      const dirPuts = env.calls.puts.filter((b) => b.directions);
      check(dirPuts.length === 0, `${op}: 0 次 directions PUT`, JSON.stringify(env.calls.puts));
      check(JSON.stringify(env.store.prefs.directions) === JSON.stringify(["A", "B"]), `${op}: 远端 A+B 不变`);
      if (op === "choose") {
        check(env.toasts.includes("方向已确认，收藏列表没读到，没有改「我的」"), `${op}: 提示收藏没改`, JSON.stringify(env.toasts));
      } else {
        check(env.toasts.includes("没读到已收藏的方向，这次没有改"), `${op}: 提示这次没有改`, JSON.stringify(env.toasts));
        check(JSON.stringify(env.trail()) === trailBefore && env.calls.trailPuts.length === tp0,
          `${op}: trail.dirs 没被旧快照覆盖`, JSON.stringify(env.trail()));
      }
    }
  }
  {
    console.log("=== R03 读成功：用这次读回的列表构造 ===");
    const env = prefsEnv(["A"]);
    await env.saveTrail({ code: "", dirs: ["A"], sel: ["A"] });
    await env.loadPrefs();
    env.store.prefs.directions = ["A", "B"];
    await env.addMyDirection("C");
    check(JSON.stringify(env.store.prefs.directions) === JSON.stringify(["A", "B", "C"]), "addC 后远端 A+B+C", JSON.stringify(env.store.prefs.directions));
  }
}

async function boot() {
  console.log("=== R03 boot 恢复方向 ===");
  const bootSrc = extract(appSrc, "boot");
  const start = bootSrc.indexOf("needConsent = !me.consent_ok;");
  const end = bootSrc.indexOf("await ensurePortrait();", start);
  if (start < 0 || end < 0) throw new Error("找不到 boot 里的 prefs 段");
  const block = bootSrc.slice(start + "needConsent = !me.consent_ok;".length, end);
  const S = { myDirs: undefined, uid: "u1", token: "t" };
  const kits = [];
  const api = async (m, url) => {
    if (m === "GET" && url === "/api/me/prefs") return { kit_id: "llm-eval", directions: ["A", "B"] };
    return {};
  };
  const ls = memStorage();
  const loadPrefs = new Function("S", "api", "useKit", extract(appSrc, "loadPrefs") + "; return loadPrefs;")(
    S, api, (k) => { kits.push(k); S.kitId = k; });
  const run = new Function("S", "api", "loadPrefs", "localStorage",
    "return (async () => { let needConsent = false; " + block + " })();");
  await run(S, api, loadPrefs, ls);
  check(Array.isArray(S.myDirs) && JSON.stringify(S.myDirs) === JSON.stringify(["A", "B"]), "boot 后 S.myDirs 恢复", JSON.stringify(S.myDirs));
  check(S.kitId === "llm-eval", "kit_id 照常恢复", String(S.kitId));
}

/* ---------- R4-03/04/05/09 ---------- */
function formTa(value) {
  const inserted = [];
  return { value, inserted, insertAdjacentElement: (_, el) => { inserted.push(el); return el; } };
}

async function rev() {
  console.log("=== R4-03 GET 快照先于 ACK、重开后 ===");
  {
    const srv = draftServer();
    await srv.api("PUT", "/api/tasks/t1/draft", { text: "server old", base_rev: 0 });
    const c = draftClient(srv);
    await c.hydrateDraft("t1", formTa(""));
    const p = hold(); srv.putHold = p.p;
    c.writeDraft("t1", "just typed and saved"); await tick();
    const g = hold(); srv.getHold = g.p;
    const ta = formTa("just typed and saved");
    const h = c.hydrateDraft("t1", ta); await tick();
    srv.getHold = null;
    p.open(); await tick(); await tick();
    g.open(); await h; await tick();
    const m = c.readDraftMeta("t1");
    check(ta.value === "just typed and saved" && c.readDraft("t1") === "just typed and saved", "旧 GET 快照不回退文本", ta.value + "/" + c.readDraft("t1"));
    check(m.base_rev === 2 && m.dirty === false && m.edit_seq === 1, "base_rev 保持 2、dirty=false", JSON.stringify(m));
  }
  console.log("=== R4-03 旧 ACK 不降低较新 GET 版本 ===");
  {
    const srv = draftServer();
    await srv.api("PUT", "/api/tasks/t1/draft", { text: "server old", base_rev: 0 });
    const c = draftClient(srv);
    await c.hydrateDraft("t1", formTa(""));
    const inner = srv.api;
    const ack = hold();
    srv.api = async (m, u, b) => { const r = await inner(m, u, b); if (m === "PUT") await ack.p; return r; };
    c.writeDraft("t1", "same"); await tick();
    await inner("PUT", "/api/tasks/t1/draft", { text: "same", base_rev: 2 });
    srv.api = inner;
    const ta = formTa("same");
    const h = c.hydrateDraft("t1", ta); await tick(); await tick();
    const m1 = c.readDraftMeta("t1");
    check(m1.base_rev === 3 && m1.dirty === false, "GET 确认同文本 rev3", JSON.stringify(m1));
    ack.open(); await h; await tick();
    const m = c.readDraftMeta("t1");
    check(m.base_rev === 3 && m.dirty === false && m.edit_seq === 1 && c.readDraft("t1") === "same", "旧 ACK rev2 不把 base_rev 降到 2", JSON.stringify(m));
  }
}

async function legacy() {
  console.log("=== R4-04 旧格式清空失败 ===");
  const srv = draftServer();
  await srv.api("PUT", "/api/tasks/t1/draft", { text: "server old", base_rev: 0 });
  const ls = memStorage();
  ls.setItem("rg_draft_meta_u1_t1", JSON.stringify({ updated_at: "2030-01-01T00:00:00Z", dirty: true }));
  const c = draftClient(srv, ls);
  const m0 = c.readDraftMeta("t1");
  check(c.readDraft("t1") === "" && m0.base_rev === 0 && m0.edit_seq === 0 && m0.dirty === true && m0.conflict === true, "迁移为空稿 dirty+conflict", JSON.stringify(m0));
  const ta = formTa("");
  const n0 = srv.puts.length;
  await c.hydrateDraft("t1", ta);
  check(ta.value === "" && c.readDraft("t1") === "" && srv.puts.length === n0, "旧稿不重新出现、不静默上传", ta.value);
  const m1 = c.readDraftMeta("t1");
  check(m1.dirty === true && m1.conflict === true, "dirty/conflict 保持 true", JSON.stringify(m1));
  check(ta.inserted.length === 1 && ta.inserted[0].text === "用本地稿覆盖云端", "显示「用本地稿覆盖云端」");
  if (ta.inserted[0] && ta.inserted[0].onclick) await ta.inserted[0].onclick();
  const last = srv.puts[srv.puts.length - 1];
  check(last && last.text === "" && last.base_rev === 1 && srv.text === "" && srv.rev === 2, "GET 最新 rev 后 CAS 清空", JSON.stringify(last));
  check(c.readDraftMeta("t1").dirty === false, "成功后 dirty=false");
}

async function reopen() {
  console.log("=== R4-05 重开冲突任务 ===");
  {
    const srv = draftServer();
    const a = draftClient(srv), b = draftClient(srv);
    await a.hydrateDraft("t1", formTa(""));
    const ta1 = formTa("");
    await b.hydrateDraft("t1", ta1);
    await a.writeDraft("t1", "别处");
    await b.writeDraft("t1", "本地");
    check(ta1.inserted.length === 1, "首次冲突在旧表单显示按钮");
    const ta2 = formTa("本地");
    await b.hydrateDraft("t1", ta2);
    check(ta1.inserted[0] && ta1.inserted[0].removed === true, "旧按钮引用被移除");
    check(ta2.inserted.length === 1 && ta2.inserted[0].text === "用本地稿覆盖云端", "当前表单有且只有一个按钮", String(ta2.inserted.length));
    if (ta2.inserted[0] && ta2.inserted[0].onclick) await ta2.inserted[0].onclick();
    const m = b.readDraftMeta("t1");
    check(srv.text === "本地" && m.dirty === false && m.conflict === false && ta2.inserted[0] && ta2.inserted[0].removed === true, "点新按钮 GET→CAS 成功并移除", JSON.stringify(m));
  }
  console.log("=== R4-05 在途 409 只挂当前表单 ===");
  {
    const srv = draftServer();
    const a = draftClient(srv), b = draftClient(srv);
    await a.hydrateDraft("t1", formTa(""));
    const ta1 = formTa("");
    await b.hydrateDraft("t1", ta1);
    await a.writeDraft("t1", "别处");
    const p = hold(); srv.putHold = p.p;
    b.writeDraft("t1", "本地"); await tick();
    const ta2 = formTa("本地");
    const h = b.hydrateDraft("t1", ta2); await tick();
    p.open(); await h; await tick();
    check(ta1.inserted.length === 0 && ta2.inserted.length === 1, "迟到 409 只挂当前表单", ta1.inserted.length + "/" + ta2.inserted.length);
  }
}

async function retry() {
  console.log("=== R4-09 非 409 失败提示 ===");
  const srv = draftServer();
  const c = draftClient(srv);
  await c.hydrateDraft("t1", formTa(""));
  srv.putFail = true;
  await c.writeDraft("t1", "unsaved");
  check(c.toasts.includes("草稿未同步，联网后重试"), "显示「草稿未同步，联网后重试」", JSON.stringify(c.toasts));
  check(c.readDraft("t1") === "unsaved" && c.readDraftMeta("t1").dirty === true, "本地稿与 dirty 保留");
  srv.putFail = false;
  await c.writeDraft("t1");
  check(srv.text === "unsaved" && c.readDraftMeta("t1").dirty === false, "恢复后重试保存成功", srv.text);
}

(async () => {
  const which = process.argv[2] || "all";
  if (which === "drafts" || which === "all") await drafts();
  if (which === "prefs" || which === "all") await prefs();
  if (which === "boot" || which === "all") await boot();
  if (which === "rev" || which === "all") await rev();
  if (which === "legacy" || which === "all") await legacy();
  if (which === "reopen" || which === "all") await reopen();
  if (which === "retry" || which === "all") await retry();
  console.log("\n" + (fails ? `失败 ${fails} 项` : "全部通过"));
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
