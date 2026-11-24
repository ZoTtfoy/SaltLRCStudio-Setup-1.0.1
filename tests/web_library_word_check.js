// 曲库页的逐字编辑器：取消 / 应用必须真的能用。
//
// Reported as "歌词获取页的取消和应用不可用". The real cause turned out to be
// markup order: `<dialog id="wordEditDialog">` sat AFTER `<script src=
// "/library.js">`, and a browser runs that script while the parser is still at
// the tag — so `bind('closeWordEdit', …)` saw null, warned to a console the
// packaged window does not show, and returned. Both buttons were never wired.
//
// NOTE: this harness CANNOT see that defect and must not be trusted for it.
// `resources: 'usable'` makes jsdom fetch external scripts asynchronously and
// finish parsing first, so the dialog exists by the time library.js runs — the
// bug's premise is silently repaired. The ordering rule therefore lives in
// tests/test_web_pages.py::test_no_script_binds_an_element_declared_later_in_
// the_page, which reads the document order directly. What this file covers is
// the behaviour *given* that the wiring exists: 应用 writes+closes, 取消 closes
// without touching the document, a stale row refuses out loud, the action bar
// stays on screen, and the trace tool can tell a dead button from a live one.
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');
const BASE = process.argv[2];

let checks = 0, failures = 0;
function check(name, actual, expected) {
  checks++;
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    failures++;
    console.log('FAIL %s\n  got      %s\n  expected %s', name, JSON.stringify(actual), JSON.stringify(expected));
  } else console.log('ok   %s', name);
}

(async () => {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (e) => errors.push(String(e.message || e)));
  virtualConsole.on('error', (m) => errors.push('console.error: ' + m));

  const dom = await JSDOM.fromURL(BASE + '/library', {
    runScripts: 'dangerously', resources: 'usable', pretendToBeVisual: true, virtualConsole,
    beforeParse(window) {
      window.ResizeObserver = class {observe() {}};
      // jsdom 没有 canvas 实现；页面画波形时会在 null 上取方法。
      const stubContext = new Proxy({}, {
        get(target, key) {
          if (key === 'measureText') return () => ({width: 10});
          if (key === 'canvas') return {width: 900, height: 400};
          return () => undefined;
        },
        set() { return true; },
      });
      window.HTMLCanvasElement.prototype.getContext = () => stubContext;
      window.fetch = (input, init) => fetch(new URL(input, BASE).href, init);
      window.confirm = () => true;
      window.matchMedia = () => ({matches: false, addEventListener() {}, removeEventListener() {}});
      // jsdom has no <dialog> methods; without this the whole editor path throws.
      const proto = window.HTMLDialogElement && window.HTMLDialogElement.prototype;
      if (proto && typeof proto.showModal !== 'function') {
        proto.showModal = function () { this.open = true; };
        proto.show = function () { this.open = true; };
        proto.close = function () { this.open = false; };
      }
    },
  });
  await new Promise((r) => setTimeout(r, 3000));
  const w = dom.window, doc = w.document;
  const $ = (id) => doc.getElementById(id);

  // ---- 1. the two buttons are actually wired ------------------------------
  check('#applyWords 已挂处理函数', typeof $('applyWords').onclick, 'function');
  check('#closeWordEdit 已挂处理函数', typeof $('closeWordEdit').onclick, 'function');

  // ---- 2. an injected document opens the editor with one cell per word ----
  const seed = (firstStart, secondStart) => w.eval(`
    state.doc = {groups: [{
      start: 1000,
      main: {text: '你好', start: 1000, end: 3000,
             words: [{text: '你', start: ${firstStart}}, {text: '好', start: ${secondStart}}]},
      translation: {text: '', start: 1000, end: null, words: []}
    }], metadata: {}, notes: [], unresolved: [], alignment: {}};
    state.editingRow = -1;
    true;
  `);
  seed(1000, 2000);
  w.eval('openWordEditor(0)');
  check('编辑器已打开', $('wordEditDialog').open, true);
  check('每个字一个格子', $('wordGrid').children.length, 2);
  check('句尾填入当前值', $('wordEnd').value, '3.000');
  check('标题写明了行号与字数', /第 1 行/.test($('wordLineText').textContent), true);

  // ---- 3. 应用 writes the values and closes ------------------------------
  $('wordGrid').children[1].querySelector('.wordTime').value = '2.400';
  $('wordEnd').value = '3.200';
  $('applyWords').click();
  await new Promise((r) => setTimeout(r, 60));
  check('点「应用」后对话框关闭', $('wordEditDialog').open, false);
  check('第二个字的时间已写入文档',
        w.eval('state.doc.groups[0].main.words[1].start'), 2400);
  check('句尾已写入文档', w.eval('state.doc.groups[0].main.end'), 3200);
  check('应用后没有残留错误', $('wordEditError').textContent, '');

  // ---- 4. 取消 closes WITHOUT changing anything --------------------------
  seed(1000, 2000);
  w.eval('openWordEditor(0)');
  $('wordGrid').children[1].querySelector('.wordTime').value = '2.900';
  $('closeWordEdit').click();
  check('点「取消」后对话框关闭', $('wordEditDialog').open, false);
  check('取消不会改动文档', w.eval('state.doc.groups[0].main.words[1].start'), 2000);

  // ---- 5. a stale row must refuse OUT LOUD, not silently -----------------
  // This is the failure that made the buttons look dead: the document had been
  // replaced behind the dialog and the handler simply returned.
  seed(1000, 2000);
  w.eval('openWordEditor(0)');
  w.eval('state.doc = {groups: []}');          // document swapped underneath
  $('applyWords').click();
  await new Promise((r) => setTimeout(r, 60));
  check('失效行不会静默通过（对话框保持打开）', $('wordEditDialog').open, true);
  check('失效行给出了可读的原因',
        /不在当前歌词里|没有正在编辑的行/.test($('wordEditError').textContent), true);
  $('closeWordEdit').click();
  check('还能正常取消', $('wordEditDialog').open, false);

  // ---- 6. the action bar is kept on screen -------------------------------
  // A tall cell grid used to push 取消/应用 below the viewport.
  const response = await fetch(BASE + '/style.css');
  const css = await response.text();
  check('逐字对话框有高度上限', /dialog[.]wordBox[{][^}]*max-height/.test(css), true);
  check('底部操作条不是可压缩的', /dialog[.]wordBox [.]dialogActions[{][^}]*flex:none/.test(css), true);
  // A closed <dialog> must be display:none. `dialog.wordBox` sets display:flex,
  // and an author rule beats the engine's `dialog:not([open]){display:none}` at
  // any specificity — without the compensation the closed editor keeps rendering
  // as a stray box at the end of the document and close() looks like a no-op.
  check('关闭状态的对话框不会被显示',
        /dialog:not\(\[open\]\)[{][^}]*display:\s*none[^}]*!important/.test(css), true);

  // ---- 7. the trace tool catches a button that does nothing --------------
  // This is the whole point of the feature: "点了没反应" must be visible in the
  // record, otherwise a report can only describe the symptom and the cause stays
  // a guess. A button with no handler is the cleanest stand-in for the bug.
  //
  // The journal lives in the SERVER process and is shared by every run of this
  // harness, so the record is cleared first. Without that, a stale event from an
  // earlier run satisfies the assertion and the check stops meaning anything —
  // which is exactly what happened here and was only caught by reverse-verifying
  // (removing the feature still passed).
  await fetch(BASE + '/api/trace/clear', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  const cleared = await (await fetch(BASE + '/api/trace', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})).json();
  check('清空后日志确实为空（否则下面的断言会被旧事件满足）', cleared.total, 0);

  check('追踪脚本已加载', typeof w.eval('typeof window.Trace'), 'string');
  check('追踪脚本提供了面板', w.eval('typeof window.Trace.toggle'), 'function');
  const dead = doc.createElement('button');
  dead.id = 'deadProbe';
  dead.textContent = '点了没反应的按钮';
  doc.body.append(dead);
  dead.click();
  await new Promise((r) => setTimeout(r, 1300));       // past the stall window
  // Some API traffic, so the next assertion has something to see. It has to
  // happen AFTER the clear, otherwise it is just another stale event.
  await w.eval("fetch('/api/capabilities').then((r) => r.status)");
  await w.eval('window.Trace.flush()');
  await new Promise((r) => setTimeout(r, 200));
  const traceReply = await fetch(BASE + '/api/trace', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  const trace = await traceReply.json();
  const names = trace.analysis.stalled.map((item) => String(item.name) + ' ' + String(item.detail || ''));
  check('点了没反应的按钮被记为 stalled',
        names.some((name) => name.includes('deadProbe') || name.includes('点了没反应的按钮')), true);
  check('有反应的点击不会被记为 stalled',
        names.some((name) => name.includes('closeWordEdit')), false);
  check('追踪记录了 API 调用',
        trace.events.some((e) => e.kind === 'api-start' || e.kind === 'api-end'), true);

  // ---- 8. an unwireable control reports itself -----------------------------
  // The original defect was invisible: bind() returned after a console.warn and
  // nothing else happened. Assert the fault reaches the journal, so a page that
  // cannot wire a control says so without anyone having to read the markup.
  // Cleared again, so this asserts on this fault and nothing older.
  await fetch(BASE + '/api/trace/clear', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  w.eval("bind('thisIdDoesNotExistAnywhere', () => {})");
  await new Promise((r) => setTimeout(r, 150));
  await w.eval('window.Trace.flush()');
  await new Promise((r) => setTimeout(r, 200));
  const after = await (await fetch(BASE + '/api/trace', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})).json();
  check('绑不上的控件被记进日志',
        after.events.some((e) => e.kind === 'error' && e.name === '控件无法绑定'
                                 && String(e.detail).includes('thisIdDoesNotExistAnywhere')), true);
  check('绑不上的控件出现在错误清单里',
        after.analysis.errors.some((e) => String(e.name).includes('控件无法绑定')), true);
  check('这一轮没有残留旧事件',
        after.events.every((e) => e.kind === 'error' && e.name === '控件无法绑定'), true);

  check('全程没有脚本错误', errors.filter((e) => !/Not implemented/.test(e)).length, 0);
  dom.window.close();

  if (failures) throw Error(failures + '/' + checks + ' 项曲库逐字编辑器断言失败');
  console.log(checks + ' library word-editor checks passed');
  process.exit(0);
})().catch((error) => {
  console.log('FAIL ' + error.message);
  process.exit(1);
});
