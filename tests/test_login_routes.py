"""HTTP-level tests for the login and credential routes.

``test_login.py`` drives the login *module* and ``test_server.py`` covers the
editing workflow; this file covers what the *routes* do with the cookie — the
JSON reply, and what is saved after a confirmed scan.

The login module is stubbed for most cases so the tests stay offline and
deterministic.
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from saltlrc import credentials, login, providers
from saltlrc.server import Handler


class _Stub:
    """Replaces one function on a module for the duration of a test."""

    def __init__(self, module, name, replacement):
        self.module, self.name, self.replacement = module, name, replacement
        self.original = getattr(module, name)

    def __enter__(self):
        setattr(self.module, self.name, self.replacement)
        return self

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.original)
        return False


class LoginRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%s" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        # credentials.path() and diagnostics.logs_dir() both derive from
        # LOCALAPPDATA; a temp directory is used here.
        self._old_local = os.environ.get("LOCALAPPDATA")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["LOCALAPPDATA"] = self._tmp.name
        credentials.clear()

    def tearDown(self):
        credentials.clear()
        if self._old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._old_local
        # The diagnostics log file is still open by the handler that wrote it;
        # the cleanup is allowed to fail on Windows.
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            self.addCleanup(shutil.rmtree, self._tmp.name, True)

    def post(self, route, body):
        request = urllib.request.Request(self.url + "/api/" + route,
                                         json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
        return urllib.request.urlopen(request)

    def post_json(self, route, body):
        with self.post(route, body) as response:
            return json.loads(response.read().decode())

    def post_status(self, route, body):
        """Return ``(status, payload)`` for a call that may be rejected."""
        try:
            with self.post(route, body) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode())

    def get_json(self, route):
        with urllib.request.urlopen(self.url + route) as response:
            return json.loads(response.read().decode())

    # ---- capabilities / catalogue ----

    def test_capabilities_list_every_provider_with_metadata(self):
        data = self.get_json("/api/capabilities")
        self.assertEqual(data["providers"], providers.names())
        catalogue = {entry["id"]: entry for entry in data["provider_catalogue"]}
        self.assertEqual(set(catalogue), set(providers.names()))
        # The dropdown is built from this: it needs a label and the login hint.
        for entry in catalogue.values():
            self.assertTrue(entry["label"])
            self.assertIn("needs_login", entry)

    def test_credentials_status_never_contains_the_cookie_value(self):
        credentials.put("netease", "MUSIC_U=SECRETVALUE; __csrf=abc")
        data = self.get_json("/api/credentials")
        self.assertNotIn("SECRETVALUE", json.dumps(data))
        self.assertTrue(data["netease"]["logged_in"])

    # ---- QR login ----

    def test_qr_create_returns_a_png_data_uri(self):
        payload = {"key": "k", "url": "https://music.163.com/login?codekey=k",
                   "image": "data:image/png;base64,iVBORw0KGgo=", "state": login.QR_WAITING}
        with _Stub(login, "qr_create", lambda cookie=None: payload):
            data = self.post_json("login/qr-create", {})
        self.assertEqual(data["key"], "k")
        self.assertTrue(data["image"].startswith("data:image/png;base64,"))

    def test_a_confirmed_scan_saves_the_cookie_and_reports_status_only(self):
        with _Stub(login, "qr_check", lambda key, cookie=None, login_type=1: login.QR_CONFIRMED):
            data = self.post_json("login/qr-check",
                                  {"key": "k", "cookie": "MUSIC_U=TOKEN; __csrf=x"})
        self.assertEqual(data["state"], login.QR_CONFIRMED)
        self.assertNotIn("TOKEN", json.dumps(data))
        self.assertEqual(credentials.cookie("netease"), "MUSIC_U=TOKEN; __csrf=x")

    def test_a_confirmed_scan_without_a_cookie_is_an_error_not_an_empty_login(self):
        # An empty login is not stored; the route replies 400.
        with _Stub(login, "qr_check", lambda key, cookie=None, login_type=1: login.QR_CONFIRMED):
            status, payload = self.post_status("login/qr-check", {"key": "k"})
        self.assertEqual(status, 400)
        self.assertIn("没有拿到凭据", payload["error"])
        self.assertFalse(credentials.get("netease"))

    def test_a_pending_scan_is_reported_without_saving_anything(self):
        with _Stub(login, "qr_check", lambda key, cookie=None, login_type=1: login.QR_WAITING):
            data = self.post_json("login/qr-check", {"key": "k"})
        self.assertEqual(data, {"state": login.QR_WAITING})
        self.assertFalse(credentials.get("netease"))

    def test_an_expired_code_is_reported_so_the_page_can_refresh(self):
        with _Stub(login, "qr_check", lambda key, cookie=None, login_type=1: login.QR_EXPIRED):
            data = self.post_json("login/qr-check", {"key": "k"})
        self.assertEqual(data["state"], login.QR_EXPIRED)

    # ---- password / sms ----

    def test_password_login_saves_the_returned_cookie(self):
        recorded = {}

        def fake(phone, password, country_code="86", cookie=None):
            recorded["phone"] = phone
            recorded["password"] = password
            return "MUSIC_U=FROM_PASSWORD"

        with _Stub(login, "login_password", fake):
            data = self.post_json("login/password",
                                  {"phone": "13800000000", "password": "hunter2"})
        self.assertEqual(data["state"], login.QR_CONFIRMED)
        self.assertEqual(credentials.cookie("netease"), "MUSIC_U=FROM_PASSWORD")
        # The route passes the password through to the module, which hashes it.
        self.assertEqual(recorded["password"], "hunter2")
        self.assertNotIn("FROM_PASSWORD", json.dumps(data))

    def test_password_login_requires_both_fields(self):
        status, payload = self.post_status("login/password", {"phone": "13800000000"})
        self.assertEqual(status, 400)
        self.assertIn("密码", payload["error"])

    def test_sms_send_requires_a_phone(self):
        status, payload = self.post_status("login/sms-send", {})
        self.assertEqual(status, 400)
        self.assertIn("手机号", payload["error"])

    def test_sms_send_forwards_the_country_code(self):
        seen = {}
        with _Stub(login, "sms_send",
                   lambda phone, country_code="86", cookie=None: seen.update(
                       phone=phone, cc=country_code)):
            data = self.post_json("login/sms-send", {"phone": "13800000000", "country_code": "852"})
        self.assertEqual(data, {"status": "sent"})
        self.assertEqual(seen, {"phone": "13800000000", "cc": "852"})

    def test_sms_verify_saves_the_cookie(self):
        with _Stub(login, "login_sms",
                   lambda phone, captcha, country_code="86", cookie=None: "MUSIC_U=FROM_SMS"):
            data = self.post_json("login/sms-verify",
                                  {"phone": "13800000000", "captcha": "1234"})
        self.assertEqual(data["state"], login.QR_CONFIRMED)
        self.assertEqual(credentials.cookie("netease"), "MUSIC_U=FROM_SMS")

    def test_sms_verify_requires_both_fields(self):
        status, payload = self.post_status("login/sms-verify", {"phone": "13800000000"})
        self.assertEqual(status, 400)
        self.assertIn("验证码", payload["error"])

    # ---- whoami / logout ----

    def test_whoami_verifies_the_stored_cookie_instead_of_trusting_the_file(self):
        credentials.put("netease", "MUSIC_U=STALE")
        with _Stub(login, "whoami", lambda cookie: None):
            data = self.post_json("login/whoami", {})
        self.assertEqual(data["provider"], "netease")
        # The cookie is still on disk, but it did not verify.
        self.assertTrue(data["stored"])
        self.assertFalse(data["verified"])
        self.assertIsNone(data["account"])

    def test_whoami_reports_the_account_when_the_cookie_is_live(self):
        credentials.put("netease", "MUSIC_U=LIVE")
        with _Stub(login, "whoami", lambda cookie: {"nickname": "八哥", "userId": 42}):
            data = self.post_json("login/whoami", {})
        self.assertTrue(data["verified"])
        self.assertEqual(data["account"]["nickname"], "八哥")

    def test_whoami_without_a_stored_cookie_does_not_call_the_network(self):
        called = []
        with _Stub(login, "whoami", lambda cookie: called.append(cookie)):
            data = self.post_json("login/whoami", {})
        self.assertEqual(called, [])
        self.assertFalse(data["stored"])
        self.assertFalse(data["verified"])

    def test_logout_removes_the_credential(self):
        credentials.put("netease", "MUSIC_U=BYE")
        data = self.post_json("login/logout", {})
        # status() only lists providers that have a record; a cleared login
        # disappears.
        self.assertNotIn("netease", data)
        self.assertIsNone(credentials.cookie("netease"))


if __name__ == "__main__":
    unittest.main()
