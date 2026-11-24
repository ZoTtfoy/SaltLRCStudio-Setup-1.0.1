import re

TIME = re.compile(r"([\[<])(\d+):(\d{2})(?:[.:](\d{1,3}))?([\]>])")

# Encoding names as shown to the user -> codec that writes bytes.
ENCODINGS = {"utf-8": "utf-8", "utf-8-bom": "utf-8-sig", "utf-8-sig": "utf-8-sig", "gb18030": "gb18030"}


def to_bytes(text, encoding="utf-8"):
    """Encode lyric text for disk. Unknown names fall back to UTF-8."""
    return text.encode(ENCODINGS.get((encoding or "utf-8").lower(), "utf-8"))


def milliseconds(match):
    if (match[1], match[5]) not in (("[", "]"), ("<", ">")):
        raise ValueError("时间标签括号不匹配")
    if int(match[3]) >= 60:
        raise ValueError("秒数必须小于 60")
    return int(match[2]) * 60000 + int(match[3]) * 1000 + int((match[4] or "").ljust(3, "0"))


def stamp(ms):
    if type(ms) is not int or ms < 0:
        raise ValueError("时间必须为非负整数毫秒")
    return f"{ms // 60000:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def decode(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "UTF-16 BOM → UTF-8"
    try:
        return raw.decode("utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        # Legacy encodings are ambiguous: require review rather than silently guessing.
        return raw.decode("gb18030"), "GB18030 候选解码，请核对文字；日文旧编码请显式指定"
