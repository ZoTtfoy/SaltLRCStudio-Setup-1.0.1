"""Interaction trace: the record, and the stall verdict.

A gesture that produced NO observation is reported as having no effect,
detectable without knowing what the handler was supposed to do.
"""


import unittest

from saltlrc import trace

from _scratch import isolate_localappdata, scratch
class RecordTests(unittest.TestCase):
    def setUp(self):
        trace.clear()

    def test_events_keep_their_order_and_sequence(self):
        trace.record("gesture", "click #applyWords", "BUTTON「应用」", "/library", t=100)
        trace.record("api-start", "export", None, "/library", t=140)
        trace.record("api-end", "export", "200 · 40ms", "/library", t=180)
        data = trace.snapshot()
        self.assertEqual([e["kind"] for e in data["events"]],
                         ["gesture", "api-start", "api-end"])
        # `n` is a process-wide counter; assert the contract (contiguous and
        # increasing), not an absolute start value.
        numbers = [e["n"] for e in data["events"]]
        self.assertEqual(numbers, list(range(numbers[0], numbers[0] + 3)))
        self.assertEqual(data["total"], 3)

    def test_recording_never_raises_on_junk(self):
        # Recording accepts junk without raising.
        self.assertIsNotNone(trace.record("gesture", object(), {"a": 1}))
        self.assertEqual(len(trace.snapshot()["events"]), 1)

    def test_the_buffer_drops_the_oldest(self):
        for index in range(trace.LIMIT + 25):
            trace.record("step", "e%d" % index)
        data = trace.snapshot(limit=trace.LIMIT)
        self.assertEqual(len(data["events"]), trace.LIMIT)
        self.assertEqual(data["total"], trace.LIMIT + 25)
        self.assertEqual(data["events"][-1]["name"], "e%d" % (trace.LIMIT + 24))

class StallTests(unittest.TestCase):
    def analyse(self, events):
        return trace.analyse(events, stall_ms=900)

    def test_a_gesture_with_an_effect_is_not_stalled(self):
        out = self.analyse([
            {"kind": "gesture", "name": "click #closeWordEdit", "t": 1000},
            {"kind": "step", "name": "已取消逐字编辑", "t": 1010},
        ])
        self.assertEqual(out["gestures"], 1)
        self.assertEqual(out["stalled"], [])

    def test_a_gesture_with_nothing_after_it_is_stalled(self):
        # The click happens and nothing observable follows.
        out = self.analyse([
            {"kind": "gesture", "name": "click #closeWordEdit", "t": 1000},
            {"kind": "gesture", "name": "click #closeWordEdit", "t": 2000},
        ])
        self.assertEqual(len(out["stalled"]), 2)
        self.assertEqual(out["stalled"][0]["name"], "click #closeWordEdit")

    def test_an_api_call_counts_even_before_it_finishes(self):
        # api-start counts as an observation.
        out = self.analyse([
            {"kind": "gesture", "name": "click #search", "t": 1000},
            {"kind": "api-start", "name": "search", "t": 1040},
        ])
        self.assertEqual(out["stalled"], [])

    def test_a_late_effect_is_reported_with_its_delay(self):
        out = self.analyse([
            {"kind": "gesture", "name": "click #save", "t": 1000},
            {"kind": "step", "name": "已保存", "t": 2500},
        ])
        self.assertEqual(len(out["stalled"]), 1)
        self.assertEqual(out["stalled"][0]["late_ms"], 1500)

    def test_observations_belong_to_the_gesture_before_them(self):
        out = self.analyse([
            {"kind": "gesture", "name": "click A", "t": 1000},
            {"kind": "step", "name": "A 的效果", "t": 1010},
            {"kind": "gesture", "name": "click B", "t": 2000},
        ])
        self.assertEqual([item["name"] for item in out["stalled"]], ["click B"])

    def test_errors_are_collected_for_the_report(self):
        out = self.analyse([
            {"kind": "gesture", "name": "click #apply", "t": 1000},
            {"kind": "error", "name": "export", "detail": "请求失败：500", "t": 1100},
        ])
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("500", out["errors"][0]["detail"])
        self.assertEqual(out["stalled"], [])          # the error IS an observation

class TimelineTests(unittest.TestCase):
    """One clock for every event; it must survive being sent in pieces.

    The page flushes entries in batches — on gesture boundaries *and* on a timer —
    so a gesture and the observation that follows it can travel in two different
    requests.
    """

    def setUp(self):
        trace.clear()

    def test_epoch_timestamps_are_kept_verbatim(self):
        trace.record_batch([{"kind": "step", "name": "x", "t": 1700000000000}])
        event = trace.snapshot()["events"][0]
        self.assertEqual(event["t"], 1700000000000)
        self.assertNotIn("relative", event)

    def test_spacing_survives_two_separate_batches(self):
        # Two batches keep their original spacing: the interval between the click
        # and the "no effect" note is preserved.
        base = 1700000000000
        trace.record_batch([{"kind": "gesture", "name": "click #dead", "t": base}],
                           now=base + 400)
        trace.record_batch([{"kind": "step", "name": "看起来没有反应",
                             "t": base + 954}], now=base + 980)
        events = trace.snapshot()["events"]
        self.assertEqual(events[1]["t"] - events[0]["t"], 954)
        out = trace.analyse(events, stall_ms=900)
        self.assertEqual([item["name"] for item in out["stalled"]], ["click #dead"])

    def test_a_page_relative_value_is_marked_not_invented(self):
        trace.record_batch([{"kind": "gesture", "name": "click", "t": 2353}],
                           now=1700000000000.0)
        event = trace.snapshot()["events"][0]
        self.assertEqual(event["t"], 1700000000000)      # arrival time
        self.assertEqual(event["since"], 2353)           # the raw value, kept
        self.assertTrue(event["relative"])               # flagged as unusable

    def test_two_pages_interleave_by_their_own_clocks(self):
        # Each page stamps its own `Date.now()`; both are epoch values on one
        # machine.
        page_a = [{"kind": "gesture", "name": "click A", "t": 1700000000000,
                   "page": "/studio.html"},
                  {"kind": "step", "name": "A 的效果", "t": 1700000000050,
                   "page": "/studio.html"}]
        page_b = [{"kind": "gesture", "name": "click B", "t": 1700000005000,
                   "page": "/library"},
                  {"kind": "step", "name": "B 的效果", "t": 1700000005050,
                   "page": "/library"}]
        trace.record_batch(page_a)
        trace.record_batch(page_b)
        events = trace.snapshot()["events"]
        self.assertEqual([item["t"] for item in events],
                         [1700000000000, 1700000000050, 1700000005000, 1700000005050])
        out = trace.analyse(events, stall_ms=900)
        self.assertEqual([item["name"] for item in out["stalled"]], [])

    def test_record_batch_never_raises_on_junk(self):
        trace.record_batch([None, "nonsense", {"kind": None}, {}, {"t": "abc"}])
        self.assertIsInstance(trace.snapshot()["events"], list)

    def test_the_report_prints_a_readable_time(self):
        trace.record_batch([{"kind": "gesture", "name": "click #close",
                             "t": 1700000000000}])
        self.assertRegex(trace.export_text(), r"\d\d:\d\d:\d\d\.\d\d\d")

class ReportTests(unittest.TestCase):
    def setUp(self):
        trace.clear()

    def test_the_text_report_names_the_stalled_gestures(self):
        trace.record("gesture", "click #closeWordEdit", "BUTTON「取消」", "/library", t=10)
        text = trace.export_text()
        self.assertIn("click #closeWordEdit", text)
        self.assertIn("点了却没有任何反应的", text)
        # The stalled gesture is listed in that section.
        head, _, tail = text.partition("== 点了却没有任何反应的（最可疑） ==")
        self.assertIn("closeWordEdit", tail)

    def test_the_json_dump_is_parseable(self):
        import json
        trace.record("gesture", "click #x", None, "/library", t=1)
        data = json.loads(trace.dump_json())
        self.assertEqual(data["analysis"]["gestures"], 1)
        self.assertEqual(len(data["stalled"] if "stalled" in data else data["analysis"]["stalled"]), 1)

class EndpointTests(unittest.TestCase):
    """The page posts events; the reply carries the verdict."""

    @classmethod
    def setUpClass(cls):
        import os
        import tempfile
        isolate_localappdata("saltlrc-trace-")
        import json
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        from saltlrc.server import Handler

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

        def post(route, payload):
            request = urllib.request.Request(
                cls.url + route, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                # Surface the server's own message instead of a bare "400".
                raise AssertionError("%s -> %s: %s" % (route, error.code,
                                                       error.read().decode("utf-8", "replace")))

        cls.post = staticmethod(post)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_posting_events_returns_the_verdict(self):
        self.post("/api/trace/clear", {})
        reply = self.post("/api/trace", {"events": [
            {"kind": "gesture", "name": "click #closeWordEdit", "detail": "BUTTON「取消」", "t": 10},
            {"kind": "gesture", "name": "click #applyWords", "t": 20},
            {"kind": "step", "name": "已应用逐字时间", "t": 30},
        ]})
        self.assertEqual(reply["analysis"]["gestures"], 2)
        # The first click had no observation before the second click: stalled.
        self.assertEqual([item["name"] for item in reply["analysis"]["stalled"]],
                         ["click #closeWordEdit"])

    def test_the_record_is_readable_without_posting_again(self):
        self.post("/api/trace/clear", {})
        self.post("/api/trace", {"event": {"kind": "gesture", "name": "click #x", "t": 1}})
        reply = self.post("/api/trace", {})
        self.assertEqual(len(reply["events"]), 1)
        self.assertEqual(reply["analysis"]["gestures"], 1)

if __name__ == "__main__":
    unittest.main()
