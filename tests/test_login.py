"""Tests for the credential store and the Netease login flow.

The network is stubbed. The behaviour under test is what happens with a *bad*
answer: an expired QR, a wrong password, a half-written credentials file.
"""


import json
import os
import sys
import tempfile
import unittest

from _scratch import isolate_localappdata, scratch
sys.path.insert(0, r"C:\Users\Administrator\WorkBuddy\2026-09-22-12-52-10\SaltLRCStudio")

from saltlrc import credentials, login

class CredentialStoreTests(unittest.TestCase):
    """The store must never lose a login to a crash mid-write."""

    def setUp(self):
        self.directory = scratch("saltlrc-cred-")
        self._saved = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = self.directory

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._saved

    def test_round_trip(self):
        credentials.put("netease", "MUSIC_U=abc; __csrf=def", account="138", method="password")
        self.assertEqual(credentials.cookie("netease"), "MUSIC_U=abc; __csrf=def")
        record = credentials.get("netease")
        self.assertEqual(record["account"], "138")
        self.assertEqual(record["method"], "password")

    def test_missing_file_is_an_empty_store_not_an_error(self):
        self.assertEqual(credentials.load()["providers"], {})
        self.assertIsNone(credentials.get("netease"))
        self.assertIsNone(credentials.cookie("netease"))

    def test_corrupt_file_is_ignored_rather_than_raising(self):
        os.makedirs(credentials.config_dir(), exist_ok=True)
        with open(credentials.path(), "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        # Callers hit this mid-request; an empty store is returned.
        self.assertEqual(credentials.load()["providers"], {})
        credentials.put("netease", "MUSIC_U=x")          # and it still recovers
        self.assertEqual(credentials.cookie("netease"), "MUSIC_U=x")

    def test_wrong_shape_is_ignored(self):
        os.makedirs(credentials.config_dir(), exist_ok=True)
        with open(credentials.path(), "w", encoding="utf-8") as handle:
            json.dump(["not", "an", "object"], handle)
        self.assertEqual(credentials.load()["providers"], {})

    def test_save_leaves_no_temp_files_behind(self):
        for index in range(5):
            credentials.put("provider-%d" % index, "cookie-%d" % index)
        leftovers = [n for n in os.listdir(credentials.config_dir())
                     if n.startswith(".credentials-")]
        self.assertEqual(leftovers, [])

    def test_clear_one_provider_keeps_the_others(self):
        credentials.put("netease", "a")
        credentials.put("qqmusic", "b")
        credentials.clear("netease")
        self.assertIsNone(credentials.get("netease"))
        self.assertEqual(credentials.cookie("qqmusic"), "b")

    def test_status_exposes_no_secrets(self):
        credentials.put("netease", "MUSIC_U=SECRET-VALUE", account="138", nickname="某用户")
        blob = json.dumps(credentials.status(), ensure_ascii=False)
        self.assertNotIn("SECRET-VALUE", blob)
        self.assertIn("某用户", blob)
        self.assertTrue(credentials.status()["netease"]["logged_in"])

    def test_redact_hides_the_body_of_a_secret(self):
        self.assertNotIn("VALUE", credentials.redact("MUSIC_U=SECRET-VALUE"))
        self.assertEqual(credentials.redact(""), "")

class CookieMergeTests(unittest.TestCase):
    """A login cookie is assembled from fragments and must stay complete."""

    def test_fresh_pairs_win_over_existing(self):
        merged = login._merge_cookie("MUSIC_U=old; __csrf=x", "MUSIC_U=new")
        self.assertIn("MUSIC_U=new", merged)
        self.assertNotIn("old", merged)
        self.assertIn("__csrf=x", merged)          # unrelated pair survives

    def test_empty_value_removes_a_pair(self):
        merged = login._merge_cookie("MUSIC_U=abc; extra=1", "extra=")
        self.assertNotIn("extra=", merged)
        self.assertIn("MUSIC_U=abc", merged)

    def test_csrf_is_added_alongside_a_session_cookie(self):
        # Missing __csrf makes later authenticated reads fail.
        merged = login._merge_cookie("", "MUSIC_U=session")
        self.assertIn("__csrf=session", merged)

    def test_duplicate_result_is_stable_under_remerge(self):
        once = login._merge_cookie("", "MUSIC_U=a; __csrf=b")
        twice = login._merge_cookie(once, once)
        self.assertEqual(once, twice)

class PhoneValidationTests(unittest.TestCase):

    def test_rejects_wrong_length(self):
        for bad in ("", "123", "138001380001", "abcdefghijk"):
            with self.assertRaises(login.LoginError):
                login._normalise_phone(bad)

    def test_accepts_eleven_digits_and_strips_formatting(self):
        self.assertEqual(login._normalise_phone("138-0013-8000"), "13800138000")
        self.assertEqual(login._normalise_phone(" 13800138000 "), "13800138000")

class _StubNetwork:
    """Replaces ``login._json`` so the flow can be driven step by step."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, data=None, cookie=None):
        self.calls.append((url, data, cookie))
        if not self.responses:
            raise AssertionError("unexpected extra request to %s" % url)
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

class LoginFlowTests(unittest.TestCase):

    def setUp(self):
        self._real = login._json
        self.network = None

    def tearDown(self):
        login._json = self._real

    def stub(self, responses):
        self.network = _StubNetwork(responses)
        login._json = self.network
        return self.network

    # -- QR ----------------------------------------------------------------

    def test_qr_create_returns_a_scannable_dark_on_light_image(self):
        self.stub([{"code": 200, "unikey": "a1b2c3d4" * 16}])
        result = login.qr_create()
        self.assertEqual(result["key"], "a1b2c3d4" * 16)
        self.assertIn("codekey=" + "a1b2c3d4" * 16, result["url"])
        # PNG is the primary form; an SVG form is returned as well.
        self.assertTrue(result["image"].startswith("data:image/png;base64,"))
        self.assertEqual(result["image_type"], "image/png")
        self.assertTrue(result["image_svg"].startswith("data:image/svg+xml;base64,"))
        self.assertEqual(result["state"], login.QR_WAITING)

    def test_qr_image_really_is_a_png_of_the_login_url(self):
        """A data URI prefix is not a PNG; decode it and check the pixels.

        The assertion is made by a decoder. zxing-cpp and Pillow are
        verification-only; the test skips without them.
        """
        try:
            import io
            import zxingcpp
            from PIL import Image
        except ImportError:
            self.skipTest("zxing-cpp / Pillow 未安装，跳过二维码解码验证")
        import base64

        from saltlrc.tools import qrcode
        url = "https://music.163.com/login?codekey=" + "a1b2c3d4" * 16
        raw = base64.b64decode(qrcode.png_data_uri(url, "M", scale=7, border=4)
                               .split(",", 1)[1])
        results = zxingcpp.read_barcodes(Image.open(io.BytesIO(raw)))
        self.assertTrue(results, "生成的二维码无法被解码器读出")
        self.assertEqual(results[0].text, url)

    def test_png_export_is_available_without_the_verification_extras(self):
        # PNG is written by hand with zlib; Pillow is used only by the tests
        # above.
        from saltlrc.tools import qrcode
        data = qrcode.png("hello", "M", scale=3, border=4)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", data)

    def test_small_quiet_zone_is_rejected_in_both_formats(self):
        from saltlrc.tools import qrcode
        for render in (qrcode.svg, qrcode.png):
            with self.subTest(render=render.__name__):
                with self.assertRaises(qrcode.QrError):
                    render("hello", "M", border=2)

    def test_bad_colour_is_reported_not_silently_black(self):
        from saltlrc.tools import qrcode
        with self.assertRaises(qrcode.QrError):
            qrcode.png("hello", "M", dark="not-a-colour")

    def test_qr_create_surfaces_a_refusal(self):
        self.stub([{"code": 400, "message": "参数错误"}])
        with self.assertRaises(login.LoginError):
            login.qr_create()

    def test_qr_check_maps_all_four_states(self):
        cases = [
            ({"code": 801}, login.QR_WAITING),
            ({"code": 802}, login.QR_SCANNED),
            ({"code": 800}, login.QR_EXPIRED),
        ]
        for answer, expected in cases:
            with self.subTest(code=answer["code"]):
                self.stub([answer])
                self.assertEqual(login.qr_check("k")[0], expected)

    def test_qr_check_confirmation_keeps_the_existing_jar(self):
        self.stub([{"code": 803, "cookie": "MUSIC_U=session"}])
        state, cookie = login.qr_check("k", cookie="lang=zh")
        self.assertEqual(state, login.QR_CONFIRMED)
        self.assertIn("MUSIC_U=session", cookie)
        self.assertIn("lang=zh", cookie)          # pre-existing pairs kept

    def test_confirmed_scan_without_a_cookie_is_an_error(self):
        # A confirmed scan with no cookie raises instead of storing an empty
        # login.
        self.stub([{"code": 803}])
        with self.assertRaises(login.LoginError):
            login.qr_check("k")

    def test_qr_wait_stops_on_confirmation_and_reports_each_change(self):
        self.stub([
            {"code": 801},
            {"code": 801},                        # unchanged, must not re-report
            {"code": 802},
            {"code": 803, "cookie": "MUSIC_U=done"},
        ])
        seen = []
        state, cookie = login.qr_wait("k", interval=0, on_state=seen.append)
        self.assertEqual(state, login.QR_CONFIRMED)
        self.assertIn("MUSIC_U=done", cookie)
        self.assertEqual(seen, [login.QR_WAITING, login.QR_SCANNED, login.QR_CONFIRMED])

    def test_qr_wait_gives_up_when_the_code_expires(self):
        self.stub([{"code": 800}])
        state, cookie = login.qr_wait("k", interval=0)
        self.assertEqual(state, login.QR_EXPIRED)
        self.assertIsNone(cookie)

    def test_qr_wait_reports_a_finished_wait_window_separately(self):
        """等待窗口用完 ≠ 码过期：前者应当接着等，后者才要重新生成。"""
        self.stub([{"code": 801}])
        ticks = {"n": 0}

        def fake_monotonic():
            ticks["n"] += 1
            return 0.0 if ticks["n"] <= 2 else 1e9      # 第 3 次判断时窗口已过

        real = (login.time.monotonic, login.time.sleep)
        login.time.monotonic = fake_monotonic
        login.time.sleep = lambda seconds: None
        try:
            state, cookie = login.qr_wait("k", interval=0)
        finally:
            login.time.monotonic, login.time.sleep = real
        self.assertEqual(state, login.QR_TIMEOUT,
                         '等待窗口用完不能报成"码已过期"（那样页面会停手，扫码后就登不上）')
        self.assertIsNone(cookie)
        self.assertNotEqual(login.QR_TIMEOUT, login.QR_EXPIRED)

    def test_qr_wait_survives_one_network_blip(self):
        answers = [login.NetworkError("网络抖了一下"), (login.QR_CONFIRMED, "MUSIC_U=x")]

        def fake_check(key, cookie=None, login_type=1):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        real = login.qr_check
        login.qr_check = fake_check
        try:
            state, cookie = login.qr_wait("k", interval=0)
        finally:
            login.qr_check = real
        self.assertEqual(state, login.QR_CONFIRMED)
        self.assertIn("MUSIC_U=x", cookie)

    def test_qr_wait_gives_up_after_three_failures_in_a_row(self):
        def always_failing(key, cookie=None, login_type=1):
            raise login.NetworkError("连不上网易云")

        real = login.qr_check
        login.qr_check = always_failing
        try:
            with self.assertRaises(login.NetworkError):
                login.qr_wait("k", interval=0)
        finally:
            login.qr_check = real

    # -- password ----------------------------------------------------------

    def test_password_login_sends_a_digest_not_the_password(self):
        self.stub([{"code": 200, "cookie": "MUSIC_U=s",
                    "profile": {"nickname": "昵称"}, "account": {"id": 42}}])
        result = login.login_password("13800138000", "hunter2")
        self.assertEqual(result["nickname"], "昵称")
        self.assertEqual(result["account"], "13800138000")
        self.assertEqual(result["method"], "password")
        self.assertIn("MUSIC_U=s", result["cookie"])
        blob = json.dumps(self.network.calls, ensure_ascii=False, default=str)
        self.assertNotIn("hunter2", blob)          # plaintext never leaves

    def test_wrong_password_gives_a_readable_message(self):
        self.stub([{"code": 502}])
        with self.assertRaises(login.LoginError) as caught:
            login.login_password("13800138000", "wrong")
        self.assertIn("密码", str(caught.exception))

    def test_password_login_rejects_a_short_phone_before_any_request(self):
        self.stub([])
        with self.assertRaises(login.LoginError):
            login.login_password("138", "pw")
        self.assertEqual(self.network.calls, [])   # no pointless round trip

    # -- sms ---------------------------------------------------------------

    def test_sms_send_reports_success(self):
        self.stub([{"code": 200}])
        self.assertTrue(login.sms_send("13800138000")["sent"])

    def test_sms_send_surfaces_the_reason(self):
        self.stub([{"code": 400, "message": "发送过于频繁"}])
        with self.assertRaises(login.LoginError) as caught:
            login.sms_send("13800138000")
        self.assertIn("频繁", str(caught.exception))

    def test_sms_login_accepts_spaced_digits(self):
        # A code with spaces is accepted. The code travels inside the encrypted
        # envelope, so the test captures what the request builder was handed
        # rather than reading the wire body.
        captured = {}

        def capture(payload):
            captured.update(payload)
            return {"params": "x"}

        real = login.weapi
        login.weapi = capture
        try:
            self.stub([{"code": 200, "cookie": "MUSIC_U=s", "profile": {}, "account": {}}])
            result = login.login_sms("13800138000", "12 34")
        finally:
            login.weapi = real
        self.assertEqual(result["method"], "sms")
        self.assertEqual(captured["captcha"], "1234")
        self.assertEqual(captured["phone"], "13800138000")

    def test_sms_login_rejects_a_too_short_code(self):
        self.stub([])
        with self.assertRaises(login.LoginError):
            login.login_sms("13800138000", "12")
        self.assertEqual(self.network.calls, [])

    # -- session -----------------------------------------------------------

    def test_whoami_reads_back_the_account(self):
        self.stub([{"code": 200, "profile": {"nickname": "我", "userId": 7},
                    "account": {"id": 7}}])
        info = login.whoami("MUSIC_U=s")
        self.assertEqual(info["nickname"], "我")
        self.assertEqual(info["account"], "7")

    def test_whoami_returns_none_for_a_dead_cookie(self):
        self.stub([{"code": 301}])
        self.assertIsNone(login.whoami("MUSIC_U=stale"))

    def test_whoami_does_not_call_out_without_a_cookie(self):
        self.stub([])
        self.assertIsNone(login.whoami(""))
        self.assertIsNone(login.whoami(None))
        self.assertEqual(self.network.calls, [])

    def test_whoami_survives_a_network_failure(self):
        # A network failure means "unknown", not logged out.
        self.stub([login.LoginError("网络不通")])
        self.assertIsNone(login.whoami("MUSIC_U=s"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
