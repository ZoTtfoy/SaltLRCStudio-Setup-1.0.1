/* 本地工程库 —— layer 4（页面侧）。
 *
 * 需求：每导入一个音频/歌词文件就在本地建一个工程；第一次打开软件问一次保存
 * 路径；退出时问两次（保留还是删掉这次建的工程文件）。
 *
 * 三条规则，各只有一个权威：
 *
 *   ① 「会不会建工程」只由 info.configured 决定（没有保存路径就**不建**，
 *      也绝不假装建了）。路径的唯一来源是 L2 的 projects.root()。
 *   ② 「什么时候该写盘」只有一个触发点：history.js 的 persist()（也就是每次
 *      改动之后）。导入、编辑、撤销/重做都汇到那儿，不给导入单独开一条路。
 *   ③ 「该删哪些」只有一处：L2 的 projects.discard_session()。页面不拼路径、
 *      不自己删文件，只传工程 id。
 *
 * 与 studio.js 共享全局作用域，但**先于它加载**（加载顺序由
 * tests/test_web_pages.py 的脚本顺序测试兜底）：这里的顶层代码不碰 DOM，
 * 所有绑定都在 start() 里做。
 */

const Project = {
  // 保存路径与配置状态（来自 /api/projects/config）
  info: {root: null, configured: false, suggested: '', session: []},
  // 队列条目 -> 工程 id（null = 问过了但没建/建不成，用来避免反复重试）
  ids: new Map(),
  // 条目 -> 上次写下去时的修订号。"该不该写"看**条目自己的修订号**，
  // 不看"谁是当前选中的歌"：一个迟到的匹配结果可能落在**没被选中**的那一条上。
  // 修订号由 history.js 的 persist() 加。
  written: new Map(),
  // 建工程失败过的条目；保存位置改好后重试（见 track()）
  failed: new Set(),
  // 每次成功建工程计数：退出确认里报给用户看的数字
  created: 0,
  // 有改动待写盘的条目。合并到一个定时器里，一次编辑高峰只落一次盘。
  // 它是个**集合**（记下是哪一条变了）而不是一个布尔标记：
  // 只问"这一条变了没"是 O(1)，扫全队列取指纹是 O(队列) 且每次都要序列化每个文档。
  pending: new Set(),
  // 写失败后的退避：条目 -> 最早能重试的时刻
  retry: new Map(),
  timer: 0,
  // 本次会话是否已经问过保存路径（问过就不再重复弹）
  asked: false,
  hinted: false,
  started: false,

  // ------------------------------------------------------------ 启动
  async start() {
    if (this.started) return;
    this.started = true;
    bind('projectUse', () => this.use(false));
    bind('projectBrowse', () => this.use(true));
    bind('projectLater', () => this.later());
    bind('projectChange', () => this.ask(true));
    try {
      // 一定要传 {}：api() 里是 JSON.stringify(body)，body 为 undefined 时
      // 请求就**没有请求体**，服务端按"空 body"判 400（这条规则保护所有 POST 路由）。
      this.info = Object.assign(this.info, await api('projects/config', {}));
    } catch (error) {
      // 后端没有这些路由（旧版本或被裁剪的构建）时静默降级。
      window.Trace?.fault?.('工程库不可用', String(error && error.message));
      return;
    }
    this.render();
    // 没有保存路径就问一次；问过的这一个会话不再重复弹，但下次启动仍会问。
    if (!this.info.configured) this.ask();
  },

  render() {
    const root = $('projectRoot');
    if (root) root.textContent = this.info.configured ? this.info.root : '未设置（本次不会自动保存工程）';
    const count = $('projectCount');
    if (count) {
      count.textContent = this.created
        ? `本次会话已自动保存 ${this.created} 个工程（每个工程一个文件夹，退出时再决定去留）`
        : '每导入一个音频/歌词文件就会在这里建一个工程。';
    }
  },

  // ------------------------------------------------------------ 保存路径
  ask(force) {
    const box = $('projectDialog');
    if (!box) return;
    if (this.asked && !force) return;
    this.asked = true;
    $('projectPath').value = this.info.root || this.info.suggested || '';
    $('projectState').textContent = '';
    box.showModal();
    window.Trace?.dialogOpen?.(box);
  },

  later() {
    $('projectDialog').close();
    this.hint();
  },

  hint() {
    if (this.hinted) return;
    this.hinted = true;
    say('还没有设置工程保存位置，本次不会自动建工程 —— '
        + '随时可以在「检查与工程」页点「设置保存位置」补上。');
  },

  async use(pick) {
    const state = $('projectState');
    state.textContent = pick ? '正在等待文件夹选择…' : '';
    let result;
    try {
      result = await api('projects/root', pick ? {pick: true} : {path: $('projectPath').value});
    } catch (error) {
      // 弹窗里的错误就地显示：状态行在弹窗背后，只写那儿等于没提示。
      state.textContent = error.message;
      return;
    }
    if (result.status === 'cancelled') {
      state.textContent = '已取消选择，工程保存位置没有改变。';
      return;
    }
    this.info = Object.assign(this.info, result);
    $('projectPath').value = result.root;
    $('projectDialog').close();
    this.render();
    say('工程将保存到 ' + result.root + '（每导入一个文件就在里面建一个工程）。');
    // 换了保存位置 = 之前失败过的值得再试一次。
    this.failed.clear();
    window.Notify?.clearBanner?.('project-create');
    // 设路径之前导入的东西也补上工程。
    await this.backfill();
  },

  // ------------------------------------------------------------ 建 / 写
  /** 给一个队列条目建工程。幂等：同一条只会建一次。 */
  async track(item) {
    if (!item) return null;
    const known = this.ids.get(item);
    if (known) return known;                 // 已经有工程了
    // 失败过的先不重试：repaintQueue 每次编辑都会被调用，一个必然失败的建工程
    // 会变成一串持续报错的请求。等保存位置改好（use() 会清空这个集合）再补上。
    if (this.failed.has(item)) return null;
    this.ids.set(item, null);
    if (!this.info.configured) { this.hint(); return null; }
    let record;
    try {
      record = await api('projects/create', {name: item.name});
    } catch (error) {
      this.failed.add(item);
      say('建工程失败：' + error.message + '（不再反复重试；改好保存位置后会自动补上）');
      // 挂横幅而不是弹框：一次「导入文件夹」可能连着失败十几条，
      // 横幅只在顶部占一行，还能说清怎么补救。
      window.Notify?.banner?.('project-create',
        '有文件没能建工程（' + item.name + '）：' + (error.message || error)
        + '　改好保存位置后会自动补上。');
      return null;
    }
    this.ids.set(item, record.id);
    this.created++;
    this.render();
    this.sync(item, true);
    window.Trace?.step?.('工程已建', item.name + ' → ' + record.dir);
    return record.id;
  },

  /** 队列里还没建工程的条目全部补上。由 repaintQueue() 调用 —— 那是"队列变了"
   *  的唯一出口，所有导入路径（文件、文件夹、曲库、交接、恢复上次会话）都经过它。 */
  trackAll(items) {
    for (const item of items || []) this.track(item);
  },

  /** 设好保存位置之后，把之前导入的内容补上工程。 */
  async backfill() {
    for (const item of queue) {
      if (this.ids.get(item) || this.failed.has(item)) continue;
      await this.track(item);
    }
  },

  /** 文档变了 → 排一次写盘。`item` 是**变的那一条**，必须传。 */
  sync(item, immediate) {
    if (item) this.pending.add(item);
    if (immediate) return this.flush();
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.flush(), 800);
  },

  /** 失败横幅的一把 key：同一首歌反复失败只占一行。 */
  bannerKey(item) {
    return 'project:' + (item.sid || item.name);
  },

  /**
   * 只写**真正变过**的那些工程。
   *
   * `persist()` 记下是哪一条变了，这里比一个修订号 —— 该不该写是 O(1)，
   * 只有真的变了的那几条才会被序列化。
   *
   * 写盘失败会退避 5 秒重试，并在顶部挂一条横幅，写成功了自己收掉。
   */
  async flush() {
    clearTimeout(this.timer);
    const due = [...this.pending];
    this.pending.clear();
    const now = Date.now();
    let deferred = false;
    for (const item of due) {
      if ((this.retry.get(item) || 0) > now) { this.pending.add(item); deferred = true; continue; }
      const id = this.ids.get(item);
      if (!id) continue;                       // 还没建工程；建好时 track() 会再排一次
      const rev = item.rev || 0;
      if (this.written.get(item) === rev) continue;
      try {
        // 不内嵌音频：工程是"歌词 + 歌名"的存档，音频留在曲库/文件夹里。
        await api('projects/write', {id, text: JSON.stringify(Doc.packProject(item, null))});
        this.written.set(item, rev);
        this.retry.delete(item);
        window.Notify?.clearBanner?.(this.bannerKey(item));
      } catch (error) {
        this.retry.set(item, Date.now() + 5000);
        this.pending.add(item);
        deferred = true;
        window.Notify?.banner?.(this.bannerKey(item),
          '工程自动保存失败（' + item.name + '）：' + (error.message || error)
          + '　已在后台每 5 秒重试；歌词不会丢，也可以点「保存工程…」先留一份。');
      }
    }
    if (deferred) {
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.flush(), 5000);
    }
  },

  // ------------------------------------------------------------ 退出
  /**
   * 退出前的两次确认。**同步**返回：'keep' / 'discard' / false（不退出）。
   *
   * **同步**返回：调用方（退出按钮）在同一个事件里就把本地会话清掉，不等一串 await。
   */
  decideExit() {
    const count = this.created;
    if (!count) {
      // 这次什么工程都没建 —— 没有"保留还是删除"的问题，照旧问一句。
      return confirm('确定退出 SaltLRC Studio？\n\n'
                     + '退出会清空本次编辑会话（队列与自动保存）。') ? 'keep' : false;
    }
    const where = this.info.root || '（保存位置）';
    const keep = confirm(
      `退出前先确认工程怎么处理（本次会话在本地建了 ${count} 个工程）。\n\n`
      + `保存位置：${where}\n\n`
      + '退出后要保留这些工程吗？\n\n'
      + '　【确定】= 保留，文件留在上面这个文件夹里\n'
      + '　【取消】= 不保留，删掉本次会话建的这些工程文件');
    // 第二问：把刚才那个选择再问一遍；两次都答同一个答案才会真的执行。
    const again = keep
      ? confirm(`再次确认：保留这 ${count} 个工程。\n\n`
                + `它们会一直留在 ${where} 里，下次可以用「打开工程」手动载入。`)
      : confirm(`再次确认：删除本次会话新建的 ${count} 个工程文件。\n\n`
                + '删除后不可恢复。只会删这次会话建的这几个工程文件夹，'
                + '不会动保存位置里别的东西。\n\n'
                + '（想先手动留一份：点「取消」，再用顶部的「保存工程…」导出 .saltproj。）');
    if (!again) {
      // 两次答案不一致 = 没拿定主意。此时**什么都不做**并取消退出 ——
      // 删除是不可逆的，含糊的答复绝不能当成同意。
      window.Trace?.step?.('退出取消', '两次确认不一致：工程未被改动');
      say('已取消退出：工程没有被改动。想退出请再点一次「退出」。');
      return false;
    }
    window.Trace?.step?.('退出确认', (keep ? '保留' : '删除') + ' ' + count + ' 个工程');
    return keep ? 'keep' : 'discard';
  },

  /** 决定之后真正干活：先把最后一次改动落盘，再保留 / 删除。 */
  async finishExit(decision) {
    await this.flush();
    if (decision === 'keep') {
      try { await api('projects/keep', {}); } catch (error) { say('清理会话记录失败：' + error.message); }
      return;
    }
    try {
      const result = await api('projects/discard', {});
      window.Trace?.step?.('工程已删除', result.removed + ' 个' +
        (result.failed.length ? '；失败 ' + result.failed.length : ''));
      say(`已删除本次会话的 ${result.removed} 个工程文件。`
          + (result.failed.length ? '\n有 ' + result.failed.length + ' 个没删掉：' + result.failed.join('；') : ''));
    } catch (error) {
      say('删除工程失败：' + error.message);
    }
  },
};
