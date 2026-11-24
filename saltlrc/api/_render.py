"""Export formatting shared by the routes and the server — layer 1 helpers.

四个函数：取格式、取扩展名、取编码、渲染（严格 / 可降级）。路由模块与服务端都从这里
导入，只此一份定义。
"""

from ..exporter import export
from ..validator import validate

__all__ = ["_fmt", "_extension", "_encoding", "_render", "_render_or_fallback"]


def _fmt(data):
    return (data.get("format") or ("salt" if data.get("salt", True) else "lrc")).lower()


def _extension(data):
    return ".yrc" if _fmt(data) == "yrc" else ".lrc"


def _encoding(data):
    return data.get("encoding") or "utf-8"


def _render(document, data):
    """严格打包：格式的要求不满足就抛（可读中文）。"""
    return export(document,
                  salt=data.get("salt", True),
                  duration=data.get("duration"),
                  translation_timestamp=data.get("translation_timestamp", True),
                  require_translation=data.get("require_translation", False),
                  require_language=data.get("require_language", False),
                  fmt=_fmt(data))


def _render_or_fallback(document, data):
    """→ ``(文本, 降级说明或 None)``。**写不出来的格式降级为「标准双语 LRC」，而不是拒了。**

    严格格式失败时退回标准双语 LRC：每行一个句首，播放器照样显示双语，只是没有逐字高亮；
    说明里带上校验器给出的第一句问题，并把"已经按标准 LRC 导出"写清楚。

    **只降 salt → lrc**：两者写出来都是 `.lrc`。
    YRC 是另一种格式（JSON），要 JSON 的照旧报错。
    """
    try:
        return _render(document, data), None
    except ValueError as error:
        if _fmt(data) != "salt":
            raise
        loose = dict(data, format="lrc", salt=False)
        try:
            text = _render(document, loose)
        except ValueError:
            raise error from None          # 连宽松格式都渲染不出来 → 报原来那个错
        report = validate(document, strict=True, duration=data.get("duration"))
        first = (report["errors"] or [str(error)])[0]
        # 只取"是什么问题"那一句：校验器后面还带着"可以改用标准 LRC 导出"的建议，
        # 与下面这句重复。
        first = first.split("——")[0].strip()
        note = ("%s；已按「标准双语 LRC」导出（播放器照样显示双语，只是没有逐字高亮）。"
                "想要逐字效果：双击波形上那一句用「按字数均分」或「从音频检测这一行」补上，"
                "或用网易云登录拿真实逐字时间。" % first)
        return text, note
