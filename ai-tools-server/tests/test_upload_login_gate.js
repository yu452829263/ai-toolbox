#!/usr/bin/env node
/**
 * 本轮两处改动的行为断言（无头浏览器，真实点击）：
 *   ① 搜索面板：点击方框外 → 关闭面板并清空关键词（与 ESC 一致）
 *   ② 上传入口：未登录点「我要上传 / 上传工具 / 上传表单深链」→ 先弹登录页
 *
 * 跑法：node test_upload_login_gate.js [baseUrl]
 */
// puppeteer 解析顺序：① 本地 node_modules  ② 环境变量 PUPPETEER_PATH  ③ 全局安装
// （写死绝对路径会导致换台机器就崩，这里改成多路回退）
function loadPuppeteer() {
  const tries = [];
  if (process.env.PUPPETEER_PATH) tries.push(process.env.PUPPETEER_PATH);
  tries.push('puppeteer');
  try { tries.push(require.resolve('puppeteer')); } catch (e) { /* 忽略 */ }
  for (const t of tries) {
    try { return require(t); } catch (e) { /* 试下一个 */ }
  }
  console.error(
    '✗ 找不到 puppeteer。安装方式：\n' +
    '  npm i -D puppeteer\n' +
    '  或设环境变量 PUPPETEER_PATH 指向 puppeteer 安装目录');
  process.exit(2);
}
const puppeteer = loadPuppeteer();

const BASE = process.argv[2] || 'http://127.0.0.1:8899';
const PAGE = BASE + '/';

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log('  ✅ ' + name); }
  else { fail++; console.log('  ❌ ' + name + (extra !== undefined ? '  → ' + JSON.stringify(extra) : '')); }
  return !!cond;
}

(async () => {
  const browser = await puppeteer.launch({
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--proxy-server=direct://', '--proxy-bypass-list=*'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });

  console.log('[1] 打开页面 ' + PAGE);
  await page.goto(PAGE, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await new Promise(r => setTimeout(r, 900));

  /* ── 强制清成未登录态（本地演示态 API_ON=false，本就不会有 ME） ── */
  await page.evaluate(() => { try { localStorage.removeItem('ai_tools_token'); } catch (e) {} });

  /* ══════════ ① 搜索面板：点方框外关闭 ══════════ */
  console.log('\n[2] 打开搜索面板并输入关键词');
  await page.evaluate(() => document.querySelector('#openSearch').click());
  await new Promise(r => setTimeout(r, 400));
  const openState = await page.evaluate(() => ({
    open: document.querySelector('#searchMask').classList.contains('open'),
    inputVisible: !!document.querySelector('#spInput'),
  }));
  check('搜索面板已打开', openState.open, openState);

  await page.type('#spInput', '会议');
  await new Promise(r => setTimeout(r, 400));
  const typed = await page.evaluate(() => ({
    val: document.querySelector('#spInput').value,
    q: (typeof state !== 'undefined') ? state.q : null,
  }));
  check('关键词已输入', typed.val === '会议', typed);

  console.log('\n[3] 点击方框外（遮罩空白处）→ 应关闭面板');
  // 点遮罩左下角空白：不在 .sp-box 内
  const boxRect = await page.evaluate(() => {
    const r = document.querySelector('.sp-box').getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height };
  });
  await page.mouse.click(Math.max(4, Math.round(boxRect.x / 2)), Math.round(boxRect.y + boxRect.h + 60));
  await new Promise(r => setTimeout(r, 500));
  const afterOutside = await page.evaluate(() => ({
    open: document.querySelector('#searchMask').classList.contains('open'),
    spInputVal: document.querySelector('#spInput').value,
    q: (typeof state !== 'undefined') ? state.q : null,
  }));
  check('点框外后面板已关闭', !afterOutside.open, afterOutside);
  check('点框外后关键词已清空（与 ESC 一致）', afterOutside.spInputVal === '' && afterOutside.q === '', afterOutside);

  console.log('\n[4] 框内点击不应关闭面板');
  await page.evaluate(() => document.querySelector('#openSearch').click());
  await new Promise(r => setTimeout(r, 350));
  const boxRect2 = await page.evaluate(() => {
    const r = document.querySelector('.sp-box').getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height };
  });
  await page.mouse.click(Math.round(boxRect2.x + boxRect2.w / 2), Math.round(boxRect2.y + 12));
  await new Promise(r => setTimeout(r, 350));
  const afterInside = await page.evaluate(() => ({
    open: document.querySelector('#searchMask').classList.contains('open'),
  }));
  check('框内点击面板仍开着', afterInside.open, afterInside);
  await page.keyboard.press('Escape');
  await new Promise(r => setTimeout(r, 300));

  /* ══════════ ② 上传入口：未登录先弹登录 ══════════ */
  console.log('\n[5] 未登录点顶栏「我要上传」→ 应先弹登录页');
  const navGate = await page.evaluate(() => {
    const a = document.querySelector('#navUpload');
    if (!a) return { found: false };
    a.click();
    return { found: true };
  });
  check('找到顶栏「我要上传」', navGate.found, navGate);
  await new Promise(r => setTimeout(r, 500));
  const navState = await page.evaluate(() => ({
    maskOpen: !document.querySelector('#loginMask').hidden,
    tabQrOn: document.querySelector('#tabQr').classList.contains('on'),
    hash: location.hash,
  }));
  check('已弹出登录页', navState.maskOpen, navState);
  check('登录页落在扫码 Tab', navState.tabQrOn, navState);
  check('未登录时未进入上传表单', navState.hash !== '#upload', navState);

  console.log('\n[6] 取消登录 → 不应跳转，且 pending 动作被清掉');
  await page.evaluate(() => document.querySelector('#loginClose').click());
  await new Promise(r => setTimeout(r, 350));
  const cancelled = await page.evaluate(() => ({
    maskOpen: !document.querySelector('#loginMask').hidden,
    hash: location.hash,
    pending: (typeof pendingAfterLogin !== 'undefined') ? pendingAfterLogin : 'undef',
  }));
  check('登录页已关闭', !cancelled.maskOpen, cancelled);
  check('取消后 pending 动作已清空', cancelled.pending === null, cancelled);

  console.log('\n[7] 未登录点第 4 屏「上传工具」→ 也应先弹登录页');
  await page.evaluate(() => {
    // 回到 deck，直接调 openUpload 入口
    const btn = document.querySelector('#openUpload');
    btn.click();
  });
  await new Promise(r => setTimeout(r, 500));
  const btnState = await page.evaluate(() => ({
    maskOpen: !document.querySelector('#loginMask').hidden,
    hash: location.hash,
  }));
  check('「上传工具」也弹了登录页', btnState.maskOpen, btnState);
  check('未登录时未进入上传表单', btnState.hash !== '#upload', btnState);
  await page.evaluate(() => document.querySelector('#loginClose').click());
  await new Promise(r => setTimeout(r, 300));

  console.log('\n[8] 直接改 hash 深链进 #upload → 也应弹登录页');
  await page.evaluate(() => { location.hash = 'upload'; });
  await new Promise(r => setTimeout(r, 600));
  const deepState = await page.evaluate(() => ({
    maskOpen: !document.querySelector('#loginMask').hidden,
    hash: location.hash,
    uploadOpen: document.querySelector('#view-upload').classList.contains('open'),
  }));
  check('深链进上传页也弹登录页', deepState.maskOpen, deepState);
  check('深链时未真正打开上传表单', !deepState.uploadOpen, deepState);

  console.log('\n[9] 模拟登录成功 → 应自动接着进入上传表单');
  const afterLogin = await page.evaluate(() => {
    // 模拟后端返回：注入登录态 → 关登录框 → 触发 pending（与真实登录流程一致）
    ME = { id: 1, name: '测试用户', username: 'tester', role: 'user', dept: '产品技术中心' };
    setToken('fake-token-for-ui-test');
    closeLogin();
    const f = pendingAfterLogin;
    pendingAfterLogin = null;
    if (f) f();
    return { hadPending: typeof f === 'function' };
  });
  await new Promise(r => setTimeout(r, 700));
  const postState = await page.evaluate(() => ({
    uploadOpen: document.querySelector('#view-upload').classList.contains('open'),
    hash: location.hash,
    maskOpen: !document.querySelector('#loginMask').hidden,
  }));
  check('登录前确实有待执行动作', afterLogin.hadPending, afterLogin);
  check('登录成功后自动进入上传表单', postState.uploadOpen && postState.hash === '#upload', postState);

  /* ══════════ ③ 深链刷新：已登录用户不应被误拦 ══════════ */
  console.log('\n[10] 已登录状态直接深链进 #upload → 不应弹登录页');
  const deepLoggedIn = await page.evaluate(async () => {
    location.hash = '#screen-tools';
    await new Promise(r => setTimeout(r, 300));
    location.hash = '#upload';
    await new Promise(r => setTimeout(r, 600));
    return {
      maskOpen: !document.querySelector('#loginMask').hidden,
      uploadOpen: document.querySelector('#view-upload').classList.contains('open'),
    };
  });
  check('已登录深链不被拦（无登录框）', !deepLoggedIn.maskOpen, deepLoggedIn);
  check('已登录深链直接打开上传表单', deepLoggedIn.uploadOpen, deepLoggedIn);

  await browser.close();
  console.log('\n═══════ 结果：通过 ' + pass + ' 项，失败 ' + fail + ' 项 ═══════');
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error('测试异常：', e.message); process.exit(2); });
