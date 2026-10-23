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
    if (url.startsWith("/api/projects/") && m === "GET") return JSON.parse(JSON.stringify(store.trail || {}));
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
    "selectedDirs", "portraitDirsAdd", "readTrailForWrite", "addMyDirection", "removeMyDirection", "chooseDirection"];
  const src = names.map((n) => extract(appSrc, n)).join("\n");
  const f = new Function("S", "api", "toast", "paintChrome", "useKit", "window", "RG_DIR", "FIELD_TREES",
    "localStorage", "setView", "dirTitle",
    dirCtxSrc() + "\n" + src + "; return {loadPrefs, trail, saveTrail, addMyDirection, removeMyDirection, chooseDirection};");
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
  const start = bootSrc.indexOf("if (!me) return;");
  const end = bootSrc.indexOf("if (ctx.live()) await Promise.race([ensurePortrait(ctx), deadline]);", start);
  if (start < 0 || end < 0) throw new Error("找不到 boot 里的 prefs 段");
  const block = bootSrc.slice(start + "if (!me) return;".length, end);
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
  if (which === "r4-dirs" || which === "all") await r4Dirs();
  if (which === "r4-boot" || which === "all") await r4Boot();
  if (which === "r4-retry" || which === "all") await r4Retry();
  if (which === "direction-context" || which === "all") await directionCtx();
  if (which === "direction-context-errors" || which === "all") await directionCtxErrors();
  if (which === "direction-context-save" || which === "all") await directionCtxSave();
  if (which === "direction-context-ui" || which === "all") await directionCtxUi();
  if (which === "login-reset-hint" || which === "all") await loginResetHint();
  if (which === "api-timeout" || which === "all") await apiTimeout();
  if (which === "session-confirm-only-401" || which === "all") await l06SessionConfirm();
  if (which === "boot-health-timeout" || which === "all") await l06BootHealthTimeout();
  if (which === "after-enter-timeout" || which === "all") await l06AfterEnterTimeout();
  if (which === "recovery-notice" || which === "all") await l06RecoveryNotice();
  if (which === "api-body-timeout" || which === "all") await r9ApiBodyTimeout();
  if (which === "boot-profile-deadline" || which === "all") await r9BootProfileDeadline();
  if (which === "recovery-notice-event" || which === "all") await r9RecoveryNoticeEvent();
  console.log("\n" + (fails ? `失败 ${fails} 项` : "全部通过"));
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });

/* ===================== R4-01 / R4-02 / R4-10（第四轮，独立区块） =====================
   只用函数声明：上面的调度 IIFE 同步调用到这里时，顶层 const 还在 TDZ。 */
function r4LoadDir() {
  const w = {};
  new Function("window", fs.readFileSync(path.join(root, "web", "js", "directions.js"), "utf8"))(w);
  return w.RG_DIR;
}

function r4Pick(names) {
  return names.map((n) => { try { return extract(appSrc, n); } catch (_) { return ""; } }).join("\n");
}

function r4Env(o) {
  const RG_DIR = r4LoadDir();
  const window = { RG_DIR };
  const store = { prefs: { directions: o.prefs.slice() }, trail: o.remote === undefined ? null : JSON.parse(JSON.stringify(o.remote)) };
  const net = { trailGetFail: !!o.trailGetFail, prefsGetFail: !!o.prefsGetFail };
  const calls = { choose: [], trailPuts: [], prefsPuts: [], trailGets: 0 };
  const S = { myDirs: undefined, uid: "u1", token: "t", portraitId: o.portraitId === undefined ? "p1" : o.portraitId, view: "today" };
  const api = async (m, url, body) => {
    if (url === "/api/me/prefs" && m === "GET") {
      if (net.prefsGetFail) { const e = new Error("服务器出错"); e.status = 500; throw e; }
      return JSON.parse(JSON.stringify(store.prefs));
    }
    if (url === "/api/me/prefs" && m === "PUT") {
      calls.prefsPuts.push(body);
      if (body.directions) store.prefs.directions = body.directions.slice();
      return JSON.parse(JSON.stringify(store.prefs));
    }
    if (url === "/api/projects/p1/trail" && m === "GET") {
      calls.trailGets++;
      if (net.trailGetFail) { const e = new Error("服务器出错"); e.status = 500; throw e; }
      return JSON.parse(JSON.stringify(store.trail || {}));
    }
    if (url === "/api/projects/p1/trail" && m === "PUT") { calls.trailPuts.push(body.data); store.trail = body.data; return {}; }
    if (url === "/api/directions/choose") { calls.choose.push(body); return {}; }
    if (url === "/api/portraits?uid=u1") return { portraits: [{ id: "p1", active: true, name: "", label: "画像一", dirs: [], sel: [] }] };
    throw new Error("未预期的请求 " + m + " " + url);
  };
  const toasts = [];
  const toast = (t) => toasts.push(t);
  const ls = memStorage();
  if (o.local !== undefined) ls.setItem("rg_trail_u1_p1", JSON.stringify(o.local));
  ls.setItem("rg_trail_owner_u1", "p1");
  return { RG_DIR, window, store, net, calls, S, api, toasts, toast, ls };
}

function r4Writes(env) { return env.calls.choose.length + env.calls.trailPuts.length + env.calls.prefsPuts.length; }

async function r4Dirs() {
  console.log("=== R4-01 方向写入以新读的 trail 为准 ===");
  const remote = {
    code: "", dir: "", done: [], tasks: { t1: { note: "keep" } },
    byDir: { "110.67": { done: [], tasks: { k: 1 } } },
    dirs: ["190.20", "110.67"], sel: ["110.67"],
  };
  const local = { code: "", dir: "", done: [], tasks: {}, byDir: {}, dirs: ["190.20"], sel: ["190.20"] };
  const mk = (extra) => {
    const env = r4Env(Object.assign({ prefs: ["190.20"], remote, local }, extra));
    const names = ["loadPrefs", "ensureMyDirs", "putPrefs", "trailStorageKey", "trail", "saveTrail",
      "selectedDirs", "portraitDirsAdd", "readTrailForWrite", "addMyDirection", "removeMyDirection", "chooseDirection"];
    const f = new Function("S", "api", "toast", "paintChrome", "useKit", "window", "RG_DIR", "FIELD_TREES",
      "localStorage", "setView", "dirTitle",
      dirCtxSrc() + "\n" + r4Pick(names) + "; return {trail, addMyDirection, removeMyDirection, chooseDirection};");
    return Object.assign(env, f(env.S, env.api, env.toast, () => {}, () => {}, env.window, env.RG_DIR, {}, env.ls, () => {}, (x) => x));
  };
  const J = JSON.stringify;
  const run = (env, op) => (op === "add" ? env.addMyDirection("520.20")
    : op === "choose" ? env.chooseDirection("520.20") : env.removeMyDirection("190.20"));
  for (const op of ["add", "choose", "remove"]) {
    // trail 200 + prefs 200
    {
      const env = mk({});
      await run(env, op);
      const last = env.calls.trailPuts[env.calls.trailPuts.length - 1] || {};
      const wantDirs = op === "remove" ? ["110.67"] : ["190.20", "110.67", "520.20"];
      const wantSel = op === "remove" ? ["110.67"] : ["110.67", "520.20"];
      check(env.calls.trailGets >= 1, `${op} trail200: 写前读了 trail`, String(env.calls.trailGets));
      check(env.calls.trailPuts.length === 1, `${op} trail200: 恰 1 次 trail PUT`, String(env.calls.trailPuts.length));
      check(J(last.dirs) === J(wantDirs) && J(last.sel) === J(wantSel), `${op} trail200: dirs/sel 按新读值构造`, J({ dirs: last.dirs, sel: last.sel }));
      check(last.byDir && J(last.byDir["110.67"]) === J(remote.byDir["110.67"]), `${op} trail200: byDir 保留`, J(last.byDir));
      if (op !== "choose") check(J(last.tasks) === J(remote.tasks), `${op} trail200: tasks 保留`, J(last.tasks));
      check(env.calls.choose.length === (op === "choose" ? 1 : 0), `${op} trail200: choose POST 次数`, String(env.calls.choose.length));
      const wantPrefs = op === "remove" ? 1 : 1;
      check(env.calls.prefsPuts.length === wantPrefs, `${op} trail200: prefs PUT 次数`, J(env.calls.prefsPuts));
    }
    // trail 500：零写入、精确提示
    {
      const env = mk({ trailGetFail: true });
      const before = J(env.trail());
      await run(env, op);
      check(r4Writes(env) === 0, `${op} trail500: 零写入`, J(env.calls));
      check(env.toasts.length === 1 && env.toasts[0] === "没读到已收藏的方向，这次没有改", `${op} trail500: 精确提示`, J(env.toasts));
      check(J(env.trail()) === before, `${op} trail500: 本地 trail 不变`, J(env.trail()));
    }
  }
  // choose + prefs 500：trail 已按新值写入，保留原提示，不 PUT prefs
  {
    const env = mk({ prefsGetFail: true });
    await env.chooseDirection("520.20");
    const last = env.calls.trailPuts[env.calls.trailPuts.length - 1] || {};
    check(J(last.dirs) === J(["190.20", "110.67", "520.20"]) && J(last.sel) === J(["110.67", "520.20"]), "choose prefs500: trail 按新读值写", J(last));
    check(env.calls.prefsPuts.length === 0, "choose prefs500: 0 次 prefs PUT", J(env.calls.prefsPuts));
    check(env.toasts.includes("方向已确认，收藏列表没读到，没有改「我的」"), "choose prefs500: 原提示", J(env.toasts));
  }
  // add 已在新读的 trail 里：按新值判重，零写入
  {
    const env = mk({});
    await env.addMyDirection("110.67");
    check(r4Writes(env) === 0 && env.toasts.includes("已经在我的方向里"), "add 已在远端 trail：按新值判重", J(env.toasts));
  }
}

async function r4Boot() {
  console.log("=== R4-02 boot 不覆盖明确的 trail 状态 ===");
  const bootSrc = extract(appSrc, "boot");
  const start = bootSrc.indexOf("if (!me) return;");
  const endMark = "if (ctx.live()) await Promise.race([ensurePortrait(ctx), deadline]);";
  const end = bootSrc.indexOf(endMark, start);
  if (start < 0 || end < 0) throw new Error("找不到 boot 里的 prefs/画像段");
  const block = bootSrc.slice(start + "if (!me) return;".length, end + endMark.length);
  const J = JSON.stringify;
  const cases = [
    { name: "远端 dirs 有、sel 显式空", remote: { code: "", dirs: ["190.20", "110.67"], sel: [] }, puts: 0, dirs: ["190.20", "110.67"], sel: [] },
    { name: "远端 dirs/sel 都显式空", remote: { code: "", dirs: [], sel: [] }, puts: 0, dirs: [], sel: [] },
    { name: "trail GET 500（本地空）", fail: true, puts: 0, dirs: [], sel: [] },
    { name: "trail GET 500（本地缺 sel）", fail: true, local: { code: "", dirs: ["190.20"] }, puts: 0, dirs: ["190.20"], sel: [] },
    { name: "旧 schema 成功读到：从 prefs 迁移一次", remote: { code: "" }, puts: 1, dirs: ["190.20", "110.67"], sel: ["190.20", "110.67"] },
    { name: "远端仅缺 sel：保留补全", remote: { code: "", dirs: ["190.20"] }, puts: 1, dirs: ["190.20"], sel: ["190.20"] },
  ];
  for (const c of cases) {
    const env = r4Env({ prefs: ["190.20", "110.67"], remote: c.remote, trailGetFail: c.fail, local: c.local, portraitId: "" });
    const names = ["loadPrefs", "trailStorageKey", "trail", "saveTrail", "pullTrail", "ensureSelKey", "adoptLegacyTrail", "ensurePortrait"];
    const fns = new Function("S", "api", "useKit", "window", "RG_DIR", "FIELD_TREES", "localStorage",
      dirCtxSrc() + "\n" + r4Pick(names) + "; return {loadPrefs, ensurePortrait, trail};")(
      env.S, env.api, () => {}, env.window, env.RG_DIR, {}, env.ls);
    const run = new Function("S", "api", "loadPrefs", "ensurePortrait", "localStorage",
      "return (async () => { let needConsent = false; " + block + " })();");
    await run(env.S, env.api, fns.loadPrefs, fns.ensurePortrait, env.ls);
    const t = fns.trail();
    check(J(env.S.myDirs) === J(["190.20", "110.67"]), `${c.name}: S.myDirs 恢复`, J(env.S.myDirs));
    check(env.calls.trailPuts.length === c.puts, `${c.name}: trail PUT ${c.puts} 次`, J(env.calls.trailPuts));
    check(J(t.dirs) === J(c.dirs) && J(t.sel) === J(c.sel), `${c.name}: 最终 dirs/sel`, J({ dirs: t.dirs, sel: t.sel }));
  }
}

function r4Dom() {
  const mk = (tag) => {
    const n = {
      tag, className: "", innerHTML: "", children: [], hidden: false, onclick: null, onsubmit: null,
      attrs: {}, value: "", type: "", dataset: {},
      appendChild(c) { n.children.push(c); return c; },
      append(...cs) { cs.forEach((c) => n.children.push(c)); },
      setAttribute(k, v) { n.attrs[k] = v; },
      focus() {},
    };
    return n;
  };
  return { createElement: mk };
}

function r4Walk(n, out = []) { out.push(n); (n.children || []).forEach((c) => r4Walk(c, out)); return out; }

async function r4Retry() {
  console.log("=== R4-10 prefs 失败有可见重试 ===");
  const J = JSON.stringify;
  const mk = (o) => {
    const env = r4Env(Object.assign({ prefs: ["110.67"], prefsGetFail: true }, o));
    env.views = [];
    const document = r4Dom();
    const names = ["el", "esc", "loadPrefs", "trailStorageKey", "trail", "saveTrail", "selectedDirs",
      "ensureSelKey", "pullTrail", "portraitSection"];
    const fns = new Function("S", "api", "toast", "useKit", "window", "RG_DIR", "FIELD_TREES", "localStorage",
      "document", "setView", "dirTitle", "paintChrome", "portraitOp", "removeMyDirection", "ensurePortrait", "enterApp",
      dirCtxSrc() + "\n" + r4Pick(names) + "; return {loadPrefs, portraitSection, trail};")(
      env.S, env.api, env.toast, () => {}, env.window, env.RG_DIR, {}, env.ls,
      document, (v) => env.views.push(v), (x) => "T" + x, () => {}, () => {}, () => {}, () => {}, () => {});
    return Object.assign(env, fns);
  };
  const texts = (box) => r4Walk(box).map((n) => n.innerHTML);
  // 1. prefs500 + 本地无 trail：显示提示与同名按钮，不显示假空态
  {
    const env = mk({ local: { code: "", dirs: [], sel: [] }, remote: { code: "", dirs: ["110.67"], sel: ["110.67"] } });
    const ok = await env.loadPrefs();
    check(ok === false && env.S.myDirs === undefined, "loadPrefs500 → S.myDirs undefined");
    const box = await env.portraitSection();
    const nodes = r4Walk(box);
    const tip = nodes.filter((n) => n.tag === "p" && n.innerHTML === "收藏未加载，请重试");
    const btn = nodes.filter((n) => n.tag === "button" && n.innerHTML === "收藏未加载，请重试");
    check(tip.length === 1 && btn.length === 1, "显示「收藏未加载，请重试」文字与同名按钮", J(texts(box)));
    check(!texts(box).some((x) => String(x).includes("这份画像还没有加方向")), "不显示「这份画像还没有加方向」");
    // 仍失败：点击后不重绘、状态保留
    if (btn[0] && btn[0].onclick) await btn[0].onclick();
    check(env.views.length === 0 && env.S.myDirs === undefined, "重试仍失败：不重绘、保留未加载状态", J(env.views));
    // 恢复 200：点击 → loadPrefs → pullTrail → 重绘
    env.net.prefsGetFail = false;
    const g0 = env.calls.trailGets;
    if (btn[0] && btn[0].onclick) await btn[0].onclick();
    check(J(env.S.myDirs) === J(["110.67"]), "重试成功：S.myDirs 恢复", J(env.S.myDirs));
    check(env.calls.trailGets === g0 + 1 && J(env.trail().dirs) === J(["110.67"]), "重试成功后拉了 trail", J(env.trail()));
    check(env.views.length === 1 && env.views[0] === "me", "重试成功后重绘「我的」", J(env.views));
    const box2 = await env.portraitSection();
    check(!texts(box2).some((x) => String(x).includes("收藏未加载")) && texts(box2).some((x) => String(x).includes("T110.67")),
      "重绘后显示收藏、不再提示未加载", J(texts(box2)));
  }
  // 2. prefs500 但已确认的 trail 非空：照常展示
  {
    const env = mk({ local: { code: "", dirs: ["110.67"], sel: ["110.67"] } });
    await env.loadPrefs();
    const box = await env.portraitSection();
    check(texts(box).some((x) => String(x).includes("T110.67")), "未加载时已确认的 trail 仍展示", J(texts(box)));
    check(!texts(box).some((x) => String(x).includes("这份画像还没有加方向")), "未加载时不显示空收藏结论");
  }
}

/* ===================== R5-01 方向操作绑定会话/画像纪元（第五轮，独立区块） ===================== */
function dirCtxSrc() {
  const decl = [/^const STALE_DIR\b.*$/m, /^let directionEpoch\b.*$/m, /^let portraitBusy\b.*$/m]
    .map((re) => (re.exec(appSrc) || [""])[0]).join("\n");
  return decl + "\n" + r4Pick(["directionContext", "dirAwait"]);
}

function r5Env(holdOn) {
  const RG_DIR = r4LoadDir();
  const window = { RG_DIR, confirm: () => true };
  const A = { code: "psy", dir: "190.20", dirs: ["190.20"], sel: ["190.20"], tasks: { a: { note: "A keep" } }, done: [], byDir: {} };
  const B = { code: "stat", dir: "110.67", dirs: ["190.20", "110.67"], sel: ["110.67"], tasks: { b: { note: "B keep" } }, done: [], byDir: {} };
  const C = (x) => JSON.parse(JSON.stringify(x));
  const store = { active: "p1", trails: { p1: C(A), p2: C(B) }, prefs: { directions: ["190.20"] } };
  const writes = [];
  const gate = hold();
  const st = { held: false };
  const api = async (m, url, body) => {
    if (m !== "GET") writes.push({ m, url, body: C(body || {}) });
    const match = holdOn && !st.held && holdOn(m, url);
    if (match) { st.held = true; await gate.p; }
    if (url === "/api/me/prefs" && m === "GET") return C(store.prefs);
    if (url === "/api/me/prefs" && m === "PUT") { if (body.directions) store.prefs.directions = body.directions.slice(); return C(store.prefs); }
    const tm = /^\/api\/projects\/(p1|p2)\/trail$/.exec(url);
    if (tm && m === "GET") return C(store.trails[tm[1]]);
    if (tm && m === "PUT") { store.trails[tm[1]] = C(body.data); return {}; }
    if (url === "/api/directions/choose") return {};
    if (url === "/api/portraits/activate") { store.active = body.id; return list(); }
    if (url === "/api/portraits?uid=u1") return list();
    throw new Error("未预期的请求 " + m + " " + url);
  };
  const list = () => ({ portraits: ["p1", "p2"].map((id, i) => ({
    id, active: store.active === id, name: "", label: i ? "画像二" : "画像一", default_label: "", custom: false, dirs: [], sel: [] })) });
  const toasts = [];
  const toast = (t) => toasts.push(t);
  const ls = memStorage();
  ls.setItem("rg_trail_u1_p1", JSON.stringify(A));
  ls.setItem("rg_trail_owner_u1", "p1");
  const S = { myDirs: ["190.20"], uid: "u1", token: "t", portraitId: "p1", portraitName: "", mePortrait: "p2", view: "me" };
  const names = ["el", "esc", "loadPrefs", "putPrefs", "trailStorageKey", "trail", "saveTrail", "selectedDirs",
    "portraitDirsAdd", "readTrailForWrite", "addMyDirection", "removeMyDirection", "chooseDirection",
    "ensureSelKey", "pullTrail", "portraitOp", "portraitSection", "clearLocalDrafts", "clearSession"];
  const fns = new Function("S", "api", "toast", "useKit", "window", "RG_DIR", "FIELD_TREES", "localStorage",
    "document", "setView", "dirTitle", "paintChrome", "ensurePortrait", "enterApp",
    dirCtxSrc() + "\n" + r4Pick(names) +
    "; return {addMyDirection, removeMyDirection, chooseDirection, portraitSection, clearSession};")(
    S, api, toast, () => {}, window, RG_DIR, {}, ls, r4Dom(), () => {}, (x) => x, () => {}, async () => {}, () => {});
  return Object.assign({ A, B, store, writes, gate, st, toasts, ls, S }, fns);
}

async function r5Swap(env) {
  const box = await env.portraitSection();
  const btn = r4Walk(box).find((n) => n.tag === "button" && n.innerHTML === "更换到该画像");
  if (!btn || !btn.onclick) return false;
  await btn.onclick();
  return true;
}

async function directionCtx() {
  console.log("=== R5-01 切画像后旧方向操作静默作废 ===");
  const J = JSON.stringify;
  const holds = {
    trail: (m, url) => m === "GET" && url === "/api/projects/p1/trail",
    prefs: (m, url) => m === "GET" && url === "/api/me/prefs",
    choose: (m, url) => m === "POST" && url === "/api/directions/choose",
  };
  const run = (env, op) => (op === "add" ? env.addMyDirection("520.20")
    : op === "choose" ? env.chooseDirection("520.20") : env.removeMyDirection("190.20"));
  const waitHeld = async (env) => { for (let i = 0; i < 100 && !env.st.held; i++) await tick(); return env.st.held; };
  const cases = [["add", "trail"], ["remove", "trail"], ["choose", "trail"], ["add", "prefs"], ["remove", "prefs"], ["choose", "choose"]];
  for (const [op, h] of cases) {
    const name = `${op} 挂起 ${h}`;
    const env = r5Env(holds[h]);
    const p = run(env, op);
    check(await waitHeld(env), `${name}: 请求已挂起`);
    check(await r5Swap(env), `${name}: 点了真实「更换到该画像」`);
    const cache2 = () => JSON.parse(env.ls.getItem("rg_trail_u1_p2") || "null");
    check(env.S.portraitId === "p2" && env.store.active === "p2" && J(cache2()) === J(env.B) && J(env.store.trails.p2) === J(env.B),
      `${name}: p2 已正确加载`, J({ pid: env.S.portraitId, cache: cache2() }));
    const w0 = env.writes.length, t0 = env.toasts.length;
    env.gate.open();
    await p.catch((e) => { check(false, `${name}: 旧操作不抛错`, String(e)); });
    await tick();
    check(J(env.store.trails.p2) === J(env.B), `${name}: p2 远端每个字段不变`, J(env.store.trails.p2));
    check(J(cache2()) === J(env.B), `${name}: p2 缓存每个字段不变`, J(cache2()));
    check(env.writes.length === w0, `${name}: 放行后零 POST/PUT`, J(env.writes.slice(w0)));
    check(env.toasts.length === t0, `${name}: 无旧操作提示`, J(env.toasts.slice(t0)));
  }
  // 纪元对照一：切走再切回 p1，旧操作仍作废
  {
    const env = r5Env(holds.trail);
    const p = env.addMyDirection("520.20");
    check(await waitHeld(env), "切回 p1: 请求已挂起");
    check(await r5Swap(env), "切回 p1: 先换到 p2");
    env.S.mePortrait = "p1";
    check(await r5Swap(env), "切回 p1: 再换回 p1");
    const cache1 = () => JSON.parse(env.ls.getItem("rg_trail_u1_p1") || "null");
    check(env.S.portraitId === "p1" && J(cache1()) === J(env.A), "切回 p1: p1 已加载", J(cache1()));
    const w0 = env.writes.length, t0 = env.toasts.length;
    env.gate.open();
    await p.catch((e) => { check(false, "切回 p1: 旧操作不抛错", String(e)); });
    await tick();
    check(J(env.store.trails.p1) === J(env.A) && J(cache1()) === J(env.A), "切回 p1: p1 远端/缓存不变", J(env.store.trails.p1));
    check(env.writes.length === w0, "切回 p1: 放行后零 POST/PUT", J(env.writes.slice(w0)));
    check(env.toasts.length === t0, "切回 p1: 无旧操作提示", J(env.toasts.slice(t0)));
  }
  // 纪元对照二：会话失效（clearSession）
  {
    const env = r5Env(holds.trail);
    const p = env.addMyDirection("520.20");
    check(await waitHeld(env), "clearSession: 请求已挂起");
    env.clearSession();
    const w0 = env.writes.length, t0 = env.toasts.length;
    env.gate.open();
    await p.catch((e) => { check(false, "clearSession: 旧操作不抛错", String(e)); });
    await tick();
    check(J(env.store.trails.p1) === J(env.A) && J(env.store.prefs.directions) === J(["190.20"]), "clearSession: 远端不变", J(env.store));
    check(env.writes.length === w0, "clearSession: 放行后零 POST/PUT", J(env.writes.slice(w0)));
    check(env.toasts.length === t0, "clearSession: 无旧操作提示", J(env.toasts.slice(t0)));
  }
}

/* ===================== R6-01 / R6-02 拒绝响应、保存完成与 UI 后续动作也校验纪元（第六轮，独立区块） ===================== */
function r6Storage() {
  const mem = {};
  return {
    mem,
    get length() { return Object.keys(mem).length; },
    key: (i) => Object.keys(mem)[i] ?? null,
    getItem: (k) => (k in mem ? mem[k] : null),
    setItem: (k, v) => { mem[k] = String(v); },
    removeItem: (k) => { delete mem[k]; },
  };
}

function r6Dom() {
  const parse = (sel) => {
    if (sel.includes("[")) return null;
    const m = /^([a-z0-9]*)((?:[.#][\w-]+)*)$/i.exec(sel);
    if (!m) return null;
    const parts = m[2].match(/[.#][\w-]+/g) || [];
    return { tag: m[1].toLowerCase(), cls: parts.filter((p) => p[0] === ".").map((p) => p.slice(1)),
      id: (parts.find((p) => p[0] === "#") || "").slice(1) };
  };
  const matches = (n, q) => q && (!q.tag || n.tag === q.tag) && (!q.id || n.id === q.id)
    && q.cls.every((c) => String(n.className).split(/\s+/).includes(c));
  const walk = (n, out = []) => { n.children.forEach((c) => { out.push(c); walk(c, out); }); return out; };
  const mk = (tag) => {
    let html = "";
    const n = {
      tag: String(tag).toLowerCase(), id: "", className: "", children: [], parent: null, hidden: false,
      onclick: null, onsubmit: null, attrs: {}, dataset: {}, value: "", type: "", textContent: "",
      get innerHTML() { return html; },
      set innerHTML(v) { html = String(v); n.children.forEach((c) => { c.parent = null; }); n.children = []; },
      appendChild(c) { if (c.parent) c.remove(); c.parent = n; n.children.push(c); return c; },
      append(...cs) { cs.forEach((c) => n.appendChild(c)); },
      remove() { if (n.parent) { n.parent.children = n.parent.children.filter((x) => x !== n); n.parent = null; } },
      setAttribute(k, v) { n.attrs[k] = v; },
      removeAttribute(k) { delete n.attrs[k]; },
      focus() {},
      querySelector(s) { const q = parse(s); return walk(n).find((x) => matches(x, q)) || null; },
      querySelectorAll(s) { const q = parse(s); return walk(n).filter((x) => matches(x, q)); },
      classList: {
        contains: (c) => String(n.className).split(/\s+/).includes(c),
        add: (c) => { if (!n.classList.contains(c)) n.className = (n.className + " " + c).trim(); },
        remove: (c) => { n.className = String(n.className).split(/\s+/).filter((x) => x && x !== c).join(" "); },
        toggle: (c, on) => { const v = on === undefined ? !n.classList.contains(c) : !!on; if (v) n.classList.add(c); else n.classList.remove(c); return v; },
      },
    };
    return n;
  };
  const body = mk("body");
  return {
    body,
    createElement: mk,
    getElementById: (id) => walk(body).find((x) => x.id === id) || null,
    querySelector: (s) => body.querySelector(s),
    querySelectorAll: (s) => body.querySelectorAll(s),
  };
}

function r6Walk(n, out = []) { out.push(n); (n.children || []).forEach((c) => r6Walk(c, out)); return out; }

function r6Env() {
  const RG_DIR = r4LoadDir();
  const window = { RG_DIR, confirm: () => true, scrollTo: () => {}, innerWidth: 1280 };
  const A = { code: "", dir: "190.20", dirs: ["190.20"], sel: ["190.20"], tasks: { a: { note: "A keep" } }, done: [], byDir: {} };
  const B = { code: "", dir: "110.67", dirs: ["110.67"], sel: ["110.67"], tasks: { b: { note: "B keep" } }, done: [], byDir: {} };
  const C = (x) => JSON.parse(JSON.stringify(x));
  const store = { active: "p1", trails: { p1: C(A), p2: C(B) }, prefs: { directions: ["190.20"] } };
  const calls = [];
  const env = { A, B, store, calls, toasts: [], draws: [], holdOn: null, gate: hold(), st: { held: false }, fail: [] };
  const list = () => ({ portraits: ["p1", "p2"].map((id, i) => ({
    id, active: store.active === id, name: "", label: i ? "画像二" : "画像一", default_label: "", custom: false, dirs: [], sel: [] })) });
  const err500 = () => { const e = new Error("服务器出错"); e.status = 500; return e; };
  const serve = (m, url, body) => {
    if (env.fail.some((f) => f(m, url))) throw err500();
    if (url === "/api/me/prefs" && m === "GET") return C(store.prefs);
    if (url === "/api/me/prefs" && m === "PUT") { if (body.directions) store.prefs.directions = body.directions.slice(); return C(store.prefs); }
    const tm = /^\/api\/projects\/(p1|p2)\/trail$/.exec(url);
    if (tm && m === "GET") return C(store.trails[tm[1]]);
    if (tm && m === "PUT") { store.trails[tm[1]] = C(body.data); return {}; }
    if (url === "/api/directions/choose" && m === "POST") return {};
    if (url === "/api/portraits/activate" && m === "POST") { store.active = body.id; return list(); }
    if (url === "/api/portraits?uid=u1" && m === "GET") return list();
    if (url === "/api/tasks?uid=u1" && m === "GET") return { tasks: [] };
    if (url === "/api/onboard/result?uid=u1" && m === "GET") return { facts: [] };
    if (url === "/api/directions/recommend?uid=u1" && m === "GET") return { cards: [] };
    if (url.startsWith("/api/explore/courses?") && m === "GET") return { ok: true, source: "catalog", items: [] };
    throw new Error("未预期的请求 " + m + " " + url);
  };
  const api = async (m, url, body) => {
    calls.push({ m, url, body: C(body || {}) });
    await null;
    if (env.holdOn && !env.st.held && env.holdOn(m, url)) {
      env.st.held = true;
      let res, err;
      try { res = serve(m, url, body); } catch (e) { err = e; }  // 写已落库，回包挂起
      const mode = await env.gate.p;
      if (mode === "fail") throw err500();
      if (err) throw err;
      return res;
    }
    return serve(m, url, body);
  };
  const ls = r6Storage();
  ls.setItem("rg_trail_u1_p1", JSON.stringify(A));
  ls.setItem("rg_trail_owner_u1", "p1");
  const ss = r6Storage();
  const document = r6Dom();
  const $app = document.createElement("main");
  document.body.appendChild($app);
  const S = { myDirs: ["190.20"], uid: "u1", token: "t", nickname: "N", portraitId: "p1", portraitName: "", mePortrait: "",
    view: "me", entered: true, dirPicked: null };
  const decl = [/^const PARK_KEY\b.*$/m].map((re) => (re.exec(appSrc) || [""])[0]).join("\n");
  const names = ["el", "esc", "dirTitle", "loadPrefs", "putPrefs", "trailStorageKey", "trail", "saveTrail", "selectedDirs",
    "portraitDirsAdd", "readTrailForWrite", "addMyDirection", "removeMyDirection", "chooseDirection",
    "ensureSelKey", "pullTrail", "ensurePortrait", "adoptLegacyTrail", "portraitOp", "portraitSection",
    "clearLocalDrafts", "clearSession", "persistSession", "setSession", "setView", "stale",
    "visibleTaxonomy", "cloneDirNode", "recDirIds", "openDirSheet", "renderCards", "loadCourses"];
  const fns = new Function("S", "api", "toast", "useKit", "window", "RG_DIR", "FIELD_TREES", "localStorage", "sessionStorage",
    "document", "$app", "$nav", "$header", "paintChrome", "render", "enterApp", "closeUserMenu", "applyLlmPill", "reloadInto",
    "drawFieldTree", "TUTORIALS", "WORKSPACE", "RESUMABLE", "stopField",
    dirCtxSrc() + "\n" + decl + "\n" + r4Pick(names) +
    "; return {addMyDirection, removeMyDirection, chooseDirection, loadPrefs, portraitSection, clearSession, setSession, setView, renderCards};")(
    S, api, (t) => env.toasts.push(t), () => {}, window, RG_DIR, {}, ls, ss,
    document, $app, { hidden: false }, { hidden: false },
    () => env.draws.push("paintChrome"), () => { S.renderSeq = (S.renderSeq || 0) + 1; env.draws.push("render:" + S.view); },
    () => { throw new Error("enterApp 不应被调用"); }, () => {}, () => {}, () => { throw new Error("reloadInto 不应被调用"); },
    (tax, picked, onPick) => { env.pick = onPick; return document.createElement("svg"); },
    { stub: {} }, {}, {}, null);
  return Object.assign(env, { RG_DIR, ls, S, document }, fns);
}

async function r6WaitHeld(env) { for (let i = 0; i < 100 && !env.st.held; i++) await tick(); return env.st.held; }

async function r6Swap(env, to) {
  env.S.mePortrait = to;
  const box = await env.portraitSection();
  const btn = r6Walk(box).find((n) => n.tag === "button" && n.innerHTML === "更换到该画像");
  if (!btn || !btn.onclick) return false;
  await btn.onclick();
  return true;
}

function r6Holds() {
  return {
    trailGet: (m, url) => m === "GET" && url === "/api/projects/p1/trail",
    prefsGet: (m, url) => m === "GET" && url === "/api/me/prefs",
    trailPut: (m, url) => m === "PUT" && url === "/api/projects/p1/trail",
    prefsPut: (m, url) => m === "PUT" && url === "/api/me/prefs",
    choose: (m, url) => m === "POST" && url === "/api/directions/choose",
  };
}

function r6Run(env, op) {
  return op === "add" ? env.addMyDirection("520.20") : op === "choose" ? env.chooseDirection("520.20") : env.removeMyDirection("190.20");
}

function r6Snap(env) {
  const J = (x) => JSON.stringify(x, (k, v) => (v === undefined ? "__undefined__" : v));
  return { S: J(env.S), ls: J(env.ls.mem), remote: J(env.store), calls: env.calls.length, toasts: env.toasts.length, draws: env.draws.length };
}

function r6Same(env, name, before) {
  const J = JSON.stringify;
  const now = r6Snap(env);
  check(now.S === before.S, `${name}: 当前 S 每个字段不变`, now.S === before.S ? "" : now.S);
  check(now.ls === before.ls, `${name}: localStorage 不变`, now.ls === before.ls ? "" : now.ls);
  check(now.remote === before.remote, `${name}: 远端不变`, now.remote === before.remote ? "" : now.remote);
  check(now.calls === before.calls, `${name}: 零后续请求`, J(env.calls.slice(before.calls)));
  check(now.toasts === before.toasts, `${name}: 零提示`, J(env.toasts.slice(before.toasts)));
  check(now.draws === before.draws, `${name}: 零绘制`, J(env.draws.slice(before.draws)));
}

function r6Lifecycles() {
  return {
    "切p2": async (env) => (await r6Swap(env, "p2")) && env.S.portraitId === "p2",
    "p1→p2→p1": async (env) => (await r6Swap(env, "p2")) && (await r6Swap(env, "p1")) && env.S.portraitId === "p1",
    "clearSession()": async (env) => { env.clearSession(); return env.S.uid === ""; },
    "clearSession(true)": async (env) => { env.clearSession(true); return env.S.uid === ""; },
    "setSession同人": async (env) => !env.setSession({ uid: "u1", token: "t", nickname: "N" }) && env.S.uid === "u1",
    "切换请求失败": async (env) => {
      env.fail.push((m, url) => url === "/api/portraits/activate");
      const ok = await r6Swap(env, "p2");
      return ok && env.S.portraitId === "p1";
    },
  };
}

async function directionCtxErrors() {
  console.log("=== R6-01 拒绝回包与成功回包在生命周期变化后同样作废 ===");
  const H = r6Holds();
  const plan = { add: ["trailGet", "prefsGet", "trailPut", "prefsPut"], remove: ["trailGet", "prefsGet", "trailPut", "prefsPut"],
    choose: ["trailGet", "choose", "trailPut", "prefsGet", "prefsPut"] };
  const lifes = r6Lifecycles();
  for (const op of Object.keys(plan)) {
    for (const h of plan[op]) {
      for (const life of Object.keys(lifes)) {
        for (const mode of ["ok", "fail"]) {
          const name = `${op} 挂起 ${h} → ${life} → 放行${mode === "ok" ? "成功" : "500"}`;
          const env = r6Env();
          env.holdOn = H[h];
          const p = r6Run(env, op);
          let thrown = null;
          p.catch((e) => { thrown = e; });
          if (!(await r6WaitHeld(env))) { check(false, `${name}: 请求已挂起`); continue; }
          check(await lifes[life](env), `${name}: 生命周期已变化`);
          const before = r6Snap(env);
          env.gate.open(mode);
          await p.catch(() => {});
          await tick();
          check(thrown === null, `${name}: 旧操作不抛错`, String(thrown));
          r6Same(env, name, before);
        }
      }
    }
  }
}

async function directionCtxSave() {
  console.log("=== R6-01 trail 保存完成后先校验纪元，再读当前收藏 ===");
  const H = r6Holds();
  for (const op of ["add", "remove", "choose"]) {
    for (const how of ["原「移出」读prefs500", "直接loadPrefs500"]) {
      for (const mode of ["ok", "fail"]) {
        const name = `${op} 挂起 trail PUT → 切p2 → ${how} → 放行${mode === "ok" ? "成功" : "500"}`;
        const env = r6Env();
        env.holdOn = H.trailPut;
        const p = r6Run(env, op);
        let thrown = null;
        p.catch((e) => { thrown = e; });
        if (!(await r6WaitHeld(env))) { check(false, `${name}: 请求已挂起`); continue; }
        check(await r6Swap(env, "p2") && env.S.portraitId === "p2", `${name}: 已切到 p2`);
        env.fail.push((m, url) => m === "GET" && url === "/api/me/prefs");
        if (how === "直接loadPrefs500") {
          check((await env.loadPrefs()) === false, `${name}: 当前 loadPrefs 失败`);
        } else {
          env.S.mePortrait = "";
          const box = await env.portraitSection();
          const drop = r6Walk(box).find((n) => n.tag === "button" && n.innerHTML === "移出");
          check(!!(drop && drop.onclick), `${name}: 找到 p2 原「移出」按钮`);
          if (drop && drop.onclick) await drop.onclick();
          check(env.toasts[env.toasts.length - 1] === "没读到已收藏的方向，这次没有改", `${name}: 当前移出如实提示`, JSON.stringify(env.toasts));
        }
        check(env.S.myDirs === undefined, `${name}: 当前 S.myDirs 合法变为未知`);
        const before = r6Snap(env);
        env.gate.open(mode);
        await p.catch(() => {});
        await tick();
        check(thrown === null, `${name}: 旧操作不抛错`, String(thrown));
        check(env.S.myDirs === undefined, `${name}: 当前 S.myDirs 仍是未知`);
        r6Same(env, name, before);
      }
    }
  }
  // 失败的 portraitOp：pid 不变、纪元已变，旧 prefs GET 回包不得清当前收藏
  for (const mode of ["ok", "fail"]) {
    const name = `add 挂起 prefs GET → 切换请求失败 → 放行${mode === "ok" ? "成功" : "500"}`;
    const env = r6Env();
    env.holdOn = H.prefsGet;
    const p = env.addMyDirection("520.20");
    let thrown = null;
    p.catch((e) => { thrown = e; });
    check(await r6WaitHeld(env), `${name}: 请求已挂起`);
    check(await r6Lifecycles()["切换请求失败"](env), `${name}: 切换失败，pid 仍 p1`);
    const before = r6Snap(env);
    env.gate.open(mode);
    await p.catch(() => {});
    await tick();
    check(thrown === null, `${name}: 旧操作不抛错`, String(thrown));
    check(Array.isArray(env.S.myDirs) && env.S.myDirs.length === 1 && env.S.myDirs[0] === "190.20", `${name}: S.myDirs 仍是 ['190.20']`, String(env.S.myDirs));
    check(!env.toasts.slice(before.toasts).includes("没读到已收藏的方向，这次没有改"), `${name}: 无「没读到」提示`);
    r6Same(env, name, before);
  }
}

async function directionCtxUi() {
  console.log("=== R6-02 原 UI 调用方在切画像后不再执行后续动作 ===");
  const J = JSON.stringify;
  const H = r6Holds();
  const courses = (env) => env.calls.filter((c) => c.m === "GET" && c.url.startsWith("/api/explore/courses?")).length;
  const caches = (env) => J([env.ls.getItem("rg_trail_u1_p1"), env.ls.getItem("rg_trail_u1_p2"), env.store.trails, env.store.prefs]);
  const cases = [["添加到我的方向", "520.20"], ["取消添加", "190.20"], ["确认这个方向", "520.20"]];
  for (const [label, id] of cases) {
    for (const mode of ["ok", "fail"]) {
      const name = `原弹层「${label}」挂起 trail GET → 切p2 → 放行${mode === "ok" ? "成功" : "500"}`;
      const env = r6Env();
      env.S.view = "cards";
      await env.renderCards();
      await tick();
      check(typeof env.pick === "function", `${name}: 原 renderCards 已画树`);
      env.pick({ id });
      const sheet = env.document.getElementById("nodeSheet");
      check(!!sheet && env.S.dirPicked === id, `${name}: 原方向弹层已打开`);
      const btn = sheet && r6Walk(sheet).find((n) => n.tag === "button" && n.innerHTML === label);
      check(!!(btn && btn.onclick), `${name}: 找到弹层「${label}」按钮`);
      if (!btn || !btn.onclick) continue;
      await tick();
      env.holdOn = H.trailGet;
      const p = btn.onclick();
      let thrown = null;
      Promise.resolve(p).catch((e) => { thrown = e; });
      check(await r6WaitHeld(env), `${name}: trail GET 已挂起`);
      check(await r6Swap(env, "p2"), `${name}: 点了真实「更换到该画像」`);
      check(env.S.portraitId === "p2" && env.S.view === "me" && !env.document.getElementById("nodeSheet") && env.S.dirPicked === null,
        `${name}: 已切 p2 且旧弹层被原 setView 删除`, J({ pid: env.S.portraitId, view: env.S.view, picked: env.S.dirPicked }));
      const c0 = courses(env), k0 = caches(env), t0 = env.toasts.length;
      env.gate.open(mode);
      await Promise.resolve(p).catch(() => {});
      await tick();
      check(thrown === null, `${name}: 旧操作不抛错`, String(thrown));
      check(!env.document.getElementById("nodeSheet"), `${name}: 旧弹层没有被重新插入`);
      check(env.S.dirPicked === null, `${name}: S.dirPicked 仍是 null`, String(env.S.dirPicked));
      check(courses(env) === c0, `${name}: 没有额外课程 GET`);
      check(caches(env) === k0, `${name}: 两画像缓存/远端不变`, caches(env));
      check(env.toasts.length === t0, `${name}: 无旧操作提示`, J(env.toasts.slice(t0)));
    }
  }
  for (const mode of ["ok", "fail"]) {
    const name = `原「移出」挂起 trail GET → 切p2 → 进今日 → 放行${mode === "ok" ? "成功" : "500"}`;
    const env = r6Env();
    env.S.view = "me";
    const box = await env.portraitSection();
    const drop = r6Walk(box).find((n) => n.tag === "button" && n.innerHTML === "移出");
    check(!!(drop && drop.onclick), `${name}: 找到 p1 原「移出」按钮`);
    if (!drop || !drop.onclick) continue;
    env.holdOn = H.trailGet;
    const p = drop.onclick();
    let thrown = null;
    Promise.resolve(p).catch((e) => { thrown = e; });
    check(await r6WaitHeld(env), `${name}: trail GET 已挂起`);
    check(await r6Swap(env, "p2") && env.S.portraitId === "p2", `${name}: 已切 p2`);
    env.setView("today");
    check(env.S.view === "today", `${name}: 已进今日页`);
    const k0 = caches(env), t0 = env.toasts.length;
    env.gate.open(mode);
    await Promise.resolve(p).catch(() => {});
    await tick();
    check(thrown === null, `${name}: 旧操作不抛错`, String(thrown));
    check(env.S.view === "today", `${name}: 最终仍停在今日页`, env.S.view);
    check(caches(env) === k0, `${name}: 两画像缓存/远端不变`, caches(env));
    check(env.toasts.length === t0, `${name}: 无旧操作提示`, J(env.toasts.slice(t0)));
  }
}

/* ===================== 25-L03 换库后登录页说清「账号需要重新注册」、登录类请求超时（独立区块） ===================== */
function l03Env(hint) {
  const document = r6Dom();
  const mk = document.createElement;
  document.createElement = (tag) => { const n = mk(tag); n.addEventListener = () => {}; return n; };
  document.createTextNode = (t) => { const n = mk("#text"); n.textContent = t; return n; };
  const localStorage = r6Storage();
  if (hint) { localStorage.setItem("rg_reset_hint", "1"); localStorage.setItem("rg_last_uid", "u-old"); }
  const env = { document, localStorage, toasts: [], reply: null, S: { uid: "", token: "", auth: { wechat_login: false } } };
  const noop = () => {};
  const src = ["el", "consentRow", "renderLogin"].map((n) => extract(appSrc, n)).join("\n");
  const f = new Function("S", "api", "localStorage", "document", "toast", "$nav", "$header", "$app",
    "stopWxPoll", "startWechat", "openPrivacy", "revokeToken", "setSession", "afterEnter", "openConnect", "setView",
    src + "; return { renderLogin };");
  const $app = document.createElement("main");
  document.body.appendChild($app);
  const api = async () => { const r = env.reply; if (r instanceof Error) throw r; return r; };
  Object.assign(env, { $app }, f(env.S, api, localStorage, document, (t) => env.toasts.push(t), {}, {}, $app,
    noop, noop, noop, noop, () => false, async () => {}, noop, noop));
  env.signinBtn = () => r6Walk($app).find((n) => n.tag === "button" && n.innerHTML === "登录");
  return env;
}

/* 参数里有默认值 {} 时，函数体从 ") {" 开始数括号 */
function l03Extract(name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(appSrc);
  if (!m) throw new Error("找不到函数: " + name);
  let i = appSrc.indexOf(") {", m.index) + 2, d = 0, j = i;
  for (; j < appSrc.length; j++) {
    if (appSrc[j] === "{") d++;
    else if (appSrc[j] === "}") { d--; if (d === 0) break; }
  }
  return appSrc.slice(m.index, j + 1);
}

function l03Unauth() { const e = new Error("用户名或密码不对"); e.status = 401; return e; }

async function loginResetHint() {
  console.log("=== L03 换库后登录页说清「账号需要重新注册」 ===");
  const HINT = "登录已失效且这个账号现在不存在：服务器在 2026-10-18 更换过数据库，之前的账号需要重新注册（用户名可以沿用）。如果你刚才只是密码输错，再试一次即可。";
  // boot：/api/auth/me 401 → 记下标记与被清掉的 uid
  {
    const localStorage = r6Storage();
    localStorage.setItem("rg_uid", "u-old"); localStorage.setItem("rg_token", "t-old");
    const S = { uid: "u-old", token: "t-old" };
    const reloads = [];
    const api = async (m, url) => {
      if (url === "/api/auth/me") throw l03Unauth();
      if (url === "/api/health") return {};
      return null;
    };
    const src = ["boot", "clearSession", "clearLocalDrafts", "sessionGoneMsg"].map((n) => extract(appSrc, n)).join("\n");
    const f = new Function("S", "api", "localStorage", "sessionStorage", "applyLlmPill", "applyPaths", "buildTutorials",
      "setSession", "loadPrefs", "ensurePortrait", "toast", "reloadInto",
      "let directionEpoch = 0;\n" + src + "; return { boot };");
    const noop = () => {};
    const { boot: realBoot } = f(S, api, localStorage, r6Storage(), noop, noop, noop, noop, async () => {}, async () => {},
      noop, (v, msg) => reloads.push([v, msg]));
    await realBoot();
    check(localStorage.getItem("rg_reset_hint") === "1", "boot 401 后 rg_reset_hint=1", String(localStorage.getItem("rg_reset_hint")));
    check(localStorage.getItem("rg_last_uid") === "u-old", "boot 401 后 rg_last_uid 是被清掉的 uid", String(localStorage.getItem("rg_last_uid")));
    check(S.uid === "" && reloads.length === 1 && reloads[0][0] === "login", "仍清会话并回登录页", JSON.stringify(reloads));
  }
  // 有标记：signin 401 → 表单下方出现固定文案，点两次也只有一段，toast 照旧
  {
    const env = l03Env(true);
    env.renderLogin();
    const btn = env.signinBtn();
    check(!!(btn && btn.onclick), "找到登录按钮");
    const form = btn.parent;
    const [u, inv, p] = form.children;  // 用户名、邀请码（选填）、密码
    u.value = "xiaobei"; p.value = "pw123456";
    env.reply = l03Unauth();
    await btn.onclick();
    await btn.onclick();
    const notes = r6Walk(env.$app).filter((n) => String(n.className).split(/\s+/).includes("login-reset-hint"));
    check(notes.length === 1, "提示只出现一次", String(notes.length));
    check(!!notes[0] && notes[0].innerHTML === HINT && notes[0].className === "login-note login-reset-hint", "提示文案与样式固定", notes[0] && notes[0].innerHTML);
    check(!!notes[0] && notes[0].parent === form && form.children.indexOf(notes[0]) > form.children.indexOf(btn), "提示在表单下方");
    check(env.toasts.length === 2 && env.toasts[0] === "用户名或密码不对", "toast 照旧", JSON.stringify(env.toasts));
    env.reply = { uid: "u-new", token: "t-new", consent_ok: true };
    await btn.onclick();
    check(env.localStorage.getItem("rg_reset_hint") === null, "登录成功清掉 rg_reset_hint");
    check(env.localStorage.getItem("rg_last_uid") === null, "登录成功清掉 rg_last_uid");
  }
  // 无标记：signin 401 只有 toast
  {
    const env = l03Env(false);
    env.renderLogin();
    const btn = env.signinBtn();
    const [u, inv, p] = btn.parent.children;  // 用户名、邀请码（选填）、密码
    u.value = "xiaobei"; p.value = "pw123456";
    env.reply = l03Unauth();
    await btn.onclick();
    const notes = r6Walk(env.$app).filter((n) => String(n.className).split(/\s+/).includes("login-reset-hint"));
    check(notes.length === 0 && env.toasts.length === 1, "无标记时 401 只有 toast", JSON.stringify(env.toasts));
  }
}

async function apiTimeout() {
  console.log("=== L03 登录类请求超时 ===");
  const SLOW = "服务器响应慢（可能在冷启动），请 10 秒后再试";
  const signals = [];
  const fetch = (url, opt) => { signals.push(opt.signal); return new Promise(() => {}); };
  const src = ["api", "apiFetch", "authHeaders"].map((n) => l03Extract(n)).join("\n");
  const f = new Function("S", "fetch", "apiUrl", "errText", "recoverUser", src + "; return { api };");
  const { api } = f({ token: "" }, fetch, (p) => p, () => "", async () => false);
  const t0 = Date.now();
  const out = await Promise.race([
    api("POST", "/api/auth/signin", { username: "a", password: "b" }, { timeoutMs: 50 }).then(() => "resolved", (e) => e),
    new Promise((r) => setTimeout(() => r("pending"), 1000)),
  ]);
  check(out instanceof Error && out.message === SLOW, "带 timeoutMs 的请求 1 秒内以固定文案 reject", String(out && out.message || out));
  check(Date.now() - t0 < 1000, "reject 在 1 秒内", String(Date.now() - t0));
  check(!!signals[0] && signals[0].aborted === true, "超时后 fetch 被 abort");
  let settled = false;
  api("POST", "/api/chat", { text: "x" }).then(() => { settled = true; }, () => { settled = true; });
  await new Promise((r) => setTimeout(r, 200));
  check(!settled && !(signals[1] && signals[1].aborted), "不带 timeoutMs 的请求不被 abort");
}

/* ===================== 25-L06 只有 me 的 401/410 才清身份、启动与登录后初始化有上限、恢复状态公告（独立区块） ===================== */
function l06Clock() {
  let now = 0, seq = 0;
  const q = [];
  const c = {
    setTimeout: (fn, ms = 0) => { q.push({ id: ++seq, at: now + ms, fn }); return seq; },
    clearTimeout: (id) => { const k = q.findIndex((t) => t.id === id); if (k >= 0) q.splice(k, 1); },
    now: () => now,
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        await tick(); await tick();
        q.sort((a, b) => a.at - b.at || a.id - b.id);
        if (!q.length || q[0].at > end) break;
        const t = q.shift();
        now = t.at;
        t.fn();
      }
      now = end;
      await tick(); await tick();
    },
  };
  return c;
}

function l06Res(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function l06Api(S, fetch, clock) {
  const src = ["api", "apiFetch", "authHeaders"].map((n) => l03Extract(n)).join("\n");
  return new Function("S", "fetch", "apiUrl", "errText", "recoverUser", "confirmSessionGone", "setTimeout", "clearTimeout",
    "let sessionKick = null;\n" + src + "; return { api };")(
    S, fetch, (p) => p, (d, st) => (d && d.detail) || `请求失败 (${st})`, async () => false, async () => {},
    clock.setTimeout, clock.clearTimeout).api;
}

async function l06SessionConfirm() {
  console.log("=== L06 只有 me 的 401/410 才清身份 ===");
  const MSG = "暂时无法核验登录状态，请稍后再试";
  const src = ["lostUser", "sessionGoneMsg", "recoverUser", "confirmSessionGone"].map((n) => extract(appSrc, n)).join("\n");
  const mkErr = (st) => { const e = new Error("x"); e.status = st; return e; };
  const outcomes = [[200, false], [401, true], [404, false], [410, true], [429, false], [503, false], ["net", false]];
  for (const fn of ["confirmSessionGone", "recoverUser"]) {
    for (const [st, gone] of outcomes) {
      const S = { uid: "u1", token: "t1" };
      const toasts = [], outs = [];
      const api = async (m, url, body, opt) => {
        if (url !== "/api/auth/me" || !opt || opt.timeoutMs !== 20000) throw new Error("bad call " + url);
        if (st === 200) return { uid: "u1" };
        if (st === "net") throw new TypeError("Failed to fetch");
        throw mkErr(st);
      };
      const signedOut = (m) => { outs.push(m); S.uid = ""; S.token = ""; };
      const fns = new Function("S", "api", "signedOut", "toast", src + "; return { recoverUser, confirmSessionGone };")(
        S, api, signedOut, (t) => toasts.push(t));
      await (fn === "recoverUser" ? fns.recoverUser(new Error("user not found")) : fns.confirmSessionGone());
      if (gone) check(S.uid === "" && S.token === "" && outs.length === 1, `${fn} me ${st}: 清身份`, JSON.stringify(S));
      else check(S.uid === "u1" && S.token === "t1" && outs.length === 0, `${fn} me ${st}: 身份保留`, JSON.stringify(S));
      if (gone || st === 200) check(toasts.length === 0, `${fn} me ${st}: 无核验失败 toast`, JSON.stringify(toasts));
      else check(toasts.length === 1 && toasts[0] === MSG, `${fn} me ${st}: 「暂时无法核验登录状态」toast`, JSON.stringify(toasts));
    }
    // me 挂起期间换了令牌，返回 401：不清、不 toast
    {
      const S = { uid: "u1", token: "t1" };
      const toasts = [], outs = [];
      let rej;
      const api = () => new Promise((_, r) => { rej = r; });
      const fns = new Function("S", "api", "signedOut", "toast", src + "; return { recoverUser, confirmSessionGone };")(
        S, api, (m) => { outs.push(m); S.uid = ""; S.token = ""; }, (t) => toasts.push(t));
      const p = fn === "recoverUser" ? fns.recoverUser(new Error("user not found")) : fns.confirmSessionGone();
      await tick();
      S.token = "t2";
      rej(mkErr(401));
      await p;
      check(S.uid === "u1" && S.token === "t2" && outs.length === 0 && toasts.length === 0, `${fn} 挂起期间换账号后 401：不清、不 toast`, JSON.stringify({ S, toasts }));
    }
  }
}

async function l06BootHealthTimeout() {
  console.log("=== L06 health 卡住时启动 5 秒后继续 ===");
  const clock = l06Clock();
  const S = { uid: "u1", token: "t1", entered: true };
  const calls = [];
  const fetch = (url) => {
    calls.push(url);
    if (url === "/api/health") return new Promise(() => {});
    if (url === "/api/paths") return Promise.resolve(l06Res(200, { paths: [] }));
    if (url === "/api/auth/me") return Promise.resolve(l06Res(200, { uid: "u1", consent_ok: true, entered: true }));
    return Promise.resolve(l06Res(200, {}));
  };
  const api = l06Api(S, fetch, clock);
  const splash = { id: "boot", className: "", removedAt: null };
  splash.classList = {
    contains: (c) => String(splash.className).split(/\s+/).includes(c),
    add: (c) => { splash.className = (splash.className + " " + c).trim(); },
  };
  splash.remove = () => { if (splash.removedAt === null) splash.removedAt = clock.now(); };
  splash.outAt = null;
  const origAdd = splash.classList.add;
  splash.classList.add = (c) => { if (c === "out" && splash.outAt === null) splash.outAt = clock.now(); origAdd(c); };
  const document = { getElementById: (id) => (id === "boot" && splash.removedAt === null ? splash : null) };
  const views = [], sets = [];
  const src = ["boot", "clearSession", "clearLocalDrafts", "sessionGoneMsg"].map((n) => extract(appSrc, n)).join("\n");
  const f = new Function("S", "api", "localStorage", "sessionStorage", "applyLlmPill", "applyPaths", "buildTutorials",
    "setSession", "loadPrefs", "ensurePortrait", "toast", "reloadInto", "setView", "openPrivacy", "document", "setTimeout",
    "let directionEpoch = 0;\n" + src + "; return { boot };");
  const noop = () => {};
  const { boot: realBoot } = f(S, api, r6Storage(), r6Storage(), noop, noop, noop,
    (r) => { sets.push(r); S.uid = r.uid; if (r.entered !== undefined) S.entered = !!r.entered; }, async () => {}, async () => {},
    noop, noop, (v) => views.push(v), noop, document, clock.setTimeout);
  let done = false;
  realBoot().then(() => { done = true; });
  await clock.advance(4999);
  check(!done && views.length === 0, "5 秒前仍在等 health", JSON.stringify(calls));
  await clock.advance(1);
  check(splash.outAt !== null && splash.outAt <= 5000, "遮罩在 5 秒内开始移除", String(splash.outAt));
  await clock.advance(1000);
  check(done, "5 秒后启动继续并结束");
  check(calls.includes("/api/auth/me") && sets.length === 1, "/api/auth/me 照常请求", JSON.stringify(calls));
  check(S.uid === "u1" && S.token === "t1", "me 200 时身份保留", JSON.stringify(S));
  check(views.length === 1, "setView 被调用", JSON.stringify(views));
  check(splash.removedAt !== null && splash.removedAt <= 5600, "遮罩已移除", String(splash.removedAt));
}

function l06LoginEnv(opts = {}) {
  const document = r6Dom();
  const mk = document.createElement;
  document.createElement = (tag) => { const n = mk(tag); n.addEventListener = () => {}; return n; };
  document.createTextNode = (t) => { const n = mk("#text"); n.textContent = t; return n; };
  const localStorage = r6Storage();
  if (opts.recovery !== undefined) localStorage.setItem("rg_recovery_state", opts.recovery);
  const clock = l06Clock();
  const env = { document, localStorage, clock, toasts: [], fetches: [], S: { uid: "", token: "", auth: { wechat_login: false } } };
  const fetch = (url) => {
    env.fetches.push(url);
    if (url === "/api/auth/signin") return Promise.resolve(l06Res(200, { uid: "u-new", token: "t-new", consent_ok: true }));
    return Promise.resolve(l06Res(200, {}));
  };
  const api = l06Api(env.S, fetch, clock);
  const noop = () => {};
  const src = ["el", "consentRow", "renderLogin"].map((n) => extract(appSrc, n)).join("\n");
  const f = new Function("S", "api", "localStorage", "document", "toast", "$nav", "$header", "$app",
    "stopWxPoll", "startWechat", "openPrivacy", "revokeToken", "setSession", "afterEnter", "openConnect", "setView", "setTimeout",
    src + "; return { renderLogin };");
  const $app = document.createElement("main");
  document.body.appendChild($app);
  const setSession = (r) => { env.S.uid = r.uid; if (r.token) env.S.token = r.token; return false; };
  Object.assign(env, { $app }, f(env.S, api, localStorage, document, (t) => env.toasts.push(t), {}, {}, $app,
    noop, noop, noop, noop, setSession, opts.afterEnter || (async () => {}), noop, noop, clock.setTimeout));
  env.btn = (label) => r6Walk($app).find((n) => n.tag === "button" && n.innerHTML === label);
  return env;
}

async function l06AfterEnterTimeout() {
  console.log("=== L06 登录成功后初始化 15 秒上限 ===");
  const MSG = "登录成功，页面资料暂未加载，请刷新重试";
  const env = l06LoginEnv({ afterEnter: () => new Promise(() => {}) });
  env.renderLogin();
  const btn = env.btn("登录");
  const [u, inv, p] = btn.parent.children;  // 用户名、邀请码（选填）、密码
  u.value = "xiaobei"; p.value = "pw123456";
  let done = false;
  btn.onclick().then(() => { done = true; });
  await env.clock.advance(14999);
  check(!done && btn.disabled === true, "15 秒前仍在等 afterEnter");
  await env.clock.advance(1);
  check(done, "15 秒后 onclick 结束");
  check(env.S.token === "t-new", "S.token 仍在", env.S.token);
  check(env.toasts.includes(MSG), "toast 文案逐字", JSON.stringify(env.toasts));
  check(btn.disabled === false, "按钮恢复可点");
  check(env.fetches.filter((x) => x === "/api/auth/signin").length === 1, "signin 只请求一次", JSON.stringify(env.fetches));
}

async function l06RecoveryNotice() {
  console.log("=== L06 登录页的全局恢复状态公告 ===");
  const LOST = "服务器确认原账号数据无法恢复，请重新注册；旧记录不会自动找回。";
  {
    const env = l06LoginEnv({ recovery: '{"event_id":"2026-10-18","state":"lost"}' });
    env.renderLogin();
    const notes = r6Walk(env.$app).filter((n) => String(n.className).split(/\s+/).includes("login-recovery"));
    check(notes.length === 1 && notes[0].innerHTML === LOST && notes[0].className === "login-note login-recovery", "lost：逐字文案", notes[0] && notes[0].innerHTML);
    const rbtn = env.btn("注册并进入");
    check(!!rbtn && rbtn.parent.hidden === false, "lost：注册步骤展开");
    const sbtn = env.btn("登录");
    check(!!sbtn && sbtn.parent.hidden === true, "lost：登录步骤收起");
  }
  for (const st of ["normal", undefined]) {
    const env = l06LoginEnv({ recovery: st });
    env.renderLogin();
    const notes = r6Walk(env.$app).filter((n) => String(n.className).split(/\s+/).includes("login-recovery"));
    check(notes.length === 0, `${st || "缺省"}：没有 .login-recovery`, String(notes.length));
  }
}

/* ===================== R9-04 / R9-05 / R9-06 认证 deadline 盖住正文、启动资料 15 秒上限、恢复公告绑定 event_id（独立区块） ===================== */
function r9HangRes(opt) {
  const json = () => new Promise((_, rej) => {
    if (opt && opt.signal) opt.signal.addEventListener("abort", () => { const e = new Error("aborted"); e.name = "AbortError"; rej(e); });
  });
  return { ok: true, status: 200, json };
}

function r9BootEnv(o) {
  const clock = l06Clock();
  const S = Object.assign({ uid: "", token: "" }, o.S || {});
  const ls = o.ls || r6Storage();
  const env = { clock, S, ls, views: [], toasts: [], reloads: [], sets: [], done: false };
  const src = ["boot", "clearSession", "clearLocalDrafts", "sessionGoneMsg"].map((n) => extract(appSrc, n)).join("\n");
  const f = new Function("S", "api", "localStorage", "sessionStorage", "applyLlmPill", "applyPaths", "buildTutorials",
    "setSession", "loadPrefs", "ensurePortrait", "toast", "reloadInto", "setView", "openPrivacy", "document", "setTimeout", "clearTimeout",
    "let directionEpoch = 0;\n" + src + "; return { boot };");
  const noop = () => {};
  const setSession = (r) => { env.sets.push(r); S.uid = r.uid; if (r.token) S.token = r.token; S.entered = !!r.entered; return false; };
  env.boot = f(S, o.api, ls, r6Storage(), noop, noop, noop, setSession, o.loadPrefs || (async () => {}), o.ensurePortrait || (async () => {}),
    (t) => env.toasts.push(t), (v, m) => env.reloads.push([v, m]), (v) => env.views.push(v), noop,
    { getElementById: () => null }, clock.setTimeout, clock.clearTimeout).boot;
  env.start = () => env.boot().then(() => { env.done = true; });
  return env;
}

function r9LoginEnv(fetch) {
  const document = r6Dom();
  const mk = document.createElement;
  document.createElement = (tag) => { const n = mk(tag); n.addEventListener = () => {}; return n; };
  document.createTextNode = (t) => { const n = mk("#text"); n.textContent = t; return n; };
  const clock = l06Clock();
  const env = { document, localStorage: r6Storage(), clock, toasts: [], S: { uid: "", token: "", auth: { wechat_login: false } } };
  const api = l06Api(env.S, fetch, clock);
  const noop = () => {};
  const src = ["el", "consentRow", "renderLogin"].map((n) => extract(appSrc, n)).join("\n");
  const f = new Function("S", "api", "localStorage", "document", "toast", "$nav", "$header", "$app",
    "stopWxPoll", "startWechat", "openPrivacy", "revokeToken", "setSession", "afterEnter", "openConnect", "setView", "setTimeout",
    src + "; return { renderLogin };");
  const $app = document.createElement("main");
  document.body.appendChild($app);
  const setSession = (r) => { env.S.uid = r.uid; if (r.token) env.S.token = r.token; return false; };
  Object.assign(env, { $app }, f(env.S, api, env.localStorage, document, (t) => env.toasts.push(t), {}, {}, $app,
    noop, noop, noop, noop, setSession, async () => {}, noop, noop, clock.setTimeout));
  env.btn = (label) => r6Walk($app).find((n) => n.tag === "button" && n.innerHTML === label);
  return env;
}

async function r9ApiBodyTimeout() {
  console.log("=== R9-04 认证 deadline 盖住响应正文 ===");
  const SLOW = "服务器响应慢（可能在冷启动），请 10 秒后再试";
  const ends = [["POST", "/api/auth/signin", 20000], ["POST", "/api/auth/register", 20000], ["GET", "/api/auth/me", 20000],
    ["POST", "/api/auth/consent", 20000], ["GET", "/api/health", 5000], ["GET", "/api/paths", 5000]];
  for (const [m, url, ms] of ends) {
    const clock = l06Clock();
    const signals = [];
    const fetch = (u, opt) => { signals.push(opt.signal); return Promise.resolve(r9HangRes(opt)); };
    const api = l06Api({ token: "t1" }, fetch, clock);
    let out = "pending";
    api(m, url, m === "POST" ? {} : undefined, { timeoutMs: ms }).then(() => { out = "resolved"; }, (e) => { out = e; });
    await clock.advance(ms - 1);
    check(out === "pending" && signals[0] && signals[0].aborted === false, `${url}: 第 ${ms - 1}ms 仍在等正文`, String(out));
    await clock.advance(1);
    check(out instanceof Error && out.message === SLOW, `${url}: 第 ${ms}ms 以固定文案 reject`, String(out && out.message || out));
    check(signals[0].aborted === true, `${url}: 超时后 abort`);
    check(signals.length === 1, `${url}: 只请求一次`, String(signals.length));
  }
  // 正文不理会 abort 也照样按时 reject
  {
    const clock = l06Clock();
    const fetch = () => Promise.resolve({ ok: true, status: 200, json: () => new Promise(() => {}) });
    const api = l06Api({ token: "" }, fetch, clock);
    let out = "pending";
    api("POST", "/api/auth/signin", {}, { timeoutMs: 20000 }).then(() => { out = "resolved"; }, (e) => { out = e; });
    await clock.advance(20000);
    check(out instanceof Error && out.message === SLOW, "正文永挂且不响应 abort：第 20000ms reject", String(out && out.message || out));
  }
  // 不带 timeoutMs 的长请求继续等
  {
    const clock = l06Clock();
    const signals = [];
    const fetch = (u, opt) => { signals.push(opt.signal); return Promise.resolve(r9HangRes(opt)); };
    const api = l06Api({ token: "t1" }, fetch, clock);
    let out = "pending";
    api("POST", "/api/chat", { text: "x" }).then(() => { out = "resolved"; }, (e) => { out = e; });
    await clock.advance(60000);
    check(out === "pending" && signals.length === 1 && !signals[0], "不带 timeoutMs 的 chat 60 秒后仍在等、没有 signal", String(out));
  }
  // 登录按钮：headers 到了、正文挂起 → 20 秒复位，toast 逐字，signin 只发一次
  {
    const fetches = [];
    const env = r9LoginEnv((url, opt) => { fetches.push(url); return Promise.resolve(r9HangRes(opt)); });
    env.renderLogin();
    const btn = env.btn("登录");
    // 登录表单里多了邀请码框（#28），按 aria-label 找用户名/密码框，不按位置
    const kids = [...btn.parent.children];
    const u = kids.find((x) => (x.attrs || {})["aria-label"] === "用户名");
    const p = kids.find((x) => (x.attrs || {})["aria-label"] === "密码");
    u.value = "xiaobei"; p.value = "pw123456";
    let done = false;
    btn.onclick().then(() => { done = true; });
    await env.clock.advance(19999);
    check(!done && btn.disabled === true && btn.textContent === "登录中…", "登录：第 19999ms 仍在等");
    await env.clock.advance(1);
    check(done && btn.disabled === false && btn.textContent === "登录", "登录：第 20000ms 按钮复位", String(btn.textContent));
    check(env.toasts.length === 1 && env.toasts[0] === SLOW, "登录：toast 逐字", JSON.stringify(env.toasts));
    check(fetches.filter((x) => x === "/api/auth/signin").length === 1, "登录：signin 只请求一次", JSON.stringify(fetches));
    check(env.S.token === "", "登录：没有会话", env.S.token);
  }
  // 启动：health、me 正文都挂起 → 5 秒 + 20 秒后落页，身份保留
  {
    const fetches = [];
    const fetch = (url, opt) => {
      fetches.push(url);
      if (url === "/api/paths") return Promise.resolve(l06Res(200, { paths: [] }));
      return Promise.resolve(r9HangRes(opt));
    };
    let api = null;
    const env2 = r9BootEnv({ S: { uid: "u1", token: "t1", entered: true }, api: (...a) => api(...a) });
    api = l06Api(env2.S, fetch, env2.clock);
    env2.start();
    await env2.clock.advance(24999);
    check(!env2.done && env2.views.length === 0, "启动：第 24999ms 还在等 me 正文", JSON.stringify(fetches));
    await env2.clock.advance(1000);
    check(env2.done && env2.views.length === 1, "启动：health 5 秒、me 20 秒后落页", JSON.stringify(env2.views));
    check(env2.S.uid === "u1" && env2.S.token === "t1" && env2.reloads.length === 0, "启动：身份保留", JSON.stringify(env2.S));
    check(fetches.filter((x) => x === "/api/auth/me").length === 1, "启动：me 只请求一次", JSON.stringify(fetches));
  }
}

function r9ProfileFns(S, api, ls, kits) {
  const names = ["loadPrefs", "trailStorageKey", "trail", "saveTrail", "pullTrail", "ensureSelKey", "adoptLegacyTrail", "ensurePortrait"];
  const RG_DIR = r4LoadDir();
  return new Function("S", "api", "useKit", "window", "RG_DIR", "FIELD_TREES", "localStorage",
    dirCtxSrc() + "\n" + r4Pick(names) + "; return {loadPrefs, ensurePortrait};")(
    S, api, (k) => { kits.push(k); S.kitId = k; }, { RG_DIR }, RG_DIR, {}, ls);
}

async function r9BootProfileDeadline() {
  console.log("=== R9-05 启动后资料加载 15 秒上限、迟到结果作废 ===");
  const MSG = "页面资料暂未加载，请刷新重试";
  const J = JSON.stringify;
  const late = {
    prefs: ["GET", "/api/me/prefs", { kit_id: "k-late", directions: ["190.20"] }],
    portrait: ["GET", "/api/portraits?uid=u1", { portraits: [{ id: "p1", active: true, name: "迟到画像" }] }],
    trail: ["GET", "/api/projects/p1/trail", { code: "", dirs: ["190.20"], sel: ["190.20"] }],
  };
  for (const which of ["prefs", "portrait", "trail"]) {
    const S = { uid: "u1", token: "t1", entered: true, myDirs: undefined, portraitId: "", portraitName: "" };
    const ls = r6Storage();
    ls.setItem("rg_uid", "u1"); ls.setItem("rg_token", "t1"); ls.setItem("rg_draft_u1_t1", "没写完的草稿");
    const kits = [];
    let release = null;
    const api = async (m, url) => {
      if (url === "/api/health") return {};
      if (url === "/api/paths") return null;
      if (url === "/api/auth/me") return { uid: "u1", consent_ok: true, entered: true };
      const [lm, lu, body] = late[which];
      if (m === lm && url === lu) return new Promise((r) => { release = () => r(JSON.parse(J(body))); });
      if (url === "/api/me/prefs") return { directions: [] };
      if (url === "/api/portraits?uid=u1") return { portraits: [{ id: "p1", active: true, name: "" }] };
      if (url === "/api/projects/p1/trail") return { code: "", dirs: [], sel: [] };
      throw new Error("未预期的请求 " + m + " " + url);
    };
    const fns = r9ProfileFns(S, api, ls, kits);
    const env = r9BootEnv({ S, ls, api, loadPrefs: fns.loadPrefs, ensurePortrait: fns.ensurePortrait });
    env.start();
    await env.clock.advance(1000);
    check(env.views.length === 1 && !!release, `${which} 挂起：me 200 后首个页面已绘制`, J(env.views));
    check(!env.done && env.toasts.length === 0, `${which} 挂起：15 秒内不提示`, J(env.toasts));
    await env.clock.advance(14000);
    check(env.done, `${which} 挂起：15 秒后启动结束`);
    check(env.toasts.length === 1 && env.toasts[0] === MSG, `${which} 挂起：逐字提示`, J(env.toasts));
    check(S.uid === "u1" && S.token === "t1" && env.reloads.length === 0, `${which} 挂起：身份保留`, J(S));
    check(ls.getItem("rg_draft_u1_t1") === "没写完的草稿" && ls.getItem("rg_token") === "t1", `${which} 挂起：草稿保留`);
    const before = J({ mem: ls.mem, myDirs: S.myDirs, portraitId: S.portraitId, portraitName: S.portraitName, kitId: S.kitId, kits });
    const views = env.views.length, toasts = env.toasts.length;
    release();
    await env.clock.advance(1000);
    const after = J({ mem: ls.mem, myDirs: S.myDirs, portraitId: S.portraitId, portraitName: S.portraitName, kitId: S.kitId, kits });
    check(after === before, `${which} 迟到响应：缓存与状态不变`, after);
    check(env.views.length === views && env.toasts.length === toasts, `${which} 迟到响应：无导航、无提示`, J(env.views));
  }
  // 资料接口 404/503：不清身份
  for (const st of [404, 503]) {
    const S = { uid: "u1", token: "t1", entered: true, myDirs: undefined, portraitId: "", portraitName: "" };
    const ls = r6Storage();
    ls.setItem("rg_uid", "u1"); ls.setItem("rg_token", "t1");
    const api = async (m, url) => {
      if (url === "/api/health") return {};
      if (url === "/api/paths") return null;
      if (url === "/api/auth/me") return { uid: "u1", consent_ok: true, entered: true };
      const e = new Error("x"); e.status = st; throw e;
    };
    const fns = r9ProfileFns(S, api, ls, []);
    const env = r9BootEnv({ S, ls, api, loadPrefs: fns.loadPrefs, ensurePortrait: fns.ensurePortrait });
    env.start();
    await env.clock.advance(16000);
    check(env.done && env.views.length === 1, `资料 ${st}：落页`, J(env.views));
    check(S.uid === "u1" && S.token === "t1" && ls.getItem("rg_token") === "t1" && env.reloads.length === 0, `资料 ${st}：不清身份`, J(S));
  }
}

async function r9RecoveryNoticeEvent() {
  console.log("=== R9-06 恢复公告绑定 event_id 与会话已见事件 ===");
  const LOST = "服务器确认原账号数据无法恢复，请重新注册；旧记录不会自动找回。";
  const RECOVERING = "账号正在恢复，请稍后重试。";
  const J = JSON.stringify;
  const notes = (env) => r6Walk(env.$app).filter((n) => String(n.className).split(/\s+/).includes("login-recovery"));
  const gone = (st) => { const e = new Error("x"); e.status = st; return e; };
  const runBoot = async (ls, recovery, me) => {
    const S = me ? { uid: "u1", token: "t1" } : { uid: "", token: "" };
    const api = async (m, url) => {
      if (url === "/api/health") return { auth: { wechat_login: false, recovery } };
      if (url === "/api/paths") return null;
      if (url === "/api/auth/me") throw gone(me);
      throw new Error("未预期的请求 " + url);
    };
    const env = r9BootEnv({ S, ls, api });
    env.start();
    await env.clock.advance(16000);
    return env;
  };
  const login = (ls) => {
    const v = ls.getItem("rg_recovery_state");
    const env = l06LoginEnv(v === null ? {} : { recovery: v });
    env.renderLogin();
    return env;
  };
  // 旧 lost / 旧 recovering 遇新 normal：公告清掉，注册可点
  for (const old of ["lost", "recovering"]) {
    const ls = r6Storage();
    ls.setItem("rg_recovery_state", J({ event_id: "E1", state: old }));
    await runBoot(ls, { event_id: "", state: "normal" }, 0);
    check(ls.getItem("rg_recovery_state") === null, `旧 ${old} 遇新 normal：删键`, String(ls.getItem("rg_recovery_state")));
    const env = login(ls);
    const rbtn = env.btn("注册并进入");
    check(notes(env).length === 0 && !!rbtn && rbtn.disabled !== true, `旧 ${old} 遇新 normal：无公告、注册可点`);
  }
  // 事件变了：覆盖成新事件并逐字渲染
  {
    const ls = r6Storage();
    ls.setItem("rg_recovery_state", J({ event_id: "E1", state: "lost" }));
    await runBoot(ls, { event_id: "E2", state: "recovering" }, 0);
    check(ls.getItem("rg_recovery_state") === J({ event_id: "E2", state: "recovering" }), "不同 event：覆盖", String(ls.getItem("rg_recovery_state")));
    const env = login(ls);
    const n = notes(env);
    check(n.length === 1 && n[0].innerHTML === RECOVERING && env.btn("注册并进入").disabled === true, "不同 event：recovering 逐字、禁注册", n[0] && n[0].innerHTML);
  }
  // 旧会话尚未见过本次事故：me 401/410 展示事故公告
  for (const st of [401, 410]) {
    const ls = r6Storage();
    const env0 = await runBoot(ls, { event_id: "E1", state: "lost" }, st);
    check(env0.reloads.length === 1 && env0.reloads[0][0] === "login", `未见事故 me ${st}：回登录页`, J(env0.reloads));
    check(ls.getItem("rg_recovery_state") === J({ event_id: "E1", state: "lost" }), `未见事故 me ${st}：写公告`, String(ls.getItem("rg_recovery_state")));
    const env = login(ls);
    const n = notes(env);
    check(n.length === 1 && n[0].innerHTML === LOST && env.btn("注册并进入").parent.hidden === false, `未见事故 me ${st}：lost 逐字、默认注册`, n[0] && n[0].innerHTML);
  }
  // 同一事故下新登录：记下已见事件、删公告；之后普通到期不再重挂
  for (const which of ["登录", "注册并进入"]) {
    const env = l06LoginEnv({ recovery: J({ event_id: "E1", state: "lost" }) });
    env.S.auth = { wechat_login: false, recovery: { event_id: "E1", state: "lost" } };
    env.renderLogin();
    const btn = env.btn(which);
    const inputs = btn.parent.children.filter((n) => n.tag === "input");
    inputs.forEach((n) => { n.value = "xiaobei123"; });
    if (which === "注册并进入") r6Walk(env.$app).filter((n) => n.type === "checkbox").forEach((n) => { n.checked = true; });
    await btn.onclick();
    check(env.localStorage.getItem("rg_seen_event") === "E1", `${which}成功：rg_seen_event=E1`, String(env.localStorage.getItem("rg_seen_event")));
    check(env.localStorage.getItem("rg_recovery_state") === null, `${which}成功：删 rg_recovery_state`);
    for (const st of [401, 410]) {
      const ls = r6Storage();
      ls.setItem("rg_seen_event", env.localStorage.getItem("rg_seen_event"));
      await runBoot(ls, { event_id: "E1", state: "lost" }, st);
      check(ls.getItem("rg_recovery_state") === null, `${which}后新会话 me ${st}：不重挂公告`, String(ls.getItem("rg_recovery_state")));
      const lenv = login(ls);
      check(notes(lenv).length === 0, `${which}后新会话 me ${st}：登录页无事故公告`);
    }
  }
  // 旧的纯字符串值当 normal
  {
    const env = l06LoginEnv({ recovery: "lost" });
    env.renderLogin();
    check(notes(env).length === 0 && env.btn("注册并进入").disabled !== true, "旧纯字符串 lost：当 normal");
  }
}
