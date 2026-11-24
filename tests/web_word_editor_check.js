// Behaviour check for the simplified per-word editor (library page).
// Runs inside the DOM stub after the page scripts have loaded.
globalThis.__check = async () => {
  const doc = {groups: [{start: 1000, translation: null, main: {
    text: 'abc', end: 2000,
    words: [{text: 'a', start: 1000}, {text: 'b', start: 1200}, {text: 'c', start: 1500}]}}]};
  state.doc = doc;
  state.current = null;

  openWordEditor(0);
  const grid = () => Array.from(document.getElementById('wordGrid').children);
  const cells = grid();
  if (cells.length !== 3) throw Error('应有 3 格，实际 ' + cells.length);
  const hasTextInput = cells.some((cell) => (cell.children || [])
    .some((child) => String(child.className || '').includes('wordText')));
  if (hasTextInput) throw Error('简化后不应再有可编辑的文字输入框');
  if (!cells[0].querySelector('.wordTime')) throw Error('缺少时间输入框');
  if (cells[0].querySelector('.wordChar').textContent !== 'a') throw Error('第 1 格应显示字 a');

  // 取消：必须能关掉对话框（用户报的正是按钮点了没反应）
  let closed = false;
  const dialog = document.getElementById('wordEditDialog');
  dialog.close = () => { closed = true; };
  await document.getElementById('closeWordEdit').onclick();
  if (!closed) throw Error('「取消」没有关闭对话框');

  // 应用：只改时间，不动文字
  openWordEditor(0);
  grid()[1].querySelector('.wordTime').value = '1.300';
  document.getElementById('wordEnd').value = '2.500';
  await document.getElementById('applyWords').onclick();
  const track = doc.groups[0].main;
  if (track.words[1].start !== 1300) throw Error('应用后第 2 个字应为 1300ms，实际 ' + track.words[1].start);
  if (track.end !== 2500) throw Error('应用后句尾应为 2500ms，实际 ' + track.end);
  if (track.text !== 'abc') throw Error('行文字不应被改动');

  // 校验：时间倒退要拒绝；相同毫秒（快歌词）必须放行
  openWordEditor(0);
  grid()[2].querySelector('.wordTime').value = '0.500';
  await document.getElementById('applyWords').onclick();
  if (track.words[2].start !== 1500) throw Error('时间倒退时不应写入');
  const message = String(document.getElementById('wordEditError').textContent || '');
  if (!message.includes('早于前一个字')) throw Error('应提示时间倒退，实际提示：' + message);

  // 相同毫秒：允许写入（35 字 / 2 秒的快歌词没有它根本没法应用）
  openWordEditor(0);
  grid()[2].querySelector('.wordTime').value = '1.300';
  await document.getElementById('applyWords').onclick();
  if (track.words[2].start !== 1300) throw Error('相同毫秒应可应用，实际 ' + track.words[2].start);
};
