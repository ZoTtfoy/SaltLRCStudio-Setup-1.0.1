const $ = (id) => document.getElementById(id);

const state = {
  tracks: [],
  current: null,
  candidates: [],
  main: [],
  translation: [],
  doc: null,
  text: '',
  wordLevel: false,
  songId: null,
  dirty: false,
};

async function api(path, body) {
  const response = await fetch('/api/' + path, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || ('请求失败 ' + response.status));
  return data;
}

async function get(path) {
  const response = await fetch('/api/' + path);
  const data = await response.json();
  if (!response.ok) throw Error(data.error || ('请求失败 ' + response.status));
  return data;
}

function say(text) { $('message').textContent = text; }

function clock(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  return Math.floor(total / 60) + ':' + String(total % 60).padStart(2, '0');
}

function stamp(ms) {
  return String(Math.floor(ms / 60000)).padStart(2, '0') + ':' +
         String(Math.floor(ms / 1000) % 60).padStart(2, '0') + '.' +
         String(ms % 1000).padStart(3, '0');
}

function normalize(text) {
  return (text || '').toLowerCase().replace(/[\s\u3000,，.。!！?？、~～\-–—_'"“”‘’()（）[\]【】/\\:：;；+*&#]+/g, '');
}

function score(candidate, track) {
  let value = 0;
  const title = normalize(candidate.title), want = normalize(track.title);
  if (title && title === want) value += 60;
  else if (title && want && (title.includes(want) || want.includes(title))) value += 35;
  const artist = normalize(candidate.artist), wantArtist = normalize(track.artist);
  if (artist && wantArtist) {
    if (artist === wantArtist) value += 30;
    else if (artist.includes(wantArtist) || wantArtist.includes(artist)) value += 18;
    else value -= 8;
  }
  if (candidate.duration && track.duration) {
    const gap = Math.abs(candidate.duration - track.duration);
    value += gap <= 1500 ? 25 : gap <= 4000 ? 12 : gap <= 10000 ? 2 : -15;
  }
  return value;
}

function buildOptions() {
  return {unit: $('unit').value, tail: Number($('tail').value),
          max_line_ms: Number($('maxLine').value)};
}

function exportFlags() {
  return {translation_timestamp: $('transStamp').checked, overwrite: $('overwrite').checked,
          provider: $('provider').value, format: $('format').value, encoding: $('encodingOut').value};
}

function outExtension() {
  return $('format').value === 'yrc' ? '.yrc' : '.lrc';
}

// ---------------------------------------------------------------- library
function visibleTracks() {
  const keyword = $('filter').value.trim().toLowerCase();
  return state.tracks.filter((track) => {
    if ($('hideDone').checked && track.has_lyric) return false;
    if (!keyword) return true;
    return (track.title + track.artist + track.album).toLowerCase().includes(keyword);
  });
}

function renderQueue() {
  const list = visibleTracks();
  $('count').textContent = list.length;
  $('queue').innerHTML = '';
  for (const track of list) {
    const row = document.createElement('div');
    row.className = 'item' + (state.current && state.current.id === track.id ? ' active' : '');
    row.dataset.id = track.id;
    // Two different selections live in one list, so they are labelled apart:
    // the checkbox feeds "批量匹配所选", the name/编辑 button opens the song.
    row.innerHTML =
      '<label class="pickWrap" title="勾选后由「批量匹配所选」一起处理">' +
      '<input type="checkbox" class="pick"' + (track.has_lyric ? '' : ' checked') +
      '><span>批量</span></label>' +
      '<div class="rowmain" title="点击打开这首歌编辑"><strong></strong><span class="meta"></span></div>' +
      '<button class="open" title="打开这首歌编辑">编辑</button>';
    row.querySelector('strong').textContent = track.title || '(无标题)';
    const meta = row.querySelector('.meta');
    const badge = document.createElement('b');
    badge.className = track.has_lyric ? 'ok' : 'missing';
    badge.textContent = track.has_lyric ? '已有歌词' : '缺歌词';
    meta.append(badge);
    const details = [track.artist, track.album].filter(Boolean).join(' · ');
    if (details) meta.append(document.createTextNode(' ' + details + ' · '));
    meta.append(document.createTextNode(clock(track.duration)));
    const open = () => selectTrack(track.id);
    row.querySelector('.rowmain').onclick = open;
    row.querySelector('.open').onclick = (event) => { event.stopPropagation(); open(); };
    $('queue').append(row);
  }
  updateBatchLabel();
}

function updateBatchLabel() {
  const count = document.querySelectorAll('#queue .pick:checked').length;
  $('batch').textContent = '④ 批量匹配所选（已勾选 ' + count + ' 首）';
}

async function loadLibrary() {
  say('正在读取 Salt Player 曲库…');
  const data = await get('library');
  state.tracks = data.tracks;
  $('libraryState').textContent = data.tracks.length + ' 首 · ' + data.db_path;
  renderQueue();
  say('已读取 ' + data.tracks.length + ' 首。选择一首开始获取歌词。');
}

function selectTrack(id) {
  const track = state.tracks.find((item) => item.id === id);
  if (!track) return;
  state.current = track;
  state.candidates = [];
  state.main = [];
  state.translation = [];
  state.doc = null;
  state.text = '';
  state.songId = null;
  state.dirty = false;
  $('currentTitle').textContent = track.title || '(无标题)';
  $('currentMeta').textContent = [track.artist, track.album, clock(track.duration),
                                  track.has_lyric ? '已有歌词' : '缺歌词'].filter(Boolean).join(' · ');
  $('query').value = [track.title, track.artist].filter(Boolean).join(' ');
  $('candidates').innerHTML = '';
  $('rows').innerHTML = '';
  $('report').textContent = '暂无检查结果';
  $('problems').hidden = true;
  $('problemList').replaceChildren();
  $('text').textContent = '';
  $('buildState').textContent = '尚未生成';
  $('batchResults').innerHTML = '';
  renderQueue();
  say('已选择「' + track.title + '」。点击搜索获取歌词，或在下方手动粘贴。');
}

// ----------------------------------------------------------------- search
async function searchOnline() {
  const query = $('query').value.trim();
  if (!query) throw Error('请输入搜索关键词');
  say('正在搜索…');
  const data = await api('search', {query, provider: $('provider').value, limit: Number($('limit').value)});
  state.candidates = data.candidates;
  renderCandidates();
  say(data.candidates.length ? '找到 ' + data.candidates.length + ' 个结果，选择一个载入。'
                             : '没有找到结果，试试只输入标题，或改用手动添加。');
}

function renderCandidates() {
  const box = $('candidates');
  box.innerHTML = '';
  for (const candidate of state.candidates) {
    const value = state.current ? score(candidate, state.current) : 0;
    const gap = state.current && candidate.duration
      ? Math.abs(candidate.duration - state.current.duration) / 1000 : null;
    const row = document.createElement('div');
    row.className = 'proposal';
    const label = document.createElement('span');
    label.textContent = candidate.title + ' — ' + candidate.artist +
      (candidate.album ? '（' + candidate.album + '）' : '') +
      ' · ' + clock(candidate.duration) +
      (gap === null ? '' : ' · 时长差 ' + gap.toFixed(1) + 's') +
      ' · 评分 ' + value.toFixed(0) + (value >= 45 ? ' 可信' : ' 偏低');
    const button = document.createElement('button');
    button.textContent = '载入歌词';
    button.onclick = async () => { try { await loadLyric(candidate.id); } catch (error) { say(error.message); } };
    row.append(label, button);
    box.append(row);
  }
}

async function loadLyric(songId) {
  say('正在获取歌词…');
  const payload = await api('fetch', {song_id: songId, provider: $('provider').value,
                                      drop_credits: $('dropCredits').checked});
  if (!payload.main.length) throw Error('该结果没有可用的歌词内容，换一个结果或手动添加');
  state.main = payload.main;
  state.translation = payload.translation;
  state.wordLevel = payload.word_level;
  state.songId = songId;
  state.dirty = false;
  await build();
  say('已载入 ' + payload.main.length + ' 行原文' +
      (payload.translation.length ? ' + ' + payload.translation.length + ' 行翻译' : '（无翻译）') +
      (payload.word_level ? '；使用来源逐字时间' : '；逐字时间为本地估算'));
}

// ------------------------------------------------------------------ build
function rows() {
  return Array.from(document.querySelectorAll('#rows tr'));
}

function editedLines() {
  const main = [], translation = [];
  for (const row of rows()) {
    const time = Number(row.dataset.time);
    const mainText = row.querySelector('.mainText').value.trim();
    if (!mainText) continue;
    main.push({time, text: mainText, words: []});
    const transText = row.querySelector('.transText').value.trim();
    if (transText) translation.push({time, text: transText, words: []});
  }
  return {main, translation};
}

async function build() {
  if (!state.main.length) throw Error('还没有歌词内容：先搜索或手动添加');
  let main = state.main, translation = state.translation;
  if (state.dirty && state.doc) {
    const edited = editedLines();
    if (edited.main.length) { main = edited.main; translation = edited.translation; }
  }
  const data = await api('build', Object.assign({
    main, translation,
    duration: state.current ? state.current.duration : null,
    options: buildOptions(),
    word_level: state.wordLevel && !state.dirty,
  }, exportFlags()));
  state.doc = data.document;
  state.text = data.text;
  state.dirty = false;
  renderRows();
  renderReport(data.report);
  $('text').textContent = data.text || '（存在错误，未生成导出文本）';
  const errors = data.report.errors.length;
  $('buildState').textContent = errors
    ? '有 ' + errors + ' 个错误需要修正（见上方「需要处理的问题」），修正后重新点「① 生成逐字预览」'
    : '已生成 ' + state.doc.groups.length + ' 行逐字歌词' +
      (state.wordLevel ? '（来源逐字）' : '（本地估算逐字）') + '，可以写入歌曲目录了';
  return data;
}

function renderRows() {
  const body = $('rows');
  body.innerHTML = '';
  if (!state.doc) return;
  state.doc.groups.forEach((group, position) => {
    const row = document.createElement('tr');
    row.dataset.time = group.start;
    row.dataset.index = position;
    const timeCell = document.createElement('td');
    timeCell.textContent = stamp(group.start);
    const mainCell = document.createElement('td');
    const mainInput = document.createElement('input');
    mainInput.className = 'mainText';
    mainInput.value = group.main.text;
    mainInput.style.width = '100%';
    mainInput.oninput = () => { state.dirty = true; };
    mainCell.append(mainInput);
    // A mini timeline makes bad word spacing visible at a glance; the exact
    // per-word times stay in the tooltip instead of a wall of text.
    const words = group.main.words || [];
    const end = group.main.end ?? group.start + 1;
    const span = Math.max(1, end - group.start);
    const line = document.createElement('div');
    line.className = 'wline';
    line.title = words.map((word) => word.text + ' ' + stamp(word.start)).join('  |  ') || '没有逐字时间';
    line.style.cursor = 'pointer';
    line.onclick = () => openWordEditor(position);
    for (const word of words) {
      const tick = document.createElement('i');
      tick.style.left = ((word.start - group.start) / span * 100).toFixed(2) + '%';
      line.append(tick);
    }
    const tail = document.createElement('b');
    line.append(tail);
    const note = document.createElement('div');
    note.className = 'wnote';
    note.textContent = words.length
      ? words.length + ' 字 · 跨度 ' + (span / 1000).toFixed(2) + 's · 末字到句尾 ' +
        ((end - words[words.length - 1].start) / 1000).toFixed(2) + 's · 点时间轴可逐字编辑'
      : '没有逐字时间';
    mainCell.append(line, note);
    const transCell = document.createElement('td');
    const transInput = document.createElement('input');
    transInput.className = 'transText';
    transInput.value = group.translation ? group.translation.text : '';
    transInput.style.width = '100%';
    transInput.oninput = () => { state.dirty = true; };
    transCell.append(transInput);
    row.append(timeCell, mainCell, transCell);
    body.append(row);
  });
}

// --------------------------------------------------- per-word timing editor
// Only the times are editable here. Each unit
// is a compact chip — the character on top (read-only; line text is edited in
// the main table) and its start time below. This halves the inputs on a 50-char
// line and removes the duplicate way to break text/timing consistency.
function openWordEditor(index) {
  const group = state.doc?.groups?.[index];
  if (!group) {
    // Silence here would make the whole editor look broken: the click on the
    // mini timeline does nothing and no message explains it.
    say('这一行还没有可编辑的歌词：先「生成逐字预览」，或点某一行的时间轴');
    return;
  }
  state.editingRow = index;
  const track = group.main;
  const grid = $('wordGrid');
  grid.replaceChildren();
  const words = track.words || [];
  const end = track.end ?? (state.doc.groups[index + 1]?.start ?? track.start + 2500);
  words.forEach((word, i) => {
    const cell = document.createElement('label');
    cell.className = 'wordCell';
    const text = document.createElement('span');
    text.className = 'wordChar';
    text.textContent = word.text.trim() ? word.text : '␣';   // visible placeholder for spaces
    text.title = '第 ' + (i + 1) + ' 个字/词 · ' + stamp(word.start);
    const time = document.createElement('input');
    time.type = 'number'; time.step = '0.001'; time.min = '0';
    time.value = (word.start / 1000).toFixed(3); time.className = 'wordTime';
    time.title = '开始时间：' + stamp(word.start);
    cell.append(text, time);
    grid.append(cell);
  });
  $('wordEnd').value = (end / 1000).toFixed(3);
  $('wordLineText').textContent = '第 ' + (index + 1) + ' 行 · 共 ' + words.length + ' 个字/词';
  $('wordEditError').textContent = '';
  $('wordEditDialog').showModal();
  // Tell the trace what the click produced: this is how "点了取消没反应" can be
  // told apart from "点到了，但对话框没关".
  window.Trace?.dialogOpen?.($('wordEditDialog'));
  window.Trace?.step?.('逐字编辑器已打开', '第 ' + (index + 1) + ' 行 · ' + words.length + ' 个字');
}

// Applying must ALWAYS say something.
//
// A silent no-op (`if (!group) return;`) would produce no message, no change
// and no close when the document was replaced behind the dialog (a fresh
// search, a reload), so the 应用 button would look dead. A refusal with a
// message is fine; silence is not.
async function applyWordEditor() {
  const index = state.editingRow;
  const group = state.doc?.groups?.[index];
  if (!group) {
    $('wordEditError').textContent = index >= 0
      ? '这一行已经不在当前歌词里了（可能被重新搜索或刷新替换）——请关掉重开'
      : '没有正在编辑的行——请先点某一行的时间轴';
    return;
  }
  const track = group.main;
  const original = track.words || [];
  const cells = Array.from($('wordGrid').children);
  if (!original.length) { $('wordEditError').textContent = '这一行没有逐字内容可编辑'; return; }
  const words = original.map((word, i) => ({
    text: word.text,
    start: Math.round(Number(cells[i]?.querySelector('.wordTime')?.value) * 1000),
  }));
  const end = Math.round(Number($('wordEnd').value) * 1000);
  if (words.some((word) => !Number.isFinite(word.start) || word.start < 0)) {
    $('wordEditError').textContent = '时间必须是 >= 0 的数字'; return;
  }
  const outOfOrder = words.findIndex((word, i) => i && word.start < words[i - 1].start);
  if (outOfOrder > 0) {
    $('wordEditError').textContent = '第 ' + (outOfOrder + 1) + ' 个字早于前一个字；相同毫秒允许（快歌词常这样），但不能倒退';
    return;
  }
  if (!Number.isFinite(end) || end <= words[words.length - 1].start) {
    $('wordEditError').textContent = '句尾时间必须晚于最后一个字'; return;
  }
  track.words = words;
  track.end = end;
  $('wordEditDialog').close();
  window.Trace?.step?.('已应用逐字时间', words.length + ' 个字 · 句尾 ' + (end / 1000).toFixed(3) + 's');
  renderRows();
  try {
    const report = await api('validate', {document: state.doc, salt: true,
                                          duration: state.current ? state.current.duration : null});
    renderReport(report);
    const result = await api('export', {document: state.doc, salt: true, format: $('format').value,
                                        encoding: outEncoding(),
                                        duration: state.current ? state.current.duration : null});
    state.text = result.text;
    $('text').textContent = state.text;
    $('buildState').textContent = '已更新第 ' + (index + 1) + ' 行的逐字时间，可以直接写入歌曲目录';
  } catch (error) { say('已更新，但重新检查失败：' + error.message); }
}

function rows() {
  return Array.from(document.querySelectorAll('#rows tr'));
}

function locateRow(index) {
  const row = rows()[index];
  if (!row) return;
  row.scrollIntoView({block: 'center'});
  row.classList.add('flash');
  row.querySelector('input')?.focus();
  setTimeout(() => row.classList.remove('flash'), 1200);
}

// A bare count like "37 个问题" is useless on its own: the user cannot see what
// is wrong or where. Each problem is listed, tied to its line, and clickable.
function renderProblems(report) {
  const panel = $('problems'), list = $('problemList');
  list.replaceChildren();
  for (const row of rows()) row.classList.remove('hasError', 'hasWarning');
  const items = report?.items || [];
  if (!items.length) { panel.hidden = true; return; }
  const errors = items.filter((item) => item.level === 'error').length;
  const warnings = items.length - errors;
  panel.hidden = false;
  $('problemCount').textContent = (errors ? errors + ' 个错误' : '没有错误') +
    (warnings ? ' · ' + warnings + ' 条提示' : '');
  const groups = state.doc ? state.doc.groups : [];
  for (const item of items.slice(0, 60)) {
    const row = document.createElement('div');
    row.className = 'proposal' + (item.level === 'error' ? ' err' : '');
    const text = document.createElement('span');
    const where = item.index != null && groups[item.index] ? '第 ' + (item.index + 1) + ' 行' : '整体';
    text.textContent = where + '：' + item.message;
    row.append(text);
    if (item.index != null && groups[item.index]) {
      const button = document.createElement('button');
      button.textContent = '定位';
      button.onclick = () => locateRow(item.index);
      row.append(button);
    }
    list.append(row);
  }
  if (items.length > 60) {
    const more = document.createElement('p');
    more.className = 'hint';
    more.textContent = '……还有 ' + (items.length - 60) + ' 条，展开下方「完整检查结果」查看全部。';
    list.append(more);
  }
  for (const item of items) {
    const row = rows()[item.index];
    if (row) row.classList.add(item.level === 'error' ? 'hasError' : 'hasWarning');
  }
}

function renderReport(report) {
  const lines = [];
  if (report.errors.length) lines.push('错误：', ...report.errors.map((item) => '  · ' + item));
  if (report.warnings.length) lines.push('提示：', ...report.warnings.map((item) => '  · ' + item));
  $('report').textContent = lines.length ? lines.join('\n') : '检查通过，没有错误或提示。';
  renderProblems(report);
}

async function ensureBuilt() {
  if (!state.doc || state.dirty) await build();
  if (!state.doc) throw Error('请先生成歌词');
}

// ------------------------------------------------------------------- save
function showSaveResult(result) {
  const panel = $('saveResult');
  panel.hidden = false;
  panel.replaceChildren();
  const head = document.createElement('strong');
  head.textContent = '✓ 已写入 ' + result.bytes.toLocaleString() + ' 字节（' +
    $('format').value + ' · ' + $('encodingOut').value + '）';
  const path = document.createElement('code');
  path.textContent = result.path;
  path.title = '点击复制完整路径';
  path.onclick = () => { navigator.clipboard?.writeText(result.path); say('路径已复制到剪贴板'); };
  const actions = document.createElement('div');
  actions.className = 'toolbar';
  const reveal = document.createElement('button');
  reveal.textContent = '在文件夹中显示';
  reveal.onclick = () => guard(async () => { await api('open-folder', {path: result.path}); });
  const studio = document.createElement('button');
  studio.className = 'button';
  studio.textContent = '在波形编辑器中微调 →';
  studio.onclick = () => guard(openInStudio);
  actions.append(reveal, studio);
  panel.append(head, path, actions);
}

async function save() {
  const track = state.current;
  if (!track) throw Error('先在左侧选择一首歌曲');
  await ensureBuilt();
  const target = track.lyric_path.replace(/\.lrc$/i, '') + outExtension();
  const data = await api('save', Object.assign({
    document: state.doc, path: target, track_id: track.id,
    song_id: state.songId, title: track.title, duration: track.duration,
  }, exportFlags()));
  if (data.status === 'skipped') {
    $('saveResult').hidden = true;
    say('该歌曲已有歌词，未覆盖。勾选「覆盖已有歌词」后重试。');
    return;
  }
  track.has_lyric = outExtension() === '.lrc';
  renderQueue();
  showSaveResult(data);
  say('已写入歌曲目录。' + (outExtension() === '.lrc'
    ? '在 Salt Player 中重新扫描或下一首即可看到。'
    : '注意：这是 .yrc，Salt Player 不会读取。'));
}

// Save the generated lyric into a folder the user picks, instead of next to the song.
//
// The folder is chosen and the file is written by the local backend, not by the
// browser: showDirectoryPicker() can create the file and then silently refuse
// the actual write, which would ship as a 0-byte export. The backend writes
// through the same code path as 「写入歌曲目录」, so failures surface as a
// readable Chinese message and the real path is shown afterwards.
async function exportToFolder() {
  if (!state.text) throw Error('请先点「① 生成逐字预览」');
  const name = (state.current ? state.current.title : 'lyrics').replace(/[\\/:*?"<>|]/g, '_') + outExtension();
  say('请在弹出的文件夹窗口里选择位置…（窗口若被浏览器挡住，请查看任务栏）');
  let data;
  try {
    data = await api('export-folder', Object.assign({name, document: state.doc}, exportFlags()));
  } catch (error) {
    // Backend unreachable or refused: the download path always works.
    return exportAsDownload(name, '本机导出失败：' + error.message + '。已改为直接下载「' + name + '」。');
  }
  if (data.status === 'cancelled') { say('已取消，未导出。'); return; }
  showSaveResult(data);
  say((data.fallback ? data.fallback + '，已改存到 ' : '已导出到 ') + data.path);
}

function exportAsDownload(name, message) {
  download(name, toBlobBytes(state.text), 'text/plain;charset=utf-8');
  say(message);
}

function toBlobBytes(text) {
  return outEncoding() === 'utf-8-bom' ? '\ufeff' + text : text;
}

// Hand the current lyric (and the song it belongs to) to the waveform editor.
function openInStudio() {
  if (!state.doc) throw Error('请先点「① 生成逐字预览」');
  const track = state.current;
  sessionStorage.setItem('saltlrc-handoff', JSON.stringify({
    name: (track ? track.title : 'lyrics').replace(/[\\/:*?"<>|]/g, '_') + '.lrc',
    trackId: track ? String(track.id) : null,
    audioName: track ? libraryFileName(track) : null,
    document: state.doc,
    at: Date.now(),
  }));
  location.href = '/studio.html?from=library';
}

function libraryFileName(track) {
  const raw = (track.path || '').split('/').pop();
  try { return decodeURIComponent(raw); } catch { return raw; }
}

async function applyManual() {
  const data = await api('manual', {main_text: $('manualMain').value,
                                    translation_text: $('manualTrans').value,
                                    drop_credits: $('dropCredits').checked});
  state.main = data.main;
  state.translation = data.translation;
  state.wordLevel = false;
  state.songId = null;
  state.dirty = false;
  await build();
  say('已解析手动歌词：' + data.main.length + ' 行原文' +
      (data.translation.length ? ' + ' + data.translation.length + ' 行翻译' : '（无翻译）'));
}

// ------------------------------------------------------------------ batch
function picked() {
  const ids = new Set(Array.from(document.querySelectorAll('#queue .pick'))
    .filter((box) => box.checked)
    .map((box) => box.closest('.item').dataset.id));
  return state.tracks.filter((track) => ids.has(track.id));
}

async function runBatch() {
  const tracks = picked();
  if (!tracks.length) throw Error('先在左侧勾选要处理的歌曲');
  if (!confirm('将对 ' + tracks.length + ' 首歌曲联网匹配歌词并写入歌曲目录，继续？')) return;
  say('正在批量匹配 ' + tracks.length + ' 首，请稍候…');
  const data = await api('batch', Object.assign({
    tracks: tracks.map((track) => ({id: track.id, title: track.title, artist: track.artist,
                                    album: track.album, duration: track.duration,
                                    lyric_path: track.lyric_path})),
    options: buildOptions(),
  }, exportFlags()));
  const box = $('batchResults');
  box.innerHTML = '';
  for (const item of data.results) {
    const row = document.createElement('div');
    row.className = 'proposal';
    row.innerHTML = '<span></span>';
    row.querySelector('span').textContent =
      (item.title || '(无标题)') + ' — ' + (item.artist || '') + '：' +
      ({written: '已写入', skipped: '跳过（已有歌词）', needs_review: '待人工确认', failed: '失败'}[item.status] || item.status) +
      (item.detail ? ' · ' + item.detail : '');
    box.append(row);
  }
  for (const track of tracks) {
    const found = data.results.find((item) => item.track_id === track.id);
    if (found && found.status === 'written' && outExtension() === '.lrc') track.has_lyric = true;
  }
  renderQueue();
  const summary = data.summary;
  say('批量完成：写入 ' + summary.written + ' · 跳过 ' + summary.skipped +
      ' · 待确认 ' + summary.needs_review + ' · 失败 ' + summary.failed);
}

// -------------------------------------------------------------------- wire
$('reload').onclick = () => loadLibrary().catch((error) => say(error.message));
$('filter').oninput = renderQueue;
$('hideDone').onchange = renderQueue;
$('selectAll').onclick = () => document.querySelectorAll('#queue .pick').forEach((box) => { box.checked = true; });
$('selectNone').onclick = () => document.querySelectorAll('#queue .pick').forEach((box) => { box.checked = false; });

async function guard(fn) { try { await fn(); } catch (error) { say(error.message); } }
$('search').onclick = () => guard(searchOnline);
$('query').onkeydown = (event) => { if (event.key === 'Enter') guard(searchOnline); };
$('build').onclick = () => guard(async () => { await build(); say('已重新生成逐字时间轴。'); });
$('manualApply').onclick = () => guard(applyManual);
$('save').onclick = () => guard(save);
$('exportFolder').onclick = () => guard(exportToFolder);
$('batch').onclick = () => guard(runBatch);
$('queue').addEventListener('change', (event) => {
  if (event.target.classList && event.target.classList.contains('pick')) updateBatchLabel();
});
// The backend runs headless, so the footer is where you stop it.
$('exitApp').onclick = (event) => {
  event.preventDefault();
  if (!confirm('确定退出 SaltLRC Studio？正在编辑的内容已自动保存到浏览器。')) return;
  fetch('/api/exit', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})
    .finally(() => { document.title = '已退出'; say('后端已退出，本页不再有响应。'); });
};
bind('applyWords', applyWordEditor);
bind('closeWordEdit', () => {
  $('wordEditDialog').close();
  $('wordEditError').textContent = '';
  window.Trace?.step?.('已取消逐字编辑', '未改动文档');
});
// Show the real version in the header so the page never looks stale, and build
// the source dropdown from the backend catalogue.
//
// The list is generated rather than written into the HTML: a single hardcoded
// <option> would mean editing the page as well as the backend whenever a source
// was added.
get('capabilities').then((data) => {
  const badge = document.getElementById('appVersion');
  if (badge && data.version) badge.textContent = 'v' + data.version;
  fillProviders(data);
}).catch(() => {});

// 主题的权威是设置（settings.json）。首屏那份缓存只负责"别闪一下"，
// 这里把它校准回来 —— 否则清过缓存或首次直接打开本页时会停在默认深色上。
api('settings', {}).then((settings) => {
  Theme.apply((settings.features || {}).darkMode === false ? 'light' : 'dark');
}).catch(() => {});

function fillProviders(data) {
  const select = $('provider');
  const catalogue = data.provider_catalogue || [];
  const previous = select.value;
  select.innerHTML = '';
  for (const entry of catalogue) {
    const option = document.createElement('option');
    option.value = entry.id;
    option.textContent = entry.label;
    option.dataset.note = entry.login_note || '';
    select.append(option);
  }
  if (previous && catalogue.some((entry) => entry.id === previous)) select.value = previous;
  select.onchange = updateProviderNote;
  updateProviderNote();
}

function currentProvider() {
  const select = $('provider');
  const entry = (select.options[select.selectedIndex] || {});
  return {id: select.value, note: entry.dataset ? (entry.dataset.note || '') : ''};
}

function updateProviderNote() {
  const note = $('providerNote');
  if (note) note.textContent = currentProvider().note;
}

loadLibrary().catch((error) => say(error.message));

// ------------------------------------------------------------------- login
//
// Three ways in (QR / password / SMS). All of them end with the server saving a
// cookie, so the page never receives the credential itself and only tracks
// status. QR uses the server's long-poll endpoint instead of a JS interval: a
// timer left running after the dialog closes is exactly what makes a login
// dialog misbehave later.

let qrKey = null;
let qrPolling = false;

function loginMessage(text, kind) {
  const box = $('loginMessage');
  box.textContent = text;
  box.style.color = kind === 'error' ? Theme.token('--lib-error-ink') : '';
}

function showLoginPane(name) {
  for (const pane of ['Qr', 'Password', 'Sms']) {
    $('pane' + pane).hidden = pane.toLowerCase() !== name.toLowerCase();
  }
  for (const tab of ['Qr', 'Password', 'Sms']) {
    $('tab' + tab).classList.toggle('button', tab.toLowerCase() !== name.toLowerCase());
  }
}

function setLoginState(status) {
  const badge = $('loginState');
  const stored = ((status || {}).providers || {}).netease || {};
  const loggedIn = status && status.verified === true;
  if (loggedIn) {
    badge.textContent = '已登录' + (status.account && status.account.nickname
      ? '（' + status.account.nickname + '）' : '');
  } else if (stored.logged_in || (status && status.stored)) {
    badge.textContent = '已保存凭据（未验证）';
  } else {
    badge.textContent = '未登录';
  }
}

async function refreshLoginState() {
  try {
    setLoginState(await api('login/whoami', {}));
  } catch (error) {
    // A network failure must not read as "logged out": the credential may still
    // be valid, we just could not confirm it.
    const badge = $('loginState');
    if (badge) badge.textContent = '登录状态未知';
  }
}

async function startQr() {
  if (qrPolling) return;
  loginMessage('正在生成二维码…');
  const data = await api('login/qr-create', {});
  qrKey = data.key;
  // PNG rather than inline SVG: the packaged build draws this in WebView2, which
  // does not reliably render an SVG data URI, and a blank code is worse than a
  // larger one.
  $('qrBox').innerHTML = '<img alt="登录二维码" style="width:220px;height:220px;'
    + 'image-rendering:pixelated" src="' + data.image + '">';
  $('qrState').textContent = '请用网易云音乐 App 扫码';
  loginMessage('');
  qrPolling = true;
  try {
    while (qrPolling && qrKey) {
      const state = await api('login/qr-wait', {key: qrKey, timeout: 150});
      if (state.state === 'confirmed') {
        loginMessage('登录成功，之后可以用真实逐字时间。');
        $('qrState').textContent = '已登录';
        await refreshLoginState();
        break;
      }
      if (state.state === 'scanned') { $('qrState').textContent = '已扫码，请在手机上确认'; continue; }
      // 等待窗口用完：码本身通常还有效，继续等下一轮（不要当成过期停下）。
      if (state.state === 'timeout') { $('qrState').textContent = '仍在等待扫码…'; continue; }
      if (state.state === 'expired') { $('qrState').textContent = '二维码已过期，请重新生成'; break; }
      if (state.state === 'rejected') { $('qrState').textContent = '已在手机上取消登录'; break; }
    }
  } finally {
    qrPolling = false;
    qrKey = null;
  }
}

$('loginOpen').onclick = () => {
  $('loginDialog').showModal();
  showLoginPane('Qr');
  loginMessage('');
  refreshLoginState().catch(() => {});
};
$('loginClose').onclick = () => { qrPolling = false; $('loginDialog').close(); };
$('tabQr').onclick = () => showLoginPane('Qr');
$('tabPassword').onclick = () => showLoginPane('Password');
$('tabSms').onclick = () => showLoginPane('Sms');

$('qrStart').onclick = () => {
  startQr().catch((error) => loginMessage(error.message, 'error'));
};

$('pwSubmit').onclick = async () => {
  loginMessage('正在登录…');
  try {
    await api('login/password', {phone: $('pwPhone').value.trim(),
                                 password: $('pwPassword').value,
                                 country_code: $('pwCountry').value.trim() || '86'});
    $('pwPassword').value = '';
    loginMessage('登录成功，之后可以用真实逐字时间。');
    await refreshLoginState();
  } catch (error) {
    loginMessage(error.message, 'error');
  }
};

$('smsSend').onclick = async () => {
  loginMessage('正在发送验证码…');
  try {
    await api('login/sms-send', {phone: $('smsPhone').value.trim(),
                                 country_code: $('smsCountry').value.trim() || '86'});
    loginMessage('验证码已发送，请查收短信。');
  } catch (error) {
    loginMessage(error.message, 'error');
  }
};

$('smsSubmit').onclick = async () => {
  loginMessage('正在登录…');
  try {
    await api('login/sms-verify', {phone: $('smsPhone').value.trim(),
                                   captcha: $('smsCaptcha').value.trim(),
                                   country_code: $('smsCountry').value.trim() || '86'});
    loginMessage('登录成功，之后可以用真实逐字时间。');
    await refreshLoginState();
  } catch (error) {
    loginMessage(error.message, 'error');
  }
};

$('loginLogout').onclick = async () => {
  try {
    await api('login/logout', {});
    loginMessage('已退出登录，凭据已从本机删除。');
    await refreshLoginState();
  } catch (error) {
    loginMessage(error.message, 'error');
  }
};

refreshLoginState().catch(() => {});
