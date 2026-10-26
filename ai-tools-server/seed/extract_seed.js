#!/usr/bin/env node
/* 从前端 demo 的 TOOLS 数组导出种子 JSON（仅开发用；部署运行不需要 node）
   用法：node tools/extract_seed.js [源html] [输出json]
   典型：node tools/extract_seed.js ../demo4-卡片风滚动分页.html seed/seed_tools.json */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = process.argv[2] || path.join(__dirname, '..', '..', 'demo4-卡片风滚动分页.html');
const out = process.argv[3] || path.join(__dirname, '..', 'seed', 'seed_tools.json');

const html = fs.readFileSync(src, 'utf8');
const m0 = html.match(/\b(?:let|const|var)\s+TOOLS\s*=\s*\[/);
if (!m0) { console.error('未找到 TOOLS 数组'); process.exit(1); }
const start = m0.index;
const arrStart = m0.index + m0[0].length - 1;
// 扫描配对的方括号，跳过字符串里的括号
let depth = 0, end = -1, quote = null;
for (let i = arrStart; i < html.length; i++) {
  const ch = html[i];
  if (quote) {
    if (ch === '\\') { i++; continue; }
    if (ch === quote) quote = null;
    continue;
  }
  if (ch === '"' || ch === "'" || ch === '`') { quote = ch; continue; }
  if (ch === '[') depth++;
  else if (ch === ']') { depth--; if (depth === 0) { end = i + 1; break; } }
}
if (end < 0) { console.error('TOOLS 数组未闭合'); process.exit(1); }

const literal = html.slice(arrStart, end);
const TOOLS = vm.runInNewContext('(' + literal + ')');

const tools = TOOLS.map(t => ({
  name: t.name,
  dept: t.dept,
  type: t.type,
  desc: t.desc,
  link: t.link || null,
  detail: t.detail || { intro: '', usage: [], highlights: [] },
  files: (t.files || []).map(f => ({ name: f.name, size: f.size || 0, type: f.type || '', url: f.url || null })),
  added_at: t.addedAt || '',
  is_new: !!t.isNew,
  downloads: t.downloads || 0,
  likes: t.likes || 0,
  favorites: 0
}));

fs.writeFileSync(out, JSON.stringify(tools, null, 2) + '\n', 'utf8');
console.log('导出 %d 个工具 → %s' % (tools.length, out));
console.log('带附件的工具：%d' % tools.filter(t => t.files.length).length);
