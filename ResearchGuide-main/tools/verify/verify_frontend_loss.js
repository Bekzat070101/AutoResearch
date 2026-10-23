/* 18-F01 / F06 / F05：刷新后加方向不覆盖、草稿不被旧稿盖、历史加载不冲输入。 */
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "..", "..");
const appSrc = fs.readFileSync(path.join(root, "web", "js", "app.js"), "utf8");
const chatSrc = fs.readFileSync(path.join(root, "web", "js", "chat.js"), "utf8");

let fails = 0;
const check = (c, l, e = "") => { console.log(`  [${c ? "OK " : "!! "}] ${l} ${e}`); if (!c) fails++; };

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

(async () => {
  console.log("=== 18-F01 刷新后添加方向 ===");
  const store = { prefs: { directions: ["190.20"] } };
  const puts = [];
  let failGet = false;
  const S = { myDirs: undefined, uid: "u1", token: "t", portraitId: "p1" };
  const api = async (m, url, body) => {
    if (m === "GET" && url === "/api/me/prefs") {
      if (failGet) throw new Error("offline");
      return JSON.parse(JSON.stringify(store.prefs));
    }
    if (m === "PUT" && url === "/api/me/prefs") {
      puts.push(body);
      store.prefs = { directions: body.directions };
      return JSON.parse(JSON.stringify(store.prefs));
    }
    if (m === "GET" && url.startsWith("/api/projects/")) return { code: "", dir: "", dirs: [], sel: [], done: [], tasks: {}, byDir: {} };
    return {};
  };
  const toast = () => {};
  const paintChrome = () => {};
  const useKit = () => {};
  // addMyDirection 现在还会写本机 trail（方向跟着画像走），沙箱里给最小桩
  const memF01 = {};
  const lsF01 = {
    getItem: (k) => (k in memF01 ? memF01[k] : null),
    setItem: (k, v) => { memF01[k] = String(v); },
    removeItem: (k) => { delete memF01[k]; },
  };
  const RG_DIR = { byId: { "190.20": {}, "520.20": {}, "110.67": {} }, legacyDir: {} };
  const window = { RG_DIR };
  const FIELD_TREES = {};
  const ctxDecl = [/^const STALE_DIR\b.*$/m, /^let directionEpoch\b.*$/m, /^let portraitBusy\b.*$/m]
    .map((re) => re.exec(appSrc)[0]).join("\n");
  const fns = ctxDecl + "\n" + ["directionContext", "dirAwait", "loadPrefs", "ensureMyDirs", "putPrefs", "trailStorageKey", "trail", "saveTrail",
    "selectedDirs", "portraitDirsAdd", "readTrailForWrite", "addMyDirection"].map((n) => extract(appSrc, n)).join("\n");
  const fn = new Function("S", "api", "toast", "paintChrome", "useKit",
    "window", "RG_DIR", "FIELD_TREES", "localStorage",
    fns + "; return {loadPrefs, ensureMyDirs, addMyDirection};");
  const { loadPrefs, addMyDirection } = fn(S, api, toast, paintChrome, useKit, window, RG_DIR, FIELD_TREES, lsF01);
  await loadPrefs();
  S.myDirs = undefined;
  await addMyDirection("520.20");
  check(store.prefs.directions.includes("190.20") && store.prefs.directions.includes("520.20"),
    "刷新后未加载时添加，旧收藏还在", JSON.stringify(store.prefs.directions));
  const nPuts = puts.length;
  failGet = true;
  S.myDirs = undefined;
  await addMyDirection("110.67");
  check(puts.length === nPuts, "loadPrefs 失败时不发 PUT");

  console.log("=== 18-F06 旧服务器草稿不盖本地新稿 ===");
  const mem = {};
  const localStorage = {
    getItem: (k) => (k in mem ? mem[k] : null),
    setItem: (k, v) => { mem[k] = String(v); },
    removeItem: (k) => { delete mem[k]; },
  };
  let putFails = true;
  let remote = { text: "older", updated_at: "2026-01-01T00:00:00+00:00" };
  const S2 = { token: "t" };
  const api2 = async (m, url, body) => {
    if (m === "GET") return remote;
    if (m === "PUT") {
      if (putFails) throw new Error("offline");
      remote = { text: body.text, updated_at: body.updated_at || new Date().toISOString() };
      return remote;
    }
    return {};
  };
  const draftFns = ["readDraft", "readDraftMeta", "writeDraftMeta", "writeDraft", "hydrateDraft"]
    .map((n) => extract(appSrc, n)).join("\n");
  const dfn = new Function("S", "api", "localStorage", "document",
    draftFns + "; return {readDraft, writeDraft, hydrateDraft};");
  const { readDraft, writeDraft, hydrateDraft } = dfn(S2, api2, localStorage, { activeElement: null });
  writeDraft("tid1", "newer");
  await new Promise((r) => setTimeout(r, 10));
  const ta = { value: "newer" };
  await hydrateDraft("tid1", ta);
  check(readDraft("tid1") === "newer", "PUT 失败后 GET 旧稿，本地仍是 newer", readDraft("tid1"));

  console.log("=== 18-F05 历史加载不覆盖输入 ===");
  const histIdx = chatSrc.indexOf("await api(\"GET\", `/api/dialogue/history");
  const restoreIdx = chatSrc.indexOf("sessionStorage.getItem(draftKey)");
  const listenIdx = chatSrc.indexOf("input.addEventListener(\"input\"");
  check(restoreIdx >= 0 && restoreIdx < histIdx, "草稿恢复在 await history 之前");
  check(listenIdx >= 0 && listenIdx < histIdx, "input 监听在 await history 之前");
  const afterHist = chatSrc.slice(histIdx, chatSrc.indexOf("async function send"));
  check(!/input\.value\s*=/.test(afterHist), "history 回来后不碰 input.value");

  console.log("\n" + (fails ? `失败 ${fails} 项` : "全部通过"));
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
