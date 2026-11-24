"""Dependency-free RMS onset refinement for existing line anchors; not speech recognition."""
import array
import io
import math
import sys
import wave

from .document import Word


def read_pcm(raw):
    """读一段 16kHz 单声道 PCM16 WAV。

    **坏数据一律抛 ValueError（带中文原因）**，不要漏出 ``wave.Error`` /
    ``struct.error`` 这类异常：服务端只把 ``ValueError`` 转成"400 + 原文"，
    别的都会变成一句空的「500 服务器内部错误」—— 用户看到的等于没有信息
    （这一条是 tests/test_align_routes.py 抓出来的：喂了一段垃圾数据，
    回来的正是 ``{'error': '服务器内部错误：'}``）。
    """
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() != 1 or wav.getframerate() != 16000:
                raise ValueError("分析输入必须是 16kHz 单声道 PCM16 WAV")
            if wav.getnframes() > 16000 * 1200:
                raise ValueError("单次分析最多 20 分钟")
            samples = array.array("h", wav.readframes(wav.getnframes()))
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("这段数据不是有效的 WAV（16kHz 单声道 PCM16）：%s"
                         % (error or error.__class__.__name__)) from error
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


FRAME_MS = 20
FRAME_SAMPLES = 320
NEAR_MS = 100
SNAP_MS = 120          # how far a proportional unit may move to meet an onset


def envelope(samples):
    """20 ms RMS envelope, normalised to 0..1."""
    frames = []
    for i in range(0, len(samples), FRAME_SAMPLES):
        chunk = samples[i:i + FRAME_SAMPLES]
        if not chunk:
            break
        total = 0
        for value in chunk:
            total += value * value
        frames.append(math.sqrt(total / len(chunk)) / 32768)
    return frames


def detect_onsets(rms):
    """Return (onset_ms, active_flags, threshold) for an RMS envelope."""
    if not rms:
        raise ValueError("音频为空")
    floor = sorted(rms)[len(rms) // 5]
    threshold = max(.012, floor * 2.5, max(rms) * .08)
    active = [value > threshold for value in rms]
    onsets = [i * FRAME_MS for i in range(1, len(active) - 2)
              if not active[i - 1] and all(active[i:i + 3])]
    if active[0]:
        onsets.insert(0, 0)
    return onsets, active, threshold


def _distance_to_onset(rms, onsets):
    """Per-frame distance (ms) to the nearest onset, via a two-pass sweep."""
    big = 10 ** 9
    marks = [False] * len(rms)
    for onset in onsets:
        index = onset // FRAME_MS
        if 0 <= index < len(marks):
            marks[index] = True
    distance = [big] * len(rms)
    last = -big
    for i, marked in enumerate(marks):
        if marked:
            last = i
        value = (i - last) * FRAME_MS
        if value < distance[i]:
            distance[i] = value
    last = big
    for i in range(len(rms) - 1, -1, -1):
        if marks[i]:
            last = i
        value = (last - i) * FRAME_MS
        if value < distance[i]:
            distance[i] = value
    return distance


def _track_start(group, field):
    track = getattr(group, field)
    return track.start if track.start is not None else group.start


def global_offset(doc, raw, field="main", radius=4000, step=10):
    """Estimate one global shift that puts every line start onto a vocal onset.

    A single offset only makes sense when the whole lyric is off by a constant
    amount, so the result is presented as a proposal to apply, never applied
    automatically.
    """
    if field not in ("main", "translation"):
        raise ValueError("未知对齐轨道")
    if not 0 <= radius <= 20000 or not 1 <= step <= 200:
        raise ValueError("搜索范围或步长无效")
    samples = read_pcm(raw)
    onsets, _, _ = detect_onsets(envelope(samples))
    if not onsets:
        raise ValueError("没有检测到可靠的声音起点，无法估计整体偏移")
    starts = [int(_track_start(group, field)) for group in doc.groups if getattr(group, field).text]
    if not starts:
        raise ValueError("该轨道没有歌词行")
    distance = _distance_to_onset(envelope(samples), onsets)
    frames = len(distance)
    best = None
    for delta in range(-radius, radius + 1, step):
        total = 0
        hits = 0
        for start in starts:
            target = start + delta
            # Outside the audio there is no evidence; penalise instead of guessing.
            if target < 0 or target // FRAME_MS >= frames:
                total += radius
                continue
            value = distance[target // FRAME_MS]
            total += value
            if value <= NEAR_MS:
                hits += 1
        if best is None or (hits, -total) > (best["hits"], -best["total"]):
            best = {"delta": delta, "hits": hits, "total": total}
    mean = round(best["total"] / len(starts), 1)
    return {"field": field, "delta": best["delta"], "hits": best["hits"], "lines": len(starts),
            "mean_ms": mean, "onsets": len(onsets), "radius": radius, "step": step,
            "notice": ("整首按同一偏移移动 %+d ms：%d/%d 行落到了声音起点 100ms 内，平均偏差 %.0f ms。"
                       "变奏、间奏或变速的歌曲不要整首平移，改用分句分析。"
                       % (best["delta"], best["hits"], len(starts), mean))}


def refine_words(doc, raw, field="main"):
    """Re-time in-line word starts from detected onsets.

    Bottom layer: every line with text gets a proposal, in two grades —
    an exact onset/unit count match is required only for the high-confidence
    case; real songs almost never match exactly.

    * exact match (onsets == units, or units-1 with the line anchor) →
      high-confidence re-timing;
    * otherwise → a proportional baseline snapped to the nearest onset within
      ``SNAP_MS``; units without a nearby onset keep the estimated time and are
      reported as such, so nothing is presented as transcribed truth.

    Lines without a word axis get one generated (smart tokenization) so the
    refine feature works on freshly imported lyrics too.
    """
    from .wordtime import _classify, tokenize

    if field not in ("main", "translation"):
        raise ValueError("未知对齐轨道")
    samples = read_pcm(raw)
    onsets, _, _ = detect_onsets(envelope(samples))
    duration = len(samples) * 1000 // 16000
    proposals = []
    for index, group in enumerate(doc.groups):
        track = getattr(group, field)
        start = int(_track_start(group, field))
        end = int(track.end) if track.end is not None else None
        words = list(track.words)
        entry = {"index": index, "field": field, "text": track.text, "start": start, "end": end,
                 "units": len(words), "onsets": 0, "words": [], "status": "skip",
                 "confidence": "", "reason": ""}
        if not track.text:
            entry["reason"] = "该行没有文字"
            proposals.append(entry)
            continue
        created_axis = False
        if len(words) < 1:
            words = [Word(start + 0, unit) for unit in tokenize(track.text, "smart")]
            # distribute() style weights keep the axis inside the line; here a
            # simple even spread is enough: times are re-derived below.
            if len(words) > 1:
                step = max(1, ((end or start + 2000) - start) // len(words))
                for offset, word in enumerate(words):
                    word.start = start + offset * step
            created_axis = True
        elif len(words) < 2:
            entry["units"] = len(words)
            entry["words"] = [{"start": start, "text": words[0].text}]
            entry["status"] = "ready"
            entry["confidence"] = "approx"
            entry["reason"] = "该行只有一个字组，保持句首"
            proposals.append(entry)
            continue
        entry["units"] = len(words)
        if end is None:
            entry["reason"] = "该行缺少句尾时间，先补句尾再细化"
            proposals.append(entry)
            continue
        inside = [t for t in onsets if start <= t < end]
        entry["onsets"] = len(inside)
        units = len(words)
        confidence = "approx"
        if len(inside) == units:
            times = list(inside)
            confidence = "exact"
        elif len(inside) == units - 1:
            times = [start] + inside
            confidence = "exact"
        else:
            # Proportional baseline by display weight, then snap each unit to
            # the nearest onset inside the tolerance window.
            weights = [_classify(w.text) for w in words]
            total = sum(weights) or float(units)
            cursor = 0.0
            times = []
            for weight in weights:
                times.append(start + int(round((end - start) * cursor / total)))
                cursor += weight
            snapped = 0
            for i, value in enumerate(times):
                near = min(inside, key=lambda t: abs(t - value)) if inside else None
                if near is not None and abs(near - value) <= SNAP_MS:
                    times[i] = near
                    snapped += 1
            entry["snapped"] = snapped
            entry["reason"] = ("近似对齐：%d/%d 字组吸附到声音起点，其余按比例估算"
                               % (snapped, units))
        times[0] = start
        cleaned = []
        fits = True
        for value in times:
            if cleaned and value <= cleaned[-1]:
                value = cleaned[-1] + 10
            if value >= end or value > duration:
                fits = False  # 该行放不下这么多字组，宁可不改也不造假
                break
            cleaned.append(value)
        if not fits:
            entry["reason"] = "起点过密或超出音频，未改动"
        else:
            entry["words"] = [{"start": cleaned[i], "text": words[i].text} for i in range(units)]
            entry["status"] = "ready"
            entry["confidence"] = confidence
            if created_axis:
                entry["reason"] = "该行原本没有逐字轴：已按音组生成并" + (
                    "精确匹配 %d 个起点" % len(inside) if confidence == "exact" else entry["reason"])
            elif confidence == "exact":
                entry["reason"] = "精确匹配 %d 个声音起点（高置信）" % len(inside)
        proposals.append(entry)
    exact = sum(1 for item in proposals if item.get("confidence") == "exact")
    ready = sum(1 for item in proposals if item["status"] == "ready")
    return {"field": field, "duration_ms": duration, "onsets": len(onsets), "proposals": proposals,
            "ready": ready, "exact": exact,
            "notice": ("%d/%d 行已生成逐字候选（%d 行精确匹配起点，其余为近似对齐）。"
                       "近似行务必试听，不合适就取消勾选。" % (ready, len(proposals), exact))}


def apply_words(doc, proposals, indices):
    """Apply selected word refinements, keeping the text and line box intact."""
    if not isinstance(indices, list) or not indices:
        raise ValueError("请选择要应用的行")
    wanted = set(indices)
    for entry in proposals:
        if entry["index"] not in wanted or entry["status"] != "ready":
            continue
        index = entry["index"]
        if not 0 <= index < len(doc.groups):
            raise ValueError("候选行无效")
        track = getattr(doc.groups[index], entry["field"])
        if track.text != entry["text"] or int(_track_start(doc.groups[index], entry["field"])) != entry["start"]:
            raise ValueError("歌词已改变，请重新细化")
        words = entry["words"]
        if track.words and len(words) != len(track.words):
            raise ValueError("逐字数量与当前行不一致")
        if "".join(w["text"] for w in words) != track.text:
            raise ValueError("逐字文字与歌词不一致")
        times = [w["start"] for w in words] + ([track.end] if track.end is not None else [])
        if any(a >= b for a, b in zip(times, times[1:])):
            raise ValueError("细化后的逐字时间不递增")
        if not track.words:
            # This line had no word axis; the proposal creates one from scratch.
            track.words = [Word(int(w["start"]), w["text"]) for w in words]
        else:
            for word, source in zip(track.words, words):
                word.start = int(source["start"])
    doc.notes.append("已按声音起点细化所选行的逐字时间，请试听核对")
    return doc


def onset_proposals(doc, raw, field="translation", radius=1500):
    if field not in ("main", "translation") or not 100 <= radius <= 5000:
        raise ValueError("轨道或搜索半径无效")
    samples = read_pcm(raw)
    duration = len(samples) * 1000 // 16000
    onsets, active, _ = detect_onsets(envelope(samples))
    proposals = []
    for i, group in enumerate(doc.groups):
        track = getattr(group, field)
        old = track.start if track.start is not None else group.start
        candidates = [t for t in onsets if abs(t - old) <= radius]
        start = min(candidates, key=lambda t: abs(t - old)) if candidates and track.text else None
        end = track.end
        if start is not None:
            f = min(len(active) - 1, start // FRAME_MS)
            while f < len(active) and active[f]:
                f += 1
            end = min(duration, f * FRAME_MS)
            if track.end is not None:
                # Onset refinement moves existing phrase duration intact; pauses inside
                # a phrase must not truncate an existing word timeline.
                end = track.end + start - old
                if end > duration:
                    start = None
        proposals.append({"index": i, "field": field, "old_start": old, "start": start, "end": end,
                          "coverage": 0, "words": [], "text": track.text, "matched_text": "",
                          "status": "review", "kind": "energy-onset",
                          "reasons": ["仅检测声音起点，可能是伴奏；必须试听"] if start is not None else ["邻近没有可靠声音起点"]})
    return {"engine": "RMS-onset", "language": None, "duration_ms": duration, "field": field,
            "proposals": proposals, "notice": "能量辅助微调只寻找已有时间附近的声音起点，不能判断歌词或人声。"}
