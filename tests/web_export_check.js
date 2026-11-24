// 「导出歌词」必须在**任何标签页**都能点、而且真的落盘。
//
// 用户报的（v0.23.3）："导出歌词功能又失效了…点了没反应"，
// 并点名"**确保我在这个程序内无论干什么，我的导入和导出功能都是正常的**"。
// 真因之一：`#export` 原来住在 `#editDock` 里，而 editDock **只存在于「波形编辑」
// 标签页** —— 切到另外两个标签页时按钮根本不在屏幕上。
//
// 这个检查走完整链路：真页面（jsdom）→ 真后端 → /api/save-as → 渲染 → 写盘。
// **原生"另存为"对话框由 Python 侧打桩**（这台机器不许弹窗），但只打桩"路径从哪来"
// 那一层，渲染/校验/写盘全是真代码。
//
// usage: node tests/web_export_check.js <base-url>
const { JSDOM, VirtualConsole } = require('jsdom');

const BASE = process.argv[2];
if (!BASE) { console.error('usage: node web_export_check.js <base-url>'); process.exit(2); }

let checks = 0, failures = 0;
function check(name, actual, expected) {
  checks++;
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    failures++;
    console.log('FAIL %s\n  got      %s\n  expected %s', name, JSON.stringify(actual), JSON.stringify(expected));
  } else console.log('ok   %s', name);
}

function stubCanvas(window) {
  const ctx = new Proxy({}, {
    get(t, p) {
      if (p === 'measureText') return () => ({width: 10});
      if (p === 'canvas') return {width: 900, height: 400};
      if (p === 'createLinearGradient' || p === 'createRadialGradient') return () => ({addColorStop() {}});
      return () => undefined;
    },
    set() { return true; },
  });
  window.HTMLCanvasElement.prototype.getContext = () => ctx;
}

const LRC = '[00:01.000]今晚[00:02.000]月色很美[00:04.000]\n'
          + '[00:01.000]今夜は[00:02.000]月が綺麗だね[00:04.000]\n';

(async () => {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(e.message));
  vc.on('error', (m) => errors.push('console.error: ' + m));

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  // 就绪判据要覆盖**这条检查真正会用到的东西**：`queue` 与 `api` 在 studio.js 里，
  // 而 `renderHistory` 在 history.js 里 —— 只查前两个的话，history.js 没加载时页面
  // 看起来"就绪"，随后在 `select() → refresh() → render()` 抛一句
  // `renderHistory is not defined`，报出来像是"导出坏了"。
  const READY = "typeof queue !== 'undefined' && typeof api === 'function'"
    + " && typeof renderHistory === 'function' && typeof switchPane === 'function'";

  // jsdom 用 `resources: 'usable'` **异步**取外部脚本，机器繁忙时会掉一个
  //（实测：同一份代码一次 10 秒通过、一次 40 秒后失败）。掉了一个脚本就等于页面
  // 缺了一半，而报出来的错和真正的原因毫无关系 —— 所以加载失败要**重试一次**，
  // 并且每次都把页面自己的错误打出来。
  async function loadPage() {
    errors.length = 0;
    const page = await JSDOM.fromURL(BASE + '/studio.html', {
      runScripts: 'dangerously', resources: 'usable', pretendToBeVisual: true, virtualConsole: vc,
      beforeParse(window) {
        stubCanvas(window);
        const dialog = window.HTMLDialogElement && window.HTMLDialogElement.prototype;
        if (dialog && !dialog.showModal) {
          dialog.showModal = function () { this.open = true; };
          dialog.close = function () { this.open = false; };
        }
        window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
        window.fetch = (input, init) => fetch(new URL(input, BASE).href, init);
        window.confirm = () => true;
        window.alert = () => {};
        window.matchMedia = () => ({matches: false, addEventListener() {}, removeEventListener() {}});
      },
    });
    const pageRun = (code) => page.window.eval(code);
    for (let i = 0; i < 600; i++) {
      let ok = false;
      try { ok = !!pageRun(READY); } catch { ok = false; }
      if (ok) return {dom: page, run: pageRun, ok: true};
      await wait(50);
    }
    return {dom: page, run: pageRun, ok: false};
  }

  let loaded = null;
  for (let attempt = 1; attempt <= 2; attempt++) {
    loaded = await loadPage();
    if (loaded.ok) break;
    console.log(`  第 ${attempt} 次加载没就绪，页面错误：${JSON.stringify(errors.slice(0, 3))}`);
    try { loaded.dom.window.close(); } catch (error) { /* 关掉再试 */ }
  }

  const dom = loaded.dom;
  const win = dom.window;
  const run = loaded.run;
  const say = () => String(run(`document.getElementById('message').textContent`));
  const real = () => errors.filter((e) => !/Not implemented|Could not parse CSS/i.test(e));

  if (!loaded.ok) {
    // 走到这里说明**两次**都没加载起来。把原因说出来：`checks`/`failures` 是本文件的
    // 计数变量（`results` 是另一个脚手架的写法，这里没有那个变量）。
    check('页面就绪（队列与 api 都在）', false, true);
    console.log('\n' + checks + ' checks, ' + failures + ' failed');
    process.exit(1);
  }
  await wait(300);

  // ---- ① 入口必须在顶部 dock 里（任何标签页都够得着）------------------------
  const nav = run(`document.getElementById('appNav').outerHTML`);
  check('导出按钮在顶部 dock 里', /id="export"/.test(String(nav)), true);
  check('编辑坞里没有第二个导出按钮',
        run(`!!document.querySelector('#editDock #export')`), false);

  // ---- 前置：导入一份歌词 ---------------------------------------------------
  run(`(async () => {
        const r = await api('parse', {bytes: SaltAudio.base64(new TextEncoder().encode(${JSON.stringify(LRC)}))});
        queue.push({name: '导出检查.lrc', document: r.document,
                    report: {errors: [], warnings: []}, history: [], sourceDir: 'D:\\\\我的歌词'});
        await select(queue.length - 1);
      })()`);
  await wait(1200);
  // 两行歌词、句首时间相同 → 解析成**1 组**（主轨 + 翻译轨），不是 2 组。
  check('歌词已载入（主轨+翻译轨合成 1 组）', run('current().document.groups.length'), 1);
  check('翻译轨在里面', run(`current().document.groups[0].translation.text`), '今夜は月が綺麗だね');
  const report = await run('check()');
  check('这份歌词能过 salt 校验', report.errors, []);

  // ---- ② 三个标签页各点一次导出，都要真的落盘 ------------------------------
  const before = await fetch(BASE + '/api/export-probe').catch(() => null);
  const exports = [];
  for (const [pane, label] of [['wave', '波形编辑'], ['align', '识别与对齐'], ['project', '检查与工程']]) {
    run(`switchPane(${JSON.stringify(pane)})`);
    await wait(200);
    run(`document.getElementById('message').textContent = ''`);
    errors.length = 0;
    await run(`document.getElementById('export').onclick()`);
    await wait(500);
    exports.push({pane, message: say()});
    check(`在「${label}」页点导出，状态行报出导出成功`,
          /已导出到 /.test(say()), true);
    check(`在「${label}」页点导出，页面没有异常`, real(), []);
  }

  // ---- ③ 三种格式都按格式落盘 ---------------------------------------------
  // 顺序有讲究：salt 与 lrc 写的是**同一个文件名**（只有 yrc 换扩展名），
  // 所以把 salt 放最后 —— 磁盘上那份 .lrc 就是 salt 的输出，Python 那侧才能
  // 断言"每行带多个时间戳"。lrc 格式本身的正确性由 test_core.py 覆盖。
  const formats = [];
  for (const fmt of ['lrc', 'yrc', 'salt']) {
    run(`document.getElementById('format').value = ${JSON.stringify(fmt)}`);
    run(`document.getElementById('message').textContent = ''`);
    await run(`document.getElementById('export').onclick()`);
    await wait(400);
    formats.push({fmt, message: say()});
    check(`格式 ${fmt} 能导出`, /已导出到 /.test(say()), true);
  }

  // ---- ④ 四步都要有交代（用户两次投诉"点了没反应"）----------------------
  run(`document.getElementById('format').value = 'salt'`);
  run(`document.getElementById('message').textContent = ''`);
  run(`document.getElementById('revealLast').hidden = true`);
  const pending = run(`document.getElementById('export').onclick()`);   // 故意不 await
  // "正在读歌词 / 正在打包"这两句是在**第一个 await 之前**同步写进去的，
  // 所以要**立刻**读 —— 等 60ms 请求早回来了，读到的就是最终那句
  // （第一版就是这么写错的：断言看起来对，其实什么都没验到）。
  check('① 导出过程中状态行说明了正在做什么（读歌词 → 打包）',
        /正在读取|正在打包/.test(say()), true);
  await pending;
  await wait(400);
  check('② 完成后状态行给出落点、行数、以及"找不到怎么办"',
        /已导出到/.test(say()) && /打开所在文件夹/.test(say()), true);
  check('③ 导出后出现「打开所在文件夹」按钮',
        run(`!document.getElementById('revealLast').hidden`), true);
  errors.length = 0;
  await run(`document.getElementById('revealLast').onclick()`);
  await wait(250);
  check('④ 点它不会报错（后端在测试里被打桩 —— 真开窗口是用户点了才做的事）',
        real(), []);

  // ---- ⑤ 没有歌词的时候要给可读原因（不是"点了没反应"）--------------------
  // 注意：`select(-1)` 在 JS 里是"取最后一个"（数组负下标），不是取消选择 ——
  // 直接改 `selected` 才是"没有选中任何歌"这个状态。
  run(`selected = -1`);
  run(`document.getElementById('message').textContent = ''`);
  await run(`document.getElementById('export').onclick()`);
  await wait(200);
  check('没选歌时点导出会说话', /请先导入歌词/.test(say()), true);

  run(`select(0)`);
  await wait(700);
  run(`current().document.groups = []`);
  run(`document.getElementById('message').textContent = ''`);
  await run(`document.getElementById('export').onclick()`);
  await wait(300);
  check('空文档时点导出会说话', /还没有行/.test(say()), true);

  win.close();
  console.log(`\n${checks} checks, ${failures} failed`);
  process.exit(failures ? 1 : 0);
})().catch((e) => {
  console.error('FAIL export check: ' + (e && e.stack || e));
  process.exit(1);
});
