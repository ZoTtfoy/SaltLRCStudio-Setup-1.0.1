"""另存为：唯一的写文件出口。

导出歌词与保存工程都走 /api/save-as —— 页面的 blob download() 在打包窗口里
没有下载界面，点了什么都不会落盘（"现在导出功能不可用"）。这里用打桩替换原生
对话框，把"选路径"和"写文件"分开验证，不会弹出任何窗口。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer

from saltlrc import folders
from saltlrc.server import Handler

# 一份"能通过导出校验"的文档：salt 格式要求主歌词有逐字时间，
# 所以 words 必须带 start（夹具写错的话导出会正确地拒绝，而不是写出坏文件）。
from _scratch import isolate_localappdata, scratch
DOCUMENT = {"groups": [{"start": 1000,
                        "main": {"text": "hello world", "start": 1000, "end": 2500,
                                 "words": [{"text": "hello ", "start": 1000},
                                           {"text": "world", "start": 1800}]},
                        "translation": {"text": "你好世界", "start": 1000, "end": None,
                                        "words": []}}],
            "metadata": {}, "notes": [], "unresolved": [], "alignment": {}}

class SaveAsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        isolate_localappdata("saltlrc-saveas-")

        class Quiet(Handler):
            def log_message(self, *args):
                pass

            def handle_one_request(self):
                try:
                    Handler.handle_one_request(self)
                except Exception:
                    self.close_connection = True

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.folder = Path(scratch("saltlrc-out-"))
        # 打桩原生对话框：记录被请求的默认文件名，返回一个真实路径。
        self.asked = []
        self.filters = ()
        self.chosen = ["out.lrc"]
        self.real = folders.save_file

        def fake(default_name, title=None, filters=(), preferred=None):
            # 假的签名必须跟真的**完全一致** —— 少一个参数就会 TypeError → 400，
            # 看起来像"接口坏了"。
            self.asked.append(default_name)
            self.filters = filters
            self.preferred = preferred
            if self.chosen is None:
                return None
            return str(self.folder / self.chosen[0])

        folders.save_file = fake
        self.addCleanup(lambda: setattr(folders, "save_file", self.real))
        # 文件夹选择同样打桩：这条路径也要能测，且绝不能弹出真对话框。
        self.real_pick = folders.pick_folder
        folders.pick_folder = lambda title=None, preferred=None: str(self.folder)
        self.addCleanup(lambda: setattr(folders, "pick_folder", self.real_pick))

    def post(self, route, payload):
        request = urllib.request.Request(
            self.url + route, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise AssertionError("%s -> %s: %s" % (route, error.code,
                                                   error.read().decode("utf-8", "replace")))

    def post_raw(self, route, blob):
        """发**原始字节**（不是 JSON）—— /api/save-binary 的调用方式。"""
        request = urllib.request.Request(
            self.url + route, data=blob,
            headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise AssertionError("%s -> %s: %s" % (route, error.code,
                                                   error.read().decode("utf-8", "replace")))

    def test_raw_bytes_go_straight_to_disk(self):
        """二进制出口：字节进去、字节落地，中间**不许有任何解析**。

        这条路上唯一该做的事就是写字节 —— 不经过 base64、JSON 或 UTF-8 解码。

        判据是"落盘的内容与发出去的**逐字节**一致"，而且夹具里放**全部 256 种字节值** ——
        只要中间过了一次 base64 或 UTF-8 解码，内容就会变。
        """
        self.chosen = ["song.saltproj"]
        blob = bytes(range(256)) * 400          # 102400 字节
        reply = self.post_raw("/api/save-binary?name=song.saltproj", blob)
        self.assertEqual(reply["status"], "written")
        self.assertEqual(Path(reply["path"]).read_bytes(), blob,
                         "二进制出口必须逐字节一致（不许经过 base64 / JSON）")
        self.assertEqual(reply["bytes"], len(blob))
        self.assertEqual(self.asked[0], "song.saltproj")

    def test_raw_route_honours_a_cancel(self):
        self.chosen = None
        reply = self.post_raw("/api/save-binary?name=x.saltproj", b"abc")
        self.assertEqual(reply["status"], "cancelled")

    def test_raw_route_prefers_the_songs_own_folder(self):
        self.chosen = ["song.saltproj"]
        quoted = urllib.parse.quote(str(self.folder))
        self.post_raw("/api/save-binary?name=song.saltproj&preferred=" + quoted, b"x")
        self.assertEqual(self.preferred, str(self.folder),
                         "对话框的起始目录要跟着这首歌自己的文件夹走")

    def test_raw_route_rejects_a_suffix_it_does_not_own(self):
        with self.assertRaises(AssertionError) as caught:
            self.post_raw("/api/save-binary?name=x.exe", b"hello")
        self.assertIn("不支持的文件类型", str(caught.exception))

    def test_raw_route_needs_a_name(self):
        with self.assertRaises(AssertionError) as caught:
            self.post_raw("/api/save-binary", b"hello")
        self.assertIn("缺少文件名", str(caught.exception))

    def test_raw_route_refuses_an_empty_body(self):
        with self.assertRaises(AssertionError) as caught:
            self.post_raw("/api/save-binary?name=x.saltproj", b"")
        self.assertIn("没有内容", str(caught.exception))

    def test_raw_route_only_takes_the_file_name(self):
        # 调用方给路径也没用：写哪儿由用户在对话框里定（与 _safe_name 同一条边界）。
        self.chosen = ["song.saltproj"]
        reply = self.post_raw("/api/save-binary?name=" + urllib.parse.quote(r"..\..\evil.saltproj"),
                              b"x")
        self.assertEqual(Path(reply["path"]).name, "song.saltproj")

    def test_a_lyric_document_is_rendered_and_written(self):
        reply = self.post("/api/save-as", {"name": "晴天", "document": DOCUMENT,
                                           "format": "salt", "encoding": "utf-8"})
        self.assertEqual(reply["status"], "written")
        self.assertEqual(Path(reply["path"]).name, "out.lrc")
        # 默认文件名是页面给的名字（引擎在服务端决定扩展名）
        self.assertEqual(self.asked[0], "晴天.lrc")
        text = Path(reply["path"]).read_text(encoding="utf-8")
        self.assertIn("hello", text)
        self.assertIn("你好", text)
        self.assertNotIn("audio", text.lower())

    def test_ready_made_text_is_written_verbatim(self):
        body = json.dumps({"version": 2, "name": "工程"})
        self.chosen = ["song.saltproj"]
        reply = self.post("/api/save-as", {"name": "歌曲.saltproj", "text": body})
        self.assertEqual(reply["status"], "written")
        self.assertEqual(Path(reply["path"]).read_text(encoding="utf-8"), body)

    def test_the_extension_follows_the_format_not_the_client(self):
        # 页面只给名字主干；.yrc 由格式决定 —— 扩展名只有一个权威。
        self.chosen = ["x.yrc"]
        reply = self.post("/api/save-as", {"name": "x.lrc", "document": DOCUMENT, "format": "yrc"})
        self.assertEqual(reply["status"], "written")
        self.assertEqual(self.asked[0], "x.yrc", "扩展名必须跟格式走，而不是客户端传的")
        self.assertTrue(Path(reply["path"]).read_text(encoding="utf-8").lstrip().startswith(("{", "[")))

    def test_ready_made_bytes_are_written_verbatim(self):
        """批量导出 ZIP 也走这一个出口（二进制）。

        以前它用页面的 blob 下载 —— 打包窗口里没有下载界面，点了什么都不会落盘
        （和「导出当前歌词」同一个坑，只是那条先修的）。
        这里验的是"二进制原样落盘"，并顺带钉住扩展名白名单。
        """
        import base64
        blob = bytes([0x50, 0x4B, 0x03, 0x04, 0x00, 0xFF, 0x10])
        self.chosen = ["批量导出.zip"]
        reply = self.post("/api/save-as", {"name": "SaltLRC 批量导出.zip", "suffix": ".zip",
                                           "bytes_base64": base64.b64encode(blob).decode()})
        self.assertEqual(reply["status"], "written")
        self.assertEqual(Path(reply["path"]).read_bytes(), blob, "二进制必须逐字节原样")
        self.assertEqual(reply["bytes"], len(blob))

    def test_only_whitelisted_suffixes_can_be_written(self):
        """扩展名由服务端定：调用方只能从白名单里挑，不能随便写后缀。"""
        for payload in ({"name": "x.exe", "text": "hi", "suffix": ".exe"},
                        {"name": "x.zip", "bytes_base64": "UEsDBA==", "suffix": ".bat"}):
            with self.assertRaises(AssertionError) as caught:
                self.post("/api/save-as", payload)
            self.assertIn("不支持的文件类型", str(caught.exception))

    def test_the_analysis_json_is_written_through_the_same_door(self):
        """「保存分析结果」：.json 也在白名单里，而且带上自己的类型筛选。"""
        self.chosen = ["晴天-分析结果.json"]
        reply = self.post("/api/save-as", {"name": "晴天-分析结果.json", "suffix": ".json",
                                           "text": '{"proposal": null}'})
        self.assertEqual(reply["status"], "written")
        self.assertEqual(Path(reply["path"]).read_text(encoding="utf-8"), '{"proposal": null}')
        self.assertEqual(Path(reply["path"]).name, "晴天-分析结果.json")
        # 对话框里的筛选要跟着扩展名走（否则默认类型是"歌词文件"，用户会一脸问号）
        self.assertIn(".json", str(self.filters), "另存为的筛选器要认得 .json")

    def test_cancelling_writes_nothing(self):
        self.chosen = None
        reply = self.post("/api/save-as", {"name": "x", "document": DOCUMENT})
        self.assertEqual(reply, {"status": "cancelled"})
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [])

    def test_a_path_from_the_client_cannot_escape_the_chosen_folder(self):
        reply = self.post("/api/save-as", {"name": "../../evil", "document": DOCUMENT})
        self.assertEqual(Path(reply["path"]).parent, self.folder)
        self.assertEqual(Path(reply["path"]).name, "out.lrc")
        self.assertFalse((self.folder.parent.parent / "evil.lrc").exists())

    def test_a_missing_name_is_refused_out_loud(self):
        with self.assertRaises(AssertionError) as caught:
            self.post("/api/save-as", {"name": "  ", "document": DOCUMENT})
        self.assertIn("缺少文件名", str(caught.exception))

    def test_export_folder_still_uses_one_writer(self):
        """两条出口共用同一个写文件实现：产物必须一模一样。"""
        self.chosen = ["via-save-as.lrc"]
        via_dialog = self.post("/api/save-as", {"name": "x", "document": DOCUMENT})
        via_folder = self.post("/api/export-folder", {"name": "via-folder", "document": DOCUMENT})
        self.assertEqual(via_folder["status"], "written")
        self.assertEqual(Path(via_folder["path"]).parent, self.folder)
        self.assertEqual(Path(via_folder["path"]).read_text(encoding="utf-8"),
                         Path(via_dialog["path"]).read_text(encoding="utf-8"),
                         "两条出口写出的歌词必须完全一致")
        self.assertEqual(Path(via_folder["path"]).name, "via-folder.lrc")

if __name__ == "__main__":
    unittest.main()
