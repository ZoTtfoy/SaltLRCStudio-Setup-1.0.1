"""Per-language word splitting.

The word-timing editor works one unit at a time; what counts as a unit is a
language question, not a formatting one:

===========  ============================  ==============================
language     rule                          one unit is
===========  ============================  ==============================
中文         按字                          一个汉字
日语         按发音                        漢字+送りがな / 一个可唱的假名组
英语         按单词                        一个单词
韩语         按音节                        一个谚文音节块
未识别       混合规则                      可用的折中
===========  ============================  ==============================

Each rule lives in its own module (``chinese`` / ``japanese`` / ``english`` /
``korean`` / ``mixed``); this file only decides **which** one to apply:

1. an explicit ``lang`` from the caller wins;
2. otherwise the line's script is detected (:func:`saltlrc.language.detect`).

:func:`split` returns the module together with the language code, the rule that
ran, and where the decision came from.
"""

from . import chinese, english, japanese, korean, mixed
from .common import first_pass, merge_spaces

# Language code -> module. `detect` may also return "und"; anything unknown
# lands on the fallback rather than raising.
MODULES = {
    "zh": chinese,
    "ja": japanese,
    "en": english,
    "ko": korean,
    "und": mixed,
}

# Mode names accepted by the public entry point.
MODES = ("smart", "char", "word")


def module_for(lang):
    return MODULES.get((lang or "").lower(), mixed)


def split(text, lang=None, detector=None):
    """Split ``text`` into display units for ``lang``.

    ``detector`` is injected; it defaults to :func:`saltlrc.language.detect`.
    """
    if detector is None:
        from ..language import detect as detector
    hint = (lang or "").strip().lower() or None
    if hint and hint in MODULES:
        return module_for(hint), {"lang": hint, "source": "hint"}
    found = detector(text) or {}
    code = (found.get("code") or "und").lower()
    return module_for(code), {
        "lang": code,
        "source": "detect",
        "method": found.get("method"),
        "confidence": found.get("confidence"),
    }


def tokenize(text, mode="smart", lang=None, detector=None):
    """Public entry point. Returns ``(units, info)``.

    ``char`` and ``word`` are language-independent granularities. Only ``smart``
    consults the per-language rules.
    """
    if mode not in MODES:
        mode = "smart"
    info = {"mode": mode, "lang": None, "source": None}
    if mode == "char":
        units = first_pass(text, per_char_latin=True)
    elif mode == "word":
        units = first_pass(text)
    else:
        module, info = split(text, lang=lang, detector=detector)
        units = module.regroup(first_pass(text))
        info = dict(info, mode=mode, rule=module.LABEL)
    return merge_spaces(units), info
