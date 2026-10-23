/* Local browser acceptance, with isolated synthetic users and real model replies.
 * NODE_PATH=<playwright modules> node tools/verify/verify_direction_chat_ui.js <local-url> <output>
 */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const base = process.argv[2] || "http://127.0.0.1:8123";
assert.match(base, /^http:\/\/127\.0\.0\.1:\d+$/);
const out = path.resolve(process.argv[3] || ".probe-direction-chat");
fs.mkdirSync(out, { recursive: true });
const result = { version: "w110", checks: [], turns: [], errors: [] };
const save = () => fs.writeFileSync(path.join(out, "results.json"), JSON.stringify(result, null, 2));
const check = (name, detail) => { result.checks.push({ name, detail }); save(); console.log("PASS " + name); };
let browser, failurePage;
async function register(page, prefix) {
  const username = prefix + Date.now();
  const response = await page.request.post(base + "/api/auth/register", { data: { username, password: "Direction-QA-2026", nickname: "方向界面验收", consent: true } });
  assert.equal(response.status(), 200); const account = await response.json();
  await page.addInitScript((u) => { localStorage.setItem("rg_uid", u.uid); localStorage.setItem("rg_token", u.token); sessionStorage.setItem("rg_last_view", "today"); }, account);
  return { ...account, username };
}
async function ready(page) { await page.waitForFunction(() => document.querySelector('.chat-input-row .btn')?.disabled === false); }
async function boot(page) { await page.goto(base); await page.locator('#boot').waitFor({ state: "hidden" }); }
async function say(page, input, scene, duringGeneration) {
  await ready(page);
  await page.getByRole('textbox', { name: '对话输入', exact: true }).fill(input);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  if (duringGeneration) await duringGeneration();
  await page.waitForFunction(() => !ChatView.isBusy(), null, { timeout: 180000 });
  const history = await page.evaluate(() => api('GET', `/api/dialogue/history?uid=${S.uid}&portrait_id=${S.portraitId}`));
  const answer = history.messages.filter((m) => m.role === 'assistant').at(-1);
  assert.ok(answer?.text); result.turns.push({ scene, input, reply: answer.text, conversation_id: history.conversation_id }); save();
}
(async () => {
  browser = await chromium.launch({ headless: true, channel: "msedge" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = failurePage = await context.newPage(); page.on('pageerror', e => result.errors.push(e.message));
  const user = await register(page, 'dirfirst'); result.newAccount = user.username;
  await boot(page); await page.locator('#app .chat-onboarding-content textarea').waitFor(); await ready(page);
  assert.equal(await page.locator('.chat-launcher').isVisible(), false);
  await page.screenshot({ animations: "disabled", path: path.join(out, '01-first-dialogue.png') });
  check('新账号保留完整首次对话', '主区域聊天与背景摘要，应用气泡隐藏');
  await say(page, '我是信息管理专业的大二学生，学过高数和线性代数，会一点 Python。', '首次对话：学习背景');
  await say(page, '我对大模型评测和测试集污染感兴趣，读论文主要卡在实验设计。每周有三小时，希望你帮我选方向。', '首次对话：兴趣与目标');
  await page.getByRole('button', { name: '查看推荐方向', exact: true }).waitFor({ timeout: 15000 });
  await page.screenshot({ animations: "disabled", path: path.join(out, '02-onboarding-directions-cta.png') });
  await page.getByRole('button', { name: '查看推荐方向', exact: true }).click();
  const early = page.getByRole('button', { name: '直接进入', exact: true }); if (await early.isVisible()) await early.click();
  await page.getByRole('tab', { name: '方向', exact: true }).waitFor();
  assert.equal(await page.getByRole('tab', { name: '方向', exact: true }).getAttribute('aria-selected'), 'true');
  assert.equal(await page.evaluate(() => trail().dir || ''), '');
  check('首次对话→推荐方向', '实际点击入口，打开方向标签；未自动替用户确认');
  await page.route('**/api/directions/choose**', route => route.fulfill({ status: 500, json: { detail: '验收模拟：方向保存失败，请稍后重试' } }));
  await page.getByRole('button', { name: '确认这个方向', exact: true }).first().click();
  await page.getByText('验收模拟：方向保存失败，请稍后重试', { exact: true }).waitFor();
  assert.equal(await page.evaluate(() => trail().dir || ''), '');
  check('确认方向失败提示', '仅此请求模拟500；显示错误，未产生确认状态');
  await page.unroute('**/api/directions/choose**');
  await page.getByRole('button', { name: '确认这个方向', exact: true }).first().click();
  await page.waitForFunction(() => !!trail().dir);
  await page.waitForFunction(() => document.getElementById('dirChip').textContent.includes(dirTitle(trail().dir)));
  const dir = await page.evaluate(() => ({ id: trail().dir, name: dirTitle(trail().dir), chip: document.getElementById('dirChip').textContent }));
  assert.ok(dir.chip.includes(dir.name));
  await page.screenshot({ animations: "disabled", path: path.join(out, '03-direction-confirmed.png') });
  await page.locator('.nav-btn[data-view="today"]').click();
  const daily = await page.evaluate(() => api('GET', `/api/daily?uid=${S.uid}`));
  assert.ok(daily.direction?.name); result.daily = { direction: daily.direction, empty_reason: daily.empty_reason, error: daily.error, items: daily.items.length };
  await page.locator('.nav-btn[data-view="read"]').click();
  await page.waitForFunction(() => S.view === 'read');
  assert.ok((await page.locator('#dirChip').innerText()).includes(dir.name));
  await page.screenshot({ animations: "disabled", path: path.join(out, '04-reading-direction.png') });
  check('确认方向→今日／研读', { dir, daily: result.daily, reading: '研读页面显示相同方向状态' });
  await page.locator('.chat-launcher').click(); await ready(page);
  assert.equal(await page.getByRole('button', { name: '关闭', exact: true }).count(), 0);
  assert.equal(await page.locator('.chat-dialog .ws-head,.chat-dialog .chat-profile,.chat-dialog .side-panel').count(), 0);
  const originalCount = await page.locator('.chat-conversation').count();
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  await page.waitForFunction(n => document.querySelectorAll('.chat-conversation').length === n + 1, originalCount); await ready(page);
  const input = page.getByRole('textbox', { name: '对话输入' });
  await input.fill('新会话的独立草稿');
  const switcher = page.getByRole('button', { name: '切换会话', exact: true });
  const before = await page.locator('.chat-scroll').boundingBox(); await switcher.click();
  assert.deepEqual(await page.locator('.chat-scroll').boundingBox(), before);
  await page.screenshot({ animations: "disabled", path: path.join(out, '05-chat-440-list.png') });
  const items = page.locator('.chat-conversation'); assert.ok(await items.count() >= 2);
  await items.nth(1).click(); await ready(page); await input.fill('原会话的独立草稿');
  await switcher.click(); await items.nth(0).click(); await ready(page);
  assert.equal(await input.inputValue(), '新会话的独立草稿');
  await page.getByRole('button', { name: '查看我的画像', exact: true }).click();
  await page.getByRole('heading', { name: '我的', exact: true }).waitFor();
  assert.equal(await page.locator('.chat-dialog').isVisible(), false);
  await page.screenshot({ animations: "disabled", path: path.join(out, '06-my-profile.png') });
  await page.locator('.chat-launcher').click(); assert.equal(await input.inputValue(), '新会话的独立草稿');
  check('浮层结构／弹出列表／草稿／画像入口', '无重复关闭与内层标题；列表不挤压消息区；两会话草稿独立；跳转我的收起，返回草稿保留');
  for (const width of [440, 720, 390]) {
    if (width === 720) await page.getByRole('button', { name: '扩大', exact: true }).click();
    if (width === 390) await page.setViewportSize({ width: 390, height: 844 });
    const rect = await page.locator('.chat-dialog').boundingBox(); assert.equal(Math.round(rect.width), width);
    const metrics = await page.evaluate(() => {
      const buttons = [...document.querySelectorAll('.chat-widget-head > button')].map(n => ({ name: n.getAttribute('aria-label'), y: n.getBoundingClientRect().y, height: n.getBoundingClientRect().height, width: n.getBoundingClientRect().width, font: getComputedStyle(n).fontSize }));
      return { buttons, body: getComputedStyle(document.querySelector('.chat-dialog .bubble')).fontSize, input: getComputedStyle(document.querySelector('.chat-dialog textarea')).fontSize, inputBottom: document.querySelector('.chat-input').getBoundingClientRect().bottom, viewportHeight: innerHeight, pageFits: document.documentElement.scrollWidth <= innerWidth };
    });
    assert.ok(metrics.buttons.every(b => b.height >= 36 && b.width >= 36 && b.font === (b.name === '新对话' ? '14px' : '20px')));
    assert.equal(new Set(metrics.buttons.map(b => b.y)).size, 1); assert.equal(metrics.body, '15px'); assert.equal(metrics.input, '16px'); assert.ok(metrics.inputBottom <= metrics.viewportHeight); assert.equal(metrics.pageFits, true);
    await switcher.click(); await page.screenshot({ animations: "disabled", path: path.join(out, `07-chat-${width}.png`) }); await switcher.click();
    check(`布局${width}px`, { rect, ...metrics });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await say(page, '你记得我的专业、年级、阅读兴趣和每周投入时间吗？', '新会话共享事实；生成中切页', async () => {
    await page.waitForFunction(() => ChatView.isBusy());
    await switcher.click(); await items.nth(1).click();
    assert.equal(await page.getByRole('button', { name: '发送', exact: true }).isDisabled(), true);
    await input.fill('生成期间仍可编辑原会话草稿');
    await page.getByRole('button', { name: '收起', exact: true }).click();
    await page.locator('.nav-btn[data-view="today"]').click();
    await page.locator('.chat-launcher').click();
    assert.equal(await input.inputValue(), '生成期间仍可编辑原会话草稿');
  });
  await switcher.click(); await items.nth(0).click(); await ready(page);
  assert.match(await page.locator('.chat-scroll').innerText(), /信息管理|大二|三小时|3小时/);
  await page.screenshot({ animations: "disabled", path: path.join(out, '08-chat-memory-reply.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ animations: "disabled", path: path.join(out, '09-mobile-memory-reply.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });
  check('生成中切会话／收起／换页', '回复写回发起会话，其他会话仍可编辑草稿且发送禁用；生成完成后可切回查看');
  await page.getByRole('button', { name: '收起', exact: true }).click();
  await page.reload(); await page.locator('#boot').waitFor({ state: 'hidden' });
  assert.equal(await page.locator('.chat-onboarding-content').count(), 0);
  check('已有记录账号刷新', '同一账号已进入应用，刷新不重复首次引导');
  // A second isolated account intentionally skips onboarding, reproducing no_topics.
  const ctx2 = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const p2 = failurePage = await ctx2.newPage(); p2.on('pageerror', e => result.errors.push(e.message));
  const skip = await register(p2, 'dirskip');
  const enter = await p2.request.post(base + '/api/onboard/enter', { headers: { Authorization: 'Bearer ' + skip.token }, data: { uid: skip.uid } }); assert.equal(enter.status(), 200);
  await boot(p2); await p2.locator('.nav-btn[data-view="today"]').click();
  await p2.getByRole('button', { name: '去选方向', exact: true }).first().waitFor();
  await p2.locator('.chat-launcher').click(); await ready(p2);
  await p2.getByRole('button', { name: '去选方向', exact: true }).first().click();
  await p2.getByRole('tab', { name: '方向', exact: true }).waitFor(); assert.equal(await p2.locator('.chat-dialog').isVisible(), false);
  check('截图中的去选方向实际点击', '无方向账号、聊天展开状态下点击今日卡片，进入方向页且收起浮层');
  await p2.getByRole('tab', { name: '教程', exact: true }).click();
  await p2.locator('#dirChip').click();
  await p2.waitForFunction(() => S.view === 'cards' && S.cardsPane === 'direction');
  await p2.waitForFunction(() => document.querySelector('.ws-tab[aria-selected="true"]')?.textContent === '方向');
  assert.equal(await p2.getByRole('tab', { name: '方向', exact: true }).getAttribute('aria-selected'), 'true');
  check('右上角选个方向', '从教程标签切回方向标签');
  assert.deepEqual(result.errors, []); save(); console.log(JSON.stringify({ checks: result.checks.length, turns: result.turns.length, errors: result.errors, out }));
})().catch(async e => { result.failure = e.stack; save(); if (failurePage) { await failurePage.screenshot({ animations: "disabled", path: path.join(out, 'failure.png') }).catch(() => {}); fs.writeFileSync(path.join(out, 'failure-page.txt'), await failurePage.locator('body').innerText().catch(() => '')); } console.error(e); process.exitCode = 1; }).finally(async () => { if (browser) await browser.close(); });
