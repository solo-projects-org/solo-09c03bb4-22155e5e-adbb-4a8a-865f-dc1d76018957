// 前端合并流程的轻量 DOM 冒烟测试（node tests/test_frontend_merge.js，无需外部依赖）
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(
  path.join(__dirname, '..', 'app', 'static', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

// ---- 极简 DOM stub ----
const handlers = {};
const elements = {};
function makeEl(id) {
  return {
    id, style: {}, dataset: {}, classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, on) { on ? this._set.add(c) : this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    _listeners: {},
    addEventListener(ev, fn) { this._listeners[ev] = fn; },
    dispatch(ev, payload) {
      return this._listeners[ev] && this._listeners[ev](payload || {target: this});
    },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html || ''; },
    set textContent(v) { this._text = v; },
    get textContent() { return this._text || ''; },
    set disabled(v) { this._disabled = v; },
    get disabled() { return !!this._disabled; },
    set value(v) { this._value = v; },
    get value() { return this._value; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    closest() { return { querySelectorAll: () => [], querySelector: () => null }; },
  };
}
const ids = [
  'tabClean', 'tabMerge', 'paneClean', 'paneMerge',
  'fileInput', 'sampleBtn', 'solveBtn', 'fileName', 'exportAllBtn',
  'global-error', 'summary', 'results',
  'baseInput', 'groundInput', 'airlineInput', 'baseName', 'groundName',
  'airlineName', 'mergeSampleBtn', 'analyzeBtn', 'merge-error',
  'mergeSummaryCard', 'mergeChips', 'verifyBtn', 'mergeDownloadBtn',
  'verifyHint', 'verifyPanel', 'mergeResults',
];
ids.forEach(id => { elements[id] = makeEl(id); });

global.document = {
  getElementById: (id) => elements[id] || (elements[id] = makeEl(id)),
  querySelectorAll: (sel) => sel === '.cell-pick' ? pickElements : [],
  createElement: () => ({ click() {}, set href(v) {}, set download(v) {} }),
};
// 模拟渲染后 DOM 中的 select：记录页面通过 addEventListener 绑定的 change 处理
const pickElements = [];
function makePick(dataset) {
  const el = {
    dataset, value: null, _listeners: {},
    addEventListener(ev, fn) { this._listeners[ev] = fn; },
    closest: () => ({
      querySelectorAll: () => [],
      querySelector: () => ({textContent: ''}),
    }),
  };
  pickElements.push(el);
  return el;
}
global.alert = (m) => { global.__lastAlert = m; };

// ---- fetch stub：按路径回放后端真实逻辑（用子进程跑 Python 太重，这里内置夹具）----
const analysisFixture = {
  summary: {rows: 6, total_tails: 4, touched_tails: 3, auto_cells: 13, conflict_cells: 1},
  tails: [
    {tail: 'B-8001', touched: true, segments: [
      {seq: 1, cells: [
        {field: 'dep_local', status: 'unchanged', options: [{source: 'base', value: 'd'}],
         base: 'd', ground: 'd', airline: 'd', chosen: 'd', chosen_source: 'base'},
        {field: 'arr_local', status: 'auto',
         options: [{source: 'base', value: '14:00'}, {source: 'ground', value: '15:00'}],
         base: '14:00', ground: '15:00', airline: '14:00', chosen: '15:00', chosen_source: 'ground'},
      ]},
      {seq: 2, cells: [
        {field: 'dep_local', status: 'auto',
         options: [{source: 'base', value: '16:00'}, {source: 'airline', value: '14:30'}],
         base: '16:00', ground: '16:00', airline: '14:30', chosen: '14:30', chosen_source: 'airline'},
      ]},
    ]},
    {tail: 'B-8002', touched: true, segments: [
      {seq: 1, cells: [
        {field: 'arr_local', status: 'conflict',
         options: [
           {source: 'base', value: '12:00'},
           {source: 'ground', value: '11:30'},
           {source: 'airline', value: '12:30'}],
         base: '12:00', ground: '11:30', airline: '12:30', chosen: null, chosen_source: null},
      ]},
    ]},
    {tail: 'B-8003', touched: true, segments: [{seq: 1, cells: []}, {seq: 2, cells: []}]},
    {tail: 'B-8004', touched: false, segments: [{seq: 1, cells: []}]},
  ],
};

let lastVerifyChoices = null;
let lastExportChoices = null;
global.fetch = async (url, opts) => {
  const formEntries = [...opts.body.entries()];
  const choicesRaw = (formEntries.find(([k]) => k === 'choices') || [])[1];
  const json = (code, body) => ({ok: code < 400, status: code,
    json: async () => body, headers: {get: () => ''}, blob: async () => ({})});
  if (url === '/api/merge/analyze') {
    return json(200, analysisFixture);
  }
  if (url === '/api/merge/verify') {
    lastVerifyChoices = JSON.parse(choicesRaw || '[]');
    const picked = lastVerifyChoices.find(
      c => c.tail === 'B-8002' && c.field === 'arr_local');
    const unresolved = picked && picked.source ? [] : [
      {tail: 'B-8002', seq: 1, field: 'arr_local'}];
    return json(200, {
      unresolved_conflicts: unresolved,
      all_conflicts_resolved: unresolved.length === 0,
      affected_tails: ['B-8001', 'B-8002', 'B-8003'],
      checks: unresolved.length ? [] : [
        {tail: 'B-8001', status: 'no_solution', unique: false,
         reasons: ['航段 seq=2 无法衔接：起飞早于上一段落地']},
        {tail: 'B-8002', status: 'unique', unique: true, reasons: []},
        {tail: 'B-8003', status: 'unique', unique: true, reasons: []},
      ],
      all_unique: false,
      download_allowed: false,
    });
  }
  if (url === '/api/merge/export') {
    lastExportChoices = JSON.parse(choicesRaw || '[]');
    return json(200, {});
  }
  if (url.startsWith('/api/merge/sample/')) {
    return {ok: true, blob: async () => ({})};
  }
  throw new Error('unexpected fetch ' + url);
};

// FormData stub
global.FormData = class {
  constructor() { this._entries = []; }
  append(k, v) { this._entries.push([k, v]); }
  entries() { return this._entries; }
};
global.URL = {createObjectURL: () => 'blob:x', revokeObjectURL() {}};

// 按夹具里所有非 unchanged 单元格预建伪 select，页面渲染时会为它们绑定 change
[
  {tail: 'B-8001', seq: '1', field: 'arr_local'},
  {tail: 'B-8001', seq: '2', field: 'dep_local'},
  {tail: 'B-8002', seq: '1', field: 'arr_local'},
].forEach(makePick);

// 执行页面脚本
new Function(script)();

const assert = (cond, name) => {
  console.log((cond ? 'PASS' : 'FAIL') + ' - ' + name);
  if (!cond) process.exit(1);
};

(async () => {
  // 1) 三份文件未齐时比对按钮禁用
  const fire = (id, ev, payload) => elements[id].dispatch(ev, payload);
  const file = (name) => ({name});
  fire('baseInput', 'change', {target: {files: [file('base.csv')]}});
  assert(elements.analyzeBtn.disabled, '仅选底稿时比对按钮禁用');
  fire('groundInput', 'change', {target: {files: [file('g.csv')]}});
  fire('airlineInput', 'change', {target: {files: [file('a.csv')]}});
  assert(!elements.analyzeBtn.disabled, '三份齐后比对按钮可用');

  // 2) 分析
  await elements.analyzeBtn.dispatch('click');
  assert(elements.mergeSummaryCard.style.display === 'block', '分析后展示汇总卡');
  assert(elements.mergeResults.innerHTML.includes('B-8001'), '渲染机尾 B-8001');
  assert(elements.mergeResults.innerHTML.includes('冲突 · 待选'), '渲染冲突标记');
  assert(elements.mergeResults.innerHTML.includes('三方一致 · 不重跑'), '渲染未改动标记');
  assert(elements.mergeDownloadBtn.disabled, '核对前下载禁用');

  // 3) 未选完冲突先核对：后端返回未解决 → 仍禁止下载
  await elements.verifyBtn.dispatch('click');
  assert(elements.mergeDownloadBtn.disabled, '冲突未选完核对后仍禁止下载');
  assert(elements.verifyPanel.innerHTML.includes('冲突未选值'), '展示未选冲突列表');

  // 4) 通过真实 change 事件驱动页面 onPickChange：给 B-8002 冲突三选一。
  //    页面 renderMergeAnalysis 已对 pickElements 中的伪 select 绑定 change 监听。
  const conflictPick = pickElements.find(
    sel => sel.dataset.tail === 'B-8002' && sel.dataset.field === 'arr_local');
  assert(!!conflictPick && typeof conflictPick._listeners.change === 'function',
    '页面为冲突单元格绑定了 change 监听');
  conflictPick.value = 'base';
  conflictPick._listeners.change({target: conflictPick});

  const t = analysisFixture.tails.find(x => x.tail === 'B-8002');
  const cell = t.segments[0].cells[0];
  assert(cell.chosen_source === 'base' && cell.chosen === '12:00',
    'onPickChange：冲突单元格写入选值');

  await elements.verifyBtn.dispatch('click');
  assert(lastVerifyChoices.some(c => c.tail === 'B-8002' && c.source === 'base'),
    '核对请求携带值班员选值');
  assert(elements.verifyPanel.innerHTML.includes('B-8001'), '列出未通过机尾 B-8001');
  assert(elements.verifyPanel.innerHTML.includes('无法衔接'), '列出未通过原因');
  assert(elements.mergeDownloadBtn.disabled, 'B-8001 非唯一解 → 下载仍禁用');

  // 5) 改选（base → airline）后签名失效：未重新核对直接下载 → 前端兜底拦截
  conflictPick.value = 'airline';
  conflictPick._listeners.change({target: conflictPick});
  assert(cell.chosen_source === 'airline' && cell.chosen === '12:30',
    'onPickChange：改选覆盖原值');
  await elements.mergeDownloadBtn.dispatch('click');
  assert(global.__lastAlert && global.__lastAlert.includes('重新核对'),
    '改选后未重新核对 → 前端拦截下载');

  // 6) 重新核对后若后端放行，则下载按钮可用
  //    （把夹具改为全机尾唯一解，模拟冲突已消解且链可衔接的情形）
  const origFetch = global.fetch;
  global.fetch = async (url, opts) => {
    if (url === '/api/merge/verify') {
      const formEntries = [...opts.body.entries()];
      lastVerifyChoices = JSON.parse(
        (formEntries.find(([k]) => k === 'choices') || [])[1] || '[]');
      return {ok: true, status: 200, json: async () => ({
        unresolved_conflicts: [], all_conflicts_resolved: true,
        affected_tails: ['B-8001', 'B-8002', 'B-8003'],
        checks: [
          {tail: 'B-8001', status: 'unique', unique: true, reasons: []},
          {tail: 'B-8002', status: 'unique', unique: true, reasons: []},
          {tail: 'B-8003', status: 'unique', unique: true, reasons: []},
        ],
        all_unique: true, download_allowed: true,
      }), headers: {get: () => ''}, blob: async () => ({})};
    }
    return origFetch(url, opts);
  };
  await elements.verifyBtn.dispatch('click');
  assert(!elements.mergeDownloadBtn.disabled, '核对全通过 → 下载按钮放行');
  assert(lastVerifyChoices.some(c => c.tail === 'B-8002' && c.source === 'airline'),
    '重新核对携带最新选值');

  console.log('\n前端合并流程冒烟全部通过 ✅');
})();
