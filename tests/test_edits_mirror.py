"""L1 结构性编辑的两个实现必须一致：`saltlrc/edits.py` ↔ `web/document.js`。

`tests/test_edits.py` 与
`tests/web_document_check.js` 用的是**同一批夹具**（三行 `[1000,3000,9000]`）——
夹具一样、两边都绿，只证明它们在**那几个输入**上一致。这里换成随机文档（带逐字轴、
句尾、翻译轨自己的句首）跑同一批操作，逐一比对结果：成功的起始时间序列，
或失败的理由原文。

一个刻意的已知差异（不在这条测试的范围里，写在这里免得以后有人当成 bug）：
镜像的 `insertGroup` / `deleteGroup` **不跑不变量校验**（Python 侧会跑
`check(doc, duration)`），因为页面用它们做"即时反馈"，权威始终是 `/api/edit`。
`tests/web_block_drag_check.js` 把镜像当假服务端用，所以镜像值不对会直接
污染那条行为检查 —— 这才是这条差分测试存在的理由。

有效输入上两者必须**完全一致**；超出不变量范围的输入本来就不该出现（服务器会
400，页面会先被 `validateEdit` 拦下），那种情况只断言"都没崩"。
"""

import json
import os
import pathlib
import random
import shutil
import subprocess
import tempfile
import unittest

from saltlrc import edits
from saltlrc.document import Document, DocumentError, Group, Track, Word, ticks

from _scratch import isolate_localappdata, scratch
ROOT = pathlib.Path(__file__).parent.parent

TEMPLATE = """// 由 tests/test_edits_mirror.py 生成（用例内联：vm 上下文里没有 require）。
const CASES = %s;
globalThis.__check = async () => {
  const out = CASES.map(([op, doc, index, duration, gap]) => {
    try {
      const copy = JSON.parse(JSON.stringify(doc));
      if (op === 'insert') Doc.insertGroup(copy, index, {duration: duration == null ? null : duration, gap});
      else Doc.deleteGroup(copy, index);
      return {ok: true, starts: copy.groups.map(g => g.start)};
    } catch (error) { return {ok: false, why: error.message}; }
  });
  console.log('__DIFF__' + JSON.stringify(out));
};
"""

SEED = 20260926
COUNT_INSERT = 300
COUNT_DELETE = 150

# `gap` 必须一起随机：固定用 DEFAULT_GAP 时，镜像里
# `high = following.start - MIN_SLACK` 那道钳制**永远不会被触发** ——
# 把它改坏（`high = following.start`）差分测试照样全绿。
GAPS = [0, 1, 2, 50, 1000, 5000]

def _node():
    return shutil.which("node") or shutil.which("node.exe") or \
        r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"

def _node_path():
    candidates = [os.environ.get("NODE_PATH", ""),
                  r"C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules"]
    return os.pathsep.join(c for c in candidates if c)

def random_document(rng):
    """随机但**合法**的文档：句尾不早于最后一个字，全部时间在跨度之内。

    入参本身非法的文档会让差分出的不一致都来自夹具，而不是被测代码。
    """
    starts = sorted(rng.sample(range(0, 40000), rng.randint(1, 6)))
    groups = []
    for start in starts:
        words = []
        if rng.random() < 0.5:
            cursor, items = start, []
            for _ in range(rng.randint(1, 4)):
                items.append((cursor, "字"))
                cursor += rng.randint(50, 900)
            words = [Word(value, text) for value, text in items]
        latest = words[-1].start if words else start
        end = latest + rng.randint(1, 4000) if rng.random() < 0.6 else None
        groups.append(Group(start, Track("歌词", words, end, None),
                            Track("訳", [], None, start if rng.random() < 0.3 else None)))
    doc = Document(groups, {}, [], [], {})
    span = max((ticks(g, name)[-1] for g in groups for name in ("main", "translation")
                if ticks(g, name)), default=0)
    return doc, span

def build_cases():
    rng = random.Random(SEED)
    cases = []
    for _ in range(COUNT_INSERT):
        doc, span = random_document(rng)
        duration = rng.choice([None, span, span + 5000, span + 60000])
        cases.append(["insert", doc.to_dict(), rng.randint(-2, len(doc.groups) + 2),
                      duration, rng.choice(GAPS)])
    for _ in range(COUNT_DELETE):
        doc, _ = random_document(rng)
        cases.append(["delete", doc.to_dict(), rng.randint(-2, len(doc.groups) + 2), None, None])
    return cases

class MirrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = _node()
        if not node or not pathlib.Path(node).exists():
            raise unittest.SkipTest("node not available")
        cls.node, cls.env = node, dict(os.environ, NODE_PATH=_node_path())

    def test_python_and_the_browser_mirror_agree_on_random_documents(self):
        cases = build_cases()
        folder = scratch("saltlrc-mirror-")
        check = pathlib.Path(folder) / "check.js"
        check.write_text(TEMPLATE % json.dumps(cases, ensure_ascii=False), encoding="utf-8")

        result = subprocess.run(
            [self.node, str(ROOT / "tests" / "web_load_harness.js"),
             str(ROOT / "saltlrc" / "web" / "document.js"), "--run=" + str(check)],
            env=self.env, capture_output=True, text=True, timeout=300)
        marker = [line for line in result.stdout.splitlines() if line.startswith("__DIFF__")]
        self.assertTrue(marker, "镜像侧没有回结果：\n" + result.stdout + result.stderr)
        mirror = json.loads(marker[-1][len("__DIFF__"):])
        self.assertEqual(len(mirror), len(cases))

        mismatches = []
        for (op, raw, index, duration, gap), got in zip(cases, mirror):
            doc = Document.from_dict(json.loads(json.dumps(raw)))
            try:
                if op == "insert":
                    edits.insert_group(doc, index, duration=duration, gap=gap)
                else:
                    edits.delete_group(doc, index)
                mine = {"ok": True, "starts": [g.start for g in doc.groups]}
            except DocumentError as error:
                mine = {"ok": False, "why": str(error)}
            if mine == got:
                continue
            mismatches.append((op, [g["start"] for g in raw["groups"]], index, duration, gap, mine, got))

        if mismatches:
            detail = "\n".join(
                "op=%s 入参 starts=%s index=%s duration=%s gap=%s\n  Python: %s\n  镜像  : %s"
                % (op, starts, index, duration, gap, json.dumps(mine, ensure_ascii=False),
                   json.dumps(got, ensure_ascii=False))
                for op, starts, index, duration, gap, mine, got in mismatches[:5])
            self.fail("Python 与页面镜像在 %d/%d 例上不一致（同一条规则两处各算一遍）：\n%s"
                      % (len(mismatches), len(cases), detail))

if __name__ == "__main__":
    unittest.main()
