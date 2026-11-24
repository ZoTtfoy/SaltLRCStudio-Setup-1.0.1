"""AI translation client + settings store.

The network is faked with a real local HTTP server rather than a monkeypatched
``urlopen``: the request shape (path, auth header, JSON body) is half the
contract with DeepSeek/OpenAI, and a stub would happily accept a wrong one.
"""


import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The settings store reads LOCALAPPDATA at import time of each call; isolating it
# here keeps the tests off the developer's real settings file.
from _scratch import isolate_localappdata, scratch
isolate_localappdata("saltlrc-test-")

from saltlrc import settings, translator  # noqa: E402

class FakeProvider(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint whose reply is chosen by the test."""

    mode = "single"
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        FakeProvider.seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": body,
        })
        mode = FakeProvider.mode
        if mode == "unauthorized":
            return self._json(401, {"error": {"message": "Invalid API key provided"}})
        if mode == "missing":
            return self._json(404, {"error": {"message": "model not found"}})
        if mode == "html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>gateway</html>")
            return
        if mode == "empty":
            return self._json(200, {"choices": [{"message": {"content": "   "}}]})
        if mode == "mismatch":
            return self._json(200, {"choices": [{"message": {"content": '["一","二"]'}}]})
        if mode == "fenced":
            return self._json(200, {"choices": [{"message": {"content": '```json\n["甲","乙"]\n```'}}]})
        # The happy path: echo the input lines back with a marker, so the pairing
        # between request order and reply order is what the test can see.
        user = body["messages"][-1]["content"]
        try:
            lines = json.loads(user)
        except ValueError:
            return self._json(200, {"choices": [{"message": {"content": "译:" + user}}]})
        return self._json(200, {"choices": [{"message": {"content": json.dumps(
            ["译:" + line for line in lines], ensure_ascii=False)}}]})

    def _json(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

class TranslatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeProvider.mode = "single"
        FakeProvider.seen = []
        self.options = {"base_url": "http://127.0.0.1:%d/v1" % self.server.server_port,
                        "model": "fake-model", "api_key": "sk-test", "target": "zh",
                        "instructions": ""}

    def test_request_is_openai_shaped(self):
        settings.UPDATE = None                        # no-op, keeps flake8 quiet
        translator.translate_line("你好", self.options)
        call = FakeProvider.seen[-1]
        self.assertEqual(call["path"], "/v1/chat/completions")
        self.assertEqual(call["auth"], "Bearer sk-test")
        self.assertEqual(call["body"]["model"], "fake-model")
        roles = [m["role"] for m in call["body"]["messages"]]
        self.assertEqual(roles, ["system", "user"])
        # The target language must be in the system prompt: it is what the model
        # actually obeys, so a missing language would translate into anything.
        self.assertIn("简体中文", call["body"]["messages"][0]["content"])

    def test_a_full_url_is_accepted_as_well_as_a_base(self):
        options = dict(self.options, base_url=self.options["base_url"] + "/chat/completions")
        translator.translate_line("你好", options)
        self.assertEqual(FakeProvider.seen[-1]["path"], "/v1/chat/completions")

    def test_single_line_returns_clean_text(self):
        self.assertEqual(translator.translate_line("你好", self.options), "译:你好")

    def test_batch_keeps_lines_in_step(self):
        lines = ["一", "二", "三"]
        self.assertEqual(translator.translate_lines(lines, self.options),
                         ["译:一", "译:二", "译:三"])

    def test_a_fenced_json_reply_is_unwrapped(self):
        FakeProvider.mode = "fenced"
        self.assertEqual(translator.translate_lines(["一", "二"], self.options), ["甲", "乙"])

    def test_batch_length_mismatch_is_refused_not_misaligned(self):
        # The dangerous failure: pairing a 2-line reply with 3 input lines would
        # silently attach every translation to the wrong line.
        FakeProvider.mode = "mismatch"
        with self.assertRaises(translator.TranslationError) as caught:
            translator.translate_lines(["一", "二", "三"], self.options)
        self.assertIn("行数不一致", str(caught.exception))

    def test_bad_key_names_the_setting_to_fix(self):
        FakeProvider.mode = "unauthorized"
        with self.assertRaises(translator.TranslationError) as caught:
            translator.translate_line("你好", self.options)
        message = str(caught.exception)
        self.assertIn("401", message)
        self.assertIn("API Key", message)

    def test_wrong_url_is_reported_readably(self):
        FakeProvider.mode = "missing"
        with self.assertRaises(translator.TranslationError) as caught:
            translator.translate_line("你好", self.options)
        self.assertIn("接口地址或模型名", str(caught.exception))

    def test_an_html_reply_is_not_reported_as_success(self):
        FakeProvider.mode = "html"
        with self.assertRaises(translator.TranslationError) as caught:
            translator.translate_line("你好", self.options)
        self.assertIn("OpenAI 兼容", str(caught.exception))

    def test_an_empty_reply_is_an_error(self):
        FakeProvider.mode = "empty"
        with self.assertRaises(translator.TranslationError):
            translator.translate_line("你好", self.options)

class FillTests(unittest.TestCase):
    """``fill`` decides which lines to send and where the results land."""

    def setUp(self):
        from saltlrc.document import Document
        self.doc = Document.from_dict({"groups": [
            {"start": 1000, "main": {"text": "一", "words": [], "end": None, "start": None},
             "translation": {"text": "", "words": [], "end": None, "start": None}},
            {"start": 2000, "main": {"text": "二", "words": [], "end": None, "start": None},
             "translation": {"text": "已有", "words": [], "end": None, "start": None}},
            {"start": 3000, "main": {"text": "三", "words": [], "end": None, "start": None},
             "translation": {"text": "", "words": [], "end": None, "start": None}},
        ]})
        self.calls = []

    def fake(self, lines, options):
        self.calls.append(list(lines))
        return ["T:" + line for line in lines]

    def test_one_line_only_touches_that_line(self):
        report = translator.fill(self.doc, {}, index=1, scope="line", translate=self.fake)
        self.assertEqual(report["translated"], 1)
        self.assertEqual(self.calls, [["二"]])
        self.assertEqual([g.translation.text for g in self.doc.groups], ["", "T:二", ""])

    def test_all_scope_translates_every_line_with_text(self):
        translator.fill(self.doc, {}, scope="all", translate=self.fake)
        self.assertEqual([g.translation.text for g in self.doc.groups], ["T:一", "T:二", "T:三"])

    def test_only_missing_skips_lines_that_already_have_a_translation(self):
        translator.fill(self.doc, {}, scope="all", only_missing=True, translate=self.fake)
        self.assertEqual(self.calls, [["一", "三"]])
        self.assertEqual([g.translation.text for g in self.doc.groups], ["T:一", "已有", "T:三"])

    def test_translated_lines_get_the_line_start(self):
        # Without this the exported translation has no time and Salt Player shows
        # it on the wrong line.
        translator.fill(self.doc, {}, scope="all", translate=self.fake)
        self.assertEqual([g.translation.start for g in self.doc.groups], [1000, 2000, 3000])

    def test_a_late_failure_keeps_what_already_succeeded(self):
        # Needs MORE than one chunk, otherwise there is nothing "already
        # succeeded" to keep — the whole request either lands or fails.
        from saltlrc.document import Document
        many = Document.from_dict({"groups": [
            {"start": 1000 * (i + 1), "main": {"text": "l%d" % i, "words": [], "end": None, "start": None},
             "translation": {"text": "", "words": [], "end": None, "start": None}}
            for i in range(translator.MAX_LINES_PER_REQUEST + 1)]})

        def flaky(lines, options):
            if len(lines) < translator.MAX_LINES_PER_REQUEST:
                raise translator.TranslationError("额度用完")
            return ["T:" + line for line in lines]

        report = translator.fill(many, {}, scope="all", translate=flaky)
        self.assertEqual(report["translated"], translator.MAX_LINES_PER_REQUEST)
        self.assertEqual(many.groups[0].translation.text, "T:l0")
        self.assertEqual(many.groups[-1].translation.text, "")
        self.assertTrue(any("额度用完" in note for note in report["notes"]))

    def test_a_failure_on_the_first_chunk_still_raises(self):
        def broken(lines, options):
            raise translator.TranslationError("Key 无效")
        with self.assertRaises(translator.TranslationError):
            translator.fill(self.doc, {}, scope="all", translate=broken)

    def test_out_of_range_index_is_refused(self):
        with self.assertRaises(translator.TranslationError):
            translator.fill(self.doc, {}, index=99, scope="line", translate=self.fake)

class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = scratch("saltlrc-settings-")
        os.environ["LOCALAPPDATA"] = self.dir
        settings.save(settings.DEFAULTS.copy())

    def test_defaults_ship_no_vendor(self):
        # An empty base_url is deliberate: a default would quietly send the
        # user's lyrics to whichever vendor is baked in.
        self.assertEqual(settings.load()["translation"]["base_url"], "")
        self.assertIsNone(settings.translate_options()[0])
        self.assertIn("API Key", settings.translate_options()[1])

    def test_the_key_never_reaches_the_page(self):
        settings.update({"translation": {"api_key": "sk-secret", "model": "m",
                                         "base_url": "http://x/v1"}})
        exposed = settings.public()
        self.assertEqual(exposed["translation"]["api_key"], "")
        self.assertTrue(exposed["translation"]["has_key"])
        self.assertNotIn("sk-secret", json.dumps(exposed, ensure_ascii=False))

    def test_saving_another_field_does_not_wipe_the_key(self):
        settings.update({"translation": {"api_key": "sk-secret", "model": "m",
                                         "base_url": "http://x/v1"}})
        settings.update({"translation": {"model": "other"}})
        self.assertEqual(settings.load()["translation"]["api_key"], "sk-secret")
        self.assertEqual(settings.load()["translation"]["model"], "other")

    def test_clearing_needs_the_explicit_sentinel(self):
        settings.update({"translation": {"api_key": "sk-secret"}})
        settings.update({"translation": {"api_key": settings.CLEAR_KEY}})
        self.assertEqual(settings.load()["translation"]["api_key"], "")

    def test_unknown_keys_are_dropped(self):
        settings.update({"translation": {"api_key": "k", "evil": "payload"},
                         "features": {"followPlayback": False, "backdoor": True},
                         "keys": {"playPause": "KeyP"},
                         "extra_section": {"x": 1}})
        with open(settings.path(), encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertNotIn("evil", raw["translation"])
        self.assertNotIn("backdoor", raw["features"])
        self.assertNotIn("extra_section", raw)
        self.assertFalse(settings.load()["features"]["followPlayback"])

    def test_a_corrupt_file_falls_back_to_defaults(self):
        with open(settings.path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertTrue(settings.load()["features"]["followPlayback"])

    def test_the_file_is_written_where_the_credentials_live(self):
        self.assertEqual(os.path.dirname(settings.path()), settings.store.config_dir())

if __name__ == "__main__":
    unittest.main()
