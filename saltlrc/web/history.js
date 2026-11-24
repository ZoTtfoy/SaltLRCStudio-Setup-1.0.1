/* 历史与自动保存 —— 从 studio.js 拆出。
 *
 * 撤销/重做与"跳到任意一点"都是**同一条时间轴上的游标移动**：
 * 没有独立的重做分支要维护，两种快照也不会互相矛盾。
 *
 * 这里是普通 script（不是模块），与 studio.js 共享全局作用域，
 * 所以函数声明照旧可被 studio.js 调用 —— 前提是它**先于** studio.js 加载。
 * index.html 里的顺序由静态测试
 * (test_no_script_binds_an_element_declared_later_in_the_page) 兜底。
 */

// Every edit appends one state to a single timeline with a cursor. Undo,
// redo and "jump to any point" are all just cursor moves, which is what makes
// the model provably correct — there is no separate redo branch to keep in
// sync, and no way for the two snapshot kinds to disagree.
/** 每个"编辑"的入口都必须拿到**歌曲条目**，而不是一个标签。
 *
 * 这是"改完了却什么都没记录"那一类缺陷的唯一守卫。`remember()` 的签名是
 * `(item, label, next)`，而且三个参数都有默认值 —— 所以把标签写进第一个参数
 * **不会报错**：`timeline()` 会在那个字符串上建时间轴（给原始值赋属性静默失败），
 * 于是返回 `undefined`，紧接着 `line.steps` 抛 TypeError。后果是最坏的一种：
 * **文档已经被改过，但既没进历史（撤不回来）也没落盘（工程还是旧的）**，
 * 而且异常把后面的 `refresh()` 一起带走，界面停在旧样子。
 *
 * 传错时的处理刻意选"出声"而不是"兜底"：兜底会让它继续静默地丢编辑记录。
 */
function assertEntry(item, where) {
  if (item && item.document && typeof item.document === 'object') return item;
  const message = '内部错误：' + where + '() 的第一个参数必须是歌曲条目，收到 ' +
    (typeof item === 'string'
      ? '字符串「' + item + '」（像是把标签写在了第一个参数上）'
      : String(typeof item));
  window.Trace?.fault?.('编辑未记录', message);
  throw Error(message);
}

function timeline(item = current()) {
  if (!item) return null;
  assertEntry(item, 'timeline');
  if (!item.timeline) {
    item.timeline = {steps: [{label: '载入', document: clone(item.document)}], cursor: 0};
  }
  return item.timeline;
}

// Call AFTER the mutation: without ``next`` the (already mutated) document is
// recorded; with it, the given document is applied first.
function remember(item = current(), label = '编辑', next = null) {
  if (!item) return;
  assertEntry(item, 'remember');
  const line = timeline(item);
  const document = clone(next === null ? item.document : next);
  if (next !== null) item.document = document;
  line.steps.splice(line.cursor + 1);
  line.steps.push({label, document});
  if (line.steps.length > 80) line.steps.shift();
  line.cursor = line.steps.length - 1;
  renderHistory();
  persist(item);
}

/** 文档被**整份换掉**（迟到的联网歌词、手动锚定候选）时，把历史重新以新文档为基线。
 *
 * 它们不是"编辑"，而是"这首歌终于有歌词了"。沿用旧时间轴时，
 * 「载入」还是换之前那份（**空文档**），于是用户随后删一行、再撤销一步 ——
 * 时间轴把他带回「载入」，**整份歌词当场清空**。
 *
 * 顺带落盘：换进来的歌词原本要等下一次编辑才会写进工程（`persist()` 只有
 * `remember`/`goTo` 会调），这也是"工程里没有歌词"的一个来源。
 */
function resetHistory(item = current()) {
  if (!item) return;
  assertEntry(item, 'resetHistory');
  item.timeline = {steps: [{label: '载入', document: clone(item.document)}], cursor: 0};
  renderHistory();
  persist(item);
}

function goTo(index, verb) {
  const item = current();
  if (!item) { say('请先在左侧队列选择一首歌'); return false; }
  const line = timeline(item);
  if (!(index >= 0 && index < line.steps.length)) { say('没有可' + verb + '的操作'); return false; }
  if (index === line.cursor) return false;
  line.cursor = index;
  item.document = clone(line.steps[index].document);
  refresh(); renderHistory(); persist(item);
  return true;
}

function undo() {
  const item = current();
  if (!item) { say('请先在左侧队列选择一首歌'); return false; }
  return goTo(timeline(item).cursor - 1, '撤销');
}

function redo() {
  const item = current();
  if (!item) { say('请先在左侧队列选择一首歌'); return false; }
  return goTo(timeline(item).cursor + 1, '重做');
}

function jumpTo(index) {
  const item = current();
  if (!item) return false;
  return goTo(index, '跳转到');
}

function renderHistory() {
  const item = current(), box = $('historyList');
  if (!box) return;
  box.replaceChildren();
  if (!item) return;
  const line = timeline(item);
  for (let i = line.steps.length - 1; i >= 0; i--) {
    const step = line.steps[i], isNow = i === line.cursor;
    const entry = document.createElement(isNow ? 'div' : 'button');
    if (!isNow) entry.type = 'button';
    entry.className = 'hstep' + (isNow ? ' now' : '');
    entry.textContent = (i === 0 ? '初始' : i + '. ' + step.label) + (isNow ? '　← 当前' : '');
    if (!isNow) {
      entry.title = i < line.cursor ? '恢复到这一步（其后的操作进入可重做区）' : '重做到这一步';
      entry.onclick = () => jumpTo(i);
    }
    box.append(entry);
  }
  if (line.steps.length <= 1) {
    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = '每次改动都会记录在这里，点任意一步就能回到那时。';
    box.append(hint);
  }
}

// ---- auto-save: survives an abnormal close, never a normal exit ------------
//
// The queue is per-session. Nothing is loaded at launch: a saved session is
// only *offered*, and joining it is an explicit click. Loading it straight
// into the editor collides with the library hand-off, which also selects at
// boot — two async chains then race (the restore's select(0) and the
// hand-off's select(last)), and the queue grows by a song every session,
// as each launch restores N and then re-saves N + whatever was added.
//
// The 退出 button clears the storage, so a normal quit starts clean next
// time; the saved session only survives an abnormal close, and is offered
// once when the page loads.
const SESSION_KEY = 'saltlrc-studio-session';            // 索引（条目清单），很小
const SESSION_ITEM = 'saltlrc-studio-session:item:';     // 每一条文档**单独**存
let savedSession = null;
let saveTimer = 0;
let sessionPending = new Set();
let sessionSeq = 0;
let sessionQuota = false;

/** 给条目一个稳定的 id：localStorage 的键不能靠队列下标（删一条就全错位），
 *  也不能靠文件名（同一首歌在两个文件夹里是两条）。 */
function sessionId(item) {
  if (!item.sid) item.sid = (++sessionSeq).toString(36) + '-' + Date.now().toString(36);
  return item.sid;
}

/**
 * 文档变了 → 排一次落盘。
 *
 * **必须传条目**：`item` 说明"哪一条变了"，落盘只针对这一条。
 * 不传就要对**整个队列**逐个 `JSON.stringify` 取指纹 —— 300 首的队列每编辑一次就是
 * 1 秒的主线程开销（`tests/_bench_io.py` 的 F 段：1039ms）。
 * 传条目后是"记下这一条 + 比修订号"：O(1) 而不是 O(队列)。
 *
 * `item.rev` 是"这份文档变过几次"的唯一权威，而 `persist()` 是**唯一**会加它的地方 ——
 * 因为 persist() 本来就是"每次改动的唯一出口"（导入、编辑、撤销/重做都汇到这里，
 * 由 `tests/test_web_pages.py` 的静态断言钉着）。少一个需要跟所有编辑路径保持一致的前提。
 */
function persist(item = current()) {
  if (item) {
    item.rev = (item.rev || 0) + 1;
    Project.sync(item);
    sessionPending.add(item);
  }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flushSession, 500);
}

/**
 * 队列变了 → 把**新增的条目**排进会话快照。
 *
 * 落盘按"一条一个键、只写变过的那条"进行，所以导入进来但
 * 还没编辑过的歌**没人给它写快照** —— 崩溃恢复时那些歌会消失。
 * 本函数把这件事**显式**做掉：任何进入队列的条目都排进待写集合。
 * 挂在 `repaintQueue()` 上（"队列变了"的唯一出口），和建工程同一个位置。
 */
function touchSession(items) {
  for (const item of items || []) if (item) sessionPending.add(item);
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flushSession, 500);
}

/**
 * 把待写的那几条落进 localStorage。
 *
 * **一条一个键**：整队列不拼成一个大 JSON 全写一遍，一次编辑只写变过的那首
 *（24KB 而不是 7MB），不是 O(队列)。
 * 索引里只放 `[id, 名字]`，所以它很小（300 首约 9KB），每批都写也不心疼。
 *
 * 配额不足**不静默**：静默吞掉写入失败会让"每次启动都是空队列"看起来像设计；
 * 这里挂一条横幅说清楚，并报出能存下几条。
 */
function flushSession() {
  clearTimeout(saveTimer);
  if (!queue.length) {
    // An empty queue must not leave a stale session behind — but it also must
    // not wipe an offer the user has not answered yet.
    if (!hasSavedSession()) forgetSession();
    return;
  }
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify({
      at: Date.now(), selected, v: 2,
      entries: queue.map((item) => [sessionId(item), item.name]),
    }));
  } catch (error) {
    complainSession(0, queue.length, error);
    sessionPending.clear();
    return;
  }
  let saved = 0;
  for (const item of sessionPending) {
    try {
      localStorage.setItem(SESSION_ITEM + sessionId(item),
                           JSON.stringify({name: item.name, document: item.document}));
      saved++;
    } catch (error) {
      complainSession(saved, queue.length, error);
      sessionPending.clear();
      return;
    }
  }
  sessionPending.clear();
  if (sessionQuota) { sessionQuota = false; window.Notify?.clearBanner?.('session'); }
}

function complainSession(saved, total, error) {
  if (sessionQuota) return;                 // 同一件事只挂一条横幅（反复失败别刷屏）
  sessionQuota = true;
  window.Notify?.banner?.('session',
    '自动保存会话失败：浏览器存储空间不够了（已存 ' + saved + ' / ' + total + ' 首）。'
    + '歌词内容不会丢 —— 工程库里那份还在，也可以用「保存工程…」留档。'
    + '（' + ((error && error.name) || error) + '）');
}

/** Read the saved session without touching the queue. */
function readSession() {
  try {
    const index = JSON.parse(localStorage.getItem(SESSION_KEY) || 'null');
    if (!index || Date.now() - (index.at || 0) > 7 * 864e5) return null;
    // v1 格式：整队列一个大 JSON（`index.items`）。读到它就当兼容路径，别丢恢复的机会。
    if (Array.isArray(index.items)) {
      const items = index.items.filter((entry) => entry && entry.document && entry.document.groups);
      return items.length ? items : null;
    }
    if (!Array.isArray(index.entries)) return null;
    const items = [];
    for (const pair of index.entries) {
      if (!Array.isArray(pair)) continue;
      try {
        const saved = JSON.parse(localStorage.getItem(SESSION_ITEM + pair[0]) || 'null');
        if (saved && saved.document && saved.document.groups) {
          items.push({name: saved.name || pair[1] || '未命名.lrc', document: saved.document});
        }
      } catch { /* 这一条坏了不影响其它条 */ }
    }
    return items.length ? items : null;
  } catch { return null; }
}

/** Whether a recoverable session was waiting when this page loaded. */
function hasSavedSession() {
  return !!(savedSession && savedSession.length);
}

/** Join the offered session — the only path that loads saved documents. */
function adoptSession() {
  if (!hasSavedSession()) return 0;
  let count = 0;
  for (const entry of savedSession) {
    queue.push({name: entry.name, document: normalizeDocument(entry.document),
                report: {errors: [], warnings: []}, history: []});
    count++;
  }
  savedSession = null;
  refreshQueueHint();
  repaintQueue();
  return count;
}

function refreshQueueHint() {
  const button = $('restoreSession');
  if (!button) return;
  button.hidden = !hasSavedSession();
  button.textContent = hasSavedSession() ? `恢复上次会话（${savedSession.length}）` : '恢复上次会话';
}

function forgetSession() {
  try {
    localStorage.removeItem(SESSION_KEY);
    // 条目是**分散**存的：只删索引会留下一地孤儿键（下次会话的 id 不同，
    // 那些数据永远读不回来，还白占配额）。所以按前缀扫一遍，把残留的条目
    // （含崩溃时留下的）也一起收掉。
    const doomed = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && key.indexOf(SESSION_ITEM) === 0) doomed.push(key);
    }
    for (const key of doomed) localStorage.removeItem(key);
  } catch { /* 忽略 */ }
  sessionPending.clear();
  sessionQuota = false;
  window.Notify?.clearBanner?.('session');
  savedSession = null;
  refreshQueueHint();
}

function clearSession() {
  forgetSession();
  queue.splice(0, queue.length);
  selected = -1;
  resetEditor();
  repaintQueue();
  say('已清空自动保存的会话（歌词内容并未删除磁盘上的文件）。');
}
// The editor goes back to a blank slate whenever nothing is selected —
// stale rows/history/waveform on screen make every current()-driven
// control (undo/redo, row actions, export) dead: they act on nothing.
