// End-to-end: the real studio page, in jsdom, against the real backend.
//
// Nothing here stubs the server rules — this walks the whole chain the user
// touches: a button -> api() -> POST /api/edit -> saltlrc/edits.py -> the
// document comes back -> the table re-renders. It is the only check that would
// catch a wiring mistake that the unit tests (each side tested separately)
// cannot see.
// Usage: node tests/web_insert_e2e_check.js <base-url>
const { JSDOM, VirtualConsole } = require('jsdom');

const BASE = process.argv[2];
if (!BASE) {
  console.error('usage: node web_insert_e2e_check.js <base-url>');
  process.exit(2);
}

let checks = 0, failures = 0;
function check(name, actual, expected) {
  checks++;
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    failures++;
    console.log('FAIL %s\n  got      %s\n  expected %s', name, JSON.stringify(actual), JSON.stringify(expected));
  } else {
    console.log('ok   %s', name);
  }
}

// jsdom has no canvas; a no-op 2D context keeps the waveform from throwing
// while still exercising the real code paths.
function stubCanvas(window) {
  const ctx = new Proxy({}, {
    get(target, prop) {
      if (prop === 'measureText') return () => ({ width: 10 });
      if (prop === 'canvas') return { width: 900, height: 400 };
      if (prop === 'createLinearGradient' || prop === 'createRadialGradient') return () => ({ addColorStop() {} });
      return () => undefined;
    },
    set() { return true; },
  });
  window.HTMLCanvasElement.prototype.getContext = () => ctx;
}

(async () => {
  // Collects jsdom errors across load attempts; the successful attempt's list
  // is merged in below.
  const errors = [];

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  // Loading the editor in jsdom means fetching seven scripts through Node, and
  // when the whole test suite is running the machine can be busy enough that a
  // script never arrives ("Could not load script" from jsdom). That is a
  // property of the test environment, not of the page, so the load is retried
  // rather than reported as a defect.
  //
  // The per-attempt budget is deliberately SHORT. A healthy load finishes in
  // about 100 ms (measured), while a failed one never recovers — so waiting 60 s
  // to find that out cost a minute on every flaky run and turned a 12 s test
  // into a 73 s one. Failing fast and retrying is both quicker and more honest:
  //   observed: attempts 1-3, 60s budget -> 73 s worst case
  //             attempts 1-5,  8s budget -> ~25 s worst case, ~12 s typical
  const attemptTimeout = 8000;
  const openEditor = async (attempts = 5) => {
    for (let attempt = 1; attempt <= attempts; attempt++) {
      const jsdomErrors = [];
      const console2 = new VirtualConsole();
      console2.on('jsdomError', (e) => jsdomErrors.push(e.message));
      console2.on('error', (m) => jsdomErrors.push('console.error: ' + m));
      const dom = await JSDOM.fromURL(BASE + '/studio.html', {
        runScripts: 'dangerously',
        resources: 'usable',
        pretendToBeVisual: true,
        virtualConsole: console2,
        beforeParse(window) {
          stubCanvas(window);
          // This jsdom build has no <dialog> implementation; a minimal shim keeps
          // the real page code (showModal/close/open) exercisable.
          const dialog = window.HTMLDialogElement && window.HTMLDialogElement.prototype;
          if (dialog && !dialog.showModal) {
            dialog.showModal = function () { this.setAttribute('open', ''); this.open = true; };
            dialog.close = function () { this.removeAttribute('open'); this.open = false; };
          }
          window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
          window.fetch = (input, init) => fetch(new URL(input, BASE).href, init);
          window.confirm = () => true;
          window.alert = () => {};
          window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
        },
      });
      const win = dom.window;
      const ready = async (timeout = attemptTimeout) => {
        const deadline = Date.now() + timeout;
        while (Date.now() < deadline) {
          if (win.eval("typeof queue !== 'undefined' && typeof insertRow === 'function' && typeof wave === 'object'")) return true;
          await wait(50);
        }
        return false;
      };
      if (await ready()) {
        await wait(300);            // let the remaining bindings settle
        return { dom, win, jsdomErrors };
      }
      dom.window.close();
      if (attempt < attempts) {
        console.log(`编辑器脚本第 ${attempt} 次未在时限内加载，重试`);
      }
    }
    return null;
  };

  const opened = await openEditor();
  if (!opened) {
    // Continuing would run every later assertion against a dead page and report
    // the first confusing symptom ("queue.push is not a function") instead of
    // the actual cause, so stop here with the real reason.
    console.error('编辑器脚本始终未能在时限内加载完成，后续断言无法进行');
    check('编辑器脚本加载完成', false, true);
    console.log(`\n${checks} checks, ${failures} failed`);
    process.exit(1);
  }
  const { dom, win, jsdomErrors } = opened;
  errors.push(...jsdomErrors);

  const real = errors.filter((e) => !/Not implemented|Could not parse CSS|Error: Not implemented/i.test(e));
  check('页面加载无脚本错误', real.slice(0, 2), []);

  const run = (code) => win.eval(code);
  const starts = () => JSON.parse(run('JSON.stringify(current().document.groups.map(g => g.start))'));
  const rowCount = () => run('document.querySelectorAll("#rows tr").length');

  // A three-line song, exactly the fixture the two layer-0 test suites use.
  run(`queue.push({name: 'e2e.lrc', history: [], report: {errors: [], warnings: []}, document: {groups: [1000, 3000, 9000].map(s => ({start: s,
        main: {text: 'X', words: [], end: null, start: null},
        translation: {text: '', words: [], end: null, start: null}})),
        metadata: {}, notes: [], unresolved: [], alignment: {}}});
        select(queue.length - 1);`);
  await wait(600);
  check('载入三行歌词', starts(), [1000, 3000, 9000]);
  check('表格渲染三行', rowCount(), 3);

  // 1) the per-row ＋插入 button: insert below the first line
  const clicked = run(`(() => {
    const row = document.querySelectorAll('#rows tr')[0];
    const button = [...row.querySelectorAll('button')].find(b => b.textContent === '＋插入');
    if (!button) return false;
    button.click();
    return true;
  })()`);
  check('每行都有＋插入按钮', clicked, true);
  await wait(600);
  check('第 1 行下方插入取中点', starts(), [1000, 2000, 3000, 9000]);
  check('表格同步变成四行', rowCount(), 4);

  // 2) the toolbar 添加行 follows the anchor: the line just inserted is now
  //    "where you are", so the next one lands below it.
  win.document.getElementById('add').click();
  await wait(600);
  check('添加行跟在刚插入的行下面', starts(), [1000, 2000, 2500, 3000, 9000]);

  // 3) locating a line moves the anchor there
  run('locate(1)');
  win.document.getElementById('add').click();
  await wait(600);
  check('定位后添加行插在该行下方', starts(), [1000, 2000, 2250, 2500, 3000, 9000]);

  // 4) undo walks it all back
  run('undo(); undo(); undo();');
  await wait(400);
  check('三次撤销回到原始三行', starts(), [1000, 3000, 9000]);

  // 5) 两个入口是同一功能：都没有选中行时，按当前时间轴（播放位置）插入。
  //    旧实现是「追加到末尾」，这正是用户报的「两个按钮功能不统一」。
  //    这里没有真实音频，所以位置由波形播放头（wave.time）承载 —— 贴条拖动
  //    和播放都会写它，playheadMs() 在无音频时读同一个值。
  run('anchorRow = -1');
  run('wave.time = 0;');                                  // 播放头在第 1 行之前
  win.document.getElementById('add').click();
  await wait(600);
  check('没选中且播放头在开头时插在最前', starts(), [0, 1000, 3000, 9000]);
  run('undo()');
  await wait(300);

  //    播放头落在第 2 行（3000ms）时，新行应插在第 2 行之后
  run('anchorRow = -1');
  run('wave.time = 4.0;');
  win.document.getElementById('add').click();
  await wait(600);
  check('没选中时按播放位置在其所属行后插入', starts(), [1000, 3000, 6000, 9000]);
  run('undo()');
  await wait(300);
  check('撤销后回到原始三行', starts(), [1000, 3000, 9000]);

  // 5) a neighbour that is too close is refused with a readable message
  run(`current().document.groups[1].start = 1001;`);
  run(`insertRow(0).catch(e => { window.__err = e.message; });`);
  await wait(600);
  const message = String(run('window.__err || ""'));
  check('挨太近时给出可读提示', /中间插不进新行/.test(message), true);

  // 6) open a .saltproj project file (no embedded audio)
  const project = {kind: 'saltlrc-project', version: 1, app: 'SaltLRC Studio',
                   savedAt: '2026-09-23T00:00:00Z', name: '工程测试.lrc', duration: 20000, audio: null,
                   document: {groups: [1000, 5000, 9000].map(s => ({start: s,
                     main: {text: '词', words: [], end: null, start: null},
                     translation: {text: '', words: [], end: null, start: null}})),
                     metadata: {}, notes: [], unresolved: [], alignment: {}}};
  await win.eval(`openProjectFile(new File([${JSON.stringify(JSON.stringify(project))}], '工程测试.saltproj'))`);
  await wait(800);
  check('工程打开后歌名', run('current().name'), '工程测试.lrc');
  check('工程打开后行数', run('current().document.groups.length'), 3);
  check('工程打开后时长', run('current().duration'), 20000);
  check('工程进入队列', run('queue.length') > 1, true);

  // 7) word-timing editor: chips, tap mode, apply; auto-split via /api/tokenize
  run(`audio.currentTime = 1.0; openWords(0, 'main');`);
  await wait(200);
  check('逐字编辑器已打开', run('document.getElementById("wordDialog").open'), true);
  check('单字词一行一张卡', run('document.querySelectorAll("#wordChips .wordCell").length'), 1);
  run('wordTap()');
  check('打点写入当前播放位置', run('wordUnits[0].start'), 1000);
  run('audio.currentTime = 1.5; wordTap();');
  check('全部打完后 Enter 设句尾', run('document.getElementById("wordEndTime").value'), '1.500');
  run('applyWords()');
  await wait(300);
  check('应用后逐字轴写入文档', JSON.parse(run('JSON.stringify(current().document.groups[0].main.words)')),
        [{start: 1000, text: '词'}]);

  run(`current().document.groups[0].main.words = [];
      current().document.groups[0].main.text = 'あいうえおか';
      openWords(0, 'main');`);
  await wait(200);
  await run('autoSplitWords()');
  await wait(400);
  check('自动分词：6 假名切成 2 组', run('document.querySelectorAll("#wordChips .wordCell").length'), 2);
  check('分词后全部待打点', JSON.parse(run('JSON.stringify(wordUnits.map(u => u.start))')), [null, null]);
  run(`try { applyWords(); } catch (e) { window.__werr = e.message; }`);
  check('缺时间给可读报错', /2 个字词没有时间/.test(String(run('window.__werr'))), true);
  run(`document.getElementById('closeWords').click()`);
  check('取消关闭编辑器', run('document.getElementById("wordDialog").open'), false);

  // 9) 帮助菜单 + 编辑器内的曲库对话框
  win.document.getElementById('help').click();
  await wait(200);
  check('帮助对话框打开', run('document.getElementById("helpDialog").open'), true);
  check('帮助里有快捷键说明', /快捷键/.test(String(run('document.getElementById("helpDialog").textContent'))), true);
  run(`document.getElementById('closeHelp').click()`);
  check('帮助对话框关闭', run('document.getElementById("helpDialog").open'), false);

  win.document.getElementById('pickLibrary').click();
  await wait(1500);
  check('曲库对话框打开', run('document.getElementById("libraryDialog").open'), true);
  check('曲库已读取', run('Array.isArray(libTracks)'), true);
  const filtered = run(`renderLibrary('zzzz-no-such-track'); document.querySelectorAll('#libList .libRow').length`);
  check('曲库搜索能过滤', filtered, 0);
  const rows = run(`renderLibrary(''); document.querySelectorAll('#libList .libRow').length`);
  check('清空搜索后恢复列表', rows > 0 || run('libTracks.length') === 0, true);
  run(`document.getElementById('libClose').click()`);

  // 8) 一次「歌词编辑」的四条不变量：①文档真的改了 ②历史里多**一步** ③落盘
  //    ④重绘。v0.22.1 之前「表格里改歌词文字」和「交换双轴」把**标签**当条目
  //    传给了 remember()，于是②③④全丢，还抛异常把 refresh() 一起带走 ——
  //    表现就是"文字改了、波形没跟上、撤销撤不回来、工程里还是旧的"。
  //    这里用真实事件路径（textarea 的 change）走一遍，并盯住"页面没有抛异常"。
  const noise = /Not implemented|Could not parse CSS/i;
  const newErrors = () => jsdomErrors.filter((e) => !noise.test(e));
  const labels = () => JSON.parse(run(`JSON.stringify(timeline().steps.map(s => s.label))`));
  const texts = () => JSON.parse(run(`JSON.stringify(current().document.groups.map(g => g.main.text))`));
  const histRows = () => run(`document.querySelectorAll('#historyList .hstep').length`);

  run(`queue.push({name: '编辑不变量.lrc', history: [], report: {errors: [], warnings: []}, document: {groups: [1000, 3000, 9000].map(s => ({start: s,
        main: {text: 'X', words: [], end: null, start: null},
        translation: {text: '', words: [], end: null, start: null}})),
        metadata: {}, notes: [], unresolved: [], alignment: {}}});
      select(queue.length - 1);`);
  await wait(600);
  check('载入时历史里就有「载入」这一步（时间轴不再懒创建）', labels(), ['载入']);
  check('历史面板当场就画出来了', histRows(), 1);

  // 表格里的文字框：真实 change 事件
  let before = newErrors().length;
  run(`(() => {
        const area = document.querySelectorAll('#rows tr')[0].querySelector('textarea');
        area.value = '新歌词';
        area.dispatchEvent(new Event('change', {bubbles: true}));
      })()`);
  await wait(400);
  check('表格改文字不抛异常', newErrors().slice(before), []);
  check('表格改文字进历史（一步）', labels(), ['载入', '改歌词文字']);
  check('表格改文字改了文档', texts(), ['新歌词', 'X', 'X']);
  check('表格改文字后历史面板同步', histRows(), 2);
  check('表格改文字能撤销', (run('undo()'), texts()), ['X', 'X', 'X']);
  check('再重做回来', (run('redo()'), texts()), ['新歌词', 'X', 'X']);

  // 行内的「交换」双轴：同一类缺陷
  before = newErrors().length;
  run(`[...document.querySelectorAll('#rows tr')[0].querySelectorAll('button')]
        .find(b => b.textContent === '交换').click()`);
  await wait(400);
  check('交换双轴不抛异常', newErrors().slice(before), []);
  check('交换双轴进历史', labels().slice(-1), ['交换双轴']);
  check('交换双轴能撤销', (run('undo()'), texts()), ['新歌词', 'X', 'X']);

  // 行号已经不存在时，删除必须出声（静默 return 等于按钮坏了）
  run(`document.getElementById('message').textContent = ''`);
  await run('deleteRow(99)');
  await wait(200);
  check('删不存在的行会出声', /已经不在了/.test(String(run(`document.getElementById('message').textContent`))), true);

  // 「选中的行」已经越界时，「添加行」不许说"选中的第 41 行"这种谎
  run('anchorRow = 40; wave.time = 0;');
  run(`document.getElementById('message').textContent = ''`);
  win.document.getElementById('add').click();
  await wait(600);
  const addMessage = String(run(`document.getElementById('message').textContent`));
  check('越界锚点不说谎', /选中的第 41 行/.test(addMessage), false);
  check('越界锚点退回按当前时间轴', /当前时间轴/.test(addMessage), true);
  check('越界锚点仍插在最前（播放头在开头）', starts(), [0, 1000, 3000, 9000]);
  run('undo()');
  await wait(300);

  // 逐字编辑器开着时文档形状被改过：拒绝写入，而不是把字写进别的行。
  run(`current().document.groups[1].main.text = '乙';
       anchorRow = -1; refresh(); openWords(1, 'main');`);
  await wait(300);
  check('逐字编辑器已打开第 2 行', run(`document.getElementById('wordTitle').textContent`),
        '主歌词 · 第 2 行 · 逐字时间');
  run(`current().document.groups.splice(0, 1);`);      // 模拟"上面少了一行"
  run(`try { applyWords(); window.__g = ''; } catch (e) { window.__g = e.message; }`);
  check('形状变了就拒绝写逐字时间', /行号可能已经移位/.test(String(run('window.__g'))), true);
  check('拒绝之后文档没被改坏',
        JSON.parse(run(`JSON.stringify(current().document.groups.map(g => [g.start, g.main.text, g.main.words.length]))`)),
        [[3000, '乙', 0], [9000, 'X', 0]]);
  run(`document.getElementById('closeWords').click()`);

  // 迟到的歌词（pending → adoptDocument）是**整份换文档**，不是编辑：
  // 历史基线必须跟着重开。否则「载入」还停在换之前那份空文档上，
  // 用户删一行再撤销 → 时间轴把他带回空文档 → **整份歌词当场清空**。
  run(`queue.push({name: '迟到歌词.lrc', history: [], report: {errors: [], warnings: []}, pending: true,
        document: {groups: [], metadata: {}, notes: [], unresolved: [], alignment: {}}});
      select(queue.length - 1);`);
  await wait(500);
  check('待配歌词时是空文档', starts(), []);
  run(`adoptDocument(current(), {groups: [1000, 3000, 9000].map(s => ({start: s,
        main: {text: '词', words: [], end: null, start: null},
        translation: {text: '', words: [], end: null, start: null}})),
        metadata: {}, notes: [], unresolved: [], alignment: {}})`);
  await wait(400);
  check('迟到歌词进来后有三行', starts(), [1000, 3000, 9000]);
  check('历史基线换成有歌词的那一份', labels(), ['载入']);
  await run('deleteRow(0)');
  await wait(400);
  check('删掉第一行', starts(), [3000, 9000]);
  check('撤销删行后歌词还在（不是空文档）', (run('undo()'), starts()), [1000, 3000, 9000]);
  check('撤销之后历史光标回到「载入」', run('timeline().cursor'), 0);

  // 格式检查的结果：①同类问题不许刷成一堵墙 ②每条要**真的能点进去**
  // （界面上本来就写着"每条都能点「定位」跳到表格那一行"，而它以前是个点不动的 <pre>）。
  run(`queue.push({name: '检查结果.lrc', history: [], report: {errors: [], warnings: []},
        document: {groups: [1, 3, 5, 7, 9, 11, 13, 15].map((s, i) => ({start: s * 1000,
          main: {text: '第' + (i + 1) + '句', words: [], end: null, start: null},
          translation: {text: '', words: [], end: null, start: null}})),
          metadata: {}, notes: [], unresolved: [], alignment: {}}});
      select(queue.length - 1);`);
  await wait(600);
  await run('check()');
  const rowCount2 = () => run(`document.querySelectorAll('#report .reportRow').length`);
  const firstText = () => String(run(`document.querySelector('#report .reportRow .text').textContent`));
  check('8 行都缺逐字时间 → 报告只有一行（服务端收成汇总）', rowCount2(), 1);
  check('汇总里写了共几行', /共 8 行/.test(firstText()), true);
  // v0.24.2：这条提示的措辞变了 —— 缺逐字不再要求"换格式导出"，
  // 而是"想逐字高亮就双击波形补上"，所以判据跟着换成那两个具体动作。
  check('汇总里给了下一步（怎么补逐字）',
        /按字数均分|从音频检测/.test(firstText()), true);
  check('汇总条带「定位」按钮', run(`!!document.querySelector('#report .reportRow button')`), true);

  run(`document.querySelector('#report .reportRow button').click()`);
  await wait(300);
  check('点「定位」切回波形页',
        run(`document.querySelector('.tab[data-pane="wave"]').classList.contains('active')`), true);
  check('点「定位」把锚点设到那一行', run('anchorRow'), 0);

  // 少数几行有问题时仍然逐条列出（≤5），每条都能单独定位。
  // 给前 5 行补上逐字时间 → 只剩后 3 行有问题 → 应当逐条列出 3 条。
  run(`current().document.groups.slice(0, 5).forEach(g => {
         g.main.words = [{start: g.start, text: g.main.text}];
         g.main.end = g.start + 1500; });`);
  await run('check()');
  check('只剩 3 行有问题 → 逐条列出 3 条', rowCount2(), 3);
  check('逐条时每条都带「定位」', run(`document.querySelectorAll('#report .reportRow button').length`), 3);
  check('逐条时不再出现"共 N 行"汇总', /共 \d+ 行/.test(firstText()), false);
  run(`document.querySelector('#report .reportRow button').click()`);
  await wait(300);
  check('逐条时点第一行「定位」跳到第 6 行（第一个缺逐字的）', run('anchorRow'), 5);

  // 8) 清空会话必须把整个编辑器重置 —— 否则队列清空后表格/历史还留在屏幕上，
  //    撤销/重做变成无声无效（用户报告的「按键全部无效」就是这个状态）。
  run(`document.getElementById('clearSession').click()`);
  await wait(400);
  check('清空后队列为空', run('queue.length'), 0);
  check('清空后表格清空', run('document.querySelectorAll("#rows tr").length'), 0);
  check('清空后历史清空', run('document.querySelectorAll("#historyList .hstep").length'), 0);
  check('清空后预览复位', run('document.getElementById("previewMain").textContent'), '等待载入歌词');
  win.document.getElementById('undo').click();
  await wait(300);
  check('无选中时撤销给出提示', /请先在左侧队列/.test(String(run('document.getElementById("message").textContent'))), true);

  dom.window.close();
  console.log(`\n${checks} checks, ${failures} failed`);
  process.exit(failures ? 1 : 0);
})().catch((e) => {
  console.error('FAIL e2e: ' + e.message);
  process.exit(1);
});
