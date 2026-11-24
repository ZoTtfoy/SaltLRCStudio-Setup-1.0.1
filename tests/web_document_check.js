// Layer 0 mirror — web/document.js must behave exactly like saltlrc/edits.py.
// The fixtures here are deliberately identical to tests/test_edits.py: if one
// side changes a rule, the other side fails instead of silently drifting.
// Usage: node tests/web_load_harness.js saltlrc/web/document.js --run=tests/web_document_check.js
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
const starts = (doc) => doc.groups.map(g => g.start);

// [1000, 3000, 9000] — the same three-line fixture the Python suite uses.
const fixture = (startsIn = [1000, 3000, 9000]) =>
  ({groups: startsIn.map(s => Doc.newGroup(s)), metadata: {}, notes: [], unresolved: [], alignment: {}});

globalThis.__check = async () => {
  // 1) between two lines: the midpoint of the gap
  let doc = fixture();
  let out = Doc.insertGroup(doc, 1);
  check('中间插入落在第 1 行位置', out.index, 1);
  check('中间插入取两行中点', starts(doc), [1000, 2000, 3000, 9000]);
  check('新行是空行', doc.groups[1].main, {text: '', words: [], end: null, start: null});

  // 2) before the first line
  Doc.insertGroup(fixture(), 0);
  check('插到最前面', starts(fixture([1000, 3000, 9000])), [1000, 3000, 9000]); // sanity: fixture untouched
  let head = fixture();
  Doc.insertGroup(head, 0);
  check('插到最前面：句首为 0', starts(head)[0], 0);

  // 3) at the end: one second after the last line, inside the audio
  let tail = fixture();
  Doc.insertGroup(tail, 3, {duration: 12000});
  check('末尾插入 +1000ms', starts(tail), [1000, 3000, 9000, 10000]);

  // 4) at the end but the audio ends first: clamp, never past the audio
  let clamped = fixture();
  Doc.insertGroup(clamped, 3, {duration: 9500});
  check('末尾插入被音频时长夹住', starts(clamped), [1000, 3000, 9000, 9499]);

  // 5) no room: refuse instead of inventing an overlapping line
  let tight = fixture([1000, 1001]);
  let message = '';
  try { Doc.insertGroup(tight, 1); } catch (error) { message = error.message; }
  check('两行挨太近时拒绝插入', /中间插不进新行/.test(message), true);

  // 6) deleting keeps the rest in order
  let removed = fixture();
  Doc.deleteGroup(removed, 1);
  check('删除第 2 行', starts(removed), [1000, 9000]);

  // 7) sorting is stable and leaves a note
  let messy = fixture([3000, 1000, 9000]);
  Doc.sortGroups(messy);
  check('按时间重排', starts(messy), [1000, 3000, 9000]);
  check('重排留下说明', messy.notes.length, 1);
  Doc.sortGroups(messy);
  check('已排好时不再加说明', messy.notes.length, 1);

  // 8) retarget keeps each unit's length, not one character per unit
  const track = {text: 'The night', words: [{start: 0, text: 'The'}, {start: 100, text: ' '}, {start: 200, text: 'night'}], end: 900};
  check('字数不变时保留时间', Doc.retargetWords(track, 'The moon!'), true);
  check('按单元长度重排文字', track.words.map(w => w.text), ['The', ' ', 'moon!']);
  check('拼接后与原文一致', track.words.map(w => w.text).join(''), 'The moon!');
  const shorter = {text: 'The night', words: [{start: 0, text: 'The'}, {start: 100, text: 'night'}], end: 900};
  check('字数变化则清除', Doc.retargetWords(shorter, 'Hi'), false);
  check('清除后没有遗留逐字', shorter.words.length + Number(shorter.end === null), 0 + 1);

  // 9) invariants
  let bad = fixture();
  bad.groups[2].start = 2000;
  let invariant = '';
  try { Doc.validateEdit(bad); } catch (error) { invariant = error.message; }
  check('句首倒退被拒绝', invariant, '主轨句首必须非负、唯一且递增');
  let over = fixture();
  over.groups[2].main.end = 50000;
  let tooLong = '';
  try { Doc.validateEdit(over, 12000); } catch (error) { tooLong = error.message; }
  check('超出音频时长被拒绝', tooLong, '轨道时间必须递增且在音频范围内');

  // 10) project file (.saltproj) round-trip
  const song = {name: '星野.lrc', duration: 157000,
                document: {groups: [Doc.newGroup(1000, 'あ'), Doc.newGroup(3000, 'い')],
                           metadata: {ti: '星野'}, notes: [], unresolved: [], alignment: {}}};
  const bytes = new Uint8Array(90000);            // > 8192: exercises chunking
  for (let i = 0; i < bytes.length; i++) bytes[i] = i % 251;
  const packed = Doc.packProject(song, {name: '星野.mp3', type: 'audio/mpeg', bytes});
  const parsed = JSON.parse(JSON.stringify(packed));   // through the wire
  check('工程标记', parsed.kind, 'saltlrc-project');
  const back = Doc.unpackProject(parsed);
  check('工程里的歌名', back.name, '星野.lrc');
  check('工程里的时长', back.duration, 157000);
  check('工程文档原样恢复', back.document.metadata, {ti: '星野'});
  check('工程音频字节数一致', back.audio.bytes.length, 90000);
  check('工程音频逐字节一致', back.audio.bytes[8193], 8193 % 251);
  check('音频文件名一致', back.audio.name, '星野.mp3');
  const bare = Doc.unpackProject(Doc.packProject(song, null));
  check('不内嵌音频时 audio 为空', bare.audio, null);
  let rejected = '';
  try { Doc.unpackProject({kind: 'something-else'}); } catch (error) { rejected = error.message; }
  check('非工程文件给可读错误', /不是 SaltLRC 工程文件/.test(rejected), true);

  // 10b) 容器形态（v0.24.0）：内嵌音频不再走 base64。
  //
  // 为什么要单开一段：这是"保存工程慢"的主因 —— 30MB 音频 base64 成 40MB 文本，
  // 浏览器编码一趟、服务端再解析一趟、写盘再一趟（实测 273ms + ~250MB 临时内存）。
  // 容器把音频**原样**放在 JSON 头之后，一次搬运都不多。
  const container = Doc.packContainer(
    Doc.packProject(song, {name: '星野.flac', type: 'audio/flac', bytes, raw: true}), bytes);
  check('容器以魔数开头', new TextDecoder().decode(container.subarray(0, 8)), 'SaltLRC1');
  const opened = Doc.readProject(container);
  check('容器里读回工程', opened.project.kind, 'saltlrc-project');
  check('容器里音频来自文件尾巴', opened.audioBytes.length, 90000);
  check('容器里音频逐字节一致', opened.audioBytes[8193], 8193 % 251);
  check('容器里没有 base64 数据', opened.project.audio.data, null);
  check('容器读回后音频可用',
        Doc.unpackProject(opened.project, opened.audioBytes).audio.bytes[8193], 8193 % 251);
  check('容器比 base64 的 JSON 小（省掉 33%）',
        container.length < JSON.stringify(
          Doc.packProject(song, {name: '星野.flac', type: 'audio/flac', bytes})).length, true);

  // 老文件（纯 JSON，可能带 base64 音频）必须继续能读 —— 用户手上就有这类文件。
  const legacy = Doc.readProject(new TextEncoder().encode(JSON.stringify(packed)));
  check('老的 JSON 工程仍然能读', legacy.project.kind, 'saltlrc-project');
  check('老的 JSON 工程没有尾部音频', legacy.audioBytes, null);
  check('老的 base64 音频照样解出来',
        Doc.unpackProject(legacy.project, legacy.audioBytes).audio.bytes[8193], 8193 % 251);

  // 坏文件要给人能看懂的一句话，而不是一个空异常。
  const broken = container.slice();
  new DataView(broken.buffer).setUint32(8, 99999999, true);
  let brokenMessage = '';
  try { Doc.readProject(broken); } catch (error) { brokenMessage = error.message; }
  check('容器长度对不上时给可读错误', /工程文件损坏/.test(brokenMessage), true);

  // ---- word-tick editing near the tail ----------------------------------
  // The old flow assigned the dragged time and let validateEdit reject the
  // RESULT, so a word dragged past the line end simply would not move, with a
  // message about "递增且在音频范围内" that never mentioned the line end — the
  // user's "调整完时长就很难调整逐字了，容易卡在尾部". These cases pin the new
  // behaviour AND run the outcome through the real validator, so the helper
  // cannot drift from the rule it exists to satisfy.
  const wordTrack = (starts, end, lineStart) => {
    const group = Doc.newGroup(lineStart === undefined ? starts[0] : lineStart);
    group.main.text = starts.map((_, i) => 'w' + i).join('');
    group.main.words = starts.map((s, i) => ({start: s, text: 'w' + i}));
    group.main.end = end;
    return {group, track: group.main};
  };
  const legal = (group, duration) => {
    try { Doc.validateEdit({groups: [group], metadata: {}, notes: [], unresolved: [], alignment: {}}, duration); return true; }
    catch (error) { return error.message; }
  };
  const applied = (wt, word, time, options) => {
    const moved = Doc.moveWordStart(wt.track, word, time, options);
    wt.track.words[word].start = moved.start;
    if (moved.end != null) wt.track.end = moved.end;
    return moved;
  };

  // A middle word never passes the one before it.
  let wt = wordTrack([1000, 2000, 3000], 5000);
  // 500 is BEFORE the word in front of it, which is what actually needs clamping;
  // 1500 would be legal and must be left alone.
  let moved = applied(wt, 1, 500);
  check('中间字被前一个字挡住', moved.start, 1001);
  check('被挡住时说明是递增规则', /递增/.test(moved.note), true);
  check('不涉及句尾时句尾不动', moved.end, 5000);
  check('被挡住后的文档仍通过校验', legal(wt.group, 20000), true);

  // The last word carries the line end along, keeping the tail gap it had.
  wt = wordTrack([1000, 2000, 3000], 5000);
  moved = applied(wt, 2, 6000);
  check('最后一个字可以越过原来的句尾', moved.start, 6000);
  check('句尾随之延后并保留原有尾距（5000-3000）', moved.end, 8000);
  check('延后句尾时给出提示', /句尾已随之延后/.test(moved.note), true);
  check('延后后的文档通过校验', legal(wt.group, 20000), true);

  // Dragging the last word EARLIER must not shorten the line.
  wt = wordTrack([1000, 2000, 3000], 5000);
  moved = applied(wt, 2, 2500);
  check('最后一个字提前时句尾保持不动', moved.end, 5000);
  check('提前后仍通过校验', legal(wt.group, 20000), true);

  // Nothing may leave the audio — and a carried end has to fit inside it too,
  // which is what caps the move rather than the audio alone.
  wt = wordTrack([1000, 2000, 9800], 9900);
  moved = applied(wt, 2, 99999, {duration: 10000});
  check('最后字被音频末尾挡住（留出尾距）', moved.start, 9899);
  check('句尾不会超出音频时长', moved.end, 9999);
  check('顶到音频末尾后仍通过校验', legal(wt.group, 10000), true);

  // A track with no explicit end has nothing to carry, and none is invented.
  wt = wordTrack([1000, 2000], null);
  moved = applied(wt, 1, 4000);
  check('没有句尾时不凭空造一个', moved.end, null);
  check('没有句尾时按音频末尾限制', applied(wordTrack([1000, 2000], null), 1, 99999, {duration: 10000}).start, 9999);

  // fitLineEnd: a typed end that is too early cannot be stored, so it is raised.
  let fitted = Doc.fitLineEnd(2500, [{start: 1000}, {start: 3000}], 20000);
  check('句尾早于最后一个字时被推后', fitted.end, 3001);
  check('推后时说明原因', /晚于最后一个字/.test(fitted.note), true);
  check('句尾本来就合法时原样返回', Doc.fitLineEnd(5000, [{start: 1000}], 20000).end, 5000);
  check('合法句尾不给多余提示', Doc.fitLineEnd(5000, [{start: 1000}], 20000).note, '');
  check('句尾被音频时长封顶', Doc.fitLineEnd(90000, [{start: 1000}], 20000).end, 20000);
  check('没有逐字时间时句尾只需为正', Doc.fitLineEnd(2500, [], 20000).end, 2500);

  // The two helpers must agree with each other: what one produces, the other
  // must accept.
  wt = wordTrack([1000, 2000, 3000], 5000);
  moved = applied(wt, 2, 6000);
  const after = Doc.fitLineEnd(wt.track.end, wt.track.words, 20000);
  check('moveWordStart 产出的句尾，fitLineEnd 不再改动', after.end, wt.track.end);
  check('两者一致时也不报警', after.note, '');

  // ---- a word must never be REJECTED, only clamped ------------------------
  // Reported as "所有歌词不能分开，只能连在一起". moveWordStart clamped against the
  // previous word only, so dragging a word RIGHT past the next one produced a
  // non-monotonic track, validateEdit threw the whole edit away, and the word
  // would not move at all — with a message that never mentioned the neighbour.
  // Spreading a line therefore required pushing the words right-to-left.
  wt = wordTrack([1000, 2000, 3000], 5000);
  moved = applied(wt, 1, 3400);                      // right, PAST word 3 at 3000
  check('中间字向右拖不会越过下一个字', moved.start, 2999);
  check('被下一个字挡住后的文档通过校验', legal(wt.group, 20000), true);
  check('被下一个字挡住时说明了原因和办法',
        /下一个字/.test(moved.note) && /Alt/.test(moved.note), true);
  // ...and the drag still lands where it was asked to when there IS room.
  wt = wordTrack([1000, 2000, 3000], 5000);
  check('有空间时向右拖仍按目标落点', applied(wt, 1, 2500).start, 2500);

  // ---- Alt-drag: carry the followers, keeping their gaps ------------------
  const rippleTo = (starts, word, target, end, duration = 60000) => {
    const fixture = wordTrack(starts, end);
    const result = Doc.rippleWordStart(fixture.track, word, target,
                                       {duration, lineStart: fixture.track.start});
    // rippleWordStart returns starts for `word..end` only, so only those may move.
    fixture.track.words.slice(word).forEach((unit, offset) => { unit.start = result.starts[offset]; });
    if (result.end != null) fixture.track.end = result.end;
    return {result, fixture};
  };
  let rippled = rippleTo([1000, 2000, 3000], 1, 2500, 5000);
  check('推动模式下被拖的字落在目标位置', rippled.result.starts[0], 2500);
  check('推动模式保住了后面的间距', rippled.result.starts.slice(1), [3500]);
  check('推动模式下前面的字不动', rippled.fixture.track.words[0].start, 1000);
  check('推动后的文档通过校验', legal(rippled.fixture.group, 20000), true);
  check('推动没有越界时不给提示', rippled.result.note, '');

  // The shift cannot overtake the word in front.
  rippled = rippleTo([1000, 2000, 3000], 1, 500, 5000);
  check('推动模式被前一个字挡住', rippled.result.starts[0], 1001);
  check('被挡住时说明是递增规则', /递增/.test(rippled.result.note), true);
  check('被挡住后的文档仍通过校验', legal(rippled.fixture.group, 20000), true);

  // ...and it cannot push the tail out of the audio. The line end rides along,
  // so the audio end — minus that tail — is the real cap.
  rippled = rippleTo([1000, 2000, 9800], 1, 90000, 9900, 10000);
  check('推动不会把后面的字推出音频', rippled.result.starts[1] <= 10000, true);
  check('推动后句尾仍在音频内', rippled.fixture.track.end <= 10000, true);
  check('推到音频末尾后的文档通过校验', legal(rippled.fixture.group, 10000), true);

  // A ripple on the LAST word is the same gesture as dragging the line end.
  rippled = rippleTo([1000, 2000, 3000], 2, 4000, 5000);
  check('推动最后一个字会带着句尾走', rippled.fixture.track.end, 6000);
  check('推动最后一个字后的文档通过校验', legal(rippled.fixture.group, 20000), true);

  // ---- the line handles must MOVE, not be rejected ------------------------
  // Reported as "整句歌词现在时间轴都没办法改". changeTime assigned 句首 and 句尾
  // directly and let validateEdit judge: crossing either one's neighbour threw the
  // whole edit away, so the handle did not move at all and the message named a
  // constraint the user could not see. The third occurrence of the same disease
  // (tail in v0.11.14, word neighbours in v0.12.2, line handles here).
  const twoLines = () => {
    const a = Doc.newGroup(1000);
    a.main.text = 'ab'; a.main.words = [{text: 'a', start: 1000}, {text: 'b', start: 2000}];
    a.main.end = 2500;
    const b = Doc.newGroup(5000);
    b.main.text = 'cd'; b.main.words = [{text: 'c', start: 5000}, {text: 'd', start: 6000}];
    b.main.end = 6500;
    return {groups: [a, b], metadata: {}, notes: [], unresolved: [], alignment: {}};
  };
  const at = (make, index, field, kind, word, duration) =>
    Doc.tickRange(make(), index, field, kind, word, duration);

  check('句尾的下限由最后一个字决定', at(twoLines, 0, 'main', 'end', null, 20000).low, 2001);
  check('句尾的上限是音频时长', at(twoLines, 0, 'main', 'end', null, 20000).high, 20000);
  check('第二行句首的下限是上一行之后', at(twoLines, 1, 'main', 'start', null, 20000).low, 1001);
  check('末行句首的上限是音频末尾', at(twoLines, 1, 'main', 'start', null, 20000).high, 19999);
  check('中间行句首的上限是下一行之前', at(twoLines, 0, 'main', 'start', null, 20000).high, 4999);
  check('逐字的上限是下一个字之前', at(twoLines, 0, 'main', 'word', 1, 20000).high, 2500 - 1);

  // 句尾 shortened: clamped (and legal), never rejected.
  let line = wordTrack([1000, 2000, 3000], 5000);
  let ended = Doc.moveLineEnd(line.track, 2500);
  check('句尾缩到最后一个字之前时被夹住', ended.end, 3001);
  check('夹住时说明怎么办（按住 Alt）', /Alt/.test(ended.note), true);
  line.track.end = ended.end;
  check('夹住后的文档通过校验', legal(line.group, 20000), true);

  // ...and延长 is free.
  line = wordTrack([1000, 2000, 3000], 5000);
  check('句尾可以往后延', Doc.moveLineEnd(line.track, 8000, {duration: 20000}).end, 8000);
  check('句尾不超过音频', Doc.moveLineEnd(line.track, 90000, {duration: 20000}).end, 20000);

  // Alt: the syllables past the new end come along, and the result is legal.
  line = wordTrack([1000, 2000, 3000, 4000], 5000);
  ended = Doc.moveLineEnd(line.track, 3200, {ripple: true});
  check('Alt 缩短时后面的字一起前移', ended.carried.map((c) => c.start), [3199]);
  check('Alt 缩短后句尾仍在最后字之后', ended.end > 3199, true);
  for (const carry of ended.carried) carry.word.start = carry.start;
  line.track.end = ended.end;
  check('Alt 缩短后的文档通过校验', legal(line.group, 20000), true);
  check('Alt 缩短时说明了前移了几个字', /1 个字一起前移/.test(ended.note), true);

  // Pushing too far is clamped by the word in front, not rejected.
  line = wordTrack([1000, 2000, 3000, 4000], 5000);
  ended = Doc.moveLineEnd(line.track, 500, {ripple: true});
  for (const carry of ended.carried) carry.word.start = carry.start;
  line.track.end = ended.end;
  check('缩得过多时被前一个字挡住', ended.carried[0].start, 1001);
  check('挡不住时也给出说明', /顶住|不能再短/.test(ended.note), true);
  check('被挡住后的文档通过校验', legal(line.group, 20000), true);

  if (failures) throw Error(failures + '/' + checks + ' 项前端文档层断言失败');
  console.log(checks + ' document-layer checks passed');
};
