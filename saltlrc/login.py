"""Netease Cloud Music login, for the three methods the app offers.

Signing in upgrades what the service will hand back -- word-level (YRC) lyric
timings and the full translation track are only served to authenticated
sessions -- so the login flow exists to improve lyric quality, not to gate the
app. Every method here returns a cookie and nothing else; a failed login leaves
lyric fetching exactly as it was.

Three methods, all against the public web endpoints:

* **QR scan** -- ask for a key, show the QR, poll until the phone confirms.
* **Phone + password** -- one request, password sent as an MD5 digest.
* **Phone + SMS code** -- send a code, then verify it.

The QR flow is asynchronous by nature: the key is minted in one request and the
outcome only exists in a later poll, so :func:`qr_wait` owns the polling loop
and the caller just watches the status strings change.
"""

import hashlib
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .tools import qrcode as qrcode_module
from .crypto import weapi

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 20
REFERER = "https://music.163.com/"

# QR poll outcomes, mapped to the states the UI shows.
QR_WAITING = "waiting"          # not scanned yet
QR_SCANNED = "scanned"          # scanned, waiting for the user to confirm
QR_EXPIRED = "expired"          # 网易云答复 800：这个码不再有效，要重新生成
QR_CONFIRMED = "confirmed"      # logged in, cookie is ready
QR_REJECTED = "rejected"        # the user declined on the phone
QR_TIMEOUT = "timeout"          # 本次等待窗口用完；码本身通常仍然有效

# Netease answers 800/801/802/803 for the four QR states.
_QR_CODES = {800: QR_EXPIRED, 801: QR_WAITING, 802: QR_SCANNED, 803: QR_CONFIRMED}


class LoginError(RuntimeError):
    """登录步骤失败，且这条失败要让人看见。"""


class NetworkError(LoginError):
    """连不上网易云（网络层失败）。`qr_wait` 只对这类失败重试。"""


def _context():
    return ssl.create_default_context()


def _request(url, data=None, cookie=None, timeout=TIMEOUT):
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
        "Origin": "https://music.163.com",
    }
    if cookie:
        headers["Cookie"] = cookie
    body = None
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_context()) as response:
            return response.read().decode("utf-8", "replace"), response.headers
    except urllib.error.HTTPError as error:
        raise LoginError("网易云返回 HTTP %s" % error.code) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise NetworkError("无法连接网易云：%s" % error) from error


def _json(url, data=None, cookie=None):
    text, _ = _request(url, data, cookie)
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise LoginError("网易云返回内容无法解析") from error


def _check(payload):
    """Turn a Netease response into a cookie, or raise a readable message."""
    code = payload.get("code")
    if code == 200:
        cookie = payload.get("cookie") or ""
        if not cookie and isinstance(payload.get("data"), dict):
            cookie = payload["data"].get("cookie") or ""
        return cookie
    hints = {
        400: "请求参数有问题，请检查手机号或验证码格式",
        501: "该账号不存在或未注册网易云音乐",
        502: "密码错误",
        503: "账号被限制登录（可能触发了风控，请稍后再试）",
        504: "该账号需要完成安全验证，请在网易云客户端登录一次后重试",
        509: "登录过于频繁，请等待一段时间再试",
    }
    if code in hints:
        raise LoginError(hints[code])
    message = (payload.get("message") or payload.get("msg") or "").strip()
    raise LoginError(message or "登录失败（code=%s）" % code)


def _merge_cookie(existing, fresh):
    """Fold a fresh Set-Cookie string into an existing one.

    A login response only carries the new session keys; the account's other
    cookies may already be in the stored jar. Dropping them would produce a
    cookie that looks valid but is rejected by the lyric endpoint.
    """
    jar = {}
    for source in (existing or "", fresh or ""):
        for part in source.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name, value = name.strip(), value.strip()
            if not value:
                jar.pop(name, None)          # an expiring pair
            else:
                jar[name] = value
    # Netease's write endpoints expect __csrf to accompany a session cookie;
    # dropping it makes later reads fail in a way that looks like an expiry.
    if "__csrf" not in jar and "MUSIC_U" in jar:
        jar["__csrf"] = jar["MUSIC_U"]
    return "; ".join("%s=%s" % (k, v) for k, v in jar.items() if v)



def _normalise_phone(phone):
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) != 11 or not digits.startswith("1"):
        raise LoginError("请输入 11 位手机号")
    return digits


# --------------------------------------------------------------------------
# Method 1: QR scan
# --------------------------------------------------------------------------

QR_CREATE = "https://music.163.com/api/login/qrcode/unikey?type=1"
QR_CHECK = "https://music.163.com/api/login/qrcode/client/login"


def qr_create(cookie=None):
    """Mint a login key and the QR image that carries it.

    ``type=1`` in the URL is the same field as the ``type`` parameter the check
    endpoint needs, and the two must agree or the poll never confirms.
    """
    payload = _json(QR_CREATE, data={"type": 1}, cookie=cookie)
    code = payload.get("code")
    if code != 200:
        raise LoginError(payload.get("message") or "无法获取登录二维码（code=%s）" % code)
    key = payload.get("unikey")
    if not key:
        raise LoginError("网易云没有返回登录密钥")
    url = "https://music.163.com/login?codekey=" + key
    return {
        "key": key,
        "url": url,
        "type": 1,
        # The QR must be dark-on-light: an inverted code is unreadable on many
        # phones, and this is the one screen where that failure is silent.
        # PNG rather than SVG is deliberate: WebView2 (the packaged app's host)
        # renders inline SVG data URIs inconsistently, and a blank code is worse
        # than a heavier payload. The SVG form stays available for the browser.
        "image": qrcode_module.png_data_uri(url, "M", scale=7, border=4),
        "image_type": "image/png",
        "image_svg": qrcode_module.data_uri(url, "M", scale=7, border=4),
        "state": QR_WAITING,
    }


def qr_check(key, cookie=None, login_type=1):
    """Poll once. Returns ``(state, cookie)``; cookie is set when confirmed."""
    payload = _json(QR_CHECK,
                    data={"key": key, "type": login_type},
                    cookie=cookie)
    code = payload.get("code")
    state = _QR_CODES.get(code)
    if state is None:
        message = (payload.get("message") or "").strip()
        if "过期" in message or "失效" in message:
            state = QR_EXPIRED
        elif message:
            return QR_REJECTED, None
        else:
            return QR_WAITING, None
    if state == QR_CONFIRMED:
        merged = _merge_cookie(cookie, payload.get("cookie") or "")
        if not merged:
            raise LoginError("扫码已确认，但网易云没有下发登录凭据")
        return QR_CONFIRMED, merged
    return state, None


def qr_wait(key, cookie=None, login_type=1, timeout=150, interval=2.0, on_state=None):
    """轮询到确认 / 拒绝 / 码过期为止；**等待窗口用完时返回 `QR_TIMEOUT`**。

    `QR_TIMEOUT` 与 `QR_EXPIRED` 是两件事：前者表示"这次等待到此为止"，
    后者表示"网易云说这个码已经失效"。调用方拿到 `QR_TIMEOUT` 应当接着等 ——
    二维码本身在网易云那边的有效期通常比一次等待窗口长。
    """  # noqa: D401
    deadline = time.monotonic() + max(10, timeout)
    last = None
    misses = 0
    while time.monotonic() < deadline:
        try:
            state, fresh = qr_check(key, cookie, login_type)
            misses = 0
        except NetworkError:
            # 连续 3 次连不上才报错；中间的网络抖动不影响这次登录。
            misses += 1
            if misses >= 3:
                raise
            time.sleep(max(0.5, interval))
            continue
        if state != last and on_state is not None:
            on_state(state)
        last = state
        if state == QR_CONFIRMED:
            return QR_CONFIRMED, fresh
        if state == QR_REJECTED:
            return QR_REJECTED, None
        if state == QR_EXPIRED:
            return QR_EXPIRED, None
        time.sleep(max(0.5, interval))
    return QR_TIMEOUT, None


# --------------------------------------------------------------------------
# Method 2: phone + password
# --------------------------------------------------------------------------

LOGIN_CELLPHONE = "https://music.163.com/weapi/w/login/cellphone"


def login_password(phone, password, country_code="86", cookie=None):
    """Sign in with a phone number and password.

    The password is sent as an MD5 digest, which is the wire format the web
    client uses. The plaintext never leaves this function.
    """
    number = _normalise_phone(phone)
    if not password:
        raise LoginError("请输入密码")
    digest = hashlib.md5(str(password).encode("utf-8")).hexdigest()
    payload = _json(LOGIN_CELLPHONE,
                    weapi({
                        "phone": number,
                        "countrycode": str(country_code or "86"),
                        "password": digest,
                        "rememberLogin": "true",
                        "csrf_token": "",
                    }),
                    cookie=cookie)
    fresh = _check(payload)
    return {
        "cookie": _merge_cookie(cookie, fresh),
        "account": number,
        "nickname": (payload.get("profile") or {}).get("nickname") or "",
        "user_id": (payload.get("account") or {}).get("id") or (payload.get("profile") or {}).get("userId"),
        "method": "password",
    }


# --------------------------------------------------------------------------
# Method 3: phone + SMS code
# --------------------------------------------------------------------------

SMS_SEND = "https://music.163.com/weapi/sms/captcha/sent"
SMS_VERIFY = "https://music.163.com/weapi/w/login/cellphone"
SMS_VERIFY_ALT = "https://music.163.com/weapi/sms/captcha/verify"


def sms_send(phone, country_code="86", cookie=None):
    """Ask Netease to text a verification code."""
    number = _normalise_phone(phone)
    payload = _json(SMS_SEND,
                    weapi({
                        "cellphone": number,
                        "ctcode": str(country_code or "86"),
                        "csrf_token": "",
                    }),
                    cookie=cookie)
    code = payload.get("code")
    if code == 200:
        return {"sent": True, "message": "验证码已发送"}
    message = (payload.get("message") or "").strip()
    raise LoginError(message or "验证码发送失败（code=%s）" % code)


def login_sms(phone, captcha, country_code="86", cookie=None):
    """Sign in with the code from :func:`sms_send`."""
    number = _normalise_phone(phone)
    code_text = "".join(ch for ch in str(captcha or "") if ch.isdigit())
    if len(code_text) < 4:
        raise LoginError("请输入收到的验证码")
    payload = _json(SMS_VERIFY,
                    weapi({
                        "phone": number,
                        "countrycode": str(country_code or "86"),
                        "captcha": code_text,
                        "rememberLogin": "true",
                        "csrf_token": "",
                    }),
                    cookie=cookie)
    fresh = _check(payload)
    return {
        "cookie": _merge_cookie(cookie, fresh),
        "account": number,
        "nickname": (payload.get("profile") or {}).get("nickname") or "",
        "user_id": (payload.get("account") or {}).get("id") or (payload.get("profile") or {}).get("userId"),
        "method": "sms",
    }


# --------------------------------------------------------------------------
# Session check
# --------------------------------------------------------------------------

ACCOUNT_INFO = "https://music.163.com/api/nuser/account/get"


def whoami(cookie):
    """Read back the signed-in account, or ``None`` when the cookie is dead.

    A cookie can be present and still be worthless (expired, or half a session),
    so the stored login is validated by asking rather than by trusting the file.
    """
    if not cookie:
        return None
    try:
        payload = _json(ACCOUNT_INFO, cookie=cookie)
    except LoginError:
        return None
    if payload.get("code") not in (200, None):
        return None
    profile = payload.get("profile") or {}
    account = payload.get("account") or {}
    if not profile and not account:
        return None
    return {
        "account": str(account.get("id") or profile.get("userId") or ""),
        "nickname": profile.get("nickname") or "",
        "vip": bool(profile.get("vipType")),
    }
