"""「识别与对齐」页那几个按钮背后的接口 —— 用真音频跑一遍。

这里把接口的正确行为钉住：声音起点对齐真的能找出偏移、整首行首对齐真的给得出 delta、
行内细化真的给得出候选、未安装的引擎真的给得出可读理由 —— 这些都不该在重构里悄悄退化。

夹具是和「载入示例」同一份合成节拍（两段 220Hz 音），歌词**故意早 150ms** ——
所以"找到了偏移"是有一个可验证的真值的，而不是"返回了 200 就算过"。
"""
import base64
import json
import math
import struct
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from saltlrc.server import Handler

RATE = 16000
LRC = ("[00:01.000]今晚[00:02.000]月色很美[00:04.000]\n"
       "[00:01.000]今夜は[00:02.000]月が綺麗だね[00:04.000]\n"
       "[00:05.000]一起[00:06.000]回家吧[00:08.000]\n"
       "[00:05.000]一緒に[00:06.000]帰ろう[00:08.000]\n")


def tone_wav(seconds=9.0):
    """两段 220Hz 音（1.15~4.1s / 5.12~8.1s），带 44 字节 WAV 头。

    和页面的 `SaltAudio.pcm16` 产出同一种东西：16kHz 单声道 16 位 PCM 的完整 WAV。
    """
    frames = bytearray()
    for index in range(int(seconds * RATE)):
        t = index / RATE
        value = 0.0
        if (1.15 <= t < 4.1) or (5.12 <= t < 8.1):
            value = 0.2 * math.sin(t * 2 * math.pi * 220) * min(1, (t % 1) * 15)
        frames += struct.pack("<h", int(max(-1, min(1, value)) * 32767))
    return (b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16) +
            b"data" + struct.pack("<I", len(frames)) + bytes(frames))


def wav_silence(seconds=6.0):
    frames = b"\x00\x00" * int(seconds * RATE)
    return (b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16) +
            b"data" + struct.pack("<I", len(frames)) + frames)


class AlignRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Quiet(Handler):
            def log_message(self, *args):
                pass

            def handle_one_request(self):
                try:
                    Handler.handle_one_request(self)
                except Exception:
                    self.close_connection = True

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        cls.server.request_queue_size = 128
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_port
        cls.audio = base64.b64encode(tone_wav()).decode()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, route, payload, expect_error=False):
        request = urllib.request.Request(self.url + route, json.dumps(payload).encode(),
                                         {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            if not expect_error:
                raise AssertionError("%s -> %s: %s" % (route, error.code, body))
            try:
                return error.code, json.loads(body)
            except ValueError:
                return error.code, body

    def document(self):
        status, payload = self.post("/api/parse",
                                    {"bytes": base64.b64encode(LRC.encode()).decode()})
        self.assertEqual(status, 200)
        return payload["document"]

    def test_smart_fit_gives_a_fitted_document(self):
        """`/api/align/fit`（智能贴唱）：一次给出**整首**行级提案 + 贴好的文档。

        这是「识别与对齐」页的贴唱入口，对应"分析当前音频 + 整首行首对齐"这两件事。
        真值同样来自夹具：歌词整体晚 1500ms，所以 `moved_ms` 必须为负、贴回真值附近。
        """
        document = self.document()
        # 把整首歌词推后 1500ms。解析出来的文档里 end 有可能缺失（`None`），
        # 所以只挪**确实存在**的量 —— 夹具不能假设字段一定有值（否则测的是 NoneType）。
        shifted = 0
        for group in document["groups"]:
            if group["start"] is not None:
                group["start"] += 1500
                shifted += 1
            for track in ("main", "translation"):
                line = group.get(track)
                if not line:
                    continue
                if line.get("start") is not None:
                    line["start"] += 1500
                if line.get("end") is not None:
                    line["end"] += 1500
        self.assertTrue(shifted, "夹具没挪动任何一行，后面的断言就没有真值可依")
        status, report = self.post("/api/align/fit",
                                   {"document": document, "audio": self.audio,
                                    "field": "auto", "max_shift": 8000})
        self.assertEqual(status, 200, report)
        self.assertTrue(report["onsets"], "没找到人声起点")
        self.assertEqual(len(report["lines"]), len(document["groups"]))
        self.assertEqual(report["moved"] + report["weak"] + report["kept"], len(report["lines"]))
        self.assertIn("预览", report["notice"], "要告诉用户下一步能预览")
        for line in report["lines"]:
            self.assertLess(line["moved_ms"], 0, "歌词是晚的，应该往前扳：%s" % line)
            self.assertGreater(line["end"], line["start"])
            self.assertTrue(line["reason"])
        # 返回的是一份**贴好的完整文档**：页面直接拿它预览/应用，不用自己再算一遍
        fitted = report["document"]
        self.assertEqual(len(fitted["groups"]), len(document["groups"]))
        for original, after in zip(document["groups"], fitted["groups"]):
            self.assertEqual(after["start"], after["main"]["start"], "句首只有一个事实")
            self.assertLess(after["start"], original["start"], "贴后应该更早")

    def test_smart_fit_refuses_readably_when_there_is_nothing_to_fit(self):
        """没有歌词行 / 音频没人声：都要给可读理由（不是 500）。"""
        empty = {"groups": [], "metadata": {}, "notes": [], "unresolved": [], "alignment": {}}
        status, payload = self.post("/api/align/fit",
                                    {"document": empty, "audio": self.audio}, expect_error=True)
        self.assertEqual(status, 400, payload)
        self.assertIn("歌词行", str(payload))
        status, payload = self.post("/api/align/fit",
                                    {"document": self.document(),
                                     "audio": base64.b64encode(wav_silence(6.0)).decode()},
                                    expect_error=True)
        self.assertEqual(status, 400, payload)
        self.assertIn("人声起点", str(payload))

    def test_analyzing_the_audio_gives_candidates(self):
        """「分析当前音频」：受理 → 轮询 → 完成，候选里带着逐行的新起点。"""
        status, job = self.post("/api/align/start",
                                {"document": self.document(), "audio": self.audio,
                                 "engine": "acoustic", "field": "auto",
                                 "radius": 1500, "max_shift": 30000})
        self.assertIn(status, (200, 202), job)
        self.assertEqual(job["state"], "running")
        for _ in range(60):
            time.sleep(1)
            status, state = self.post("/api/align/status", {"id": job["id"]})
            if state.get("state") != "running":
                break
        self.assertEqual(state["state"], "completed", state)
        result = state["result"]
        self.assertTrue(result["proposals"], "没有候选就没有可应用的东西")
        first = result["proposals"][0]
        self.assertIn("start", first)
        self.assertIn("kind", first)
        # 夹具的歌词比声音早 ~150ms，所以起点应当被往**后**推
        self.assertGreater(first["start"], first["old_start"] - 1,
                           "候选起点方向反了：%s" % first)

    def test_whole_song_offset_reports_a_delta_and_an_explanation(self):
        """「整首行首对齐」：给得出 delta，也必须给得出人话说明。"""
        status, result = self.post("/api/align/offset",
                                   {"document": self.document(), "audio": self.audio,
                                    "field": "auto", "radius": 4000})
        self.assertEqual(status, 200)
        self.assertIn("delta", result)
        self.assertIn("notice", result)
        self.assertTrue(result["notice"], '没有说明就成了「点了没反应」的按钮')
        # 半径是**用户能改的**：给一个小到找不到东西的半径，应当老实说找不到，
        # 而不是硬凑一个偏移出来。
        status, tight = self.post("/api/align/offset",
                                  {"document": self.document(), "audio": self.audio,
                                   "field": "auto", "radius": 100})
        self.assertEqual(status, 200)
        self.assertLessEqual(abs(tight["delta"]), abs(result["delta"]) + 1)

    def test_refining_words_gives_per_line_candidates(self):
        """「行内逐字细化」：给得出每行的发声起点候选。"""
        status, result = self.post("/api/align/words",
                                   {"document": self.document(), "audio": self.audio,
                                    "field": "auto"})
        self.assertEqual(status, 200)
        self.assertIn("proposals", result)
        self.assertTrue(result["proposals"])
        self.assertIn("onsets", result)
        entry = result["proposals"][0]
        self.assertIn("status", entry)
        self.assertIn("reason", entry)

    def test_an_uninstalled_engine_is_refused_in_plain_chinese(self):
        """选了没装的引擎：必须是"怎么装"这种可读理由，而不是 500。"""
        status, payload = self.post("/api/align/start",
                                    {"document": self.document(), "audio": self.audio,
                                     "engine": "faster-whisper"}, expect_error=True)
        self.assertEqual(status, 400, payload)
        self.assertIn("setup-ai", str(payload))

    def test_bad_audio_is_refused_in_plain_chinese(self):
        status, payload = self.post("/api/align/offset",
                                    {"document": self.document(), "audio": base64.b64encode(b"x").decode()},
                                    expect_error=True)
        self.assertEqual(status, 400, payload)
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
