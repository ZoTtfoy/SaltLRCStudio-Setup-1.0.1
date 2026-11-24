"""HTTP for the lyric sources — layer 2, shared.

The providers keep a thin module-level wrapper each (``_request``, ``_get``,
``_fetch``) rather than calling straight into here. Those names are the seam the
tests replace to exercise retry and status-code mapping without a network.
"""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["ProviderError", "TIMEOUT", "USER_AGENT", "ssl_context", "request", "json_body"]

TIMEOUT = 20

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class ProviderError(RuntimeError):
    """Raised when a source cannot answer; the caller may fall back to another."""


def ssl_context():
    """Certificate verification stays ON."""
    return ssl.create_default_context()


def request(url, params=None, data=None, headers=None, cookie=None, referer=None,
            accept=None, timeout=TIMEOUT, label="歌词源"):
    """One attempt. Returns decoded text; raises :class:`ProviderError`.

    ``label`` is the source's Chinese name; it appears in failure messages.
    """
    final = {"User-Agent": USER_AGENT}
    if referer:
        final["Referer"] = referer
    if accept:
        final["Accept"] = accept
    if cookie:
        final["Cookie"] = cookie
    if headers:
        final.update(headers)

    body = None
    if data is not None:
        final["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode(data).encode("utf-8")

    target = url
    if params:
        target = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

    call = urllib.request.Request(target, data=body, headers=final)
    try:
        with urllib.request.urlopen(call, timeout=timeout, context=ssl_context()) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        raise ProviderError("%s返回 HTTP %s" % (label, error.code)) from error
    except ssl.SSLError as error:
        raise ProviderError("%s的 HTTPS 证书与主机名不匹配：%s" % (label, error)) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ProviderError("无法连接%s：%s" % (label, error)) from error


def raw_text(url, params=None, headers=None, cookie=None, referer=None, accept=None,
             timeout=TIMEOUT):
    """One attempt, transport exceptions left untouched.

    For callers that own their own retry policy (LRCLIB retries 5xx and treats a
    404 as a definitive "no such lyric").
    """
    final = {"User-Agent": USER_AGENT}
    if referer:
        final["Referer"] = referer
    if accept:
        final["Accept"] = accept
    if cookie:
        final["Cookie"] = cookie
    if headers:
        final.update(headers)
    target = url
    if params:
        target = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    call = urllib.request.Request(target, headers=final)
    with urllib.request.urlopen(call, timeout=timeout, context=ssl_context()) as response:
        return response.read().decode("utf-8", "replace")


def json_body(text, label="歌词源", unwrap=None, not_json=None):
    """Decode a JSON reply, tolerating a JSONP callback wrapper.

    ``unwrap`` strips a source's own envelope before parsing; the callback form
    is handled here. ``not_json`` overrides the message for a body that is not
    JSON at all, per source.
    """
    body = (text or "").strip()
    if unwrap:
        body = unwrap(body)
    if not body.startswith(("{", "[")):
        raise ProviderError(not_json or "%s返回了无法解析的内容" % label)
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise ProviderError("%s返回内容无法解析" % label) from error
