"""Conservative script detection, optionally enriched by langid's statistical model."""
import re
from functools import lru_cache

NAMES = {"zh": "中文", "ja": "日语", "ko": "韩语", "en": "英语", "und": "未确定"}


@lru_cache(maxsize=1)
def classifier():
    try:
        from langid.langid import LanguageIdentifier, model
        return LanguageIdentifier.from_modelstring(model, norm_probs=True)
    except ImportError:
        return None


def detect(text):
    letters = re.sub(r"[\W\d_]", "", text, flags=re.UNICODE)
    if not letters:
        return {"code": "und", "confidence": 0.0, "method": "script", "review": True}
    if re.search(r"[\u3040-\u30ff]", letters):
        code, score, method = "ja", .99, "kana"
    elif re.search(r"[\uac00-\ud7af]", letters):
        code, score, method = "ko", .99, "hangul"
    elif re.fullmatch(r"[\u3400-\u9fff]+", letters):
        # Pure Kanji is genuinely ambiguous: report `han-ambiguous` and skip the model.
        code, score, method = "zh", .55, "han-ambiguous"
    elif classifier() is not None and len(letters) >= 8:
        code, score = classifier().classify(text)
        method = "langid"
    elif re.search(r"[\u3400-\u9fff]", letters):
        code, score, method = "zh", .6, "mixed-han"
    elif re.fullmatch(r"[A-Za-z]+", letters):
        code, score, method = "en", .5, "latin-ambiguous"
    else:
        code, score, method = "und", 0., "script"
    return {"code": code, "confidence": round(float(score), 3), "method": method, "review": score < .8}


def identify(doc):
    result = {field: detect("\n".join(getattr(g, field).text for g in doc.groups))
              for field in ("main", "translation")}
    result["rows"] = [{field: detect(getattr(g, field).text) for field in ("main", "translation")}
                      for g in doc.groups]
    return result
