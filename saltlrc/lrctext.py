"""LRC text handling shared by every lyric source — layer 1.

LRC text handling lives here so the tokenizer (:mod:`saltlrc.wordtime`, L1) can
use ``CREDIT`` and ``parse_lrc`` without importing a network provider (L2).
Reading LRC text is processing, not networking: this module is the single owner
of those rules and sits at layer 1, above the providers and below the model
layer.

The parser is separate from :func:`saltlrc.parser.parse`: that one
builds a :class:`~saltlrc.document.Document`, this one returns the raw
``(time, text, words)`` payload a provider serves. One turns text into the model,
the other turns a wire format into text-shaped data.
"""

import json
import re

__all__ = ["CREDIT", "TIME", "parse_lrc", "clean_lines", "is_credit"]

TIME = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")

# 作词/作曲 之类的制作信息不是歌词。各行来源（网易云、酷狗、酷我、QQ、LRCLIB）
# 都必须按同一份名单过滤，否则"同一首歌换一个源就多一行"。
CREDIT = re.compile(
    r"^\s*(作词|作曲|编曲|制作人|原唱|翻唱|混音|母带|和声|吉他|贝斯|鼓|监制|录音|词|曲)\s*[:：]")

# 纯音乐占位文案：不是歌词，也不该占一行时间轴。
INSTRUMENTAL = ("纯音乐，请欣赏", "此歌曲为没有填词的纯音乐，请您欣赏")


def is_credit(text):
    """True when a line is production credit rather than lyric."""
    text = (text or "").strip()
    return bool(CREDIT.match(text)) or text.startswith("作词") or text.startswith("作曲")


def _milliseconds(minutes, seconds, fraction):
    """``[mm:ss.xx]`` → ms. The fraction is decimal, not a fixed-width field.

    "5" → 500ms, "05" → 50ms, "053" → 53ms. Reading it as a fixed 2-digit
    centisecond field turns ".5" into 5ms and shifts whole lines. All providers
    use the decimal reading, which is the one implemented here.
    """
    value = int(minutes) * 60000 + int(seconds) * 1000
    if fraction:
        value += int(round(int(fraction) * 1000 / (10 ** len(fraction))))
    return value


def parse_lrc(text):
    """Parse LRC text into ``(time_ms, text, words)`` records.

    Netease serves some lyric bodies as one JSON object per line
    (``{"t":123,"c":[{"tx":"..."}]}``); those are handled alongside classic
    ``[mm:ss.xx]`` lines; the same endpoint returns both.
    """
    lines = []
    metadata = {}
    word_level = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("{") and line.endswith("}"):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            parts = entry.get("c") or []
            words = []
            for part in parts:
                piece = part.get("tx") or ""
                if not piece:
                    continue
                if part.get("t") is not None:
                    word_level = True
                    words.append({"time": int(entry.get("t", 0)) + int(part["t"]),
                                  "text": piece,
                                  "duration": int(part["d"]) if part.get("d") else None})
                else:
                    words.append({"time": int(entry.get("t", 0)), "text": piece, "duration": None})
            joined = "".join(w["text"] for w in words)
            if joined.strip():
                lines.append({"time": int(entry.get("t", 0)), "text": joined.strip(), "words": words})
            continue
        stamps = list(TIME.finditer(line))
        if not stamps:
            continue
        body = line[stamps[-1].end():].strip()
        for stamp in stamps:
            time = _milliseconds(stamp[1], stamp[2], stamp[3])
            if body:
                lines.append({"time": time, "text": body, "words": []})
    lines.sort(key=lambda item: item["time"])
    return {"lines": lines, "metadata": metadata, "word_level": word_level}


def clean_lines(lines, drop_credits=True):
    """Drop empty lines, production credits and instrumental placeholders."""
    out = []
    for item in lines or []:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        if drop_credits and is_credit(text):
            continue
        if text in INSTRUMENTAL:
            continue
        out.append(item)
    return out
