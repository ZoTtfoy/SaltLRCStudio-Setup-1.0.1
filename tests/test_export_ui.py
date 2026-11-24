"""「导出歌词」的四步流程（真页面 + 真后端）。

流程是四步：① 读取已编辑的歌词 → ② 打包成 LRC → ③ 选择保存路径 → ④ 保存文件。

这条链路里唯一的"开窗"动作是**原生另存为对话框**，而本机不许弹窗，
所以这里把 `folders.save_file` / `folders.reveal` 打桩
（**只换"路径从哪来"和"谁去开窗口"**），其余（渲染、校验、写盘、页面接线）全是真代码。

守的是两条契约：
  * 三个标签页里的 `#export` 都要能点到、点了都要真的落盘；
  * **对话框用不了也不等于导出失败** —— 退回一定可写的目录，并把落点说清楚。
"""

import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from _scratch import isolate_localappdata, scratch                      # noqa: E402

isolate_localappdata("saltlrc-export-")

from saltlrc import folders                                             # noqa: E402
from saltlrc.server import Handler                                      # noqa: E402

DOCUMENT = {"groups": [{"start": 1000,
                        "main": {"text": "今晚", "words": [{"start": 1000, "text": "今晚"}],
                                 "end": 3000, "start": None},
                        "translation": {"text": "", "words": [], "end": None, "start": None}}],
            "metadata": {}, "notes": [], "unresolved": [], "alignment": {}}


def _node():
    return shutil.which("node") or shutil.which("node.exe")


def _node_path():
    candidates = [os.environ.get("NODE_PATH", ""),
                  r"C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules"]
    return os.pathsep.join(c for c in candidates if c)


class ExportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = _node()
        if not node:
            raise unittest.SkipTest("node not available")
        cls.node = node
        cls.env = dict(os.environ, NODE_PATH=_node_path())
        cls.out = pathlib.Path(scratch("saltlrc-export-out-"))
        cls.asked = []
        cls.revealed = []

        def fake_save_file(default_name, title=None, filters=(), preferred=None):
            """只换"路径从哪来"：名字、筛选器、初始目录仍由真代码算好再传进来。"""
            cls.asked.append({"name": default_name, "filters": filters, "preferred": preferred})
            return str(cls.out / default_name)

        def fake_reveal(path):
            """把"在资源管理器里定位"打桩 —— 测试**不许开任何窗口**，
            这里只记录页面有没有真的请求它。"""
            cls.revealed.append(str(path))
            return str(path)

        cls.real_save_file = folders.save_file
        cls.real_reveal = folders.reveal
        cls.fake_save_file = staticmethod(fake_save_file)
        folders.save_file = fake_save_file
        folders.reveal = fake_reveal
        cls.addClassCleanup(setattr, folders, "save_file", cls.real_save_file)
        cls.addClassCleanup(setattr, folders, "reveal", cls.real_reveal)

        class Quiet(Handler):
            def log_message(self, *args):
                pass

            def handle_one_request(self):
                try:
                    Handler.handle_one_request(self)
                except Exception:
                    self.close_connection = True

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        cls.server.request_queue_size = 256
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, route, payload):
        request = urllib.request.Request(
            self.url + route, json.dumps(payload).encode(),
            {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            try:
                return error.code, json.loads(body)
            except ValueError:
                return error.code, {"error": body}

    # ---------------------------------------------------------------- ① 页面
    def test_export_works_from_every_tab_and_actually_writes(self):
        """真页面 + 真后端：三个标签页各点一次导出，每次都要真的落盘。"""
        result = subprocess.run(
            [self.node, str(HERE / "web_export_check.js"), self.url],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         "导出检查失败：\n" + result.stdout + result.stderr)
        self.assertIn("0 failed", result.stdout)

        # 页面真的调了另存为，而且**把它自己知道的目录**带上了（对话框的初始位置）
        self.assertTrue(self.asked, "页面一次都没有请求另存为")
        self.assertIn("D:\\我的歌词", [item["preferred"] for item in self.asked],
                      "导出时没把「这首歌从哪来」传给后端 —— 对话框又会开在别处")

        # 三种格式都真的落了盘，而且内容各按各的格式
        written = {path.name: path.read_text(encoding="utf-8")
                   for path in self.out.iterdir() if path.is_file()}
        self.assertIn("导出检查.lrc", written, "没有写出 .lrc")
        self.assertIn("导出检查.yrc", written, "没有写出 .yrc（YRC 格式）")
        lrc = written["导出检查.lrc"]
        self.assertIn("今夜は月が綺麗だね", lrc, "翻译轨丢了")
        # 逐字时间戳写在 <> 里，[] 只留句首 ——
        # 判据跟着格式走，但"每个字一个时间戳"这件事仍然被钉住。
        self.assertTrue(any(line.count("<") >= 3 for line in lrc.splitlines()),
                        "salt 格式的每一行应当带逐个字的时间戳（写在 <> 里）：\n" + lrc)
        self.assertEqual(lrc.splitlines()[0].count("["), 1,
                         "行首只该有一个 []：其余时间戳属于 <>")
        self.assertIn('"c":[', written["导出检查.yrc"], "YRC 应当逐字成 JSON")

        # 同一首歌导出两次（不同标签页）就是两次写盘 —— 不能是"只有第一次生效"
        self.assertGreaterEqual(len(self.asked), 5, "三次切页 + 三种格式都该各请求一次")
        # 「打开所在文件夹」：页面真的把**刚导出的那个路径**发过来了
        self.assertTrue(self.revealed, "页面没有请求「打开所在文件夹」")
        self.assertEqual(self.revealed[-1], str(self.out / "导出检查.lrc"),
                         "定位的必须是刚导出的那个文件")

    # ------------------------------------------------- ② 对话框用不了也要能导出
    def test_a_broken_dialog_still_exports_and_says_where_it_went(self):
        """**对话框用不了 ≠ 导出失败。**

        只有**用户自己点取消**才算取消；对话框失败会走兜底 —— 文件写出来、
        落在"这首歌自己的目录"（否则「文档」）、并把原因和落点报给页面。
        """
        folder = pathlib.Path(scratch("saltlrc-export-fallback-"))

        def broken(default_name, title=None, filters=(), preferred=None):
            raise folders.PickerError("模拟：对话框弹不出来")

        folders.save_file = broken
        try:
            code, payload = self.post("/api/save-as",
                                      {"name": "回退", "document": DOCUMENT, "format": "salt",
                                       "preferred": str(folder)})
        finally:
            folders.save_file = self.fake_save_file

        self.assertEqual(code, 200, payload)
        self.assertNotEqual(payload.get("status"), "cancelled",
                            "对话框失败不该被当成「用户取消」")
        self.assertIn("对话框", payload.get("fallback", ""), payload)
        self.assertIn("已改存到", payload.get("fallback", ""), "要告诉用户文件去哪了")
        self.assertIn(str(folder), payload.get("path", ""),
                      "兜底应当落在**这首歌自己的目录**里，而不是藏起来的应用目录")
        written = pathlib.Path(payload["path"])
        self.assertTrue(written.is_file(), "兜底也要真的把文件写出来")
        self.assertIn("今晚", written.read_text(encoding="utf-8"))

    def test_the_user_cancelling_is_not_an_error(self):
        """用户自己点「取消」是正常操作：不写文件、也不报错。"""
        folders.save_file = lambda default_name, title=None, filters=(), preferred=None: None
        try:
            code, payload = self.post("/api/save-as",
                                      {"name": "取消", "document": DOCUMENT, "format": "salt"})
        finally:
            folders.save_file = self.fake_save_file
        self.assertEqual(code, 200, payload)
        self.assertEqual(payload["status"], "cancelled")

    # ------------------------------------------------------------------ ③ 打包
    def test_dry_run_renders_the_lrc_without_any_dialog(self):
        """`dry_run` 只打包、不开窗口 —— 这是**唯一能在交付的 exe 上验证导出内容**的口子。

        本机不许弹窗，所以"交付 exe 里导出的 LRC 到底长什么样"只能这样验。
        """
        before = len(self.asked)
        code, payload = self.post("/api/save-as",
                                  {"name": "预演", "document": DOCUMENT,
                                   "format": "salt", "dry_run": True})
        self.assertEqual(code, 200, payload)
        self.assertEqual(payload["status"], "dry-run")
        self.assertIn("[00:01.000]<00:01.000>今晚", payload["text"],
                      "salt 格式应当逐字带时间戳（逐行 [] + 逐字 <>）")
        self.assertEqual(payload["lines"], 1)
        self.assertTrue(payload["directory"], "要告诉调用方它默认会落在哪儿")
        self.assertEqual(len(self.asked), before, "dry_run 不许碰对话框")

    def test_a_song_without_word_timings_still_exports(self):
        """**没做逐字的歌词也要导得出文件。**

        默认的「Salt 逐字」格式要求**每字一个时间戳**，而一首还没做过逐字的歌词
        （45 行很常见）也要能写出文件：没有逐字的行整行当一个单元（逐行形式），
        严格格式不再拦路。YRC 是另一种格式（JSON），要 JSON 的人不该拿到 LRC。
        """
        lines = [{"start": 1000 + i * 3000,
                  "main": {"text": "第%d句" % (i + 1), "words": [], "end": None, "start": None},
                  "translation": {"text": "译文%d" % (i + 1), "words": [], "end": None, "start": None}}
                 for i in range(45)]
        document = {"groups": lines, "metadata": {}, "notes": [], "unresolved": [], "alignment": {}}

        code, payload = self.post("/api/save-as",
                                  {"name": "没逐字", "document": document, "format": "salt"})
        self.assertEqual(code, 200, payload)
        # **连降级都不需要** —— 没有逐字的行本来就写得出来（逐行形式），
        # 所以格式仍然是 Salt、文件里也就不会"把已经写好的逐字截掉"。
        self.assertNotIn("degraded", payload, "不该再降级：" + str(payload)[:200])
        written = pathlib.Path(payload["path"])
        self.assertTrue(written.is_file(), "文件必须真的写出来")
        body = written.read_text(encoding="utf-8")
        self.assertIn("[00:01.000]第1句", body)
        self.assertIn("[00:01.000]译文1", body, "翻译轨要一起写出来")
        self.assertNotIn("<", body, "一行逐字都没有时不该凭空出现逐字标记")
        self.assertEqual(payload["lines"], 90, "45 行主轨 + 45 行翻译")

        # YRC 是另一种格式：**不许**偷偷换成 LRC。
        # 没有逐字的行整行当一个单元，所以判据是
        # "写出来的仍然是 JSON" —— 这才是不降级的证据。
        code, payload = self.post("/api/save-as",
                                  {"name": "没逐字", "document": document, "format": "yrc"})
        self.assertEqual(code, 200, payload)
        yrc = pathlib.Path(payload["path"]).read_text(encoding="utf-8")
        self.assertIn('"c":[', yrc, "必须是 YRC 的 JSON")
        self.assertNotIn("[00:01.000]第1句", yrc, "绝不能把 LRC 冒充成 YRC")

    def test_an_unrenderable_document_says_why_in_chinese(self):
        """连宽松格式都渲染不出来时（空文档）要给中文原因，而不是 500。"""
        empty = {"groups": [], "metadata": {}, "notes": [], "unresolved": [], "alignment": {}}
        code, payload = self.post("/api/save-as",
                                  {"name": "空的", "document": empty, "format": "salt"})
        self.assertEqual(code, 400, payload)
        self.assertIn("歌词", str(payload), payload)


if __name__ == "__main__":
    unittest.main()
