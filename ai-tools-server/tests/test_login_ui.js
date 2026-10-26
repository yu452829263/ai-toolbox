#!/usr/bin/env node
/**
 * 登录弹层 UI 行为断言（无头浏览器，真实点击验证）
 *
 * 验证用户明确要求的两点：
 *   ① 点击「登录」入口 → 弹层自动落在「扫码登录」Tab
 *   ② 「扫码登录 / 账号登录」两个 Tab 切换时，卡片尺寸固定不跳动
 *
 * 附带验证：切换到账号登录再切回扫码，二维码面板仍可见。
 */
const path = require('path');
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

const BASE = process.argv[2] || 'http://127.0.0.1:8888';
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
  await new Promise(r => setTimeout(r, 800));

  // 关掉可能自动弹出的其它遮罩，只留登录弹层
  console.log('\n[2] 点击顶栏「登录」→ 应自动落在扫码登录 Tab');
  const clicked = await page.evaluate(() => {
    const b = document.querySelector('#uaLogin');
    if (!b) return false;
    b.click();
    return true;
  });
  check('顶栏存在「登录」按钮', clicked);
  await new Promise(r => setTimeout(r, 500));

  const state1 = await page.evaluate(() => {
    const mask = document.querySelector('#loginMask');
    const qr = document.querySelector('#paneQr');
    const pwd = document.querySelector('#panePwd');
    const cs = (el) => el ? getComputedStyle(el) : null;
    const card = document.querySelector('#loginMask .login-card');
    return {
      maskOpen: !!mask && !mask.hidden,
      tabQrOn: document.querySelector('#tabQr').classList.contains('on'),
      tabPwdOn: document.querySelector('#tabPwd').classList.contains('on'),
      qrOff: qr ? qr.classList.contains('off') : null,
      pwdOff: pwd ? pwd.classList.contains('off') : null,
      qrVisible: qr ? cs(qr).visibility : null,
      pwdVisible: pwd ? cs(pwd).visibility : null,
      cardW: card ? card.getBoundingClientRect().width : null,
      cardH: card ? card.getBoundingClientRect().height : null,
    };
  });
  check('登录弹层已打开', state1.maskOpen, state1);
  check('扫码 Tab 高亮', state1.tabQrOn, state1);
  check('账号 Tab 未高亮', !state1.tabPwdOn, state1);
  check('扫码面板可见', state1.qrVisible === 'visible', state1);
  check('账号面板已隐藏', state1.pwdVisible === 'hidden', state1);

  console.log('\n[3] 切到「账号登录」→ 卡片尺寸应与扫码时完全一致');
  await page.evaluate(() => document.querySelector('#tabPwd').click());
  await new Promise(r => setTimeout(r, 400));
  const state2 = await page.evaluate(() => {
    const card = document.querySelector('#loginMask .login-card');
    const panes = document.querySelector('.lg-panes');
    const qr = document.querySelector('#paneQr');
    const pwd = document.querySelector('#panePwd');
    return {
      cardW: card.getBoundingClientRect().width,
      cardH: card.getBoundingClientRect().height,
      panesH: panes.getBoundingClientRect().height,
      qrVisible: getComputedStyle(qr).visibility,
      pwdVisible: getComputedStyle(pwd).visibility,
      tabPwdOn: document.querySelector('#tabPwd').classList.contains('on'),
    };
  });
  check('账号 Tab 已高亮', state2.tabPwdOn, state2);
  check('账号面板可见', state2.pwdVisible === 'visible', state2);
  check('扫码面板已隐藏', state2.qrVisible === 'hidden', state2);
  check('卡片宽度不变', Math.abs(state2.cardW - state1.cardW) < 0.5, { qr: state1.cardW, pwd: state2.cardW });
  check('卡片高度不变（切换不跳动）', Math.abs(state2.cardH - state1.cardH) < 0.5, { qr: state1.cardH, pwd: state2.cardH });

  console.log('\n[4] 再切回「扫码登录」→ 卡片尺寸仍应一致');
  await page.evaluate(() => document.querySelector('#tabQr').click());
  await new Promise(r => setTimeout(r, 400));
  const state3 = await page.evaluate(() => {
    const card = document.querySelector('#loginMask .login-card');
    return { cardW: card.getBoundingClientRect().width, cardH: card.getBoundingClientRect().height };
  });
  check('回到扫码后宽度一致', Math.abs(state3.cardW - state1.cardW) < 0.5, { a: state1.cardW, b: state3.cardW });
  check('回到扫码后高度一致', Math.abs(state3.cardH - state1.cardH) < 0.5, { a: state1.cardH, b: state3.cardH });

  console.log('\n[5] 评论区「登录」链接 → 也应落在扫码 Tab');
  // 评论区在工具详情页里：先进详情页，再找 .td-login-link
  await page.evaluate(() => { document.querySelector('#loginMask').hidden = true; });
  await page.evaluate(() => {
    const link = document.querySelector('.tool-card[data-id]') || document.querySelector('[data-id]');
    if (link) link.click();
  });
  await new Promise(r => setTimeout(r, 900));
  await page.evaluate(() => {
    const lg = document.querySelector('.td-login-link');
    if (lg) { lg.id = '__probe_login'; lg.click(); }
  });
  await new Promise(r => setTimeout(r, 400));
  const state4 = await page.evaluate(() => {
    const el = document.querySelector('#__probe_login');
    if (!el) return { found: false };
    const mask = document.querySelector('#loginMask');
    return {
      found: true,
      maskOpen: !mask.hidden,
      tabQrOn: document.querySelector('#tabQr').classList.contains('on'),
      qrVisible: getComputedStyle(document.querySelector('#paneQr')).visibility,
      pwdVisible: getComputedStyle(document.querySelector('#panePwd')).visibility,
    };
  });
  if (state4.found) {
    check('评论区登录入口已找到并点击', true);
    check('评论区入口也弹到扫码 Tab', state4.tabQrOn && state4.qrVisible === 'visible', state4);
    check('评论区入口账号面板隐藏', state4.pwdVisible === 'hidden', state4);
  } else {
    console.log('  ⚠️  评论区未找到「登录」链接元素，跳过（不影响主结论）');
  }

  await browser.close();
  console.log('\n═══════ 结果：通过 ' + pass + ' 项，失败 ' + fail + ' 项 ═══════');
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error('测试异常：', e.message); process.exit(2); });
