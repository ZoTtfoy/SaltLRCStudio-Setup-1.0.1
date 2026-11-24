"""AI translation over any OpenAI-compatible chat-completions endpoint.

One adapter covers DeepSeek, OpenAI, 智谱, 通义, a local Ollama/LM Studio server
and anything else that speaks the same protocol: the user supplies a base URL, a
model name and a key, and the request shape below is identical for all of them.
There is no per-vendor code — a vendor table would be a second place to keep up
to date, and it would still not cover the next endpoint.

Two design points:

* **The key travels with the request, never through the page.** The browser asks
  this module to translate; it never sees the key (see ``settings.public``).
* **Errors are translated too.** A raw HTTP status tells the user nothing about
  what to fix, so 401/404/429/timeout are mapped to a sentence that names the
  setting to change.
"""

import json
import urllib.error
import urllib.request

TIMEOUT = 60
MAX_LINES_PER_REQUEST = 40

LANGUAGES = {
    "zh": "简体中文", "zh-tw": "繁體中文", "en": "English", "ja": "日本語",
    "ko": "한국어", "fr": "français", "de": "Deutsch", "es": "español",
    "ru": "русский", "pt": "português", "it": "italiano", "th": "ไทย",
}

BASE_PROMPT = (
    "You translate song lyrics. Translate the user's line into {language}.\n"
    "Rules:\n"
    "- Output ONLY the translation. No quotes, no romanisation, no notes.\n"
    "- Keep it singable: match the original's length and tone where you can.\n"
    "- Never merge or split lines; one input line gives exactly one output line.\n"
    "- Leave names, placeholders and emoji as they are.\n"
)


class TranslationError(RuntimeError):
    """Anything the user can act on: bad key, bad URL, rate limit, timeout."""


def _endpoint(base_url):
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise TranslationError("接口地址为空")
    # Accept both the base ("https://api.deepseek.com/v1") and the full path —
    # people paste either one, and rejecting the second is a needless papercut.
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def _describe(error, base_url):
    if isinstance(error, urllib.error.HTTPError):
        detail = ""
        try:
            body = json.loads(error.read().decode("utf-8", "replace"))
            detail = (body.get("error") or {}).get("message") or body.get("message") or ""
        except Exception:
            pass
        hint = {401: "API Key 无效或已过期", 403: "这个 Key 没有权限调用该模型",
                404: "接口地址或模型名不对（地址通常以 /v1 结尾）",
                429: "请求太频繁或额度用完"}.get(error.code, "")
        parts = ["HTTP %d" % error.code, hint, detail]
        return "翻译失败：" + "；".join(p for p in parts if p)
    if isinstance(error, urllib.error.URLError):
        return "翻译失败：连不上 %s（%s）" % (base_url, error.reason)
    if isinstance(error, TimeoutError):
        return "翻译失败：接口 %d 秒没有响应，可能是网络或服务端太慢" % TIMEOUT
    return "翻译失败：%s" % error


def _post(section, messages):
    payload = {"model": section["model"], "messages": messages, "temperature": 0.2}
    request = urllib.request.Request(
        _endpoint(section["base_url"]),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + section["api_key"]},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode("utf-8", "replace")
    except Exception as error:                      # noqa: BLE001 - mapped below
        raise TranslationError(_describe(error, section["base_url"])) from error
    try:
        body = json.loads(raw)
    except ValueError as error:
        # A 200 carrying HTML is a proxy or a captive portal, not a model. Saying
        # so beats surfacing "Expecting value: line 1 column 1".
        raise TranslationError("翻译失败：接口返回的不是 JSON（可能是网关或登录页），"
                               "请确认接口地址是 OpenAI 兼容端点") from error
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        # A 200 with an unexpected shape is usually a proxy or an error page.
        raise TranslationError("翻译失败：接口返回的内容不是预期的格式（检查接口地址是否为 "
                               "OpenAI 兼容端点）") from error


def _clean(text):
    return (text or "").strip().strip('"“”').strip()


def translate_line(text, options=None):
    """Translate one lyric line. Returns the translated string."""
    options = options or {}
    language = LANGUAGES.get(options.get("target", "zh"), options.get("target") or "简体中文")
    system = BASE_PROMPT.format(language=language)
    if (options.get("instructions") or "").strip():
        system += "\n额外要求（用户指定，优先遵守）：\n" + options["instructions"].strip() + "\n"
    content = _post(options, [{"role": "system", "content": system},
                              {"role": "user", "content": text}])
    result = _clean(content)
    if not result:
        raise TranslationError("翻译失败：模型返回了空内容")
    return result


def fill(document, options, index=None, scope="line", only_missing=False, translate=None):
    """Write translations into ``document``. Returns a short report.

    ``scope`` is ``"line"`` (just ``index``) or ``"all"``. Work is done in chunks
    and **each chunk is kept as soon as it arrives**: a long song that fails on
    the fifth request should not throw away the four that succeeded. The failure
    is reported alongside the partial result instead.

    ``translate`` is injected so the pairing logic can be tested without a
    network — see ``tests/test_translator.py``.
    """
    translate = translate or translate_lines
    groups = document.groups
    # Imported here rather than at module level: this module is deliberately
    # dependency-free so it can be tested against a fake endpoint alone.
    from .document import track_start
    if scope == "line":
        if index is None or not (0 <= index < len(groups)):
            raise TranslationError("要翻译的行号超出范围")
        targets = [index]
    else:
        targets = [i for i, group in enumerate(groups) if group.main.text.strip()]
    if only_missing:
        targets = [i for i in targets if not groups[i].translation.text.strip()]

    done, notes = 0, []
    for start in range(0, len(targets), MAX_LINES_PER_REQUEST):
        chunk = targets[start:start + MAX_LINES_PER_REQUEST]
        try:
            results = translate([groups[i].main.text for i in chunk], options)
        except TranslationError as error:
            if done == 0:
                raise
            notes.append("已翻译 %d 行后中断：%s" % (done, error))
            break
        for position, text in zip(chunk, results):
            group = groups[position]
            group.translation.text = text
            # The format gives the translation the same start as the line it
            # translates. The line start is the TRACK start (a track carries its
            # own only when it diverges), so reading main.start directly would
            # write None here.
            if group.translation.start is None:
                group.translation.start = track_start(group, "main")
        done += len(chunk)
    if not done:
        notes.append("没有需要翻译的行")
    return {"translated": done, "total": len(targets), "notes": notes}


def translate_lines(lines, options=None):
    """Translate many lines in one request, keeping them in step.

    Numbering the lines and asking for a JSON array is what makes a batch safe:
    a single blob of text comes back with the lines merged or reordered often
    enough that pairing it back up line-by-line is guesswork. The reply is
    checked against the input length, and a mismatch is an error rather than a
    silent shift of every translation by one line.
    """
    options = options or {}
    text = [line for line in lines if (line or "").strip()]
    if not text:
        return []
    if len(text) > MAX_LINES_PER_REQUEST:
        raise TranslationError("一次最多翻译 %d 行，请分批" % MAX_LINES_PER_REQUEST)
    language = LANGUAGES.get(options.get("target", "zh"), options.get("target") or "简体中文")
    system = (BASE_PROMPT.format(language=language) +
              "\nInput is a JSON array of strings. Output ONLY a JSON array of the same "
              "length: element i is the translation of element i. No other text.\n")
    if (options.get("instructions") or "").strip():
        system += "\n额外要求（用户指定，优先遵守）：\n" + options["instructions"].strip() + "\n"
    content = _post(options, [{"role": "system", "content": system},
                              {"role": "user", "content": json.dumps(text, ensure_ascii=False)}])
    payload = _clean(content)
    if payload.startswith("```"):
        payload = payload.strip("`")
        payload = payload.split("\n", 1)[1] if "\n" in payload else payload
    try:
        parsed = json.loads(payload)
    except ValueError as error:
        raise TranslationError("翻译失败：模型没有按 JSON 数组返回，无法安全对齐行号") from error
    if not isinstance(parsed, list) or len(parsed) != len(text):
        got = len(parsed) if isinstance(parsed, list) else "非数组"
        raise TranslationError("翻译失败：返回 %s 行，输入 %d 行，行数不一致（已放弃，避免错位）"
                               % (got, len(text)))
    return [_clean(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
            for item in parsed]
