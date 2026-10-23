/* Local integration/visual QA. Requires playwright and the seeded QA server.
 * node tools/verify/verify_chat_widget.js http://127.0.0.1:8123 output-directory
 */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const base = process.argv[2] || "http://127.0.0.1:8123";
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw Error("Only local QA servers are allowed");
  const out = path.resolve(process.argv[3] || ".probe-chat-widget"); fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const errors = []; page.on("pageerror", (e) => errors.push(e.message));
    const r = await page.request.post(base + "/api/auth/signin", { data: { username: "dialogueqa", password: "Dialogue-QA-2026" } });
    assert.equal(r.status(), 200); const account = await r.json();
    await page.addInitScript((u) => { localStorage.setItem("rg_uid", u.uid); localStorage.setItem("rg_token", u.token); localStorage.setItem("rg_nick", u.nickname); sessionStorage.setItem("rg_last_view", "today"); }, account);
    await page.goto(base); await page.locator('.chat-launcher').waitFor({ state: "visible" });
    await page.locator('.nav-btn[data-view="today"]').click();
    assert.equal(await page.locator('.nav-btn[data-view="dialogue"]').count(), 0);
    await page.locator('.chat-launcher').click();
    await page.locator('.chat-dialog textarea').waitFor();
    const widget = await page.locator('#chatWidget').evaluate((n) => { n.dataset.qaIdentity = "persistent"; return n.dataset.qaIdentity; });
    const list = page.getByRole("button", { name: "切换会话", exact: true });
    await list.click(); await page.getByRole("button", { name: "阅读讨论", exact: true }).click();
    const input = page.getByRole("textbox", { name: "对话输入" });
    await input.fill("A 的草稿");
    await list.click(); await page.getByRole("button", { name: "新的研究想法", exact: true }).click();
    await input.fill("B 的草稿");
    await list.click(); await page.getByRole("button", { name: "阅读讨论", exact: true }).click();
    assert.equal(await input.inputValue(), "A 的草稿");
    await page.getByRole("button", { name: "扩大", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('.chat-input-row .btn').disabled);
    assert.equal(Math.round((await page.locator('.chat-dialog').boundingBox()).width), 720);
    await page.screenshot({ path: path.join(out, "desktop-expanded.png") });
    await page.getByRole("button", { name: "还原", exact: true }).click();
    assert.equal(Math.round((await page.locator('.chat-dialog').boundingBox()).width), 440);
    await input.fill("你能列出我已经读过哪些论文吗？");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await page.locator('.chat-launcher.is-generating').waitFor();
    await list.click(); await page.getByRole("button", { name: "新的研究想法", exact: true }).click();
    assert.equal(await input.inputValue(), "B 的草稿");
    assert.equal(await page.getByRole("button", { name: "发送", exact: true }).isDisabled(), true);
    await input.fill("B 生成期间仍能编辑草稿");
    await page.getByRole("button", { name: "收起", exact: true }).click();
    await page.locator('.nav-btn[data-view="read"]').click();
    await page.locator('.chat-launcher').click();
    assert.equal(await page.locator('#chatWidget').getAttribute("data-qa-identity"), widget);
    assert.equal(await input.inputValue(), "B 生成期间仍能编辑草稿");
    await page.waitForFunction(() => !ChatView.isBusy(), null, { timeout: 180000 });
    await list.click(); await page.getByRole("button", { name: /^阅读讨论/ }).click();
    await page.locator('.chat-reading-sources').first().waitFor({ timeout: 15000 });
    await page.locator('.chat-reading-sources').last().locator('summary').click();
    await page.screenshot({ path: path.join(out, "desktop-reading-sources.png") });
    const text = await page.locator('.chat-scroll').innerText();
    fs.writeFileSync(path.join(out, "real-model-ui.txt"), text, "utf8");
    assert.match(text, /Massive|Contamination/);
    // A refreshed page may reconnect while a previous server worker is still
    // running. Exercise that state without paying for an extra model call.
    let serverBusy = true;
    await page.route('**/api/dialogue/conversations?*', async (route) => {
      const response = await route.fetch(); const data = await response.json();
      await route.fulfill({ response, json: { ...data, busy: serverBusy || data.busy } });
    });
    await page.reload(); await page.locator('.chat-launcher').waitFor(); await page.locator('.chat-launcher').click();
    await page.waitForFunction(() => ChatView.isBusy());
    assert.equal(await page.getByRole("button", { name: "发送", exact: true }).isDisabled(), true);
    await input.fill("刷新期间保留的草稿");
    serverBusy = false;
    await page.waitForFunction(() => !ChatView.isBusy());
    await page.waitForFunction(() => !document.querySelector('.chat-input-row .btn').disabled);
    assert.equal(await input.inputValue(), "刷新期间保留的草稿");
    await page.setViewportSize({ width: 390, height: 844 });
    const rect = await page.locator('.chat-dialog').boundingBox();
    assert.equal(Math.round(rect.width), 390); assert.equal(Math.round(rect.height), 844);
    await page.screenshot({ path: path.join(out, "mobile-chat.png") });
    const fits = await page.locator('.chat-input').evaluate((n) => n.getBoundingClientRect().bottom <= innerHeight);
    assert.equal(fits, true); assert.deepEqual(errors, []);
    const apiResults = [];
    const state = await page.evaluate(() => ({ uid: S.uid, portrait_id: S.portraitId }));
    const created = await page.request.post(base + "/api/dialogue/conversations", { headers: { Authorization: "Bearer " + account.token }, data: state });
    const cid = (await created.json()).conversation.id;
    for (const message of ["请根据我读过的 Measuring Massive Multitask Language Understanding 阅读笔记，说说它的局限。", "请结合我已读的两篇论文，比较基准评测与测试集污染之间的关系。请区分我的笔记和论文原文证据。"]) {
      const answer = await page.request.post(base + "/api/dialogue/turn", { headers: { Authorization: "Bearer " + account.token }, data: { ...state, conversation_id: cid, message }, timeout: 180000 });
      assert.equal(answer.status(), 200);
      const data = await answer.json(); assert.ok(data.reply); assert.equal(data.degraded, false);
      apiResults.push({ message, ...data });
    }
    fs.writeFileSync(path.join(out, "real-model-api.json"), JSON.stringify(apiResults, null, 2));
    const before = await page.request.get(base + `/api/dialogue/conversations?uid=${account.uid}&portrait_id=${state.portrait_id}`, { headers: { Authorization: "Bearer " + account.token } });
    const oldCount = (await before.json()).conversations.length;
    await page.getByRole("button", { name: "新对话", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('.chat-input-row .btn').disabled);
    const after = await page.request.get(base + `/api/dialogue/conversations?uid=${account.uid}&portrait_id=${state.portrait_id}`, { headers: { Authorization: "Bearer " + account.token } });
    assert.equal((await after.json()).conversations.length, oldCount + 1);
    assert.equal(await input.inputValue(), "");
    await input.fill("独立会话的草稿");
    await input.press("Escape"); assert.equal(await page.locator('.chat-dialog').isHidden(), true);
    await page.locator('.chat-launcher').press("Enter");
    assert.equal(await input.inputValue(), "独立会话的草稿");
    console.log("PASS: persistent widget, isolated drafts, switching while generating, reload recovery, navigation, 440/720px, 390px mobile, reading sources; 3 real-model scenarios.");
  } finally { await browser.close(); }
})().catch((e) => { console.error(e); process.exitCode = 1; });
