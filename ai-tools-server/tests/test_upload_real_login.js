#!/usr/bin/env node
/**
 * 真实后端闭环验证（无头浏览器）：
 *   未登录 → 点顶栏「我要上传」→ 弹登录页 → 用真实账号登录 → 自动进入上传表单
 *
 * 跑法：node test_upload_real_login.js [baseUrl]
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
const USER = process.argv[3] || 'guest';
const PASS = process.argv[4] || 'guest@2026';

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

  console.log('[1] 打开页面并清空登录态');
  await page.goto(PAGE, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await new Promise(r => setTimeout(r, 900));
  await page.evaluate(() => { try { localStorage.removeItem('ai_tools_token'); } catch (e) {} });

  console.log('\n[2] 未登录点顶栏「我要上传」→ 应弹登录页');
  await page.evaluate(() => document.querySelector('#navUpload').click());
  await new Promise(r => setTimeout(r, 600));
  const s1 = await page.evaluate(() => ({
    maskOpen: !document.querySelector('#loginMask').hidden,
    hash: location.hash,
    uploadOpen: document.querySelector('#view-upload').classList.contains('open'),
  }));
  check('已弹出登录页', s1.maskOpen, s1);
  check('未进入上传表单', !s1.uploadOpen, s1);

  console.log('\n[3] 切到账号登录 Tab，用真实账号登录（' + USER + '）');
  await page.evaluate(() => document.querySelector('#tabPwd').click());
  await new Promise(r => setTimeout(r, 350));
  await page.evaluate((u, p) => {
    document.querySelector('#loginUser').value = u;
    document.querySelector('#loginPwd').value = p;
    document.querySelector('#loginGo').click();
  }, USER, PASS);
  await new Promise(r => setTimeout(r, 2500));

  const s2 = await page.evaluate(() => ({
    maskOpen: !document.querySelector('#loginMask').hidden,
    err: (document.querySelector('#loginErr') || {}).textContent || '',
    hash: location.hash,
    uploadOpen: document.querySelector('#view-upload').classList.contains('open'),
    userArea: (document.querySelector('#userArea') || {}).textContent || '',
    token: (function(){ try { return localStorage.getItem('ai_tools_token'); } catch(e){ return null; } })(),
  }));
  check('登录成功（无错误提示）', !s2.err, s2);
  check('已拿到 token', !!s2.token, { hasToken: !!s2.token });
  check('登录框已关闭', !s2.maskOpen, s2);
  check('顶栏已显示用户', s2.userArea.trim().length > 0, { userArea: s2.userArea });
  check('登录后自动进入上传表单', s2.uploadOpen && s2.hash === '#upload', s2);

  console.log('\n[4] 回到未登录，再测第 4 屏「上传工具」按钮');
  await page.evaluate(() => { localStorage.removeItem('ai_tools_token'); });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await new Promise(r => setTimeout(r, 1200));
  await page.evaluate(() => document.querySelector('#openUpload').click());
  await new Promise(r => setTimeout(r, 600));
  const s3 = await page.evaluate(() => ({
    maskOpen: !document.querySelector('#loginMask').hidden,
    uploadOpen: document.querySelector('#view-upload').classList.contains('open'),
  }));
  check('「上传工具」也弹登录页', s3.maskOpen, s3);
  check('未进入上传表单', !s3.uploadOpen, s3);

  await browser.close();
  console.log('\n═══════ 结果：通过 ' + pass + ' 项，失败 ' + fail + ' 项 ═══════');
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error('测试异常：', e.message); process.exit(2); });
