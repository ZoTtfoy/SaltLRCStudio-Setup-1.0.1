"use strict";
/**
 * Layer 0 of the editor — the mirror of saltlrc/document.py + saltlrc/edits.py.
 *
 * The server is the authority for structural edits (``/api/edit``), but every
 * keystroke-sized edit has to feel instant, so the browser applies the same
 * rules itself. Same function names, same constants, same invariants, and both
 * sides are asserted against the same fixture (tests/test_edits.py and
 * tests/web_document_check.js) — that is what stops the two copies drifting.
 *
 * One rule above all: never invent a timestamp. If a line has nowhere to go,
 * throw with a message that says what to do instead.
 */
const Doc = (() => {
  const TRACKS = ['main', 'translation'];
  const DEFAULT_GAP = 1000;   // appended line: one second after the last one
  const MIN_SLACK = 1;        // two line starts must differ by at least this
  const LINE_FALLBACK = 2500; // a line without an explicit end, for display

  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const fail = (message) => { throw Error(message); };

  // A track carries its own start only when it diverges from the line start.
  const trackStart = (group, field = 'main') => group[field].start ?? group.start;

  const shiftTrack = (track, old, delta) => {
    track.start = old + delta;
    for (const word of track.words) word.start += delta;
    if (track.end != null) track.end += delta;
  };

  // Every timestamp of one track, in order: start, each word, the end.
  const ticks = (group, field = 'main') => {
    const track = group[field], start = trackStart(group, field);
    return [start, ...track.words.slice(1).map(w => w.start), ...(track.end == null ? [] : [track.end])];
  };

  const lineEnd = (doc, index, fallback = LINE_FALLBACK) => {
    const group = doc.groups[index];
    if (group.main.end != null) return group.main.end;
    const next = doc.groups[index + 1];
    return next ? next.start : group.start + fallback;
  };

  // Same invariants as saltlrc.document.check, with the short wording the UI
  // shows: the message lands next to the row the user just touched.
  function validateEdit(doc, duration) {
    for (const [i, group] of doc.groups.entries()) {
      if (group.start < 0 || (i > 0 && group.start <= doc.groups[i - 1].start)) fail('主轨句首必须非负、唯一且递增');
      for (const field of TRACKS) {
        const track = group[field], start = trackStart(group, field);
        const sequence = ticks(group, field);
        sequence.forEach((t, j) => {
          if (!Number.isFinite(t) || t < 0 || (duration && t > duration) || (j && t <= sequence[j - 1])) {
            fail('轨道时间必须递增且在音频范围内');
          }
        });
        if (track.words.length && track.words[0].start !== start) fail('首个逐字时间必须等于轨道开始时间');
      }
    }
    return doc;
  }

  const newGroup = (start, text = '', translation = '') =>
    ({start, main: {text, words: [], end: null, start: null},
      translation: {text: translation, words: [], end: null, start: null}});

  /**
   * The legal range of one tick, in ms — the single rule for every timeline edit.
   *
   * A tick may not cross the tick before it or the one after it: 句首 before the
   * previous line, 逐字 before its neighbour, 句尾 after the last word. Every
   * editing path asks this one function. A path that decides for itself would
   * assign a value and let validateEdit throw the whole edit away, leaving the
   * user with a refusal about a constraint they cannot see — "the handle will
   * not move".
   *
   * ``low`` and ``high`` are inclusive.
   */
  const tickRange = (doc, index, field, kind, word, duration = null) => {
    const group = doc.groups[index];
    const track = group[field];
    const list = ticks(group, field);
    const at = kind === 'start' ? 0 : kind === 'end' ? list.length - 1 : Math.max(0, word ?? 0);
    const ceiling = duration == null ? Number.MAX_SAFE_INTEGER : Math.round(duration);
    let low;
    let high;
    if (at === 0) {
      // 句首 answers to the neighbouring LINES, not to ticks inside this line.
      const prev = doc.groups[index - 1];
      const next = doc.groups[index + 1];
      low = prev ? prev.start + 1 : 0;
      high = next ? next.start - 1 : Math.max(0, ceiling - 1);
    } else {
      low = list[at - 1] + 1;
      high = at + 1 < list.length ? list[at + 1] - 1 : ceiling;
    }
    return {at, list, low: Math.max(0, low), high: Math.max(0, high)};
  };

  /**
   * Move the line end (句尾).
   *
   * The end is a tick like any other: it must stay after the last word. Assigning
   * it directly and letting validateEdit judge meant that dragging it earlier than
   * the last word discarded the whole edit and the handle did not move at all —
   * "整句歌词时间轴都没办法改".
   *
   * With ``ripple`` (Alt) the words that fall past the new end come along, keeping
   * their spacing: that is the gesture for "this line should finish earlier", and
   * without it shortening is blocked by the last syllable sitting right there.
   */
  const moveLineEnd = (track, time, options = {}) => {
    const {duration = null, ripple = false} = options;
    const ceiling = duration == null ? Number.MAX_SAFE_INTEGER : Math.round(duration);
    const words = track.words || [];
    const raw = Math.max(1, Math.min(ceiling, Math.round(time)));
    const last = words.length ? words[words.length - 1] : null;
    const carried = [];
    let end = raw;
    let note = '';
    if (last && end <= last.start) {
      if (ripple) {
        const first = words.findIndex((unit) => unit.start >= raw);
        // Never carry the FIRST word: its start IS the line start (the validator
        // insists), so shifting it would silently move the line — that is the 句首
        // handle's job, and doing it here produced an invalid document.
        const start0 = Math.max(1, first < 0 ? words.length - 1 : first);
        const followers = words.slice(start0);
        const before = words[start0 - 1].start;
        // Shift them so the LAST syllable still ends before the new end (strictly:
        // landing exactly on it would make two ticks equal, which the validator
        // rejects), and so the first carried word never crosses the one in front.
        let delta = Math.min(raw - 1 - last.start, 0);
        delta = Math.max(delta, before + 1 - followers[0].start);
        for (const unit of followers) carried.push({word: unit, start: unit.start + delta});
        end = Math.max(raw, last.start + delta + 1);
        note = end > raw
          ? `不能再短了：后面的字已经顶住前面的字（句尾落在 ${(end / 1000).toFixed(3)} 秒）`
          : `句尾缩到 ${(end / 1000).toFixed(3)} 秒，后面的 ${followers.length} 个字一起前移`;
      } else {
        end = last.start + 1;
        note = `句尾必须在最后一个字之后（最后一个字在 ${(last.start / 1000).toFixed(3)} 秒）；`
             + '要缩短整句，按住 Alt 一起把后面的字带过来';
      }
    }
    return {end, carried, note};
  };

  /**
   * Move one word tick and report what had to give, so the track stays legal.
   *
   * The old behaviour assigned the time and let ``validateEdit`` judge the
   * result. Near the tail that is a dead end: ``ticks()`` puts the line end last
   * and the sequence must be strictly increasing, so the moment the last word is
   * dragged past the line end the WHOLE edit is rejected — with "轨道时间必须递增
   * 且在音频范围内", which never mentions the line end. The user is left unable to
   * move the word, which is exactly "卡在尾部".
   *
   * So the boundaries are handled here instead:
   *
   * * a word never passes the word before it (clamped, and said so);
   * * the last word **carries the line end along**, keeping the tail gap it had —
   *   a line cannot end before its last word, so moving that word later has to
   *   move the end too, and that is what dragging it means;
   * * nothing may pass the end of the audio (that is what caps the move).
   *
   * Returns ``{start, end, note}``; ``end`` is null when the track has no
   * explicit end (then there is nothing to carry).
   */
  const moveWordStart = (track, word, time, options = {}) => {
    const {duration = null, lineStart = 0} = options;
    const ceiling = duration == null ? Number.MAX_SAFE_INTEGER : Math.round(duration) - 1;
    const last = word === track.words.length - 1;
    // The first word IS the line start (validateEdit insists on it), so its floor
    // is the line start; every other word's floor is its predecessor.
    const floor = word > 0 ? track.words[word - 1].start : Math.round(lineStart);
    // ...and its CEILING is the next word. Clamping against the neighbour in both
    // directions keeps a drag from being rejected outright: timings must
    // increase, so without the clamp a word dragged past the next one would make
    // the whole edit invalid and validateEdit would throw it away, and the word
    // would simply not move. The clamp keeps the word on the legal side of its
    // neighbour and reports the stop in ``note``, so the drag always has a
    // visible result.
    const next = track.words[word + 1];
    const neighbour = next ? Math.round(next.start) - 1 : null;
    const carries = last && track.end != null;
    const gap = carries ? Math.max(0, track.end - track.words[word].start) : 0;
    // Carrying the end means the end also has to fit inside the audio, so the gap
    // — not the audio — is what caps a move of the last word.
    const limit = Math.min(carries ? ceiling - gap : ceiling,
                           neighbour == null ? Number.MAX_SAFE_INTEGER : neighbour);
    const raw = Math.round(time);
    const start = Math.max(floor + 1, Math.min(limit, raw));
    const end = carries ? Math.max(track.end, Math.min(ceiling, start + gap)) : track.end;
    let note = '';
    if (start !== raw) {
      note = raw <= floor ? '已停在前一个字之后：逐字时间必须递增'
           : raw > limit && neighbour != null
             ? '已停在下一个字之前：要拉开间距，请拖动后面的字（或按住 Alt 一起推）'
             : '已到音频末尾，不能再往后移';
    }
    if (carries && end !== track.end) {
      note = '句尾已随之延后到 ' + (end / 1000).toFixed(3) + ' 秒';
    }
    return {start, end, note};
  };

  /**
   * Move a word and carry every word after it, keeping their gaps.
   *
   * This is the gesture for "spread these apart": with monotonic timings a word
   * can never pass its neighbour, so pushing every following word in one drag is
   * the only way to open a gap. Opt-in (Alt while dragging) rather than
   * automatic: silently changing times the user already set is worse than the
   * inconvenience it saves.
   *
   * Returns ``{starts, end, note}`` — the new start for the dragged word and all
   * of its followers, so the caller can apply the whole shift or none of it.
   */
  const rippleWordStart = (track, word, time, options = {}) => {
    const {duration = null, lineStart = 0} = options;
    const ceiling = duration == null ? Number.MAX_SAFE_INTEGER : Math.round(duration) - 1;
    const floor = word > 0 ? track.words[word - 1].start : Math.round(lineStart);
    const followers = track.words.slice(word);
    const last = followers[followers.length - 1];
    const tail = track.end == null ? 0 : Math.max(0, track.end - last.start);
    const raw = Math.round(time);
    // Two caps, and the tighter one wins: the shift may not overtake the word in
    // front, and the shifted tail (plus the line end it carries) must still fit
    // inside the audio.
    const headroom = ceiling - tail - last.start;
    const delta = Math.max(floor + 1 - track.words[word].start, Math.min(headroom, raw - track.words[word].start));
    const starts = followers.map((item) => item.start + delta);
    let end = track.end;
    if (end != null) end = Math.min(ceiling, end + delta);
    let note = delta === raw - track.words[word].start ? ''
             : raw - track.words[word].start < 0 ? '已停在前一个字之后：逐字时间必须递增'
             : '已到音频末尾：后面的字不能再往后推';
    if (note) note = '（连同后面 ' + (followers.length - 1) + ' 个字一起移动）' + note;
    return {starts, end, note, delta};
  };

  /**
   * Make a typed line end legal against the word times it must follow.
   *
   * The format requires the line to end after its last word — the exporter and
   * ``validateEdit`` both enforce it — so an end that is too early cannot be
   * stored at all. Raising it keeps the word times the user just spent effort on,
   * which is what they were editing; refusing would strand them at the same
   * boundary the drag clamps to.
   */
  const fitLineEnd = (end, words, duration = null) => {
    const ceiling = duration == null ? Number.MAX_SAFE_INTEGER : Math.round(duration);
    const starts = (words || []).map(w => w.start).filter(v => Number.isFinite(v));
    const floor = starts.length ? Math.max(...starts) + 1 : 1;
    const wanted = Math.round(end);
    const value = Math.max(floor, Math.min(ceiling, wanted));
    return {end: value,
            note: value === wanted ? '' : '句尾必须晚于最后一个字，已改为 ' + (value / 1000).toFixed(3) + ' 秒'};
  };

  /**
   * Insert a line at ``index`` (that line moves down). Between two lines the
   * start is the midpoint of the gap; at the end it is ``gap`` after the last
   * line, clamped into the audio. Mirrors edits.insert_group exactly.
   */
  // 这一行**写明**唱到哪里：两条轨道里明确写的最大句尾；都没写就是 null。
  // 口径与校验器一致（"句尾与下一组重叠"是按轨道各判一次），所以取更晚的那个。
  function sungEnd(group) {
    const ends = [group.main && group.main.end, group.translation && group.translation.end]
      .filter((value) => value != null);
    return ends.length ? Math.max.apply(null, ends) : null;
  }

  function insertGroup(doc, index, options = {}) {
    const {duration = null, gap = DEFAULT_GAP, text = '', translation = ''} = options;
    const groups = doc.groups;
    const at = clamp(Math.trunc(index) || 0, 0, groups.length);
    const previous = at > 0 ? groups[at - 1] : null;
    const following = at < groups.length ? groups[at] : null;
    let start = options.start == null ? null : Math.trunc(options.start);
    if (start == null) {
      if (following) {
        // 新行的起点不许落在上一行**唱过的那一段**里：上一行写明了句尾就以句尾为准。
        const previousEnd = previous ? sungEnd(previous) : null;
        let low = previous ? previous.start + MIN_SLACK : 0;
        if (previousEnd != null) low = Math.max(low, previousEnd + MIN_SLACK);
        const high = following.start - MIN_SLACK;
        if (high < low) {
          if (previousEnd == null) {
            fail(`第 ${at} 行与下一行只差 ${Math.max(0, following.start - (previous ? previous.start : 0))} 毫秒，中间插不进新行；请先把两行时间拉开`);
          }
          fail(`上一行唱到 ${previousEnd} 毫秒，下一行 ${following.start} 毫秒才开始，中间插不进新行；请先把上一行的句尾往前收，或把下一行往后挪`);
        }
        const guess = previous ? Math.floor((previous.start + following.start) / 2) : following.start - gap;
        start = clamp(guess, low, high);
      } else if (previous) {
        const base = sungEnd(previous) != null ? sungEnd(previous) : previous.start;
        start = base + Math.max(MIN_SLACK, Math.trunc(gap));
        if (duration != null && start > duration) {
          start = duration - 1;
          if (start <= base) fail('上一行唱到音频末尾，后面插不进新行；请先把上一行的句尾往前收，或先确认音频时长');
        }
      } else {
        start = 0;
      }
    }
    if (duration != null && start > duration) fail('新行的时间超出了音频时长');
    groups.splice(at, 0, newGroup(start, text, translation));
    return {document: doc, index: at};
  }

  const deleteGroup = (doc, index) => {
    if (!(index >= 0 && index < doc.groups.length)) fail('要删除的行不存在');
    doc.groups.splice(index, 1);
    return doc;
  };

  const sortGroups = (doc, note = '句首时间倒序，已按时间重新排序') => {
    // Stable by construction: Array.prototype.sort is stable in every engine
    // we ship to, so lines sharing a start keep their original order.
    if (doc.groups.some((g, i) => i && g.start < doc.groups[i - 1].start)) {
      doc.groups.sort((a, b) => a.start - b.start);
      doc.notes.push(note);
    }
    return doc;
  };

  /**
   * Rewrite a line's text while keeping its timing when the shape allows it.
   * Units are not single characters any more (smart tokenization groups a whole
   * English word or a kanji+okurigana run), so each unit keeps its *length*
   * instead of blindly taking one character — otherwise the concatenation would
   * stop matching the text and the export would be rejected.
   */
  function retargetWords(track, text) {
    if (!track.words.length) return false;
    if (text.length !== track.text.length) { track.words = []; track.end = null; return false; }
    let at = 0;
    track.words.forEach((word, i) => {
      const size = i === track.words.length - 1 ? text.length - at : Math.min(word.text.length, text.length - at);
      word.text = text.slice(at, at + size);
      at += size;
    });
    return true;
  }

  /**
   * Project file (.saltproj): the whole editing session in one owned format —
   * document, which song it belongs to, and (optionally) the audio itself so
   * the waveform comes back without re-adding the file. Plain JSON with a
   * ``kind`` marker so a wrong file fails with a readable message instead of
   * producing garbage.
   */
  const PROJECT_KIND = 'saltlrc-project';
  const PROJECT_VERSION = 1;

  // Chunked btoa: String.fromCharCode(...hugeArray) blows the arg limit and
  // the stack on multi-MB audio.
  const bytesToBase64 = bytes => {
    let bin = '';
    for (let i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode(...bytes.subarray(i, i + 8192));
    return btoa(bin);
  };
  const base64ToBytes = text => {
    const bin = atob(text), out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  };

  // ---- 工程文件：两种形态，读的时候**都认** --------------------------------
  //
  // ① 纯 JSON（.saltproj）：没有音频时就用它 —— 人能看懂，工具也能读。
  // ② 容器：`"SaltLRC1"` + 4 字节小端 JSON 长度 + JSON + **音频原始字节**。
  //
  // 容器（内嵌音频用）：勾了「工程内嵌音频」时，音频不走 base64 塞进 JSON ——
  // 30MB 的歌要编码成 40MB 文本，浏览器侧编码一次约 116ms / 峰值 80MB，
  // 服务端还要把 40MB 再解析回对象（58ms / 41MB），写盘再翻一倍。
  // 一次「保存工程…」≈ 273ms 与 ~250MB 临时内存。
  // 容器把音频原样放在后面：没有 base64、没有第二次解析，内存里也基本只有一份。
  //
  // **读取必须两种都认**：老工程文件是 JSON（可能带 base64 音频），用户手上就有。
  const CONTAINER_MAGIC = 'SaltLRC1';
  const CONTAINER_HEAD = 12;               // 8 字节魔数 + 4 字节小端长度
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  /** 组成容器：把 project（不含音频数据）+ 音频原始字节拼成**一个字节数组**。
   *
   * 返回 `Uint8Array` 而不是 `Blob`：页面自己包一层 `new Blob([...])` 就行，
   * 而"能不能在不开浏览器的地方验它"就变成了可能（jsdom 里没有可靠的 Blob.arrayBuffer）。
   * 一处实现、一处被测 —— 而不是两个版本各写一遍。
   */
  function packContainer(project, audioBytes) {
    const json = encoder.encode(JSON.stringify(project));
    const audio = (audioBytes && audioBytes.byteLength) ? audioBytes : new Uint8Array(0);
    const out = new Uint8Array(CONTAINER_HEAD + json.length + audio.byteLength);
    out.set(encoder.encode(CONTAINER_MAGIC), 0);
    new DataView(out.buffer).setUint32(8, json.length, true);
    out.set(json, CONTAINER_HEAD);
    out.set(audio, CONTAINER_HEAD + json.length);
    return out;
  }

  /** 是容器就拆开（`{project, audioBytes}`），不是就返回 null。 */
  function unpackContainer(bytes) {
    if (!bytes || bytes.length < CONTAINER_HEAD) return null;
    if (decoder.decode(bytes.subarray(0, CONTAINER_MAGIC.length)) !== CONTAINER_MAGIC) return null;
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const size = view.getUint32(8, true);
    if (CONTAINER_HEAD + size > bytes.length) {
      throw Error('工程文件损坏：头部写着 ' + size + ' 字节的说明，文件里装不下');
    }
    const project = JSON.parse(decoder.decode(bytes.subarray(CONTAINER_HEAD, CONTAINER_HEAD + size)));
    return {project, audioBytes: bytes.subarray(CONTAINER_HEAD + size)};
  }

  function packProject(item, audio) {
    return {kind: PROJECT_KIND, version: PROJECT_VERSION, app: 'SaltLRC Studio',
            savedAt: new Date().toISOString(), name: item.name,
            duration: item.duration ?? null, document: item.document,
            audio: audio ? {name: audio.name, type: audio.type || 'audio/mpeg',
                            size: audio.bytes.length,
                            // 容器形态下**不写 data**：音频在文件尾巴里。
                            data: audio.raw ? null : bytesToBase64(audio.bytes)} : null};
  }

  /**
   * 读回一个工程（两种形态都认）。
   *
   * @param {Uint8Array} bytes 整个文件的字节
   */
  function readProject(bytes) {
    const box = unpackContainer(bytes);
    if (box) {
      if (box.project.audio && box.audioBytes.length) {
        box.project.audio.data = null;       // 数据来自文件尾巴，不走 base64
      }
      return {project: box.project, audioBytes: box.audioBytes.length ? box.audioBytes : null};
    }
    return {project: JSON.parse(new TextDecoder().decode(bytes)), audioBytes: null};
  }

  function unpackProject(data, audioBytes) {
    if (!data || data.kind !== PROJECT_KIND || !data.document) {
      throw Error('不是 SaltLRC 工程文件（.saltproj）；请用「打开工程」选择工程文件');
    }
    let audio = null;
    if (audioBytes && audioBytes.length) {
      audio = {name: (data.audio && data.audio.name) || 'audio',
               type: (data.audio && data.audio.type) || 'audio/mpeg', bytes: audioBytes};
    } else if (data.audio && data.audio.data) {
      audio = {name: data.audio.name || 'audio', type: data.audio.type || 'audio/mpeg',
               bytes: base64ToBytes(data.audio.data)};
    }
    return {name: data.name || '未命名.lrc', document: data.document,
            duration: Number.isFinite(data.duration) ? data.duration : null, audio};
  }

  return {TRACKS, DEFAULT_GAP, MIN_SLACK, LINE_FALLBACK, PROJECT_KIND, PROJECT_VERSION, clamp,
          trackStart, shiftTrack, ticks, lineEnd, validateEdit,
          moveWordStart, rippleWordStart, moveLineEnd, tickRange, fitLineEnd,
          newGroup, insertGroup, deleteGroup, sortGroups, retargetWords,
          bytesToBase64, base64ToBytes, packProject, unpackProject,
          packContainer, unpackContainer, readProject, CONTAINER_MAGIC};
})();
