// Word-tick editing affordances on the waveform.
//
// Reported as "优化这一部分的UI，让操作与用户查看编辑逐字歌词更加方便" with the two
// lyric tracks circled. Before this, editing a syllable there was blind: the
// ticks were 1px lines with an INVISIBLE ±6px grab zone, hovering only changed
// the cursor, and the only numeric feedback was one absolute time printed at the
// top of the canvas, far from the cursor and without the word's name.
//
// What is asserted here is the feedback and the targeting, plus one regression
// guard that matters more than the rest: the pointer state must NOT be stored in
// `hover`, which already belongs to the table-row highlight (the v0.10.0
// "one field, two writers" bug).
const fs = require('fs');
const path = require('path');
const web = path.join(__dirname, '..', 'saltlrc', 'web');

let checks = 0, failures = 0;
function check(name, actual, expected) {
  checks++;
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    failures++;
    console.log('FAIL %s\n  got      %s\n  expected %s', name, JSON.stringify(actual), JSON.stringify(expected));
  } else console.log('ok   %s', name);
}

// ---------- stubs ----------
const cache = {};
let seq = 0;
let rects = [];        // every rect() call of the current paint
let texts = [];        // every fillText() call of the current paint
function ctx2d() {
  return new Proxy({
    measureText: (t) => ({width: String(t).length * 7}),
    canvas: {width: 0, height: 0},
    rect: (x, y, w, h) => rects.push({x, y, w, h}),
    fillText: (text, x, y) => texts.push({text, x, y}),
  }, {
    get(t, prop) { if (prop in t) return t[prop]; return () => {}; },
    set(t, prop, v) { t[prop] = v; return true; },
  });
}
function element(id) {
  if (cache[id]) return cache[id];
  const el = {
    id, style: {setProperty() {}}, dataset: {}, hidden: false, value: '', textContent: '', innerHTML: '',
    disabled: false, max: 300, children: [],
    parentElement: {clientWidth: 900, clientHeight: 300, appendChild() {}},
    clientWidth: 900, clientHeight: 300,
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    append(...items) { this.children.push(...items); },
    replaceChildren() { this.children = []; },
    addEventListener() {}, removeEventListener() {}, setPointerCapture() {},
    closest() { return null; }, scrollIntoView() {}, focus() {}, select() {}, click() {},
    pause() {}, load() {}, play() { return Promise.resolve(); },
    removeAttribute() {}, setAttribute() {}, getAttribute() { return null; },
    getContext: () => ctx2d(),
    querySelector: () => element('q:' + id + ':' + (seq++)),
    querySelectorAll: () => [],
  };
  cache[id] = el;
  return el;
}
global.localStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.sessionStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.window = {devicePixelRatio: 1, addEventListener() {}, fetch: () => Promise.resolve({ok: true, status: 200, json: async () => ({}), text: async () => ''})};
global.document = {
  getElementById: element,
  createElement: (tag) => element('new:' + tag + ':' + (seq++)),
  querySelector: () => element('qs:' + (seq++)),
  querySelectorAll: () => [],
  addEventListener() {},
  body: element('body'),
};
global.ResizeObserver = class { observe() {} };
let rafQueue = [];
global.requestAnimationFrame = (fn) => { rafQueue.push(fn); return rafQueue.length; };
global.cancelAnimationFrame = () => {};
global.fetch = () => Promise.reject(new Error('offline test'));
global.navigator = {};
const flush = () => { const q = rafQueue; rafQueue = []; q.forEach((fn) => fn()); };
const load = (file) => fs.readFileSync(path.join(web, file), 'utf8');

const listeners = new Map();
const waveCanvas = {
  width: 0, height: 0, style: {setProperty() {}}, clientWidth: 900,
  parentElement: {clientWidth: 900},
  getBoundingClientRect: () => ({left: 0, top: 0}),
  addEventListener(type, fn) { (listeners.get(type) || listeners.set(type, []).get(type)).push(fn); },
  setPointerCapture() {},
  getContext: () => ctx2d(),
};
const emit = (type, event) => (listeners.get(type) || []).forEach((fn) => fn(event));

const SaltWaveform = new Function(load('theme.js') + '\n' + load('waveform.js') + '\nreturn SaltWaveform;')();
const seen = {pointer: [], edit: [], select: [], changed: []};
const wave = new SaltWaveform(waveCanvas, {
  seek() {}, change: (c) => seen.changed.push(c), zoom() {}, pan() {},
  pointer: (info) => seen.pointer.push(info),
  edit: (index, field, what) => seen.edit.push([index, field, what]),
  select: (mark) => seen.select.push(mark),
});
wave.duration = 180;
// The line under test: 句首 5000, words A@5000 B@5500 C@6000, 句尾 7000.
// At the default 90px/s, x = ms/1000*90 → 450 / 495 / 540 / 630.
wave.setDocument({groups: [{
  start: 5000,
  main: {text: 'ABC', start: 5000, end: 7000,
         words: [{text: 'A', start: 5000}, {text: 'B', start: 5500}, {text: 'C', start: 6000}]},
  translation: {text: '译', start: 5000, end: null, words: []},
}]});
flush();

const BAND_MAIN = 158;
// ---- 1. targeting: the grab zone is one shared constant -------------------
// 半径本身是一个类常量（不许散落到别处），并且**够大到能抓住一条细线**。
// 断言跟着常量走，不写死数字：8 还是 6 由那一处决定，测试只钉住"它是个常量且不小于 6"。
check('刻度抓取半径是类常量（不小于 6px）', SaltWaveform.TICK_GRAB >= 6, true);
check('命中判定走 hitAt（单一入口）', typeof wave.hitAt, 'function');
check('刻度左侧在半径上命中', wave.hitAt(495 - SaltWaveform.TICK_GRAB, BAND_MAIN + 10)?.word, 1);
check('刻度右侧在半径上命中', wave.hitAt(495 + SaltWaveform.TICK_GRAB, BAND_MAIN + 10)?.word, 1);
check('超出抓取半径不命中该刻度', wave.hitAt(495 + SaltWaveform.TICK_GRAB + 2, BAND_MAIN + 10)?.word ?? null, null);
check('轨道带之外不命中', wave.hitAt(495, BAND_MAIN - 20) ?? null, null);

// ---- 2. hovering names the syllable and its time -------------------------
seen.pointer.length = 0;
emit('pointermove', {clientX: 495, clientY: BAND_MAIN + 10});
const hovered = seen.pointer[seen.pointer.length - 1];
check('悬停报告的是第 2 个字', hovered && hovered.position, 2);
check('悬停报告字的总数', hovered && hovered.total, 3);
check('悬停报告字面', hovered && hovered.text, 'B');
check('悬停报告该字的时间', hovered && hovered.time, 5500);
check('悬停报告所在轨', hovered && hovered.field, 'main');

// ---- 3. THE regression guard: pointer state is not the row-hover slot ------
// `hover` drives the table-row highlight and is written by setHover(). Sharing
// it is exactly the bug that froze the highlight once before.
wave.setHover(2);
emit('pointermove', {clientX: 495, clientY: BAND_MAIN + 10});
check('悬停刻度不改动 this.hover（表格行高亮槽）', wave.hover, 2);
check('悬停刻度写入独立的 this.pointer', wave.pointer && wave.pointer.word, 1);
wave.setHover(null);

// ---- 4. the overlay draws the grab affordances ---------------------------
rects = [];
wave.pointer = {index: 0, field: 'main', kind: 'word', word: 1, x: 495, y: BAND_MAIN - 5, height: 42};
wave.paintPointer(ctx2d());
// Handles for the words AFTER the first (the first tick is the line start, which
// has its own 句首 handle) → one rect per remaining word.
// 抓手尺寸不再写死：悬停的那一个刻意画得更大（宽 4、高 11），其余是 2×7。
const handles = rects.filter((r) => r.h >= 7);
check('为悬停行的每个逐字刻度画了抓手', handles.length, 2);
check('抓手都画在带顶上方', handles.every((r) => r.y <= BAND_MAIN - 7), true);
check('悬停的那一个抓手更宽（能抓的那个更清楚）',
      Math.max.apply(null, handles.map((r) => r.w)) >= 4, true);

// ---- 5. clicking selects, and only a real tick can be nudged -------------
seen.select.length = 0;
emit('pointerdown', {clientX: 495, clientY: BAND_MAIN + 10, pointerId: 1});
check('按下刻度即选中', wave.selected && wave.selected.kind, 'word');
check('按下刻度会通知选中回调', seen.select.length, 1);
emit('pointerup', {clientX: 495, clientY: BAND_MAIN + 10, pointerId: 1});
check('原地点击不产生编辑（沿用 v0.11.3 规则）', seen.changed.length, 0);
check('选中状态在点击后保留（供键盘微调）', wave.selected && wave.selected.word, 1);

// Clicking empty space clears the selection.
emit('pointerdown', {clientX: 200, clientY: BAND_MAIN + 10, pointerId: 2});
emit('pointerup', {clientX: 200, clientY: BAND_MAIN + 10, pointerId: 2});
check('点空白处取消选中', wave.selected, null);

// ---- 6. dragging a tick reports the live value and the delta --------------
emit('pointerdown', {clientX: 495, clientY: BAND_MAIN + 10, pointerId: 3});
seen.pointer.length = 0;
emit('pointermove', {clientX: 540, clientY: BAND_MAIN + 10, pointerId: 3});
const dragging = seen.pointer[seen.pointer.length - 1];
check('拖动中给出字面', dragging && dragging.text, 'B');
check('拖动中给出第几个字', dragging && dragging.position, 2);
check('拖动中给出实时时间', dragging && dragging.time, 6000);
check('拖动中给出偏移量', dragging && dragging.delta, 500);
emit('pointerup', {clientX: 540, clientY: BAND_MAIN + 10, pointerId: 3});
check('松开把改动交给上层', seen.changed.length, 1);
check('松开时最后的读数仍带偏移量',
      seen.pointer[seen.pointer.length - 1]?.delta, 500);

// ---- 7. double-click: a tick opens the word editor, the body the text -----
seen.edit.length = 0;
emit('dblclick', {clientX: 495, clientY: BAND_MAIN + 10});
check('双击刻度打开逐字编辑器', seen.edit, [[0, 'main', 'words']]);
seen.edit.length = 0;
emit('dblclick', {clientX: 520, clientY: BAND_MAIN + 10});
check('双击块体仍改文字', seen.edit, [[0, 'main', 'text']]);

// ---- 8. the readout disappears with the cursor ---------------------------
seen.pointer.length = 0;
emit('pointerleave', {});
check('移出画布时清掉读数', seen.pointer[seen.pointer.length - 1], null);
check('移出画布时清掉指针状态', wave.pointer, null);

// ---- 9. the end/start handles describe themselves ------------------------
emit('pointermove', {clientX: 450, clientY: BAND_MAIN + 10});
check('悬停句首给出“句首”提示', seen.pointer[seen.pointer.length - 1]?.hint, '句首');
emit('pointermove', {clientX: 630, clientY: BAND_MAIN + 10});
check('悬停句尾给出“句尾”提示', seen.pointer[seen.pointer.length - 1]?.hint, '句尾');

// ---- 10. Alt-drag carries the followers -----------------------------------
// Opening a gap between two syllables is impossible without moving the ones
// after them (timings must increase), so "separate these" used to mean shoving
// words right-to-left, one at a time. Alt makes it one gesture, and it is
// opt-in: silently moving times the user already set would be worse.
seen.changed.length = 0;
emit('pointerdown', {clientX: 495, clientY: BAND_MAIN + 10, pointerId: 9, altKey: true});
check('按住 Alt 按下即进入推动模式', wave.drag && wave.drag.ripple, true);
seen.pointer.length = 0;
emit('pointermove', {clientX: 540, clientY: BAND_MAIN + 10, pointerId: 9, altKey: true});
const rippleTip = seen.pointer[seen.pointer.length - 1];
check('推动模式的读数标记了模式', rippleTip && rippleTip.ripple, true);
check('推动模式的读数说明会带走几个字', rippleTip && rippleTip.followers, 1);
emit('pointerup', {clientX: 540, clientY: BAND_MAIN + 10, pointerId: 9, altKey: true});
check('松手把推动模式一并上报', seen.changed[seen.changed.length - 1]?.ripple, true);

// Releasing Alt mid-drag must downgrade the gesture, not leave it latched.
emit('pointerdown', {clientX: 495, clientY: BAND_MAIN + 10, pointerId: 10, altKey: true});
check('按下时带着 Alt', wave.drag.ripple, true);
emit('pointermove', {clientX: 505, clientY: BAND_MAIN + 10, pointerId: 10, altKey: false});
check('拖动中松开 Alt 即退出推动模式', wave.drag.ripple, false);
emit('pointerup', {clientX: 505, clientY: BAND_MAIN + 10, pointerId: 10, altKey: false});

// Without Alt nothing is pushed.
emit('pointerdown', {clientX: 495, clientY: BAND_MAIN + 10, pointerId: 11});
check('普通拖动不进入推动模式', wave.drag.ripple, false);
emit('pointerup', {clientX: 495, clientY: BAND_MAIN + 10, pointerId: 11});

// A block drag is never a ripple: the whole line moves anyway.
emit('pointerdown', {clientX: 520, clientY: BAND_MAIN + 10, pointerId: 12, altKey: true});
check('整句拖动不受 Alt 影响', wave.drag.kind, 'block');
emit('pointerup', {clientX: 520, clientY: BAND_MAIN + 10, pointerId: 12, altKey: true});

// ---- 11. the cells must be VISIBLY separate -------------------------------
// Reported as "绿色提示框还是连在一起，能不能分开". A line used to be drawn as ONE
// rect with 1px strokes at the word boundaries, so it read as a single bar. The
// seam is the thing asserted here: a gap of at least 1px between cells, measured
// from the real rect() calls (h === SaltWaveform.CELL_HEIGHT is the band cell; highlight outlines are
// 34 tall and the hover handles 7, so they cannot be confused for cells).
const cellsIn = (calls, band) => calls.filter((r) => r.h === SaltWaveform.CELL_HEIGHT && r.y === band)
                                          .sort((a, b) => a.x - b.x);
wave.pps = 100;                       // 1px = 10ms
wave.invalidate();
flush();
rects = []; texts = [];          // clear BOTH: texts accumulates across paints
wave.invalidate();
flush();
const cells = cellsIn(rects, BAND_MAIN);
check('一行 3 个字画成 3 个格子（不再是 1 条）', cells.length, 3);
if (cells.length === 3) {
  const gap1 = cells[1].x - (cells[0].x + cells[0].w);
  const gap2 = cells[2].x - (cells[1].x + cells[1].w);
  // This file's check() is (name, actual, expected) — assert the exact seam width.
  // Cells here are 50px wide (pps=100, 500ms words) so they get the roomy seam.
  check('第 1、2 格之间的缝隙是 6px', gap1, 6);
  check('第 2、3 格之间的缝隙是 6px', gap2, 6);
  check('格子宽度之和小于整行宽度（确实分开了）',
        cells.reduce((sum, c) => sum + c.w, 0) < cells[2].x + cells[2].w - cells[0].x, true);
}
check('格子都落在轨道带里且高度一致',
      cells.length > 0 && cells.every((c) => c.h === SaltWaveform.CELL_HEIGHT), true);
const bandTexts = texts.filter((t) => t.y === BAND_MAIN + 21);
check('每个格子都画了自己的标签', bandTexts.length, 3);
check('标签都落在自己的格子范围内',
      bandTexts.every((t) => cells.some((c) => t.x >= c.x && t.x <= c.x + c.w + 2)), true);

// A track with no word timings stays one block: the translation is a whole line,
// and cutting it into cells would be meaningless.
wave.setDocument({groups: [{
  start: 5000,
  main: {text: 'ab', start: 5000, end: 7000,
         words: [{text: 'a', start: 5000}, {text: 'b', start: 6000}]},
  translation: {text: '译文', start: 5000, end: null, words: []},
}]});
wave.invalidate();
flush();
rects = []; texts = [];
wave.invalidate();
flush();
check('主轨仍是 2 个格子', cellsIn(rects, BAND_MAIN).length, 2);
check('没有逐字时间的译文轨仍是一整块', cellsIn(rects, 225).length, 1);

// ---- 12. 整句（普通 LRC）的行块之间也必须有缝 -----------------------------
// Reported as "我要你改的是整句状态下的歌词提示框，两句话连在一起我压根不知道
// 普通LRC编辑时长到哪里了". Plain LRC has no 句尾, so a block is drawn up to the NEXT
// line's start — and two consecutive blocks then abut exactly, reading as one
// continuous bar with no visible end for either line.
// Times are kept inside the first viewport (900px at pps=100 = 9s) so both lines
// are actually painted.
wave.pps = 100;
wave.offset = 0;
wave.setDocument({groups: [
  {start: 1000, main: {text: 'first', start: 1000, end: null, words: []},
   translation: {text: '', start: 1000, end: null, words: []}},
  {start: 3000, main: {text: 'second', start: 3000, end: null, words: []},
   translation: {text: '', start: 3000, end: null, words: []}},
]});
wave.invalidate();
flush();
rects = []; texts = [];
wave.invalidate();
flush();
const lines = cellsIn(rects, BAND_MAIN);
check('两行整句歌词画成 2 个块', lines.length, 2);
if (lines.length === 2) {
  const gap = lines[1].x - (lines[0].x + lines[0].w);
  check('两个整句块之间的缝隙是 10px', gap, 10);
  check('第一个块不再延伸到下一行的句首', lines[0].x + lines[0].w < lines[1].x, true);
}

// ---- 12b. 有逐字时间时，句与句之间的缝不能按"最后一个字"算 --------------
// The gap between two LINES used to be whatever the final word's cell happened
// to earn from the graduated seam: a line ending on a short syllable left 3px
// (or 1px), so two sentences read as one bar again — the "整句歌词粘连" report
// came back for every song that had word timings, and only stayed fixed for
// lines without them. pps=100 → 100ms = 10px, so the last cell is 20px wide
// (< 28) and would collapse the gap to 3px without the line-level clamp.
wave.setDocument({groups: [
  {start: 1000, main: {text: 'first line', start: 1000, end: null,
                       words: [{text: 'fir', start: 1000}, {text: 'st', start: 2600},
                               {text: 'line', start: 2800}]},
   translation: {text: '', start: 1000, end: null, words: []}},
  {start: 3000, main: {text: 'second', start: 3000, end: null, words: []},
   translation: {text: '', start: 3000, end: null, words: []}},
]});
wave.invalidate();
flush();
rects = []; texts = [];
wave.invalidate();
flush();
const worded = cellsIn(rects, BAND_MAIN);
// 3 个字格 + 第 2 行的块
check('带逐字的行：字格与下一行的块都在', worded.length, 4);
if (worded.length === 4) {
  const lastCell = worded[2];                    // 'line' — 20px wide, ends the line
  const nextLine = worded[3];
  const lineGap = nextLine.x - (lastCell.x + lastCell.w);
  check('最后一个字很窄时，句间缝隙仍是 10px（不是 3px）', lineGap, 10);
  check('句内格子之间仍有缝（3px，按格子宽度分级）',
        worded[2].x - (worded[1].x + worded[1].w), 3);
}

// A line WITH an explicit 句尾 stops at its own end, not at the next line.
wave.setDocument({groups: [
  {start: 1000, main: {text: 'first', start: 1000, end: 1500, words: []},
   translation: {text: '', start: 1000, end: null, words: []}},
  {start: 3000, main: {text: 'second', start: 3000, end: null, words: []},
   translation: {text: '', start: 3000, end: null, words: []}},
]});
wave.invalidate();
flush();
rects = []; texts = [];
wave.invalidate();
flush();
const ended = cellsIn(rects, BAND_MAIN);
check('有句尾的行块停在自己的句尾（500ms − 10px 缝）', ended[0].w, 50 - 10);
check('有句尾的行后面留出了空白（不再填到下一行）', ended[0].w < 200, true);

const BAND_TRANS = 225;

// ---- 0b. 两个轨道都必须有句间缝（不只主轨）-------------------------------
// The seam was first fixed only for the MAIN band, and the translation band was
// never asserted at all — so "主轨修好了，翻译轨没修" went unnoticed. Both bands
// go through the same loop now, and both are checked here so a one-sided fix
// cannot pass again.
wave.setDocument({groups: [
  {start: 1000, main: {text: 'first', start: 1000, end: null, words: []},
   translation: {text: '第一句', start: 1000, end: null, words: []}},
  {start: 3000, main: {text: 'second', start: 3000, end: null, words: []},
   translation: {text: '第二句', start: 3000, end: null, words: []}},
]});
wave.invalidate();
flush();
rects = []; texts = [];
wave.invalidate();
flush();
for (const [band, name] of [[BAND_MAIN, '主轨'], [BAND_TRANS, '翻译轨']]) {
  // (cellsIn 定义在后面，这里内联同样的筛选条件)
  const blocks = rects.filter((r) => r.h === SaltWaveform.CELL_HEIGHT && r.y === band).sort((a, b) => a.x - b.x);
  check(name + '画成 2 个块', blocks.length, 2);
  if (blocks.length === 2) {
    check(name + '的句间缝隙是 10px', blocks[1].x - (blocks[0].x + blocks[0].w), 10);
  }
}

// ---- 0c. 顶部迷你预览条同样不能粘连（按真实歌曲长度）----------------------
// A second, separate painter (`paintStripBackground`) draws the 44px overview,
// and it had no seam at all: a translation line without 句尾 fell back to a
// fixed 2.5s, which OVERLAPS the next line whenever they are closer than that,
// so the translation row read as one continuous bar whatever the big waveform did.
//
// The first version of this check used a 11s document — where the strip draws at
// 57px/s and everything is wide. A real song is ~4 minutes in the same 660px,
// i.e. ~2.7px/s, and there the seam collapsed to 1px and the row became a field
// of hairlines. Checking at a realistic length is the whole point of this test.
const stripGroups = [];
for (let i = 0; i < 70; i++) {
  const at = 5000 + i * 3200;                 // 4 分钟的曲子，每行约 3.2 秒
  stripGroups.push({
    start: at,
    main: {text: 'line ' + i, start: at, end: null, words: []},
    translation: {text: i === 30 ? '' : '翻译 ' + i, start: at, end: null, words: []},
  });
}
// 收尾的空行决定整首歌的长度；start 必须和 main/translation 一致，
// 否则 songSeconds() 会算出错误的时长，整条带子的比例就全错了。
const songEnd = 5000 + 70 * 3200 + 3000;
stripGroups.push({start: songEnd,
  main: {text: '', start: songEnd, end: null, words: []},
  translation: {text: '', start: songEnd, end: null, words: []}});
wave.setDocument({groups: stripGroups});
// songSeconds() 优先用解码音频的时长；前面的用例设置过它，这里必须复位，
// 否则整条带子是按 9 秒画的，块会全跑出画面（这正是第一版断言假通过的原因之一）。
wave.duration = 0;
wave.invalidate();
const stripRects = [];
const stripCtx = new Proxy({
  fillRect: (x, y, w, h) => stripRects.push({x, y, w, h}),
  rect: (x, y, w, h) => stripRects.push({x, y, w, h}),
  measureText: (t) => ({width: String(t).length * 7}),
}, {get(t, p) { return p in t ? t[p] : () => {}; }, set(t, p, v) { t[p] = v; return true; }});
// Inject the backing store: paintStripBackground draws into `stripLayer`, and the
// harness's createElement() returns a proxy that swallows every call.
wave.stripLayer = {width: 0, height: 0, getContext: () => stripCtx};
wave.paintStripBackground(660, 44, 1, 660, 44);
for (const [row, name] of [[7, '预览条·主轨'], [22, '预览条·翻译轨']]) {
  const blocks = stripRects.filter((r) => r.y === row && r.h === 12).sort((a, b) => a.x - b.x);
  check(name + '画出了整首歌的行（' + blocks.length + ' 块）', blocks.length > 60, true);
  const gaps = blocks.slice(0, -1).map((b, i) => blocks[i + 1].x - (b.x + b.w));
  check(name + '没有重叠', gaps.every((g) => g >= 0), true);
  // 够宽的块必须留出看得见的缝；太窄的块允许不留缝，否则只剩碎片。
  const wide = blocks.filter((b) => b.w >= 5);
  check(name + '够宽的块都留出了 ≥2px 的缝',
        wide.every((b, i) => i >= wide.length - 1 ||
                             wide[i + 1].x - (b.x + b.w) >= 2), true);
  check(name + '每块都不是碎片（≥3px）', blocks.every((b) => b.w >= 3), true);
}



// ---- 13. 缩放滑块的两端必须落在 0 和 100（以及换算的单一权威）----------
// Reported as "音量和缩放滑块的显示BUG，我需要这两个滑块到极左和极右的时候为 0 和 100".
// The zoom slider used to run 20–400 px/s, so its ends read "20" and "400" — the
// slider and the number box were two different scales. The slider is a 0–100
// percentage now; the pixel-per-second value stays in the box, and the pps range
// lives once, on the class.
check('缩放范围只有一个权威（类常量）', typeof SaltWaveform.clampPps, 'function');
check('滑块极左 = 20px/s', SaltWaveform.ppsForPercent(0), SaltWaveform.MIN_PPS);
check('滑块极右 = 400px/s', SaltWaveform.ppsForPercent(100), SaltWaveform.MAX_PPS);
check('极左的百分比是 0', SaltWaveform.percentForPps(SaltWaveform.MIN_PPS), 0);
check('极右的百分比是 100', SaltWaveform.percentForPps(SaltWaveform.MAX_PPS), 100);
check('越界的 pps 被夹住', SaltWaveform.clampPps(9999), SaltWaveform.MAX_PPS);
check('越界的百分比被夹住', SaltWaveform.ppsForPercent(999), SaltWaveform.MAX_PPS);
// 380 的跨度分 100 档 → 每档 3.8px/s，整数百分比往返必然有一档以内的量化误差。
// 两端必须是精确的（上面几条），中间允许一档。
check('往返换算误差不超过一档',
      Math.abs(SaltWaveform.ppsForPercent(SaltWaveform.percentForPps(90)) - 90) <= 4, true);

if (failures) throw Error(failures + '/' + checks + ' 项逐字编辑断言失败');
console.log(checks + ' word-editing checks passed');
process.exit(0);

