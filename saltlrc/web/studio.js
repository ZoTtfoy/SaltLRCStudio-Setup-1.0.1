"use strict";
const $ = id => document.getElementById(id);
const queue = [], audioFiles = new Map();
let selected = -1, wordIndex = -1, wordField = 'main', audioUrl = null, selectionVersion = 0;
let shownRow = -1;
// True while the audio element has been deliberately emptied (select() /
// resetEditor()) and its replacement has not started playing yet. In that
// window `audio.currentTime` is 0 and means NOTHING about where the user is,
// so preview() must not derive a row index from it: a stale "-1" would freeze
// the playhead and the highlight — shownRow becomes -1 and every later frame
// recomputes -1, so `index !== shownRow` never fires again and the waveform
// stops following playback for good.
let audioSettling = false;
// anchorRow: the line the user last touched (row click, "定位", waveform block
// or a fresh insert). Both insert entry points read it, so "add a line" always
// lands where the user is looking.
// focusRow: a row to reveal and focus right after the next render.
let anchorRow = -1, focusRow = -1;
let running = null, pollTimer = null;
const current = () => queue[selected];
const salt = () => $('format').value === 'salt';
// Salt and YRC both require word timings; only the plain LRC profile is loose.
const strictFormat = () => $('format').value !== 'lrc';
const outEncoding = () => $('encodingOut')?.value || 'utf-8';
const key = name => name.replace(/\.[^/.]+$/, '').toLowerCase();
// structuredClone exists in every browser we target, but a JSON round-trip is
// a fine fallback: the document is plain data, and a missing helper must never
// take the whole editor down.
const clone = value => (typeof structuredClone === 'function'
  ? structuredClone(value) : JSON.parse(JSON.stringify(value)));
const names = {zh: '中文', ja: '日语', ko: '韩语', en: '英语', und: '未确定'};
/** 一个路径的所在目录（拿不到就给 null）。只用它当对话框的初始位置。 */
const dirOf = path => {
  const text = String(path || '');
  const cut = Math.max(text.lastIndexOf('/'), text.lastIndexOf('\\'));
  return cut > 0 ? text.slice(0, cut) : null;
};
const stamp = ms => String(Math.floor(ms / 60000)).padStart(2, '0') + ':' +
  String(Math.floor(ms / 1000) % 60).padStart(2, '0') + '.' + String(ms % 1000).padStart(3, '0');
const say = message => { $('message').textContent = message; if ($('wordDialog').open) $('wordError').textContent = message; };

// 历史与自动保存在 web/history.js（本文件之前加载）
function resetEditor() {
  $('title').textContent = '歌词工作区';
  $('rows').replaceChildren();
  $('metadata').textContent = '';
  $('report').textContent = '暂无检查结果';
  $('historyList').replaceChildren();
  $('languages').textContent = '语言待识别';
  renderFitList();          // 换歌/清空时贴唱结果列表要跟着清
  $('audioInfo').textContent = '添加音频查看波形';
  const audio = $('audio');
  audio.pause(); audio.removeAttribute('src'); audio.load();
  if (audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl = null; }
  // 音源被清掉了，"下次从哪播放"的标记与"这一段"也一起作废 —— 留着指向旧音频的
  // 秒数会让下一首歌从莫名其妙的位置开始。
  playbackMark = null;
  segmentStart = null;
  wave.load(null);
  wave.setDocument({groups: []});
  wave.setHover(null);
  shownRow = -1; anchorRow = -1; focusRow = -1;
  // There is no selection to resume, so currentTime is legitimately 0 here and
  // the preview caches are reset explicitly below. Leave the settling flag off
  // or the next legitimate preview() would be skipped.
  audioSettling = false;
  // Reset the preview caches, then restore the placeholder texts — preview()
  // itself skips updates when nothing is selected.
  for (const set of PREVIEW_SETS) {
    set.index = -1; set.text = null; set.spans = []; set.active = -1;
    $(set.ids.main).textContent = '等待载入歌词';
    $(set.ids.prev).textContent = ''; $(set.ids.trans).textContent = ''; $(set.ids.next).textContent = '';
  }
}
async function api(path, body) {
  const response = await fetch('/api/' + path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (!response.ok) { const error = await response.json(); throw Error(error.error); }
  return path === 'zip' ? response.blob() : response.json();
}
// bind() lives in common.js, shared with the library page.
// （download() 也还在 common.js，但**只有联网查找字词那一页**在兜底时用它；
//   歌词编辑器一律走后端另存为 —— 打包窗口里没有下载界面。）
// Layer 0 lives in document.js (mirror of saltlrc/document.py + edits.py):
// trackStart / shiftTrack / validateEdit / insertGroup are all Doc.* now.
const {trackStart, shiftTrack, validateEdit} = Doc;
function changeTime(index, field, kind, time, word, linked = $('linkTracks').checked, delta = 0, ripple = false) {
  const item = current(); if (!item) return;
  const doc = clone(item.document), group = doc.groups[index], track = group[field];
  // Every timeline edit goes through Doc.tickRange: the legal range of a tick is
  // decided in ONE place, from its neighbours. Deciding it per path lets one path
  // assign a value and leave validateEdit to throw the whole edit away, so the
  // handle will not move.
  if (kind === 'block') {
    // The body of a line is grabbed to move the whole line: same as its 句首.
    const range = Doc.tickRange(doc, index, field, 'start', null, item.duration);
    const currentStart = trackStart(group, field);
    const target = Math.max(range.low, Math.min(range.high, currentStart + delta));
    if (target !== currentStart + delta) {
      say(delta < 0 ? '已贴到上一句：歌词块不能拖过别的行' : '已贴到下一句：歌词块不能拖过别的行');
    }
    return changeTime(index, field, 'start', target, undefined, linked);
  }
  if (kind === 'start') {
    // Clamped instead of rejected: dragging 句首 past the neighbouring line fails
    // validation ("主轨句首必须非负、唯一且递增") and leaves the handle where it was —
    // the line cannot be moved at all.
    const range = Doc.tickRange(doc, index, field, 'start', null, item.duration);
    const wanted = Math.round(time);
    const target = Math.max(range.low, Math.min(range.high, wanted));
    if (target !== wanted) {
      say(wanted < range.low
        ? '已贴到上一句：这一行不能越过上面一行'
        : '已贴到下一句：这一行不能越过下面一行');
    }
    const delta = target - trackStart(group, field);
    if (linked) {
      for (const name of ['main', 'translation']) shiftTrack(group[name], trackStart(group, name), delta);
      group.start += delta;
    } else {
      const other = field === 'main' ? 'translation' : 'main';
      group[other].start = trackStart(group, other);
      shiftTrack(track, trackStart(group, field), delta);
      if (field === 'main') group.start = target;
    }
  } else if (kind === 'end') {
    // The end is a tick like any other: it stays after the last word and inside
    // the audio. Assigning it directly makes dragging it earlier than the last
    // word discard the whole edit.
    const moved = Doc.moveLineEnd(track, time, {duration: item.duration, ripple});
    track.end = moved.end;
    for (const item_ of moved.carried) item_.word.start = item_.start;
    if (moved.note) say(moved.note);
  } else {
    // A word tick. Dragging the FIRST word means dragging the line start: they
    // are the same timestamp (validateEdit insists words[0].start === start), so
    // there is nothing else it could mean; setting it separately leaves the head
    // stuck.
    if (word === 0) return changeTime(index, field, 'start', time, undefined, linked);
    // The tail is not a dead end: crossing the line end would make validateEdit
    // reject the whole edit with a message that never mentioned the line end, so
    // the word simply would not move. Doc.moveWordStart clamps to the previous
    // word and carries the line end along instead, which is what dragging the
    // last word past the end actually means.
    if (ripple) {
      // Alt-drag: the word and everything after it move together, keeping their
      // gaps. Applied as one shift so the result is either fully valid or not
      // applied at all — a partial ripple would leave the times inconsistent.
      const moved = Doc.rippleWordStart(track, word, time,
                                        {duration: item.duration, lineStart: trackStart(group, field)});
      track.words.slice(word).forEach((unit, offset) => { unit.start = moved.starts[offset]; });
      if (moved.end != null) track.end = moved.end;
      if (moved.note) say(moved.note);
    } else {
      const moved = Doc.moveWordStart(track, word, time,
                                      {duration: item.duration, lineStart: trackStart(group, field)});
      track.words[word].start = moved.start;
      if (moved.end != null) track.end = moved.end;
      if (moved.note) say(moved.note);
    }
  }
  validateEdit(doc, item.duration);
  remember(item, '调整时间', doc); refresh();
}
const wave = new SaltWaveform($('waveCanvas'), {
  seek: time => {
    if (!$('audio').src) return;
    // 显式定位也是"下次从哪播放"的来源之一 —— 只在这一处记。
    markPlaybackPosition(time, '定位到', true);
    $('audio').currentTime = time;
  },
  change: c => { try { changeTime(c.index, c.field, c.kind, c.time, c.word, undefined, c.delta || 0, c.ripple); } catch (e) { say(e.message); } },
  edit: (index, field, what) => {
    // A double-click on a tick opens the word editor, which is the full tool for
    // that line (split / tap / spread); on the block body it edits the text.
    try { if (what === 'words') openWords(index, field); else openText(index, field); }
    catch (e) { say(e.message); }
  },
  pointer: info => showWaveTip(info),
  select: () => { /* the tip already reflects the new selection */ },
  // 波形回调给的是 pps；滑块用百分比，换算在 SaltWaveform 里（唯一权威）。
  zoom: pps => setZoom(SaltWaveform.percentForPps(pps)),
  pan: value => setPan(value),
  followChanged: on => {
    const label = $('follow')?.parentElement;
    if (label) label.classList.toggle('followOff', !on);
    if (!on) say('自动跟随已暂停：你手动平移了视窗，播放头走出视窗也不会被拉回。点「定位」、重新播放或点击贴条空白处即恢复。');
  }
});
// Full-song strip above the waveform: a compact overview of the whole song
// with the visible viewport drawn on it.
wave.attachOverview($('songStrip'));

// 换主题后立刻重画：静态层的判据里已经含主题（所以不通知也不会画错），
// 这一句只是让颜色当场就变，而不是等下一次交互。
document.addEventListener('themechange', () => wave.invalidate());

// 滑块的三件事，各自只有一个权威：
//   1. pps 的合法范围          → SaltWaveform.MIN_PPS / MAX_PPS / clampPps
//   2. 0–100 ↔ pps 的换算      → SaltWaveform.percentForPps / ppsForPercent
//   3. 填充条走到哪            → paintRange()，读滑块自己的 value
// 滑块的 value 是**百分比**（0 在极左、100 在极右），旁边的数字框仍然显示 px/s ——
// 单位是百分比，两端固定在 0 与 100。
function paintRange(input) {
  if (!input || !input.style || typeof input.style.setProperty !== 'function') return;
  const low = Number(input.min || 0), high = Number(input.max || 100);
  const percent = high > low ? (Number(input.value) - low) / (high - low) * 100 : 0;
  input.style.setProperty('--fill', Math.max(0, Math.min(100, percent)) + '%');
}

// Slider and numeric box stay in sync in both directions. `value` is a PERCENT.
function setZoom(value, redraw = true) {
  const percent = Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
  const pps = SaltWaveform.ppsForPercent(percent);
  wave.pps = pps;
  $('zoom').value = String(SaltWaveform.percentForPps(pps));
  $('zoomInput').value = pps;
  paintRange($('zoom'));
  if (redraw) wave.draw();
}
function setPan(value, redraw = true) {
  // Clamp with the waveform's own limit: `wave.duration || 0` is 0 before any
  // audio is loaded, which snaps the viewport straight back to 0 on every drag.
  wave.offset = Math.max(0, Math.min(wave.maxOffset(), Number(value) || 0));
  if (redraw) wave.draw();
}

// Double-clicking a lyric block on the waveform edits its text in place.
let textIndex = -1, textField = 'main';
function openText(index, field) {
  const item = current();
  if (!item) return;
  textIndex = index; textField = field;
  const group = item.document.groups[index], track = group[field];
  $('textTitle').textContent = (field === 'main' ? '主歌词' : '翻译') + ' · 第 ' + (index + 1) + ' 行'; 
  $('textHint').textContent = track.words.length
    ? '该行有 ' + track.words.length + ' 个逐字时间。字数不变时会按位置保留逐字时间；字数改变会清除该行逐字轴。'
    : '该行没有逐字时间。改完可双击波形上的这一句，在逐字编辑器里用「从音频检测这一行」。';
  $('textValue').value = track.text;
  $('textDialog').showModal();
  $('textValue').focus();
  $('textValue').select();
}
function applyText() {
  const item = current();
  if (!item || textIndex < 0) return;
  const value = $('textValue').value.replace(/[\r\n]+/g, ' ').trim();
  const doc = clone(item.document), track = doc.groups[textIndex][textField];
  let note = textField === 'main' ? '已修改主歌词文字' : '已修改翻译文字';
  if (track.text !== value) {
    if (track.words.length) {
      // Units are whole words / kana runs, so the timing is kept by unit
      // length, not by character index.
      note += Doc.retargetWords(track, value) ? '；已保留该行逐字时间' : '；字数变化，已清除该行逐字时间';
    }
    track.text = value;
    validateEdit(doc, item.duration);
    remember(item, '改歌词文字', doc);
  }
  $('textDialog').close();
  refresh();
  say(note);
}

// The owned project format (.saltproj): document + song identity + optionally
// the audio itself, so 保存工程 / 打开工程 round-trips the whole session.
//
// Saved through the backend's Save-As dialog rather than a page download: the
// packaged window has no download shelf, so a blob download writes nothing and
// the button looks dead. The backend writes it, exactly like 导出当前歌词.
//
// 内嵌音频送的是原始字节，不经过 base64 + JSON：
//   · 不内嵌 → **纯 JSON**（人能看懂，工具也能读）；
//   · 内嵌 → 走 `Doc.packContainer`：JSON 头 + **音频原始字节**，
//     用 `/api/save-binary` 直接把字节发过去。
//   字节直发省掉"编码成 base64 → 再解析回对象"的搬运与临时内存。
async function apiBinary(url, blob) {
  const response = await fetch(url, {method: 'POST',
    headers: {'Content-Type': 'application/octet-stream'}, body: blob});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || ('请求失败 ' + response.status));
  return data;
}

async function saveProject() {
  const item = current();
  if (!item) throw Error('请先导入歌词');
  const file = audioFiles.get(key(item.name));
  const embed = $('embedAudio').checked && !!file;
  const name = item.name.replace(/\.[^.]+$/, '') + '.saltproj';
  let body;
  if (embed) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    body = new Blob([Doc.packContainer(Doc.packProject(item, {name: file.name, type: file.type,
                                                             bytes, raw: true}), bytes)],
                    {type: 'application/octet-stream'});
  } else {
    body = new Blob([JSON.stringify(Doc.packProject(item, null))], {type: 'application/json'});
  }
  const query = new URLSearchParams({name});
  if (item.sourceDir) query.set('preferred', item.sourceDir);
  const result = await apiBinary('/api/save-binary?' + query.toString(), body);
  if (result.status === 'cancelled') { say('已取消保存工程'); return; }
  say('工程已保存到 ' + (result.path || name) + '（'
      + (embed ? '内嵌音频' : '未内嵌音频；打开时请再添加同名音频') + '）');
}
// 从**字节**开一个工程。两个来源共用这一条：用户选/拖进来的文件，
// 以及双击 .saltproj 时服务端递过来的那份（见下面的 openPendingProject）。
async function adoptProject(bytes, label) {
  let opened;
  try { opened = Doc.readProject(bytes); }
  catch (error) {
    throw Error((label ? label + '：' : '') + '工程文件损坏或不是有效的 JSON：' + error.message);
  }
  const project = Doc.unpackProject(opened.project, opened.audioBytes);
  const item = {name: project.name, document: normalizeDocument(project.document),
                report: {errors: [], warnings: []}, history: [], match: null};
  if (project.audio) {
    const restored = new File([project.audio.bytes], project.audio.name, {type: project.audio.type});
    audioFiles.set(key(item.name), restored);
    audioFiles.set(key(restored.name), restored);
  }
  queue.push(item);
  await select(queue.length - 1);
  // Without audio there is no decode step to measure the duration; the saved
  // value keeps validation and the strip's time ruler correct.
  if (!audioFiles.has(key(item.name)) && project.duration) {
    item.duration = project.duration;
    check().catch(e => say(e.message)); repaintQueue();
  }
  say(project.audio ? '工程已打开：歌词与音频都已从工程文件恢复。'
                    : '工程已打开：歌词已恢复；该工程未内嵌音频，请再添加同名音频以显示波形。');
}

async function openProjectFile(file) {
  await adoptProject(new Uint8Array(await file.arrayBuffer()), file.name);
}

// 双击 .saltproj（文件关联写的是 `"exe" --open "%1"`）时，那个路径在**本进程的
// 命令行**上。页面只问服务端"启动时带了什么"，**从不提交路径** —— 服务端也不会
// 按页面给的路径去读文件。所以这条通路不给页面任何"读任意文件"的能力。
//
// 一次性：服务端读过就清掉。刷新页面不会重复灌入磁盘上的旧内容 ——
// 刷新后队列本来就从会话快照恢复，那才是"接着改"该走的路。
//
// ⚠️ 这是**启动路径上的附加动作，它绝不能把页面弄坏**：整段包在 try 里，
// 而且只在"确实有东西要打开、又确实打不开"的时候才出声。三条 return 各有一条理由：
// 没有服务端 / 没有待打开的工程 / 拿到的不是真响应（测试替身、异常环境）。
async function openPendingProject() {
  let reply;
  try { reply = await fetch('/api/pending-open'); }
  catch (error) { return; }
  try {
    if (!reply) return;
    if (reply.status === 204) return;
    if (typeof reply.arrayBuffer !== 'function') return;
    if (reply.status !== 200) {
      // 服务端明说打不开（例如那个文件刚被删掉）—— 这一条必须出声。
      let message = '服务端打不开它';
      try { message = (await reply.json()).error || message; } catch (error) {}
      Notify.fail('这个工程文件打不开', message, [
        '双击的那个文件可能已经被移动或删除了。',
        '也可以在顶部「添加 / 打开歌词 · 音频 · 工程」里重新选一次。',
      ]);
      return;
    }
    // 文件名走的是自定义头，可能是百分号编码；缺了它也不影响打开，
    // 只是报错信息里少个名字。
    const header = reply.headers && typeof reply.headers.get === 'function'
      ? reply.headers.get('X-Project-Name') : null;
    const label = header ? decodeURIComponent(header) : '';
    await adoptProject(new Uint8Array(await reply.arrayBuffer()), label);
  } catch (error) {
    Notify.fail('这个工程文件打不开', error.message, [
      '它可能不完整，或者不是本程序写出的 .saltproj。',
      '也可以在顶部「添加 / 打开歌词 · 音频 · 工程」里重新选一次。',
    ]);
  }
}
// ------------------------------------------- Salt Player library (in-editor)
// The library is a dialog here: pick a track, get its lyric AND its audio, and
// the whole song lands in the queue ready to edit. (/library still exists for
// the batch tools: 批量匹配、写入歌曲目录.)
let libTracks = null;

async function openLibraryDialog() {
  $('libraryDialog').showModal();
  if (libTracks) return;
  $('libState').textContent = '正在读取曲库…';
  try {
    const response = await fetch('/api/library');
    const data = await response.json();
    if (!response.ok) throw Error(data.error || '读取曲库失败');
    libTracks = data.tracks || [];
    renderLibrary('');
  } catch (error) {
    libTracks = [];
    $('libState').textContent = '读取曲库失败：' + error.message;
  }
}

function renderLibrary(keyword) {
  const list = $('libList');
  list.replaceChildren();
  if (!libTracks) return;
  const needle = (keyword || '').trim().toLowerCase();
  const onlyMissing = $('libMissing').checked;
  const tracks = libTracks.filter(t =>
    (!onlyMissing || !t.has_lyric) &&
    (!needle || (t.title + t.artist + t.album).toLowerCase().includes(needle)));
  $('libState').textContent = `${tracks.length} / ${libTracks.length} 首（曲库共 ${libTracks.length} 首）`;
  for (const track of tracks.slice(0, 300)) {
    const row = document.createElement('button');
    row.className = 'libRow';
    const title = document.createElement('strong'); title.textContent = track.title || '(无标题)';
    const meta = document.createElement('small');
    meta.textContent = [track.artist, track.album].filter(Boolean).join(' · ') +
      (track.duration ? ` · ${Math.floor(track.duration / 60)}:${String(track.duration % 60).padStart(2, '0')}` : '');
    const badge = document.createElement('span');
    badge.className = 'libBadge ' + (track.has_lyric ? 'ok' : 'missing');
    badge.textContent = track.has_lyric ? '有歌词' : '待配歌词';
    row.append(title, meta, badge);
    row.onclick = () => openFromLibrary(track).catch(e => { $('libState').textContent = e.message; say(e.message); });
    list.append(row);
  }
  if (!tracks.length) $('libState').textContent = '没有匹配的歌曲';
}

// One click = lyric + audio + queue item, without leaving the editor.
async function openFromLibrary(track) {
  const name = (track.title || '未命名') + '.lrc';
  if (queue.some(item => key(item.name) === key(name))) { say('这首歌已经在队列里了。'); return; }
  $('libState').textContent = '正在载入「' + (track.title || name) + '」…';
  const item = {name, document: emptyDocument(), report: {errors: [], warnings: []},
                history: [], match: null, pending: true};
  // 这首歌**从哪来的** —— 只用来决定导出/保存的对话框从哪个文件夹打开
  // （曲库导入时路径已知）：用歌词文件所在目录，
  // 没有歌词文件就用音频所在目录。
  item.sourceDir = dirOf(track.lyric_path) || dirOf(track.path) || null;
  let lyric = false;
  if (track.lyric_path) {
    try {
      const response = await fetch('/api/lyric?path=' + encodeURIComponent(track.lyric_path));
      if (response.ok) {
        const text = (await response.json()).text;
        const parsed = await api('parse', {bytes: Doc.bytesToBase64(new TextEncoder().encode(text))});
        item.document = normalizeDocument(parsed.document);
        item.unresolved = parsed.document.unresolved || [];
        lyric = true;
      }
    } catch (error) { say('歌词读取失败，将改为搜索：' + error.message); }
  }
  try {
    const response = await fetch('/api/audio?track_id=' + encodeURIComponent(track.id));
    if (response.ok) {
      const blob = await response.blob();
      const audioName = (track.path || '').split(/[\\/]/).pop() || (track.title + '.mp3');
      const file = new File([blob], audioName, {type: blob.type || 'audio/mpeg'});
      audioFiles.set(key(item.name), file);
      audioFiles.set(key(file.name), file);
    }
  } catch (error) { say('音频载入失败：' + error.message); }
  queue.push(item);
  $('libraryDialog').close();
  await select(queue.length - 1);
  say(lyric ? '已载入「' + (track.title || name) + '」的歌词与音频，可直接编辑。'
            : '已载入「' + (track.title || name) + '」；这首还没有歌词，正在自动查找…');
}

function repaintQueue() {
  $('count').textContent = queue.length; $('queue').replaceChildren();
  queue.forEach((item, i) => {
    const button = document.createElement('button'); button.className = 'item' + (i === selected ? ' active' : '');
    button.textContent = item.name;
    const detail = document.createElement('small');
    detail.textContent = item.pending
      ? '音频已配对 · 待锚定歌词'
      : (audioFiles.has(key(item.name)) ? '音频已配对 · ' : '无音频 · ') + (item.report?.errors.length ? '待处理 ' + item.report.errors.length + ' 项' : '格式检查通过');
    button.append(detail); button.onclick = () => select(i); $('queue').append(button);
  });
  // 这是"队列变了"的唯一出口 —— 所有导入路径（文件选择器、导入文件夹、曲库、
  // 联网查找字词交接、恢复上次会话）都汇到这里，所以"每条新条目建一个工程"只需要这一个
  // 挂钩。分开挂到各条导入路径上，会漏掉几条。
  Project.trackAll(queue);
  // 同一个出口：新条目也要进会话快照（崩溃恢复靠它）。见 history.js 的 touchSession()。
  touchSession(queue);
}
function emptyDocument() {
  return {groups: [], metadata: {}, notes: [], unresolved: [], alignment: {}};
}

function normalizeDocument(doc) {
  for (const group of doc.groups || []) {
    for (const field of ['main', 'translation']) {
      // API responses are plain JSON objects; normalize each track before editing.
      group[field].words ??= []; group[field].end ??= null; group[field].start ??= null;
    }
  }
  return doc;
}

function adoptDocument(item, doc) {
  item.document = normalizeDocument(doc);
  item.pending = false;
  item.match = null;
  // 这是"整份换文档"，不是编辑：历史要以新文档为基线重开。否则「载入」还停在
  // 旧文档上，删一行再撤销就会把整份歌词清空。
  resetHistory(item);
  if (item === current()) { renderMatch(); refresh(); }
  repaintQueue();
  return item;
}

// ------------------------------------------------- audio -> lyric workflow
// `options.force` means "run even though the item is not the visible one"
// (select() needs that: the audio was decoded for this item specifically).
// It must NOT mean "skip every staleness check" — a late reply would then reach
// adoptDocument() and repaint from a reset audio element. `version`,
// when supplied, is the selection generation that asked for this lookup; if it
// has moved on, the answer belongs to a song the user has left.
async function resolveLyric(item, options = {}) {
  if (!item || !item.pending) return;
  const superseded = () => options.version !== undefined && options.version !== selectionVersion;
  if (superseded()) return;
  say('正在识别「' + item.name + '」并寻找歌词…');
  const select = $('matchProvider');
  const result = await api('audio/match', {name: item.name, duration: item.duration || null,
                                           search: options.search !== false,
                                           provider: select ? select.value : undefined});
  if (superseded()) return;
  if (item !== current() && !options.force) { item.match = result; return; }
  item.match = result;
  item.matchDuration = item.duration;
  if (result.lyric) {
    const parsed = await api('parse', {bytes: SaltAudio.base64(new TextEncoder().encode(result.lyric))});
    if (superseded()) return;
    adoptDocument(item, parsed.document);
    say('已从 Salt Player 曲库载入歌词（' + parsed.document.groups.length + ' 行）。可直接编辑或导出。');
    return;
  }
  if (result.auto && result.auto !== 'library' && result.auto_song_id) {
    // `auto` is whichever provider actually answered, not always Netease: the
    // backend reports the source it used and this must follow it, or a match
    // from another source would be fetched from the wrong one.
    await loadSong(item, result.auto_song_id, true, result.auto, options.version);
    return;
  }
  renderMatch();
  say((result.notes || []).join('；') || '没有自动匹配到歌词，请手动锚定。');
}

async function loadSong(item, songId, translate = true, provider = null, version = undefined) {
  const payload = await api('fetch', {song_id: songId, provider: provider || undefined});
  if (version !== undefined && version !== selectionVersion) return;
  if (!payload.main.length) throw Error('该结果没有歌词内容，换一个候选');
  const built = await api('build', {main: payload.main, translation: payload.translation,
                                    duration: item.duration || null, options: {},
                                    word_level: payload.word_level && translate});
  if (version !== undefined && version !== selectionVersion) return;
  if (built.report.errors.length) throw Error(built.report.errors[0]);
  adoptDocument(item, built.document);
  say('已载入 ' + payload.main.length + ' 行原文' +
      (payload.translation.length ? ' + ' + payload.translation.length + ' 行翻译' : '（这条结果没有翻译）') +
      (payload.word_level ? '；使用来源逐字时间' : '；逐字时间为本地估算'));
}

function renderMatch() {
  const item = current();
  const panel = $('matchPanel');
  const result = item?.match;
  if (!item?.pending || !result) { panel.hidden = true; return; }
  panel.hidden = false;
  $('matchNotes').textContent = (result.notes || []).join('；') || '请选择歌词来源。';
  $('matchParsed').textContent = '文件名解析：' +
    (result.parsed.title ? '标题「' + result.parsed.title + '」' : '标题未知') +
    (result.parsed.artist ? ' · 艺术家「' + result.parsed.artist + '」' : '');
  const local = $('matchLocal');
  local.hidden = !result.library;
  local.textContent = result.library
    ? (result.library.has_lyric ? '使用曲库里的歌词' : '曲库里没有歌词（点此仅绑定路径）')
    : '';
  local.disabled = !(result.library && result.library.has_lyric);
  const box = $('matchCandidates');
  box.replaceChildren();
  for (const candidate of result.candidates || []) {
    const row = document.createElement('div');
    row.className = 'proposal';
    const label = document.createElement('span');
    const gap = item.duration && candidate.duration
      ? ' · 时长差 ' + (Math.abs(candidate.duration - item.duration) / 1000).toFixed(1) + 's' : '';
    label.textContent = candidate.title + ' — ' + candidate.artist +
      (candidate.album ? '（' + candidate.album + '）' : '') + gap + ' · 评分 ' + candidate.score +
      (candidate.score >= 45 ? ' 可信' : ' 偏低');
    const use = document.createElement('button');
    use.textContent = '用这条';
    // Pass the source these candidates came from: the panel is populated by
    // /api/audio/match for one provider, so fetching without it would fall back
    // to the default source and the id would not resolve there.
    use.onclick = () => guard(async () => {
      await loadSong(item, candidate.id, true, result.provider);
      say('已载入候选「' + candidate.title + '」。');
    });
    row.append(label, use);
    box.append(row);
  }
}

async function applyPastedLyric() {
  const item = current();
  if (!item) throw Error('请先选择一首歌');
  const parsed = await api('manual', {main_text: $('matchMain').value, translation_text: $('matchTrans').value});
  const built = await api('build', {main: parsed.main, translation: parsed.translation,
                                    duration: item.duration || null, options: {}});
  if (built.report.errors.length) throw Error(built.report.errors[0]);
  adoptDocument(item, built.document);
  say('已载入粘贴的歌词：' + parsed.main.length + ' 行原文' +
      (parsed.translation.length ? ' + ' + parsed.translation.length + ' 行翻译' : '（无翻译）'));
}

/**
 * 音频扩展名清单 —— **来自服务端**（`/api/capabilities`），页面不自己写死。
 *
 * 页面上不另留一份正则 —— 那会是又一份"什么算音频"的定义，且容易漏掉几种
 * （导入文件夹时那一类会被静默跳过）。唯一权威是 `saltlrc/audioformats.py`。
 * 拿不到清单时（服务端异常）退回按 MIME 判断 —— 那是**另一个独立信号**，
 * 不是把同一份清单再抄一遍。
 */
async function audioExtensions() {
  if (!CAPS) { try { await capabilities(); } catch { /* 下面兜底 */ } }
  const list = (CAPS && CAPS.audio_extensions) || [];
  return new Set(list);
}

function extensionOf(name) {
  const match = /\.[^.\\/]+$/.exec(String(name || ''));
  return match ? match[0].toLowerCase() : '';
}

/** 这个格式应用内解不了时，服务端给的那句"怎么办"；能用就是空串。 */
function audioNoteFor(name) {
  const notes = (CAPS && CAPS.audio_notes) || {};
  return notes[extensionOf(name)] || '';
}

async function importFiles(files) {
  const failures = []; let count = 0;
  const audioExt = await audioExtensions();
  for (const file of files) {
    const name = file.webkitRelativePath || file.name;
    if (audioExt.size ? audioExt.has(extensionOf(name)) : (file.type || '').startsWith('audio/')) {
      // 列出来 ≠ 能用：.ape / .wv / .aiff 浏览器里没有解码器。**导入时就说明白**，
      // 别让用户等到"播放没声音、波形是空的"才发现（服务端给那句话，页面只负责显示）。
      const note = audioNoteFor(name);
      if (note) window.Notify?.banner?.('audio:' + name, note);
      // Index under both the relative path and the bare file name so an audio
      // imported on its own still pairs with a lyric that has the same name.
      audioFiles.set(key(name), file);
      audioFiles.set(key(file.name), file);
      continue;
    }
    if (/\.saltproj$/i.test(name)) {
      try { await openProjectFile(file); count++; }
      catch (e) { failures.push(name + '：' + e.message); }
      continue;
    }
    try {
      let doc;
      if (/\.json$/i.test(name)) {
        const saved = JSON.parse(await file.text());
        if (![1, 2].includes(saved.version) || !saved.document) throw Error('不支持的工程格式');
        doc = saved.document;
      } else if (/\.(lrc|lar|spl|spw|yrc)$/i.test(name)) {
        if (file.size > 8 * 1024 * 1024) throw Error('歌词超过 8MB');
        doc = (await api('parse', {bytes: SaltAudio.base64(new Uint8Array(await file.arrayBuffer())), encoding: $('encoding').value})).document;
      } else continue;
      const report = await api('validate', {document: doc});
      normalizeDocument(doc);
      queue.push({name: name.replace(/\.saltlrc\.json$/i, '.lrc'), document: doc, report, history: []}); count++;
    } catch (e) { failures.push(name + '：' + e.message); }
  }
  // An audio file with no lyric becomes a pending item that goes looking for
  // its lyric, instead of showing nothing at all.
  const pending = [];
  const seen = new Set();
  for (const file of new Set(audioFiles.values())) {
    if (seen.has(file)) continue;
    seen.add(file);
    if (queue.some((item) => key(item.name) === key(file.name))) continue;
    const item = {name: file.name, document: emptyDocument(), report: {errors: [], warnings: []},
                  history: [], pending: true, match: null};
    queue.push(item); pending.push(item);
  }
  const first = selected < 0 && queue.length ? 0 : selected;
  if (queue.length) await select(first);
  repaintQueue();
  const notes = [];
  if (count) notes.push('已导入 ' + count + ' 个歌词/工程');
  if (pending.length) notes.push('有 ' + pending.length + ' 个音频没有歌词，正在自动匹配');
  say((notes.join('；') || '没有可导入的文件') + (failures.length ? '\n导入失败：\n' + failures.join('\n') : ''));
  if (failures.length) {
    // 用户主动导入却没办成 → 叫住他（状态行在页面下方，容易没看见）。
    // 但**不是错误**的那一类不要弹：全部文件都没导入也算"没办成"，值得说。
    window.Notify?.fail('有 ' + failures.length + ' 个文件没导入',
                        failures.slice(0, 8).join('\n')
                        + (failures.length > 8 ? '\n…（共 ' + failures.length + ' 个）' : ''),
                        ['支持的歌词格式：.lrc / .yrc / .saltproj / .json；音频请与歌词同名'],
                        'import:' + failures.length + ':' + failures[0]);
  }
  for (const item of pending) {
    try { await resolveLyric(item, {force: true, version: selectionVersion}); } catch (error) { say('自动匹配失败：' + error.message); }
  }
}
/** 把检查结果画成**能点进去的**列表。

 * 每条渲染成一行（不再是一段纯文本 `<pre>`）；同一类问题的收敛在服务端做
 * （`validator.MAX_DETAILED`），页面这层只负责把每条渲染成一行。
 * 界面上写着"每条都能点「定位」跳到表格那一行"，所以带行号的条目真的给出按钮
 * （`report.items` 里带着行号）。
 */
function renderReport(report) {
  const box = $('report');
  if (!box) return;
  box.replaceChildren();
  const items = (report && report.items) || [];
  if (!items.length) { box.textContent = '检查通过：可以导出了。'; return; }
  for (const entry of items) {
    const row = document.createElement('div');
    row.className = 'reportRow ' + (entry.level === 'error' ? 'bad' : 'warn');
    const mark = document.createElement('span');
    mark.className = 'mark';
    mark.textContent = entry.level === 'error' ? '错误' : '提示';
    const text = document.createElement('span');
    text.className = 'text';
    text.textContent = entry.message;
    row.append(mark, text);
    if (entry.index != null) {
      const go = document.createElement('button');
      go.textContent = '定位';
      go.title = '跳到表格的第 ' + (entry.index + 1) + ' 行';
      go.onclick = () => { locate(entry.index); switchPane('wave'); };
      row.append(go);
    }
    box.append(row);
  }
}
async function check() {
  const item = current(); if (!item) return;
  const report = await api('validate', {document: item.document, salt: strictFormat(), duration: item.duration});
  item.report = report;
  if (item === current()) renderReport(report);
  repaintQueue(); return report;
}

// 「检查歌词」的结果列在「检查与工程」页 —— 而在别的页点它，画面上什么都不会变，
// 那就是一个"点了没反应"的按钮。这里统一由状态行出声；有错误时顺手切到那一页，
// 因为下一步就是去改那几条问题（每条都能点「定位」跳过去）。
async function checkOutLoud() {
  const item = current();
  if (!item) throw Error('请先导入歌词');
  const report = await check();
  const errors = report.errors.length, warnings = report.warnings.length;
  if (errors) {
    switchPane('project');
    say(`检查未通过：${errors} 个错误、${warnings} 条提示 —— 已在下方「需要处理的问题」列出，每条都能点「定位」跳到表格那一行。`);
  } else {
    say(warnings ? `检查通过，有 ${warnings} 条提示（可在「检查与工程」页查看）。` : '检查通过：可以导出了。');
  }
}
async function identify() {
  const item = current(); if (!item) return;
  const languages = await api('language', {document: item.document}); item.languages = languages;
  if (item !== current()) return;
  $('languages').textContent = ['main', 'translation'].map(field => {
    const value = languages[field];
    return (field === 'main' ? '主轨：' : '翻译轨：') + (names[value.code] || value.code) + ' ' + Math.round(value.confidence * 100) + '%' + (value.review ? '（需核对）' : '');
  }).join('  /  ');
}
function refresh() { render(); preview(); check().catch(e => say(e.message)); identify().catch(e => say(e.message)); }
// Seek, scroll the waveform to the line and highlight it — the table and the
// waveform always point at the same place after this.
function locate(index) {
  const item = current();
  if (!item || !item.document.groups[index]) return;
  const start = trackStart(item.document.groups[index], 'main');
  if ($('audio').src) { markPlaybackPosition(start / 1000, '跳到第 ' + (index + 1) + ' 行', true); $('audio').currentTime = start / 1000; }
  // An explicit locate always wins over a lingering hover: the user asked for
  // this line, so drop the pointer highlight instead of leaving the outline on
  // whatever the cursor happened to be over.
  wave.setHover(null);
  wave.focusLine(index);
  wave.setHighlight(index);
  anchorRow = index;
  preview();
}
// Scroll ONLY the table container. scrollIntoView() aligns every scrollable
// ancestor, which drags the whole page up and down during playback.
//
// The row is placed at the TOP of the visible list rather than merely kept
// visible. Keeping it visible lets it drift down the window until only the line
// being sung and the next one are on screen, which is the opposite of what a
// lyric-timing editor needs — the timing about to be fixed belongs to the lines
// that come NEXT, so those are what should be on screen.
function pinRowToTop(row) {
  const wrap = row.closest('.tablewrap');
  if (!wrap) return;
  const rect = row.getBoundingClientRect(), box = wrap.getBoundingClientRect();
  // The header is sticky, so "the top" is just below it, not at the container's
  // top edge — otherwise the row would slide under the header on every line.
  const head = wrap.querySelector ? wrap.querySelector('thead') : null;
  const headerHeight = head ? head.getBoundingClientRect().height : 0;
  const delta = rect.top - box.top - headerHeight;
  // Already pinned: leave scrollTop alone. Assigning it anyway would fight the
  // user's own scrolling on a line that happens to be exactly where it belongs.
  if (Math.abs(delta) < 1) return;
  wrap.scrollTop += delta;
}
// The single entry point for "add a line", shared by the toolbar button and
// every row's "＋插入". The rules:
//   * a line is selected  -> insert directly below it;
//   * nothing selected   -> insert at the line the playhead is inside, so the
//                           new blank line lands on the current timeline spot;
//   * empty document      -> insert the very first line at 0.
// `explicit` overrides that resolution (the row button passes its own index).
// Never invents a timestamp: the backend picks the midpoint of the two
// neighbours and refuses when there is no room.
async function insertRow(explicit) {
  const item = current();
  if (!item) { say('请先导入歌词'); return; }
  const groups = item.document.groups;
  // 「选中的行」可能已经不存在了：撤销/重做/换歌都会改文档形状，而 anchorRow
  // 只在 select() 与 locate() 里写。拿一个越界的锚点去拼提示会与事实不符
  // （"已在选中的第 7 行下方插入新行"，可文档只剩 2 行），所以就地判定，
  // 越界一律当"没选中"，退回按当前时间轴插。
  const anchor = (anchorRow >= 0 && anchorRow < groups.length) ? anchorRow : -1;
  let at, note;
  if (explicit != null && explicit >= 0 && explicit < groups.length) {
    at = explicit + 1;
    note = '已在第 ' + at + ' 行下方插入新行';
  } else if (explicit != null && explicit >= 0) {
    at = groups.length;
    note = '已在最后一行下方插入新行';
  } else if (anchor >= 0) {
    at = anchor + 1;
    note = '已在选中的第 ' + (anchor + 1) + ' 行下方插入新行';
  } else if (groups.length) {
    const here = activeRow();
    at = here < 0 ? 0 : here + 1;
    note = '未选中歌词行，已按当前时间轴（' + stamp(playheadMs()) + '）在第 ' +
      (at + 1) + ' 行插入新行';
  } else {
    at = 0;
    note = '文档是空的，已在 0 秒插入第一行';
  }
  const result = await api('edit', {op: 'insert', document: item.document, index: at,
                                    duration: item.duration ?? null});
  remember(item, '插入行', result.document);
  focusRow = at;
  anchorRow = at;
  refresh();
  say(note + '（句首 ' + stamp(result.document.groups[at].start) + '），直接填歌词；时间可在波形上拖动。');
}
async function deleteRow(index) {
  const item = current();
  if (!item) { say('请先导入歌词'); return; }
  if (!item.document.groups[index]) {
    // 用户主动触发的动作，失败也必须出声 —— 静默 return 等于"按钮坏了"。
    // 行号对不上通常是列表没跟上文档变化。
    say('要删除的第 ' + (index + 1) + ' 行已经不在了（列表可能没跟上文档），已刷新列表。');
    refresh();
    return;
  }
  const result = await api('edit', {op: 'delete', document: item.document, index});
  remember(item, '删除行', result.document);
  anchorRow = Math.min(index, item.document.groups.length - 1);
  refresh();
  say('已删除第 ' + (index + 1) + ' 行，可撤销。');
}
// Which line the playhead is inside right now. The audio element is the
// authority when a song is loaded; without audio the waveform's own playhead
// still carries a position (strip scrubbing), so "insert at the current
// timeline spot" keeps working before a song is added.
function playheadMs() {
  const audio = $('audio');
  if (audio.src && Number.isFinite(audio.currentTime)) return audio.currentTime * 1000;
  return (wave?.time || 0) * 1000;
}
function activeRow() {
  const item = current();
  if (!item) return -1;
  const ms = playheadMs();
  const groups = timelineGroups(item);     // 与 preview() 同一个来源，别各读各的
  let index = -1;
  for (let i = 0; i < groups.length; i++) {
    if (trackStart(groups[i], 'main') <= ms) index = i;
  }
  return index;
}
function render() {
  const item = current(); if (!item) return;
  $('title').textContent = item.name; $('rows').replaceChildren();
  shownRow = -1;
  // The rows are being rebuilt: any hover index held from the previous render
  // points at a row that is about to be replaced.
  wave.setHover(null);
  $('metadata').textContent = JSON.stringify({metadata: item.document.metadata, unresolved: item.document.unresolved}, null, 2);
  const groups = item.document.groups;
  if (!groups.length) {
    // 删到一行不剩时表格是一片空白，用户只会以为界面坏了。空状态必须给出下一步
    // —— 和 #fitState 的默认文案、#projectCount 的提示是同一条规矩。
    const empty = document.createElement('tr');
    empty.className = 'emptyRow';
    const cell = document.createElement('td');
    cell.colSpan = 4;
    cell.textContent = '还没有歌词行：点工具栏的「添加行」就能从 0 秒开始加第一行（也可以再导入一份歌词）。';
    empty.append(cell);
    $('rows').append(empty);
  }
  const total = item.duration || Math.max(1000, ...groups.map((g) => (g.main.end ?? g.start + 2500)));
  for (const [i, group] of groups.entries()) {
    const row = document.createElement('tr');
    row.dataset.index = i;
    const timeCell = document.createElement('td');
    for (const field of ['main', 'translation']) {
      const label = document.createElement('label'); label.className = 'axisInput'; label.textContent = field === 'main' ? '主 ' : '译 ';
      const input = document.createElement('input'); input.type = 'number'; input.min = '0'; input.step = '.001';
      input.value = (trackStart(group, field) / 1000).toFixed(3); input.setAttribute('aria-label', `第${i + 1}组${field === 'main' ? '主轨' : '翻译轨'}开始秒`);
      input.onchange = () => { try { changeTime(i, field, 'start', Math.round(Number(input.value) * 1000)); } catch (e) { say(e.message); render(); } };
      label.append(input); timeCell.append(label);
    }
    // Where this line sits in the whole song, so a row can be related to the
    // waveform without counting seconds in your head.
    const lineStart = trackStart(group, 'main');
    const lineEnd = group.main.end ?? (groups[i + 1]?.start ?? lineStart + 2500);
    const bar = document.createElement('div'); bar.className = 'posline';
    const segment = document.createElement('i');
    segment.style.left = (lineStart / total * 100).toFixed(3) + '%';
    segment.style.width = Math.max(0.5, (lineEnd - lineStart) / total * 100).toFixed(3) + '%';
    bar.append(segment);
    bar.title = `第 ${i + 1} 行 · ${stamp(lineStart)} → ${stamp(lineEnd)} · 全曲 ${(total / 1000).toFixed(0)} 秒`;
    timeCell.append(bar);
    const seek = document.createElement('button'); seek.textContent = '▶ 定位';
    seek.title = '跳到这一行并让波形滚到它';
    seek.onclick = () => locate(i);
    timeCell.append(seek); row.append(timeCell);
    for (const field of ['main', 'translation']) {
      const cell = document.createElement('td'), area = document.createElement('textarea'); area.value = group[field].text; area.rows = 2;
      area.setAttribute('aria-label', `第${i + 1}组${field === 'main' ? '主歌词' : '翻译'}`);
      // `remember(item, label)` —— 第一个参数是歌曲条目，不是标签。写成
      // `remember('改歌词文字')` 会把标签当成条目：文档改了、历史没记、工程没落盘，
      // 还抛异常把 refresh() 一起带走。
      // 顺序与「编辑歌词文字」对话框里的 applyText() 保持一致：只有真的变了才记一步，
      // 清掉逐字轴（不可逆）必须说出来。
      area.onchange = () => {
        const value = area.value;
        if (value === group[field].text) return;
        const had = group[field].words.length;
        const kept = Doc.retargetWords(group[field], value);
        group[field].text = value;
        remember(item, '改歌词文字');
        refresh();
        if (had && !kept) say('已改歌词文字；字数变了，这一行的逐字时间已清除（可撤销）。');
      };
      const edit = document.createElement('button'); edit.textContent = '编辑逐字'; edit.onclick = () => openWords(i, field);
      cell.append(area, edit); row.append(cell);
    }
    const actions = document.createElement('td');
    for (const [label, fn] of [
      ['双轴同步', () => { const start = trackStart(group, 'translation'); changeTime(i, 'main', 'start', start, null, false); say('主轨句首已同步到翻译轨；请试听中文逐字节奏'); }],
      // 同样：第一个参数必须是条目。写成 `remember('交换双轴')` 会让交换双轴
      // 既不可撤销也不落盘，还抛异常打断刷新。
      ['交换', () => { const old = group.start; group.main.start ??= old; group.translation.start ??= old; [group.main, group.translation] = [group.translation, group.main]; group.start = group.main.start; remember(item, '交换双轴'); refresh(); say('已交换主轨与翻译轨（可撤销）。'); }],
      ['＋插入', () => insertRow(i)],
      ['删除', () => deleteRow(i)]
    ]) { const b = document.createElement('button'); b.textContent = label;
      if (label === '＋插入') b.title = '与工具栏「添加行」同一功能：在这一行下方插入新行（句首取两行的中间）';
      if (label === '删除') b.title = '删掉这一行（可撤销）。删空之后工具栏的「添加行」会给出下一步提示';
      // Row actions can be async now (structural edits go through the backend),
      // so a rejected promise has to be surfaced like a thrown error.
      b.onclick = () => { try { const done = fn(); if (done && done.catch) done.catch(e => say(e.message)); } catch(e) { say(e.message); } }; actions.append(b); }
    row.append(actions);
    // Hovering a row highlights the matching block on the waveform; clicking
    // anywhere that is not an editor moves both to that line.
    //
    // Hover goes into its own slot (setHover), never into setHighlight: the
    // playback loop writes setHighlight only when the *row* changes, so a hover
    // that stole that field mid-row froze the highlight until the next row
    // boundary. Letting both coexist means playback keeps updating underneath
    // and the pointer simply takes visual priority while it is over the table.
    row.onmouseenter = () => wave.setHover(i);
    row.onmouseleave = () => wave.setHover(null);
    row.onclick = (event) => { if (event.target.closest('input,textarea,button,label')) return; locate(i); };
    $('rows').append(row);
  }
  // A freshly inserted line should be visible and ready to type into without
  // hunting for it in the table.
  if (focusRow >= 0) {
    const row = $('rows').children[focusRow];
    if (row) {
      row.classList.add('fresh');
      const area = row.querySelector('textarea');
      if (area) area.focus();
      pinRowToTop(row);
    }
    focusRow = -1;
  }
  // 历史面板每次重绘都刷新一次。它顺带把时间轴的「载入」这一步**在载入时就建好**：
  // 时间轴必须在这里就创建 —— 懒创建（只在 remember/goTo 时建）会让"某次编辑
  // 第一个抛异常"或"载入后第一次动作是改文字"时，「载入」快照里含着那次改动，
  // 那一步就永远撤不回去了。
  renderHistory();
  wave.setDocument(item.document); wave.setHighlight(activeRow()); renderFitList();
}
async function select(index) {
  const audio = $('audio');
  // Re-selecting the SAME song happens whenever a late match re-adopts a
  // document (and on every explicit re-select). Emptying the element then
  // throws away where the user was listening, so remember it before the reset
  // and put it back once the new source is seekable.
  const sameSong = selected === index && !!audio.src;
  const resumeAt = sameSong && Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
  const wasPlaying = sameSong && !audio.paused;
  // 换歌 = 上一首的位置作废（否则"从暂停点继续"会把新歌拖到旧歌的时间上）；
  // 同一首重选 = 位置照旧留着（resumeAt 也来自同一个读数），而且**这一段也不变** ——
  // 用户没有换位置，反复播放的起点不该被一次重新载入吃掉。
  if (sameSong) markPlaybackPosition(resumeAt, '重选同一首');
  else { playbackMark = null; segmentStart = null; }
  selected = index; const version = ++selectionVersion, item = current();
  // A new song starts with no anchor: "添加行" must not inherit the previous
  // song's row number.
  anchorRow = -1; focusRow = -1;
  // Clearing the element zeroes currentTime. Nothing may interpret that as
  // "playback is at 0" until the replacement source has loaded and started.
  audioSettling = true;
  audio.pause(); audio.removeAttribute('src'); audio.load();
  if (audioUrl) URL.revokeObjectURL(audioUrl); audioUrl = null;
  wave.load(null); item.duration = null; setPan(0, false); refresh(); renderMatch(); repaintQueue();
  // 换歌 = 预览层要按**这一首**重算（新歌没有贴唱结果时就是清掉）。
  // 顺序要在 refresh() 之后：render() 会重设文档，别让它把刚设好的预览冲掉。
  applyFitPreview();
  const file = audioFiles.get(key(item.name));
  if (!file) { audioSettling = false; return; }
  audioUrl = URL.createObjectURL(file); audio.src = audioUrl;
  try {
    const buffer = await SaltAudio.decode(file);
    if (version !== selectionVersion) return;
    item.duration = Math.floor(buffer.duration * 1000); wave.load(buffer);
    $('audioInfo').textContent = `${buffer.duration.toFixed(2)} 秒 · ${buffer.numberOfChannels} 声道 · ${(buffer.sampleRate / 1000).toFixed(1)} kHz`;
    // Restore the position for a re-select, then let go of the settling guard
    // so preview() may trust currentTime again.
    if (resumeAt > 0 && resumeAt < buffer.duration) audio.currentTime = resumeAt;
    audioSettling = false;
    if (wasPlaying) audio.play().catch(() => {});
    await check();
    // The duration is only known once the audio is decoded, and it is the
    // strongest matching signal — so redo the lookup if it was missing before.
    if (item.pending && item.matchDuration !== item.duration) {
      await resolveLyric(item, {force: true, version}).catch((error) => say('自动匹配失败：' + error.message));
    }
  } catch (e) {
    if (version === selectionVersion) {
      const note = audioNoteFor(item.name);
      say('音频解码失败：' + (note || e.message));
      if (note) window.Notify?.banner?.('audio:' + item.name, note);
    }
    audioSettling = false;
  }
}
// ---------------------------------------------- word-timing editor (K 方式)
// One card per unit, and tap mode — play the song, press Enter at each unit's
// onset. No invented timings: every number comes from the user (tap / typed /
// explicit spread).
let wordUnits = [];   // working copy while the dialog is open: {text, start|null}
let tapAt = 0;        // which unit the tap cursor points at next
let wordOpened = null;  // 打开那一刻的形状凭据（见 applyWords 的守卫）

function openWords(index, field) {
  wordIndex = index; wordField = field; $('wordError').textContent = '';
  const item = current(), g = item.document.groups[index], t = g[field];
  // 记下"这一行是谁"。对话框开着的时候文档仍可能被改形状（键盘撤销/重做、
  // 慢到的载入结果），行号会移位 —— 那时照旧写下去就会把这一行的字与文字
  // 写进**另一行**，是真正的数据损坏。判据与「智能贴唱」的 fit.snapshot 同一套。
  wordOpened = {index, field, text: t.text, rows: item.document.groups.length};
  $('wordTitle').textContent = (field === 'main' ? '主歌词' : '翻译') + ' · 第 ' + (index + 1) + ' 行 · 逐字时间';
  wordUnits = t.words.length
    ? t.words.map(w => ({text: w.text, start: w.start}))
    : [{text: t.text, start: trackStart(g, field)}];
  tapAt = 0;
  const end = t.end ?? Doc.lineEnd(item.document, index);
  $('wordEndTime').value = (end / 1000).toFixed(3);
  $('wordLineInfo').textContent = '行文本：' + (t.text || '（空）') + ' · ' + wordUnits.length +
    ' 个字词 · 句首 ' + stamp(trackStart(g, field)) +
    (t.words.length ? '' : ' · 该行还没有逐字轴，先「自动分词」或直接打点');
  renderWordChips();
  $('wordDialog').showModal();
  window.Trace?.dialogOpen?.($('wordDialog'));
  window.Trace?.step?.('逐字编辑器已打开', '第 ' + (index + 1) + ' 行 · ' + wordUnits.length + ' 个字词');
}

function renderWordChips(progress) {
  const grid = $('wordChips');
  grid.replaceChildren();
  wordUnits.forEach((unit, i) => {
    const bad = i && unit.start != null && wordUnits[i - 1].start != null && unit.start < wordUnits[i - 1].start;
    const cell = document.createElement('div');
    cell.className = 'wordCell' + (i === tapAt ? ' next' : '') + (bad ? ' bad' : '');
    const text = document.createElement('span'); text.className = 'wordChar'; text.textContent = unit.text;
    const input = document.createElement('input'); input.type = 'number'; input.step = '0.001'; input.min = '0';
    input.className = 'wordTime'; input.placeholder = '待打点';
    if (unit.start != null) input.value = (unit.start / 1000).toFixed(3);
    input.setAttribute('aria-label', `第${i + 1}个字词开始秒`);
    input.onchange = () => { unit.start = input.value === '' ? null : Math.round(Number(input.value) * 1000); renderWordChips(); };
    const now = document.createElement('button'); now.textContent = '● 当前';
    now.title = '把这个字的时间设为当前播放位置';
    now.onclick = () => { unit.start = Math.round($('audio').currentTime * 1000); tapAt = i + 1; renderWordChips(); };
    cell.append(text, input, now); grid.append(cell);
  });
  const done = wordUnits.filter(u => u.start != null).length;
  $('wordError').textContent = progress ||
    (wordUnits.length && done < wordUnits.length
      ? `已打点 ${done}/${wordUnits.length}：播放音频，每到该字发声的位置就按一次 Enter`
      : '');
}

function wordTap() {
  if (!$('wordDialog').open || !wordUnits.length) return;
  const time = Math.round($('audio').currentTime * 1000);
  if (tapAt < wordUnits.length) {
    wordUnits[tapAt].start = time;
    tapAt += 1;
    renderWordChips();
    if (tapAt >= wordUnits.length) $('wordError').textContent = `全部 ${wordUnits.length} 个字词已打点，句尾也已设为当前时间；核对后点「应用逐字时间」`;
  } else {
    $('wordEndTime').value = ($('audio').currentTime).toFixed(3);
    renderWordChips(`全部 ${wordUnits.length} 个字词已打点；再按 Enter 会改写句尾时间`);
  }
}

function applyWords() {
  const item = current();
  if (!item || wordIndex < 0 || !$('wordDialog').open) return;
  // 行号还指得着同一行吗？对话框是模态的，但键盘的撤销/重做、以及慢到的导入
  // 结果都能在它开着的时候改掉文档形状。写错行 = 把字和文字一起挪到别的句子上，
  // 所以这里宁可拒绝，并说清怎么继续。
  const target = item.document.groups[wordIndex];
  if (wordOpened && (item.document.groups.length !== wordOpened.rows
      || wordIndex !== wordOpened.index || !target
      || target[wordField].text !== wordOpened.text)) {
    throw Error('打开逐字编辑器之后歌词被改过（换行/改字），行号可能已经移位；'
                + '请先关掉这个对话框、重新打开一次要改的那一行，免得把字写进别的行');
  }
  if (!target) throw Error('这一行已经不在了：请重新打开逐字编辑器');
  const missing = wordUnits.filter(u => u.start == null).length;
  if (missing) throw Error(`还有 ${missing} 个字词没有时间：播放音频按 Enter 逐字打点，或直接填入秒数`);
  const words = wordUnits.map(u => ({start: u.start, text: u.text}));
  const typed = Math.round(Number($('wordEndTime').value) * 1000);
  if (!Number.isFinite(typed) || typed <= 0) throw Error('请填一个有效的句尾时间（秒）');
  // Tapping or typing near the tail can land a word at or past the 句尾, and the
  // format does not allow a line to end before its last word. Raising the end is
  // the only way to keep the word times the user just made; failing here would
  // strand them at that boundary.
  const fitted = Doc.fitLineEnd(typed, words, item.duration);
  const end = fitted.end;
  const doc = clone(item.document), g = doc.groups[wordIndex], t = g[wordField];
  t.words = words; t.end = end; t.start = trackStart(g, wordField);
  t.text = words.map(w => w.text).join('');
  validateEdit(doc, item.duration);
  remember(item, '改逐字时间', doc);
  $('wordDialog').close();
  window.Trace?.step?.('已应用逐字时间', wordUnits.length + ' 个字词');
  refresh();
  if (fitted.note) say(fitted.note + '（逐字时间已保留，可点「撤销」或重新打开改写句尾）');
}

async function autoSplitWords() {
  const item = current();
  if (!item || wordIndex < 0) return;
  const g = item.document.groups[wordIndex], t = g[wordField];
  if (!t.text) throw Error('这一行还没有歌词文字');
  // Pass the track's language when we already know it: the whole song is far
  // better evidence than one short line, and it is what keeps a kanji-only line
  // inside a Japanese song from being split as Chinese.
  const lang = item.languages?.[wordField === 'main' ? 'main' : 'translation']?.code;
  const result = await api('tokenize', {text: t.text, lang});
  const units = result.units.map(text => ({text, start: null}));
  // Inherit old timings by character offset: a new unit starts when the old
  // unit covering its first character started, so re-splitting never throws
  // the existing work away.
  let acc = 0;
  const marks = t.words.map(w => { const mark = {at: acc, start: w.start}; acc += w.text.length; return mark; });
  let pos = 0;
  for (const u of units) {
    const mark = [...marks].reverse().find(m => m.at <= pos);
    if (mark && pos < acc) u.start = mark.start;
    pos += u.text.length;
  }
  wordUnits = units; tapAt = 0;
  renderWordChips();
  const rule = result.info?.rule || '按字词';
  say(`已按「${rule}」切成 ${units.length} 个字词${t.words.length ? '，旧时间已按位置继承' : ''}；可打点、均分或直接改数字`);
}

function spreadWords() {
  const item = current();
  if (!item || wordIndex < 0 || !wordUnits.length) return;
  const g = item.document.groups[wordIndex];
  const t0 = trackStart(g, wordField);
  const t1 = Math.round(Number($('wordEndTime').value) * 1000);
  if (!Number.isFinite(t1) || t1 <= t0) throw Error('先填一个大于句首的句尾时间，再均分');
  const step = (t1 - t0) / wordUnits.length;
  wordUnits.forEach((u, i) => { u.start = Math.round(t0 + i * step); });
  tapAt = wordUnits.length;
  renderWordChips('已按字数均分，仅作参考：请对照音频微调后再应用');
}

// Preview runs at animation rate, so it must never rebuild DOM needlessly:
// spans are rebuilt only when the active line changes, otherwise only the
// highlighted word's class flips. The same renderer drives the inline preview
// and the full-screen karaoke view.
const PREVIEW_SETS = [
  {main: 'previewMain', prev: 'previewPrev', trans: 'previewTranslation', next: 'previewNext'},
  {main: 'karaokeMain', prev: 'karaokePrev', trans: 'karaokeTranslation', next: 'karaokeNext'},
].map(ids => ({ids, index: -1, text: null, spans: [], active: -1, prev: null, next: null, trans: null}));

function setLine(id, text, cache, key) {
  if (cache[key] === text) return;
  cache[key] = text;
  $(id).textContent = text || '';
}

function preview() {
  setTransport();                 // cheap; guarded so it only writes on change
  const item = current();
  if (!item) return;
  // While the audio element is being swapped, currentTime is 0 — the element was
  // just cleared, not the user sitting at the start. Deriving a row index now
  // would publish a bogus -1 that poisons shownRow and kills the follow loop.
  // Keep the last painted position and wait for the audio to settle.
  if (audioSettling) return;
  const ms = $('audio').currentTime * 1000;
  const groups = timelineGroups(item);     // 预览时读**贴后**的那一份（唯一来源）
  let index = -1;
  for (let i = 0; i < groups.length; i++) if (trackStart(groups[i], 'main') <= ms) index = i;
  for (const set of PREVIEW_SETS) {
    const g = index >= 0 ? groups[index] : null;
    setLine(set.ids.prev, index > 0 ? groups[index - 1].main.text : '', set, 'prev');
    setLine(set.ids.next, g && groups[index + 1] ? groups[index + 1].main.text : '', set, 'next');
    setLine(set.ids.trans, g ? g.translation.text : (index < 0 ? '' : ''), set, 'trans');
    const main = g ? g.main : null;
    if (set.index !== index || set.text !== (main?.text ?? null)) {
      const element = $(set.ids.main);
      element.replaceChildren();
      set.spans = []; set.active = -1;
      if (main && main.words.length) {
        for (const word of main.words) {
          const span = document.createElement('span'); span.textContent = word.text;
          element.append(span); set.spans.push(span);
        }
      } else element.textContent = main ? main.text : (index < 0 ? '等待歌词开始' : '');
      set.index = index; set.text = main?.text ?? null;
    }
    if (main && set.spans.length) {
      let activeWord = -1;
      for (let i = 0; i < main.words.length; i++) {
        const word = main.words[i];
        if (ms >= word.start && ms < (main.words[i + 1]?.start ?? main.end ?? Infinity)) { activeWord = i; break; }
      }
      if (activeWord !== set.active) {
        if (set.spans[set.active]) set.spans[set.active].className = 'word';
        if (set.spans[activeWord]) set.spans[activeWord].className = 'word active';
        set.active = activeWord;
      }
    }
  }
  // Keep the table row, the waveform highlight and playback on the same line.
  if (index !== shownRow) {
    const rows = $('rows').children;
    // Only the two rows that actually change are touched. Sweeping every row
    // would be O(lines) per change — felt when clicking quickly through a long
    // lyric — and on a per-frame path that is O(lines) *per frame*.
    // Removing from a row that is not marked is a no-op, so this is safe even
    // when the table was rebuilt underneath us.
    rows[shownRow]?.classList.remove('activeRow');
    rows[index]?.classList.add('activeRow');
    shownRow = index;
    wave.setHighlight(index);
    if ($('follow').checked && index >= 0 && rows[index]) pinRowToTop(rows[index]);
  }
  wave.setTime(ms / 1000, $('follow').checked);
}
let rafLoop = 0;
function tick() {
  rafLoop = 0;
  if ($('audio').paused) return;
  preview();
  rafLoop = requestAnimationFrame(tick);
}
// THE authority for "should the per-frame update loop be running".
//
// The invariant is simply: the loop runs exactly while audio is playing.
// Deriving the answer from the element's own state (rather than from which
// event happened to fire) makes it dependable: every caller may ask for a sync
// at any time, and the loop always ends up matching playback. Wiring separate
// handlers instead means a seek *during playback* can cancel the loop while the
// sound keeps going, and nothing restarts it — `play` only fires when playback
// STARTS, so the playhead, the karaoke highlight and the follow-scroll then
// freeze with audio still audible.
function syncPlaybackLoop() {
  const audio = $('audio');
  const playing = !audio.paused && !audio.ended;
  if (playing) {
    if (!rafLoop) rafLoop = requestAnimationFrame(tick);
  } else if (rafLoop) {
    cancelAnimationFrame(rafLoop);
    rafLoop = 0;
  }
}
// ---- transport (#playPause / #playTime) ------------------------------------
// These replace the browser's native media bar. That bar is drawn by the
// browser and ignores our CSS. Owning the transport means it can never be
// restyled out from under us, and it keeps the spacebar and the button on ONE
// code path instead of two that drift apart.
function formatClock(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return m + ':' + String(s).padStart(2, '0');
}
let clockText = '';
function setTransport() {
  const audio = $('audio');
  const label = audio.paused ? '▶ 播放' : '⏸ 暂停';
  const button = $('playPause');
  if (button && button.textContent !== label) button.textContent = label;
  const total = Number.isFinite(audio.duration) ? audio.duration : 0;
  const text = formatClock(audio.src ? audio.currentTime : 0) + ' / ' + formatClock(total);
  if (text !== clockText) {
    clockText = text;
    const clock = $('playTime');
    if (clock) clock.textContent = text;
  }
}
function togglePlay() {
  const audio = $('audio');
  if (!audio.src) { say('请先添加音频'); return; }
  if (!audio.paused) {
    audio.pause();
    // 暂停点也是一个位置 —— 由这一个函数决定它算不算（见上面的说明）。
    notePausePoint(audio.currentTime);
    return;
  }
  // Play. 位置由 playbackAnchor() 一处决定：元素自己的读数**不是**权威 ——
  // 换音源（load()）会把 currentTime 清零，那正是"再次播放不是从暂停点开始"的来源。
  const anchor = playbackAnchor(audio);
  if (Math.abs((audio.currentTime || 0) - anchor) > 0.05) {
    try { audio.currentTime = anchor; } catch { /* 元数据还没到，浏览器会记成起始位置 */ }
  }
  if (repeatSegment()) {
    // 这一段从哪儿开始 —— 反复播放接下来每次都回到这里。
    segmentStart = anchor;
    window.Trace?.step?.('反复播放', '这一段从 ' + anchor.toFixed(1) + 's 开始');
  }
  audio.play().catch(error => say(error.message));
}

// ---- 试听位置：一个权威 -------------------------------------------------------
//
// 「下次按播放从哪里开始」只有两个来源，别的都不许写：
//   ① 用户**显式**指定的位置：点某一行 / 点某句 / 点波形空白、贴条、试听按钮；
//   ② 用户按了暂停 —— 暂停点就是位置（这一条由设置里的开关决定算不算）。
//
// <audio> 自己的 currentTime 在两种情况下不是证据 ——
// 换音源（`removeAttribute('src') + load()`）会把它清零，音频解码/加载还没到位时
// 它也可能是 0。把"要从哪开始"记在**我们自己**的状态里，这件事就与浏览器时序无关了
// （同一条不变量：迟到的异步结果不能写坏权威状态）。
//
// 两个槽位，各管一个问题（别把它们合成一个 —— 那就没法同时支持"继续听"和"反复听"）：
//   playbackMark  = 位置（暂停点 / 显式指定的点）
//   segmentStart  = **这一段**的起点（打开「反复播放」时，按播放总是回到它）
let playbackMark = null;   // 秒；null = 没有标记 → 从 0 开始
let segmentStart = null;   // 秒；null = 这一段还没开始（下次按播放会重新取）

/** 记下一个位置。第二个参数只用于 L1 追踪，方便下次排查"它到底从哪开始的"。
 *
 *  `freshSegment`：这是不是"用户显式换了个位置"。是的话，反复播放的那一段作废，
 *  下次按播放就在新位置重新取起点（这正是"任意位置、指针指定"要的效果）。 */
function markPlaybackPosition(seconds, why, freshSegment) {
  if (!Number.isFinite(seconds) || seconds < 0) return;
  playbackMark = seconds;
  if (freshSegment) segmentStart = null;
  window.Trace?.step?.('试听位置', why + ' ' + seconds.toFixed(1) + 's');
}

/** 暂停点：设置里关掉"从暂停点继续"时，暂停**不**算位置。
 *  注意它**不**清 `segmentStart` —— 清了就没法"暂停后再回到这一段开头"了。 */
function notePausePoint(seconds) {
  if (resumeAfterPause()) markPlaybackPosition(seconds, '暂停于');
  else {
    playbackMark = null;
    window.Trace?.step?.('试听位置', '暂停于 ' + (seconds || 0).toFixed(1) + 's（开关已关：下次从头）');
  }
}

/** 纯函数：这次按播放应该从第几秒开始。
 *
 *  **两个功能在这里汇合，优先级是写死的：反复播放优先。**
 *  开着「反复」且这一段已经有起点 → 回到那个起点（不管"从暂停点继续"怎么设）；
 *  否则按默认规则：有标记就用标记，没有就从头。
 *
 *  两条规则各自独立（"这一段从哪开始"和"暂停算不算位置"），所以不合成一个三选一：
 *  合成之后"用户显式点的位置"这条会被开关一起吃掉。
 */
function playbackAnchor(audio) {
  if (repeatSegment() && segmentStart != null) return segmentStart;
  return playbackStart(audio);
}

/** 默认规则：没有标记 → 0；标记跑到音频长度之外（换过歌/换过文件）→ 0。 */
function playbackStart(audio) {
  if (playbackMark == null) return 0;
  const total = Number.isFinite(audio.duration) ? audio.duration : null;
  if (total != null && playbackMark > total) return 0;
  return playbackMark;
}

function resumeAfterPause() {
  return !appSettings || appSettings.features?.resumeAfterPause !== false;
}

/** 反复播放：暂停后再按播放，回到这段的起点重播。默认关。 */
function repeatSegment() {
  return !!appSettings && appSettings.features?.repeatSegment === true;
}

// ---- 倍速 ---------------------------------------------------------------------
//
// 合法范围只在这里定义一处，下拉框的选项也由它生成（别再往 HTML 里写一遍值）。
// `defaultPlaybackRate` 必须一起写：换音源（load()）时浏览器会把 `playbackRate`
// **重置回 `defaultPlaybackRate`** —— 只写前者的话，每选一首歌倍速就悄悄变回 1×。
const SPEEDS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3];
const SPEED_KEY = 'saltlrc-speed';

function snapSpeed(value) {
  const wanted = Number(value);
  if (!Number.isFinite(wanted) || wanted <= 0) return 1;
  let best = SPEEDS[0];
  for (const option of SPEEDS) if (Math.abs(option - wanted) < Math.abs(best - wanted)) best = option;
  return best;
}

function applySpeed(value, save) {
  const rate = snapSpeed(value);
  const audio = $('audio');
  audio.playbackRate = rate;
  audio.defaultPlaybackRate = rate;
  // 慢放不变调（Chromium/WebKit 的拼写；Firefox 认 mozPreservesPitch）。
  audio.preservesPitch = true;
  if (save) { try { localStorage.setItem(SPEED_KEY, String(rate)); } catch { /* 配额/隐私模式：忽略 */ } }
  const select = $('speed');
  if (select && String(select.value) !== String(rate)) select.value = String(rate);
  return rate;
}

function restoreSpeed() {
  applySpeed(Number(localStorage.getItem(SPEED_KEY)) || 1, false);
}

function fillSpeedOptions() {
  const select = $('speed');
  if (!select) return;
  select.replaceChildren(...SPEEDS.map((rate) => {
    const option = document.createElement('option');
    option.value = String(rate);
    option.textContent = (rate === 1 ? '1×（原速）' : String(rate) + '×');
    return option;
  }));
  select.value = '1';
}
// ---- volume ----------------------------------------------------------------
// The transport owns the volume control — same as the play button: our own
// control cannot be restyled out from under us.
//
// One function owns the three things that must agree: the element's volume, the
// slider position and the readout. The value is persisted: a volume that snaps
// back to 100% on every launch is worse than having no control at all.
const VOLUME_KEY = 'saltlrc-volume';
function applyVolume(value, save) {
  const percent = Math.max(0, Math.min(100, Math.round(Number(value))));
  if (!Number.isFinite(percent)) return;
  $('audio').volume = percent / 100;
  const slider = $('volume');
  if (slider && Number(slider.value) !== percent) slider.value = String(percent);
  const readout = $('volumeValue');
  if (readout) readout.textContent = percent + '%';
  paintRange(slider);          // 填充条要跟着走，否则两端到不了 0/100
  if (save) {
    try { localStorage.setItem(VOLUME_KEY, String(percent)); } catch { /* 配额不足时静默跳过 */ }
  }
}
function restoreVolume() {
  let stored = null;
  try { stored = localStorage.getItem(VOLUME_KEY); } catch { /* 存储不可用则用默认值 */ }
  applyVolume(stored === null || stored === '' ? 100 : stored, false);
}
// The panel is a standalone plugin; see debug-panel.js.
const debugPanel = new SaltDebugPanel(wave, {
  audio: () => {
    const element = $('audio');
    return element.src
      ? `${element.currentTime.toFixed(2)}s / ${(element.duration || 0).toFixed(2)}s · ${element.paused ? '暂停' : '播放中'}`
      : '未载入音频';
  },
});



// ---- 智能贴唱：一个按钮跑完整首 ------------------------------------------------
//
// 一个按钮 + 一个预览开关："识别"和"对齐"是同一件事 ——
// 找出**人声**起点（不是随便什么能量上升），再让所有行的句首**一起**走一条
// 单调路径贴上去。行内逐字细化不在这里，它在逐字编辑器里。
//
// 服务端算，页面只管"预览还是应用" —— 两边各算一遍会出现两个权威。

/** 时间轴读数只认这一个来源：预览打开且这次贴唱有结果时，读**贴后的**那一份。
 *
 * 句首有两个来源（原稿与贴唱提案）。preview() 读一个、activeRow() 读另一个时，
 * 画面上就会出现"高亮在第 3 行、表格钉在第 4 行"这种两个权威打架的现象。
 * **所有"按时间找行"的代码都必须从这里拿 groups。** */
function timelineGroups(item = current()) {
  const fit = item && item.fit;
  if (fit && fitPreviewOn()) return fit.document.groups;
  return (item && item.document.groups) || [];
}

function fitPreviewOn() {
  return !!$('fitPreview') && $('fitPreview').checked;
}

function fitSummary(report) {
  return `人声起点 ${report.onsets} 个 · ${report.moved} 行贴到起点 · `
       + `${report.weak} 行置信偏低 · ${report.kept} 行保持原时间`;
}

async function smartFit() {
  const item = current();
  if (!item) throw Error('请先导入歌词');
  if (!item.document.groups.length) throw Error('这份歌词还没有行');
  const audio = await audioPayload();       // 没音频会抛"请先添加与歌词同名的音频"
  $('fitState').textContent = '正在听音频找人声起点…';
  try {
    const report = await api('align/fit', {document: item.document, audio,
                                           field: $('sourceTrack').value,
                                           max_shift: Number($('maxShift').value) || 8000});
    item.fit = {document: normalizeDocument(report.document), lines: report.lines,
                report, snapshot: JSON.stringify(item.document)};
    $('fitState').textContent = report.notice;
    renderFitList();
    applyFitPreview();
    say(fitSummary(report) + (fitPreviewOn()
      ? ' —— 已经在波形上预览：按播放听一遍，贴合了再点「应用贴唱」。'
      : ' —— 打开「预览」可以在波形上核对。'));
  } catch (error) {
    // 用户主动触发的动作，失败必须在这一页也说清楚（状态行是全局的）
    $('fitState').textContent = '贴唱没做成：' + error.message;
    throw error;
  }
}

/** 贴唱结果列表：每行写明"原来在哪 → 贴到哪"以及该行的判断依据。 */
function renderFitList() {
  const box = $('fitList');
  if (!box) return;
  box.replaceChildren();
  const item = current(), fit = item && item.fit;
  $('fitReport').textContent = fit ? fitSummary(fit.report) : '';
  if (!fit) return;
  const stamp = ms => (ms / 1000).toFixed(3) + 's';
  for (const line of fit.lines) {
    const row = document.createElement('label');
    row.className = 'proposal' + (line.confidence === 'kept' ? ' fitKept' : '');
    const text = document.createElement('span');
    const arrow = line.moved_ms ? `${stamp(line.old_start)} → ${stamp(line.start)}` : '位置未变';
    const tag = line.confidence === 'snapped' ? '已贴合'
              : line.confidence === 'weak' ? '置信偏低' : '保持原时间';
    text.textContent = `${line.index + 1}. ${arrow}　[${tag}] ${line.reason}`;
    const button = document.createElement('button');
    button.textContent = '试听';
    button.onclick = e => {
      e.preventDefault();
      if (!$('audio').src) return;
      const at = Math.max(0, line.start / 1000 - .5);
      markPlaybackPosition(at, '试听贴唱结果', true);
      $('audio').currentTime = at;
      $('audio').play().catch(err => say(err.message));
    };
    row.append(text, button);
    box.append(row);
  }
}

/** 把贴唱结果交给"预览层"：波形叠加 + 播放时按贴后的时间走。**不改文档。** */
function applyFitPreview() {
  const item = current();
  const fit = item && item.fit;
  const on = !!fit && fitPreviewOn();
  wave.setFit(on ? fit.lines.filter(line => line.moved_ms) : null);
  render();
  preview();
  wave.invalidate();
}

function clearFit() {
  const item = current();
  if (item) delete item.fit;
  wave.setFit(null);
  renderFitList();
  render();
  wave.invalidate();
}

bind('smartFit', () => smartFit());
bind('fitPreview', () => {                  // 可选：关掉就只看原稿
  const item = current();
  applyFitPreview();
  say(item && item.fit
    ? (fitPreviewOn() ? '预览已打开：波形与播放都按贴唱结果走。' : '预览已关闭：只看原稿。')
    : '还没有贴唱结果。');
});
bind('fitApply', async () => {
  const item = current();
  if (!item) throw Error('请先导入歌词');
  if (!item.fit) throw Error('还没有贴唱结果：先点「智能贴唱」');
  if (JSON.stringify(item.document) !== item.fit.snapshot) {
    throw Error('贴唱之后歌词被改过（换行/改字），请重新贴唱，免得把后来的编辑盖掉');
  }
  const changed = item.fit.lines.filter(line => line.moved_ms).length;
  const fitted = item.fit.document;
  clearFit();
  remember(item, '智能贴唱', fitted);
  refresh();
  say(`已应用贴唱：${changed} 行的句首/句尾被改动（可撤销）。请试听确认是否贴合。`);
});
bind('fitDiscard', () => {
  if (!current() || !current().fit) throw Error('还没有贴唱结果');
  clearFit();
  say('已丢弃贴唱结果，回到原稿。');
});

async function audioPayload() {
  const item = current(), file = item && audioFiles.get(key(item.name));
  if (!file) throw Error('请先添加与歌词同名的音频');
  return SaltAudio.base64(await SaltAudio.pcm16(await SaltAudio.decode(file)));
}

async function capabilities() {
  const value = await (await fetch('/api/capabilities')).json();
  CAPS = value;
  // 只报告**事实**：贴唱用的是本机能耗分析（永远可用）；语音识别模型装没装也说出来，
  // 免得以为"没装模型就用不了"。这里没有"引擎下拉"要同步。
  const asr = value.faster_whisper && value.base_model;
  if ($('fitEngineState')) {
    $('fitEngineState').textContent = '本机能量贴唱（全离线，不需要下载任何模型）'
      + (asr ? ' · 语音识别模型已就绪' : ' · 语音识别模型未安装（setup-ai.ps1）');
  }
  return value;
}

/** 「语言」下拉只对语音识别引擎有用 —— 选着声音起点对齐时它是个死控件。
 *
 * 看得见却拧不动（或者拧了毫无反应）的控件，用户只会当成坏掉；
 * 所以这里**把它禁用并写明原因**，而不是让它装作能用。
 * 引擎/模型可用性一变就重新算一遍：capabilities() 回来时、以及切换引擎时。 */
let CAPS = null;

$('files').onchange = e => importFiles(e.target.files).catch(err => say(err.message));
$('folder').onchange = e => importFiles(e.target.files).catch(err => say(err.message));
$('audio').onplay = () => { wave.resumeFollow(); syncPlaybackLoop(); };
// `pause` / `ended`: repaint once (the loop stops, so nothing else will), then
// sync. `seeked`: repaint once for the paused case, then sync — a no-op while
// playing, where the loop is already running and must stay that way. Sharing
// one handler across all three is safe now that the handler asks the element
// what to do instead of assuming the event's meaning.
$('audio').onpause = $('audio').onseeked = () => {
  // 暂停 / 定位之后，位置就是"下次从哪播放"的答案（`ended` 不算，见下）。
  // audioSettling 期间不采信：那是我们自己把音源清掉造成的假 0。
  if (!audioSettling) notePausePoint($('audio').currentTime);
  preview();
  syncPlaybackLoop();
};
// 播到结尾：没有"暂停点"可继续，下一次播放按浏览器的规矩从头开始
// （规范里 `ended` 后 play() 会回到最早位置）。两个槽位都要清掉，
// 否则开了"从暂停点继续"/「反复」就会反手把播放头拖回结尾、看起来像卡住。
$('audio').onended = () => {
  playbackMark = null;
  segmentStart = null;
  window.Trace?.step?.('试听位置', '播到结尾：下次从 0 开始');
  preview();
  syncPlaybackLoop();
};
$('audio').onerror = () => say('浏览器无法解码此音频，请换用 WAV/MP3');
// Duration is only known once metadata arrives, and the transport shows it.
// The speed is re-applied here as well: a new source resets `playbackRate` to
// `defaultPlaybackRate`, and this is the one place that knows metadata is ready.
$('audio').onloadedmetadata = () => { applySpeed($('speed')?.value || 1, false); setTransport(); };
$('format').onchange = () => check().catch(e => say(e.message));
$('zoom').oninput = () => setZoom($('zoom').value);          // 滑块给百分比
$('zoomInput').onchange = () => setZoom(SaltWaveform.percentForPps($('zoomInput').value));
bind('applyText', applyText); bind('closeText', () => $('textDialog').close());
bind('playPause', togglePlay);
fillSpeedOptions();                  // 选项由 SPEEDS 生成：合法范围只在那一处定义
restoreSpeed();                      // 记住上次的倍速（换音源不会被浏览器重置掉）
if ($('speed')) {
  $('speed').onchange = () => {
    const rate = applySpeed($('speed').value, true);
    window.Trace?.step?.('倍速', rate + '×');
    say(`试听倍速 ${rate}×` + (rate === 1 ? '' : '（打点记录的仍是真实时间）'));
  };
}
// 「反复」：走带条上是实时控件（一边听一边开关），设置面板里是同一个值的持久入口。
// 开关时**立刻存**（和音量/倍速一个思路）：它是一个模式，用户会以为它记住了；
// 只走 /api/settings/save 的局部更新，不会碰到表单里别的字段。
if ($('repeat')) {
  $('repeat').onchange = () => {
    const on = $('repeat').checked;
    if (appSettings) {
      appSettings.features = {...(appSettings.features || {}), repeatSegment: on};
    }
    if ($('setRepeat')) $('setRepeat').checked = on;
    api('settings/save', {features: {repeatSegment: on}}).catch((error) => say('保存设置失败：' + error.message));
    window.Trace?.step?.('反复播放', on ? '开' : '关');
    say(on
      ? '反复播放已打开：暂停后再按播放会回到这次开始播放的位置重播这一段（优先于「从暂停点继续」）。'
      : '反复播放已关闭：暂停后再按播放回到「从暂停点继续」那条规则。');
  };
}
restoreVolume();                     // 内含 paintRange，音量填充条开局就位
setZoom(SaltWaveform.percentForPps(wave.pps), false);   // 滑块/数字框/填充条与 wave.pps 对齐
if ($('volume')) {
  $('volume').oninput = () => applyVolume($('volume').value, true);
}
$('matchClose').onclick = () => { $('matchPanel').hidden = true; };
bind('matchLocal', async () => {
  const item = current();
  if (!item?.match?.lyric) throw Error('曲库里这首歌没有歌词文件');
  const parsed = await api('parse', {bytes: SaltAudio.base64(new TextEncoder().encode(item.match.lyric))});
  adoptDocument(item, parsed.document);
  say('已使用 Salt Player 曲库里的歌词（' + parsed.document.groups.length + ' 行）。');
});
bind('matchApply', applyPastedLyric);
bind('matchResearch', async () => {
  const item = current();
  if (!item) throw Error('请先选择一首歌');
  item.match = null; item.pending = true;
  await resolveLyric(item, {force: true, search: true, version: selectionVersion});
});
// The source list comes from the backend catalogue, so a new source appears
// here without this page being edited.
function fillMatchProviders(data) {
  const select = $('matchProvider');
  if (!select || !data || !data.provider_catalogue) return;
  const previous = select.value;
  select.replaceChildren();
  for (const entry of data.provider_catalogue) {
    const option = document.createElement('option');
    option.value = entry.id;
    option.textContent = entry.label;
    option.title = entry.note || '';
    select.append(option);
  }
  if (previous && select.querySelector(`option[value="${previous}"]`)) select.value = previous;
}
// Switching the source re-runs the search for the current track, so the user
// does not have to press "重新搜索" after picking a different provider.
$('matchProvider').onchange = () => {
  const item = current();
  if (!item || !item.pending) { say('已切换搜索来源：切换到「识别与对齐」页可重新搜索。'); return; }
  item.match = null;
  resolveLyric(item, {force: true, search: true, version: selectionVersion}).catch(e => say(e.message));
};
bind('check', checkOutLoud); bind('applyWords', applyWords); bind('closeWords', () => { $('wordDialog').close(); window.Trace?.step?.('已取消逐字编辑'); });
bind('mark', wordTap);
bind('wordEndNow', () => { $('wordEndTime').value = $('audio').currentTime.toFixed(3); });
bind('wordAutoSplit', () => autoSplitWords().catch(e => say(e.message)));
bind('wordSpread', () => { try { spreadWords(); } catch (e) { say(e.message); } });
// 「从音频检测这一行」：行内逐字细化归逐字编辑器 —— 它改的是"这一行的字"。
// 整行的句首/句尾由「智能贴唱」负责。
bind('wordFromAudio', async () => {
  const item = current();
  if (!item || wordIndex < 0) throw Error('请先选一行，再打开逐字编辑器');
  if (!wordUnits.length) throw Error('先「自动分词」把这一行切成字组');
  const audio = await audioPayload();
  $('wordError').textContent = '正在按音频检测这一行的发声起点…';
  const result = await api('align/words', {document: item.document, audio, field: wordField});
  const entry = (result.proposals || []).find(row => row.index === wordIndex);
  if (!entry) throw Error('音频分析没有给出这一行的结果');
  if (entry.status !== 'ready' || !entry.words.length) {
    throw Error(entry.reason || '这一行没能从音频里定出字组时间');
  }
  // 按**字符位置**对齐，不按下标：分词规则可能变过，下标对齐会错位。
  const byPosition = new Map();
  let cursor = 0;
  for (const word of entry.words) { byPosition.set(cursor, word.start); cursor += word.text.length; }
  let at = 0, filled = 0;
  for (const unit of wordUnits) {
    const hit = byPosition.get(at);
    if (hit != null) { unit.start = hit; filled++; }
    at += unit.text.length;
  }
  tapAt = wordUnits.length;
  $('wordError').textContent = '';
  renderWordChips(`已按音频检测：${filled}/${wordUnits.length} 个字组拿到起点（${
    entry.confidence === 'exact' ? '精确匹配' : '近似'}）；${
    entry.reason}。请试听核对后再「应用逐字时间」`);
});
bind('undo', () => undo()); bind('redo', () => redo());
bind('undoStep', () => undo()); bind('redoStep', () => redo()); bind('clearSession', clearSession);
bind('restoreSession', () => {
  const count = adoptSession();
  if (!count) { say('没有可恢复的会话。'); return; }
  select(0).then(() => say(`已恢复上次的 ${count} 首歌词；音频需要重新添加一次。`))
           .catch((error) => say('恢复会话失败：' + error.message));
});
// "添加行" and every row's "＋插入" are the same action: below the selected
// line, or — with nothing selected — on the current playback position.
bind('add', () => insertRow(null).catch(e => say(e.message)));
bind('shift', async () => { if (!current()) return; const item = current(), result = await api('shift', {document: item.document, delta: Number($('offset').value)}); remember(item, '整体偏移', result.document); refresh(); });
bind('save', () => saveProject().catch(e => say(e.message)));
bind('pickLibrary', () => openLibraryDialog().catch(e => say(e.message)));
bind('libClose', () => $('libraryDialog').close());
$('libSearch').oninput = () => renderLibrary($('libSearch').value);
$('libMissing').onchange = () => renderLibrary($('libSearch').value);
bind('help', () => $('helpDialog').showModal());
bind('closeHelp', () => $('helpDialog').close());
// 打开工程只有一个入口：顶部的「添加 / 打开歌词 · 音频 · 工程」（importFiles 里
// 按扩展名分派到 openProjectFile），拖进窗口走的是同一个函数。这里不再另接一个按钮。
// 导出歌词的四步：
//     ① 读取已编辑的歌词  →  ② 打包成 LRC（服务端渲染）  →  ③ 用户选择保存路径  →  ④ 保存文件
//
// **每一步都要出声**：某一步失败但没人说，看起来就是"点了没反应"。
// 状态行按顺序写「正在读歌词 / 正在打包 / 请在弹出的对话框里选位置 / 结果」，
// 而且失败/兜底必须把**去哪儿了**写清楚（连同「打开所在文件夹」）。
//
// 内容永远是 **L0 文档**（`item.document` = 当前轨道上已经编辑好的那份），
// 不会拿着文件名再到磁盘上找原始歌词。`preferred` 只决定对话框从哪个文件夹打开。
let lastExportPath = null;

function showReveal(path) {
  lastExportPath = path || null;
  const button = $('revealLast');
  if (button) button.hidden = !lastExportPath;
}

const formatLabel = () => {
  const select = $('format');
  const option = select && select.options ? select.options[select.selectedIndex] : null;
  return (option && option.textContent) || 'LRC';
};

const exportPreferred = (item) => (item && item.sourceDir) || null;

bind('export', async () => {
  const item = current();
  if (!item) throw Error('请先导入歌词');
  if (!item.document.groups.length) throw Error('这份歌词还没有行，没什么可导出的');

  // ① 读取已编辑的歌词（就是当前这份 L0 文档）
  const document = item.document;
  const rows = document.groups.length;
  const base = item.name.split('/').pop().replace(/\.[^.]+$/, '');

  // ② 打包：告诉用户正在做什么（打包在服务端做，用的是同一个导出器）
  showReveal(null);
  say(`正在读取当前编辑好的歌词…（${rows} 行）→ 正在打包成 ${formatLabel()}…`);
  window.Trace?.step?.('导出歌词', `${rows} 行 → ${formatLabel()}`);

  // ③ 选路径（原生对话框）+ ④ 写盘
  const result = await api('save-as', {
    name: base, document, salt: salt(), duration: item.duration,
    preferred: exportPreferred(item),
    format: $('format').value, encoding: outEncoding()});

  if (result.status === 'cancelled') { say('已取消导出（歌词没有变，可以再来一次）'); return; }
  showReveal(result.path);
  if (result.fallback) {
    // 对话框那边出了问题，但**文件已经写出来了** —— 把原因和落点一起说清楚
    say('已导出（' + result.lines + ' 行）：' + result.fallback);
    return;
  }
  if (result.degraded) {
    // 格式降级了（例如这首歌还没做逐字时间）—— **文件照样出来了**，降级内容随结果一起说出。
    say('已导出到 ' + result.path + '（' + result.lines + ' 行）：' + result.degraded);
    return;
  }
  say(`已导出到 ${result.path}（${result.lines} 行，不含音频；源文件未修改）`
      + ' —— 找不到就点右边的「打开所在文件夹」。');
});

bind('revealLast', async () => {
  if (!lastExportPath) throw Error('还没有导出过文件');
  const result = await api('reveal', {path: lastExportPath});
  // 资源管理器窗口要一两秒才出来；这里只确认"让它打开了"，不再多说
  return result;
});
bind('zip', async () => {
  const entries = [], failed = [];
  for (const item of queue) {
    item.report = await api('validate', {document: item.document, salt: strictFormat(), duration: item.duration});
    if (item.report.errors.length) failed.push(item.name);
    else entries.push({name: item.name, document: item.document, duration: item.duration,
                       format: $('format').value, encoding: outEncoding()});
  }
  repaintQueue(); if (!entries.length) throw Error('没有通过检查的歌词');
  // 也和导出歌词一样走**后端另存为**：页面那层没有下载界面，
  // blob 链接不会落盘。
  const blob = await api('zip', {entries, salt: salt(), format: $('format').value,
                                 encoding: outEncoding()});
  const result = await api('save-as', {
    name: 'SaltLRC 批量导出.zip', suffix: '.zip',
    bytes_base64: SaltAudio.base64(new Uint8Array(await blob.arrayBuffer()))});
  if (result.status === 'cancelled') { say('已取消批量导出'); return; }
  say('已导出 ' + entries.length + ' 首到 ' + result.path
      + (failed.length ? '（跳过 ' + failed.length + ' 首没通过检查的：' + failed.join('、') + '）' : ''));
});
bind('saveTranscript', async () => {
  // 走后端另存为（页面的 blob 下载在打包窗口里点了没反应）。
  const item = current();
  if (!item) throw Error('请先导入歌词');
  if (!item.fit) throw Error('还没有贴唱结果：先点「智能贴唱」');
  const stem = item.name.split('/').pop().replace(/\.[^.]+$/, '');
  const result = await api('save-as', {
    name: stem + '-贴唱结果.json', suffix: '.json',
    text: JSON.stringify({name: item.name, savedAt: new Date().toISOString(),
                          source: 'energy-fit', report: item.fit.report}, null, 2)});
  if (result.status === 'cancelled') { say('已取消保存'); return; }
  say('贴唱结果已保存到 ' + result.path + '（结果不对时可以把它发回来核对）');
});
bind('demo', async () => {
  const text = '[ti:合成节拍演示]\n[00:01.000]今晚[00:02.000]月色很美[00:04.000]\n[00:01.000]今夜は[00:02.000]月が綺麗だね[00:04.000]\n[00:05.000]一起[00:06.000]回家吧[00:08.000]\n[00:05.000]一緒に[00:06.000]帰ろう[00:08.000]\n';
  const result = await api('parse', {bytes: SaltAudio.base64(new TextEncoder().encode(text))});
  // A locally generated tone fixture demonstrates real waveform/energy analysis, not fake ASR.
  const context = new OfflineAudioContext(1, 9 * 16000, 16000), buffer = context.createBuffer(1, 9 * 16000, 16000), data = buffer.getChannelData(0);
  for (let i = 0; i < data.length; i++) { const t = i / 16000; if ((t >= 1.15 && t < 4.1) || (t >= 5.12 && t < 8.1)) data[i] = .2 * Math.sin(t * 2 * Math.PI * 220) * Math.min(1, (t % 1) * 15); }
  const file = new File([await SaltAudio.pcm16(buffer)], '演示.wav', {type: 'audio/wav'});
  audioFiles.set(key('演示.lrc'), file); queue.push({name: '演示.lrc', ...result, history: []}); await select(queue.length - 1);
  say('已载入合成节拍音频。可测试双轨拖动与能量微调；这段音频没有人声，不能用于验证语音识别。');
});
window.addEventListener('beforeunload', e => { if (queue.length) { e.preventDefault(); e.returnValue = ''; } });
function switchPane(name) {
  for (const tab of document.querySelectorAll('.tab')) tab.classList.toggle('active', tab.dataset.pane === name);
  for (const pane of document.querySelectorAll('.pane')) pane.classList.toggle('active', pane.dataset.pane === name);
  // A hidden canvas reports width 0, so the waveform must be repainted on return.
  wave.invalidate();
}
for (const tab of document.querySelectorAll('.tab')) tab.onclick = () => switchPane(tab.dataset.pane);
$('karaoke').onclick = () => { $('karaokeView').hidden = false; preview(); };
$('closeKaraoke').onclick = () => { $('karaokeView').hidden = true; };
// Space toggles playback like every media player; typing fields keep their
// spaces. Every shortcut goes through the user's binding table, so rebinding in
// 设置 cannot silently leave a duplicate or a dead key behind.
//
// ⚠️ 网页里"空格"天生还有第二个含义：**激活当前有焦点的控件**（切换勾选框、
// 按下按钮、打开下拉）—— 点一次「反复」，焦点留在那个勾选框上，之后按空格
// 切换的是它，播放/暂停反而不响应。
//
// 两道一起上，缺一不可：
//   ① 键盘事件里把"正在输入文字"判准 —— checkbox/select 不算，否则空格被
//      让给了浏览器；
//   ② 鼠标点完非输入控件就**把焦点放掉** —— 这一道不依赖"preventDefault 能不能
//      取消浏览器对空格的激活"（各家实现在 keydown/keyup 上不一致），
//      焦点不在控件上，空格就没有第二个含义可执行。

const TEXT_INPUT_TYPES = ['text', 'search', 'password', 'email', 'url', 'tel', 'number',
                          'date', 'time', 'datetime-local', 'month', 'week'];

/** 是不是"正在输入文字"的地方 —— 只有这几种才该把按键让给控件本身。
 *
 * 必须**只**覆盖文本输入：勾选框/单选/滑块/文件/下拉都不是打字的地方。
 * 把它们也算成"输入"，空格就会被浏览器拿去切换那个控件。 */
function isTypingTarget(el) {
  if (!el || !el.tagName) return false;
  if (el.isContentEditable) return true;
  if (el.tagName === 'TEXTAREA') return true;
  if (el.tagName !== 'INPUT') return false;
  const type = String(el.type || 'text').toLowerCase();
  return TEXT_INPUT_TYPES.includes(type);
}

/** 焦点是不是**键盘**给的（Tab 走过来、或程序显式 focus）。
 *
 * 为的是不砸掉无障碍：用 Tab 走到勾选框再按空格，本来就该切换它。
 * 鼠标点过的控件不算（那时 `:focus-visible` 为假）→ 空格回到"播放 / 暂停"。
 * jsdom / 老引擎没有 `:focus-visible` 时按 false 处理 —— 也就是让空格去播放，
 * 那正是我们要的默认行为。 */
function keyboardFocus(el) {
  if (!el || typeof el.matches !== 'function') return false;
  try { return el.matches(':focus-visible'); } catch (error) { return false; }
}

// ② 鼠标点完非输入控件 → 放掉焦点（键盘来的焦点保留）。
//    捕获阶段先跑，早于控件自己的 click 处理，不影响它们的功能。
document.addEventListener('click', (event) => {
  const el = event.target && event.target.closest
    ? event.target.closest('button,select,input,textarea,[contenteditable]') : null;
  if (!el || isTypingTarget(el) || keyboardFocus(el)) return;
  if (typeof el.blur === 'function') el.blur();
}, true);

document.addEventListener('keydown', e => {
  // Arrow keys nudge the selected tick: 10ms, or 1ms with Shift for the last
  // pixel of precision. Only when a tick is selected — otherwise the arrows keep
  // whatever meaning the focused control gives them.
  if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && !e.ctrlKey && !e.altKey && !e.metaKey) {
    if (!isTypingTarget(document.activeElement)
        && nudgeSelected((e.key === 'ArrowLeft' ? -1 : 1) * (e.shiftKey ? 1 : 10))) {
      e.preventDefault();
      return;
    }
  }
  const action = actionFor(e);
  if (action) {
    const el = document.activeElement;
    const typing = isTypingTarget(el);
    if (action === 'help') { e.preventDefault(); $('helpDialog').showModal(); return; }
    // Ctrl+Z inside a field belongs to the field's own text undo.
    if (action === 'undo' || action === 'redo') {
      if (typing) return;
      e.preventDefault();
      if (action === 'redo') redo(); else undo();
      return;
    }
    if (action === 'playPause') {
      if (typing) return;                     // 文本输入里的空格归输入框
      if (keyboardFocus(el)) return;          // Tab 走到控件上：空格归那个控件
      e.preventDefault();
      togglePlay();
      return;
    }
    // Tap mode: while the word-timing editor is open, Enter stamps the play
    // position onto the next unit (K 歌词的核心操作).
    if (action === 'tap') {
      if (!$('wordDialog').open || !$('tapMode').checked) return;
      if (e.target && e.target.tagName === 'BUTTON') return;
      e.preventDefault();
      wordTap();
      return;
    }
    if (action === 'aiTranslate') {
      if (typing) return;
      e.preventDefault();
      aiTranslate('line').catch((error) => say(error.message));
      return;
    }
  }
  if (e.key === 'Escape' && !$('karaokeView').hidden) $('karaokeView').hidden = true;
});
$('perf').onchange = () => debugPanel.toggle($('perf').checked);
window.addEventListener('resize', () => wave.invalidate());

// A song handed over from the library page arrives with its lyric document and
// pulls the audio straight from the Salt Player library over HTTP.
async function acceptHandoff() {
  const raw = sessionStorage.getItem('saltlrc-handoff');
  if (!raw) return;
  sessionStorage.removeItem('saltlrc-handoff');
  let handoff;
  try { handoff = JSON.parse(raw); } catch { return; }
  if (!handoff?.document || Date.now() - (handoff.at || 0) > 600000) return;
  const item = {name: handoff.name, document: normalizeDocument(handoff.document),
                report: {errors: [], warnings: []}, history: [], match: null};
  let loaded = false;
  if (handoff.trackId) {
    try {
      const response = await fetch('/api/audio?track_id=' + encodeURIComponent(handoff.trackId));
      if (response.ok) {
        const blob = await response.blob();
        const name = handoff.audioName || handoff.name.replace(/\.[^.]+$/, '') + '.mp3';
        const file = new File([blob], name, {type: blob.type || 'audio/mpeg'});
        audioFiles.set(key(item.name), file);
        audioFiles.set(key(file.name), file);
        loaded = true;
      }
    } catch (error) { say('音频载入失败：' + error.message); }
  }
  queue.push(item);
  await select(queue.length - 1);
  say(loaded
    ? '已从「联网查找字词」把歌词和音频都带过来了，可以在波形上微调。'
    : '已从「联网查找字词」带歌词过来；音频没能取到，可手动添加同名音频。');
  switchPane('wave');
}
// ---- settings: AI translation, feature switches, key bindings -------------
//
// Settings live on the server (%LOCALAPPDATA%\SaltLRCStudio\settings.json), not
// in localStorage: the API key must never be handed to the page — the server
// translates and the key stays there. The page only ever learns whether a key
// is stored.
let appSettings = null;

const TARGET_LANGUAGES = [['zh', '简体中文'], ['zh-tw', '繁體中文'], ['en', 'English'],
                          ['ja', '日本語'], ['ko', '한국어'], ['fr', 'français'],
                          ['de', 'Deutsch'], ['es', 'español'], ['ru', 'русский'],
                          ['pt', 'português'], ['it', 'italiano'], ['th', 'ไทย']];

const KEY_ACTIONS = [
  ['playPause', '播放 / 暂停'],
  ['tap', '逐字打点（给下一个字）'],
  ['aiTranslate', '翻译当前行'],
  ['undo', '撤销'],
  ['redo', '重做'],
  ['help', '打开帮助'],
];

/**
 * Does a KeyboardEvent satisfy a binding like "Ctrl+Shift+K" or "Space"?
 *
 * Pure, so the binding table can be tested without synthesising real key
 * presses. Modifiers must match EXACTLY: "Ctrl+T" must not fire on
 * Ctrl+Shift+T, or a user who binds both loses the plain one.
 */
function bindingMatches(binding, event) {
  const parts = String(binding || '').split('+').map((p) => p.trim()).filter(Boolean);
  if (!parts.length) return false;
  const key = parts[parts.length - 1];
  const mods = new Set(parts.slice(0, -1).map((p) => p.toLowerCase()));
  const wantCtrl = mods.has('ctrl') || mods.has('cmd') || mods.has('meta');
  // Ctrl and Cmd are the same intent; either satisfies a "Ctrl+…" binding.
  const gotCtrl = !!(event.ctrlKey || event.metaKey);
  if (wantCtrl !== gotCtrl) return false;
  if (mods.has('shift') !== !!event.shiftKey) return false;
  if (mods.has('alt') !== !!event.altKey) return false;
  if (key === 'Space') return event.code === 'Space' || event.key === ' ';
  return String(event.key || '').toLowerCase() === key.toLowerCase();
}

/** The action a key event triggers, or null. */
function actionFor(event) {
  const keys = (appSettings && appSettings.keys) || {};
  for (const [action] of KEY_ACTIONS) {
    if (keys[action] && bindingMatches(keys[action], event)) return action;
  }
  return null;
}

function applySettings(settings) {
  appSettings = settings;
  const features = settings.features || {};
  // The settings dialog is a mirror of controls that already exist in the UI;
  // writing them here means the rest of the app keeps reading one place.
  if ($('follow')) $('follow').checked = features.followPlayback !== false;
  if ($('tapMode')) $('tapMode').checked = features.tapMode !== false;
  if ($('overwrite')) $('overwrite').checked = features.confirmOverwrite !== false;
  if ($('setResume')) $('setResume').checked = features.resumeAfterPause !== false;
  const repeat = features.repeatSegment === true;
  if ($('setRepeat')) $('setRepeat').checked = repeat;
  if ($('repeat')) $('repeat').checked = repeat;
  if ($('perf')) { $('perf').checked = !!features.showPerfPanel; debugPanel.toggle(!!features.showPerfPanel); }
  // 主题：设置是权威，这里把它交给 Theme；Theme 负责写 data-theme 与首屏缓存。
  if ($('setDark')) $('setDark').checked = features.darkMode !== false;
  Theme.apply(features.darkMode === false ? 'light' : 'dark');
  if ($('wordTitle')) $('wordTitle').dataset.autoSplit = features.autoSplitOnOpen ? '1' : '';
}

async function loadSettings() {
  try {
    applySettings(await api('settings', {}));
  } catch (error) { say('读取设置失败：' + error.message); }
}

function fillSettingsForm() {
  const settings = appSettings || {translation: {}, features: {}, keys: {}};
  const t = settings.translation || {}, f = settings.features || {};
  $('setBaseUrl').value = t.base_url || '';
  $('setModel').value = t.model || '';
  $('setApiKey').value = '';
  $('setApiKey').placeholder = t.has_key ? '已保存（留空 = 不修改）' : '粘贴你的 API Key';
  $('setKeyState').textContent = t.has_key ? '已保存一个 Key；这里不会显示它的内容' : '还没有保存 Key';
  $('setInstructions').value = t.instructions || '';
  const target = $('setTarget');
  target.replaceChildren(...TARGET_LANGUAGES.map(([code, name]) => {
    const option = document.createElement('option');
    option.value = code; option.textContent = name;
    if ((t.target || 'zh') === code) option.selected = true;
    return option;
  }));
  $('setFollow').checked = f.followPlayback !== false;
  $('setTapMode').checked = f.tapMode !== false;
  $('setConfirmOverwrite').checked = f.confirmOverwrite !== false;
  $('setResume').checked = f.resumeAfterPause !== false;
  $('setRepeat').checked = f.repeatSegment === true;
  $('setAutoTranslate').checked = !!t.autoTranslateMissing;
  $('setPerf').checked = !!f.showPerfPanel;
  $('setAssoc').checked = f.associateProjectFiles !== false;
  $('setDark').checked = f.darkMode !== false;
  renderKeyGrid();
  $('settingsError').textContent = '';
}

// --------------------------------------------- `.saltproj` 的文件关联（图标/双击）
// 状态的**唯一来源是服务端**：这里只显示它读到的事实（谁占着 .saltproj、图标和
// 命令各自指向哪里），不自己拼一句"应该没问题"。设了没反应的情况下，用户看到的
// 会是一句具体原因，而不是一个沉默的勾选框。
function renderAssocState(state, wanted) {
  const el = $('setAssocState');
  if (!el || !state) return;
  if (!state.supported) {
    el.textContent = state.reason || '当前环境不支持文件关联。';
    return;
  }
  const lines = [];
  // 第一行说"**生效**了没有"，不是"键写了没有"：Windows 的「打开方式」优先级更高，
  // 它可能记着一份指向已删除目录的旧命令，键写对了也不生效。所以三种状态分开说。
  lines.push(state.effective ? '已关联并生效：图标与双击打开都指向本程序。'
            : state.installed ? '关联已经写入，但还没生效（见下面）。'
            : '未关联。');
  lines.push('图标：' + (state.icon || '—'));
  if (state.icon_ok === false) {
    lines.push('⚠ 图标文件不在 exe 旁边（' + (state.icon_file || '') + '），现在退回程序自身的图标。');
  }
  lines.push('双击打开：' + (state.command || '—'));
  for (const problem of (state.problems || [])) lines.push('⚠ ' + problem);
  if (wanted === false && state.owner) lines.push('（设置里已关掉这一项；下次启动不会再关联。）');
  el.textContent = lines.join('\n');
}

async function refreshAssocState() {
  if (!$('setAssocState')) return;
  try { renderAssocState(await api('assoc', {})); }
  catch (error) { $('setAssocState').textContent = '读取关联状态失败：' + error.message; }
}

async function syncProjectFileAssoc() {
  if (!$('setAssoc')) return;
  const wanted = $('setAssoc').checked;
  try {
    renderAssocState(await api(wanted ? 'assoc/apply' : 'assoc/remove', {}), wanted);
  } catch (error) {
    // 设置本身已经存下了；这一步失败必须出声，否则用户只会看到"图标没变"。
    Notify.fail(wanted ? '关联 .saltproj 失败' : '取消关联失败', error.message, [
      '设置已经保存，可以再点一次「保存」重试。',
      '写的是当前用户的注册表（HKCU），一般不需要管理员权限。',
      '取消勾选这一项并保存，可以随时撤销。',
    ]);
    refreshAssocState();
  }
}

function renderKeyGrid() {
  const keys = (appSettings && appSettings.keys) || {};
  const grid = $('keyGrid');
  grid.replaceChildren();
  for (const [action, label] of KEY_ACTIONS) {
    const wrap = document.createElement('label');
    wrap.append(document.createTextNode(label));
    const input = document.createElement('input');
    input.className = 'keycap';
    input.readOnly = true;
    input.value = keys[action] || '';
    input.placeholder = '未绑定';
    input.dataset.action = action;
    // Capture the next combination instead of typing it: a text field would
    // swallow Ctrl+Z and neither undo nor bind.
    input.onkeydown = (event) => {
      event.preventDefault();
      if (event.key === 'Escape') { input.blur(); return; }
      if (event.key === 'Backspace' || event.key === 'Delete') { input.value = ''; return; }
      if (['Control', 'Shift', 'Alt', 'Meta'].includes(event.key)) return;
      const parts = [];
      if (event.ctrlKey || event.metaKey) parts.push('Ctrl');
      if (event.shiftKey) parts.push('Shift');
      if (event.altKey) parts.push('Alt');
      parts.push(event.code === 'Space' ? 'Space' : event.key.length === 1 ? event.key.toUpperCase() : event.key);
      input.value = parts.join('+');
    };
    wrap.append(input);
    grid.append(wrap);
  }
}

function collectSettings() {
  const keys = {};
  for (const input of $('keyGrid').querySelectorAll('input.keycap')) {
    keys[input.dataset.action] = input.value.trim();
  }
  const payload = {
    translation: {
      base_url: $('setBaseUrl').value.trim(),
      model: $('setModel').value.trim(),
      target: $('setTarget').value,
      instructions: $('setInstructions').value,
      autoTranslateMissing: $('setAutoTranslate').checked,
    },
    features: {
      followPlayback: $('setFollow').checked,
      tapMode: $('setTapMode').checked,
      confirmOverwrite: $('setConfirmOverwrite').checked,
      showPerfPanel: $('setPerf').checked,
      resumeAfterPause: $('setResume').checked,
      repeatSegment: $('setRepeat').checked,
      associateProjectFiles: $('setAssoc').checked,
      darkMode: $('setDark').checked,
    },
    keys,
  };
  // Only send the key when the user actually typed one: an empty field means
  // "keep what is stored", which is also what the server does with "".
  if ($('setApiKey').value.trim()) payload.translation.api_key = $('setApiKey').value.trim();
  return payload;
}

async function saveSettings(options) {
  const quiet = !!(options && options.quiet);
  const before = (appSettings && appSettings.features) || {};
  const wantedAssoc = $('setAssoc').checked;
  $('settingsError').textContent = '';
  try {
    applySettings(await api('settings/save', collectSettings()));
    fillSettingsForm();
    // 只有「关联」这一项要落到注册表。别的开关跟着写一遍注册表既没必要，
    // 也让"改一下跟随播放"顺手动了文件关联。
    if (wantedAssoc !== (before.associateProjectFiles !== false)) await syncProjectFileAssoc();
    if (quiet) flashSaved(); else say('设置已保存（只存在本机）。');
  } catch (error) { $('settingsError').textContent = error.message; }
}

// 静默保存时用对话框底部的状态字说话；失败仍然走 settingsError 那一条（红色、看得见）。
let savedTimer = 0;
function flashSaved() {
  const box = $('settingsSaved');
  if (!box) return;
  box.textContent = '已保存';
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => { box.textContent = ''; }, 1500);
}

async function testTranslation() {
  $('settingsError').textContent = '';
  $('setKeyState').textContent = '正在测试…';
  try {
    // Save first, then probe: the test must exercise the settings that are
    // actually stored, not the ones still sitting in the form.
    await saveSettings();
    const result = await api('translate', {probe: true});
    if (result.error) {
      $('setKeyState').textContent = '';
      $('settingsError').textContent = result.error;
      return;
    }
    $('setKeyState').textContent = `连接正常（试译结果：${result.sample || ''}）`;
  } catch (error) {
    $('setKeyState').textContent = '';
    $('settingsError').textContent = error.message;
  }
}

function currentLineIndex() {
  const item = current();
  if (!item) return -1;
  if (selected >= 0 && wordIndex >= 0) return wordIndex;
  return selected;
}

async function aiTranslate(scope) {
  const item = current();
  if (!item) throw Error('先从曲库导入或载入一首歌');
  if (!item.document.groups.length) throw Error('这份歌词还没有可翻译的行');
  const index = scope === 'line' ? currentLineIndex() : null;
  if (scope === 'line' && !(index >= 0)) throw Error('先选中要翻译的那一行（点表格里的行，或点波形上的贴条）');
  const buttons = [$('aiTranslate'), $('aiTranslateAll')];
  buttons.forEach((b) => { if (b) b.disabled = true; });
  try {
    const result = await api('translate', {
      document: item.document, scope, index,
      only_missing: scope === 'all',
    });
    if (result.error) throw Error(result.error + (result.needs_setup ? '' : ''));
    const doc = normalizeDocument(result.document);
    validateEdit(doc, item.duration);
    remember(item, scope === 'all' ? 'AI 翻译全曲' : 'AI 翻译本行', doc);
    refresh();
    const report = result.report || {};
    say(`AI 翻译完成：${report.translated}/${report.total} 行` +
        ((report.notes || []).length ? '；' + report.notes.join('；') : '') +
        '。译文可继续手工修改。');
  } finally {
    buttons.forEach((b) => { if (b) b.disabled = false; });
  }
}
bind('settings', () => { fillSettingsForm(); refreshAssocState(); $('settingsDialog').showModal(); });
bind('settingsClose', () => $('settingsDialog').close());
// 设置项一改就存，没有「保存」按钮。
//
// 委托一个 change 监听而不是逐个绑定：勾选框与下拉在 change 时立即生效，
// 文字框在离开输入框或按回车时才触发 change —— 每敲一个字就发一次请求既没必要，
// 也会和正在输入的其它字符抢先后。
$('settingsDialog').addEventListener('change', (event) => {
  const target = event.target;
  if (!target || !target.closest('#settingsDialog')) return;
  saveSettings({quiet: true});
});
bind('setTest', () => testTranslation());
bind('setClearKey', async () => {
  if (!confirm('清除已保存的 API Key？清除后 AI 翻译会不可用，直到重新填入。')) return;
  try {
    await api('settings/save', {translation: {api_key: '\u0000clear'}});
    await loadSettings();
    fillSettingsForm();
    say('已清除保存的 API Key。');
  } catch (error) { $('settingsError').textContent = error.message; }
});
bind('aiTranslate', () => aiTranslate('line').catch((e) => say(e.message)));
bind('aiTranslateAll', () => {
  const item = current();
  const missing = item ? item.document.groups.filter((g) => !g.translation.text.trim()).length : 0;
  if (!confirm(`翻译全曲中还没有翻译的 ${missing} 行？会调用 ${appSettings?.translation?.model || '你配置的模型'}。`)) return;
  aiTranslate('all').catch((e) => say(e.message));
});

// ---- word-tick readout and nudging ----------------------------------------
//
// Hovering names the syllable and its time, dragging shows the live value and
// the delta, and the tick you clicked can be nudged with the arrow keys.

function showWaveTip(info) {
  const tip = $('waveTip');
  if (!tip) return;
  if (!info) { tip.hidden = true; return; }
  // 内容由 common.js 的 waveTipHtml 一处决定（歌词文本在里面转义）。
  tip.innerHTML = waveTipHtml(info);
  // Keep the tip inside the frame: near the right edge it would otherwise hang
  // off the canvas where it cannot be read.
  tip.hidden = false;
  const frame = tip.parentElement.getBoundingClientRect();
  const box = tip.getBoundingClientRect();
  const left = Math.max(4, Math.min(info.x + 14, frame.width - box.width - 6));
  const top = Math.max(4, Math.min(info.y - box.height - 10, frame.height - box.height - 6));
  tip.style.left = left + 'px';
  tip.style.top = top + 'px';
}

function hideWaveTip() {
  const tip = $('waveTip');
  if (tip) tip.hidden = true;
}

/** Move the selected tick by ``step`` ms, through the same path as a drag. */
function nudgeSelected(step) {
  const mark = wave.selected;
  if (!mark || mark.kind === 'block') return false;
  const group = current()?.document.groups[mark.index];
  const track = group?.[mark.field];
  if (!track) return false;
  const base = mark.kind === 'word' ? track.words[mark.word]?.start
             : mark.kind === 'start' ? trackStart(group, mark.field) : track.end;
  if (base == null) return false;
  // changeTime routes through Doc.moveWordStart, so the boundaries the drag path
  // already respects (never past the previous word, carrying the line end) hold
  // here too — a nudge cannot create a document the validator would reject.
  changeTime(mark.index, mark.field, mark.kind, base + step, mark.word);
  const info = wave.pointerInfo(mark.x ?? 0, 0);
  showWaveTip(info ? {...info, delta: step, time: base + step} : null);
  return true;
}

// The backend runs headless, so the footer is where you stop it.
$('exitApp').onclick = async (event) => {
  event.preventDefault();
  // 两次确认（保留 / 删除）是**同步**的：用户在同一个事件里就该看到反应，
  // 而且下面的 forgetSession() 必须在任何 await 之前跑完 ——
  // jsdom 的回归检查在 click() 之后立刻读 localStorage。
  const decision = Project.decideExit();
  if (!decision) return;
  // Clear the storage, not the visible queue: if the exit request fails the user
  // still has the list in front of them, and the next launch is clean either way.
  forgetSession();
  // 工程要先落盘、再按用户的选择保留或删除 —— 必须等它做完，因为后端一退出
  // 这些接口就没人接了。
  await Project.finishExit(decision);
  fetch('/api/exit', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})
    .finally(() => { document.title = '已退出'; say('后端已退出，本页不再有响应。'); });
};

// A saved session is OFFERED, never auto-loaded: the queue is per-session, and
// loading it behind the user's back would let an old round bleed into a new one
// (see the auto-save block above). Recovery after an abnormal close is one
// click away; a normal quit clears it entirely.
savedSession = readSession();
refreshQueueHint();
loadSettings();
if (hasSavedSession()) {
  say(`检测到上次未正常结束的编辑会话（${savedSession.length} 首）。`
      + '要接着改就点左侧「恢复上次会话」；这次直接载入新内容也可以，'
      + '只是一旦开始编辑，上次的会话会被覆盖。');
}
acceptHandoff().catch((error) => say('曲库交接失败：' + error.message));
// 工程库：读保存路径（没有就问一次）、把已经导入的内容补建工程。
Project.start().catch((error) => say('工程库初始化失败：' + error.message));
// 双击 .saltproj（文件关联）或命令行 `--open` 时，把启动时带的那个工程放进队列。
// 排在交接之后：两条都是"往队列里加东西"，先来后到更接近用户以为的顺序。
openPendingProject().catch((error) => say('打开启动时的工程失败：' + error.message));

fetch('/api/capabilities').then(r => r.json()).then((data) => {
  const badge = document.getElementById('appVersion');
  if (badge && data.version) badge.textContent = 'v' + data.version;
}).catch(() => {});

capabilities().then(fillMatchProviders).catch(e => say(e.message));


