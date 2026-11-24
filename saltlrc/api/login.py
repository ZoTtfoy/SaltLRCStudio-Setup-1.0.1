import base64
import io
import json
import os
import platform
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path

from .. import (diagnostics, folders, library, inspect, providers, wordtime,
                trace, translator, login, credentials, edits)
from .. import settings as settings_store
from ..sources import netease
from ..document import Document
from ..parser import parse
from ..repair import decode, to_bytes
from ..validator import validate
from ..exporter import export
from ..bilingual import shift as shift_track
from ..language import identify
from ..alignment import apply_proposals, match_transcript, source_track
from ..acoustic import apply_words, global_offset, refine_words
from ..jobs import Jobs


def handle(self, path, data):
    if path == "/api/login/qr-create":
        payload = login.qr_create()
        # The image is a PNG data URI. The packaged build renders it in
        # WebView2, which does not reliably draw inline SVG.
        self.send(200, payload)
        return True
    if path == "/api/login/qr-check":
        state = login.qr_check(str(data.get("key") or ""))
        if state == login.QR_CONFIRMED:
            return self._finish_login(state, data.get("cookie"))
        self.send(200, {"state": state})
        return True
    if path == "/api/login/qr-wait":
        # Long-poll: the page does not need to poll from JS, and a
        # single blocking call cannot leave a stale timer behind when
        # the dialog closes.
        state = login.qr_wait(str(data.get("key") or ""),
                             timeout=int(data.get("timeout", 150)))
        if state == login.QR_CONFIRMED:
            return self._finish_login(state, data.get("cookie"))
        self.send(200, {"state": state})
        return True
    if path == "/api/login/password":
        phone = str(data.get("phone") or "")
        password = str(data.get("password") or "")
        if not phone or not password:
            raise ValueError("请输入手机号和密码")
        cookie = login.login_password(phone, password,
                                      str(data.get("country_code") or "86"))
        return self._finish_login(login.QR_CONFIRMED, cookie)
    if path == "/api/login/sms-send":
        phone = str(data.get("phone") or "")
        if not phone:
            raise ValueError("请输入手机号")
        login.sms_send(phone, str(data.get("country_code") or "86"))
        self.send(200, {"status": "sent"})
        return True
    if path == "/api/login/sms-verify":
        phone = str(data.get("phone") or "")
        captcha = str(data.get("captcha") or "")
        if not phone or not captcha:
            raise ValueError("请输入手机号和验证码")
        cookie = login.login_sms(phone, captcha,
                                 str(data.get("country_code") or "86"))
        return self._finish_login(login.QR_CONFIRMED, cookie)
    if path == "/api/login/logout":
        credentials.clear(data.get("provider"))
        self.send(200, credentials.status())
        return True
    if path == "/api/login/whoami":
        # Verifies the stored cookie against the account API rather than
        # trusting the file: a saved cookie can be present but expired.
        provider = data.get("provider") or providers.DEFAULT_PROVIDER
        stored = credentials.cookie(provider)
        account = login.whoami(stored) if stored else None
        self.send(200, {
            "provider": provider,
            "stored": bool(stored),
            "account": account,
            # `whoami` returns None both when there is no cookie and when
            # the network is unreachable. Only a non-None answer counts
            # as verified; a network failure must not read as "logged
            # out", which is the distinction `account is None` alone
            # cannot express.
            "verified": account is not None and bool(stored),
            "providers": credentials.status(),
        })
        return True

    return False
