"""智能贴唱：把整首歌词贴到人声起点序列上（纯标准库，全离线）。

粒度 = **只贴整行**（句首 + 句尾）；"本地模型" = 本机能量分析，不下载任何东西。
结果是**提案**：界面可以播放着预览，确认后才应用（可撤销）。

===============================================================================
一、识别方式

* **起始强度 = 对数能量的正向增量**（positive log-energy flux，信号处理里的标准做法），
  阈值取**滑窗中的中位数 + k·MAD**：音量忽大忽小的歌也稳，不会被副歌带跑，
  也不需要为不同录音调参数；
* **浊音度代理**：低频（500Hz 一阶低通）能量占比 —— 人声的元音低频强，
  镲片、掌声、鼓的"嘶"声不是。用极便宜的一阶 IIR 就能把
  "**人声起音**"和"打击乐起音"分开（没有 numpy，真做频谱代价太大）；
* 只留"**有浊音支撑、且能量持续 ≥60ms**"的候选，再按最小间隔（110ms）合并取强者。

二、对齐逻辑

**一条单调路径**：所有行的句首在 (行 × 候选起点) 的网格上一次性做 Viterbi：

    代价(候选) = **离最近人声起点的距离**（贴不上就贵）
               + 移动量（软约束，限制贴唱位移）
               + 落在长段无人的地方（纯伴奏里）的额外惩罚
    转移代价   = **与"原来行距"的偏离**（保节奏形状：整首平移可以，忽快忽慢不行）
    路径约束   = 后一行的句首**严格晚于**前一行

    于是：
      * 某一行证据弱时，**邻行会把它架住**，它不会自己跳走；
      * "整首统一偏移"和"逐行微调"是**同一件事**，不需要两个按钮；
      * 快歌/慢歌/变奏只是"候选密度不同"，不需要换算法。

句尾也从音频里找：行内**最后一段浊音的结束**（带迟滞，避免换气就断）。
找不到就**保留原句尾**。

三、界限

* 全程只读音频，**不改文档**；结果是"提案"，由界面预览、确认后才应用（可撤销）。
* 每一行都带 `confidence` 与人话 `reason`：证据不足的行**明确标出来**，
  绝不假装是高置信的结果。页面上这些行会带「置信偏低」标签，且每一行都能单独试听。
"""

import bisect
import math
from copy import deepcopy

from .acoustic import read_pcm

SAMPLE_RATE = 16000
HOP_MS = 10
WINDOW_MS = 20
HOP = SAMPLE_RATE * HOP_MS // 1000
WINDOW = SAMPLE_RATE * WINDOW_MS // 1000

# 浊音度：低频能量占比。500Hz 一阶低通足够把元音和镲片/气声分开。
VOICED_CUTOFF_HZ = 500
VOICED_MIN_RATIO = 0.55
# 起音之后至少要有这么长的一段"有声 + 浊音"，才算"唱了"（而不是一声鼓）。
MIN_RUN_MS = 60
MIN_RUN_FRAMES = MIN_RUN_MS // HOP_MS
# 两个起始点至少隔这么久，否则合并（取更强者）
MIN_GAP_MS = 110
# 静音地板：低于峰值这个分贝数的帧不算证据
SILENCE_FLOOR_DB = -42
# 起始强度阈值：中位数 + k·MAD
FLUX_K = 3.0
FLUX_FLOOR = 0.08

MAX_SHIFT_MS = 8000      # 单个句首最多挪这么多
MIN_LINE_MS = 250        # 两行句首之间的下限（硬约束）
GAP_KEEP_MS = 60         # 句尾与下一句首之间保留的空隙
TAIL_MS = 500            # 找不到浊音结束时给的最后一点尾巴
WOFF_CAP_MS = 500        # "离人声起点多远"的封顶（再远也一样差）
W_MOVE = 0.34            # 每毫秒移动量的代价（软约束）
W_SHAPE = 0.5            # 行距偏离的代价权重
KEEP_PENALTY_MS = 30     # 保持原时间的固定代价：等价的候选不会因此原地不动
BARE_PENALTY_MS = 700    # 落在长段无人的地方（纯伴奏）的额外代价
BARE_GAP_MS = 1200       # "长段无人"的定义


def _median(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def analyse(samples):
    """逐帧特征 + 人声起始点/结束点。返回纯数据，方便单测与调试。

    **样本契约**：`samples` 必须是 **int16** 的序列（就是 `acoustic.read_pcm`
    的返回值）。传浮点数会算出接近 0 的电平，必须传 int16。
    电平按满量程归一（除以 32768），而所有阈值都是**相对量**（分位数、相对峰值），
    所以歌录得轻或重都不需要调参数。

    帧长 20ms、步长 10ms（重叠一半）。每帧算三样东西：
      * `rms`：全带能量（有没有声音）
      * `low`：500Hz 低通之后的能量（是不是人声/浊音）
      * `flux`：对数能量的正向增量（起始强度）
    """
    if len(samples) < WINDOW * 4:
        raise ValueError("音频太短，听不出人声起点")
    alpha = 1.0 - math.exp(-2.0 * math.pi * VOICED_CUTOFF_HZ / SAMPLE_RATE)
    rms_level, low_level, flux = [], [], []
    previous = None
    peak = 0.0
    for start in range(0, len(samples) - WINDOW + 1, HOP):
        total = 0.0
        low_total = 0.0
        low_pass = 0.0
        for index in range(start, start + WINDOW):
            value = samples[index] / 32768.0
            total += value * value
            low_pass += alpha * (value - low_pass)
            low_total += low_pass * low_pass
        level = math.sqrt(total / WINDOW)
        peak = max(peak, level)
        rms_level.append(level)
        low_level.append(math.sqrt(low_total / WINDOW))
        log_level = math.log(level + 1e-7)
        flux.append(max(0.0, log_level - previous) if previous is not None else 0.0)
        previous = log_level

    # 静音地板**只跟峰值比**，不用绝对常数：绝对常数会把录得轻的歌整首判成静音。
    # 1e-6 只是给数字静音留个底。
    floor = max(peak * (10 ** (SILENCE_FLOOR_DB / 20.0)), 1e-6)
    voiced = [rms_level[i] >= floor and
              (low_level[i] / (rms_level[i] + 1e-9)) >= VOICED_MIN_RATIO
              for i in range(len(rms_level))]

    # 自适应阈值：中位数 + k·MAD（对忽大忽小、副歌比主歌响很多的歌也稳）
    middle = _median(flux)
    spread = _median([abs(value - middle) for value in flux]) or 1e-6
    threshold = max(middle + FLUX_K * spread, FLUX_FLOOR)

    onsets = []
    for i in range(1, len(flux) - 1):
        if flux[i] <= threshold or rms_level[i] < floor:
            continue
        # 局部最大：同一处上升只算一次
        if any(flux[j] > flux[i] for j in range(max(0, i - 2), min(len(flux), i + 3)) if j != i):
            continue
        # 要有浊音支撑、并且**持续**一段：一声鼓不会唱 60ms
        run = voiced[i:i + MIN_RUN_FRAMES]
        if sum(run) < max(2, MIN_RUN_FRAMES - 2):
            continue
        at = i * HOP_MS
        if onsets and at - onsets[-1] < MIN_GAP_MS:
            if flux[i] > flux[onsets[-1] // HOP_MS]:
                onsets[-1] = at
            continue
        onsets.append(at)

    # 浊音段：用来找句尾（带迟滞：要连续 3 帧不浊才算真的停了）
    offsets = []
    index = 0
    while index < len(voiced):
        if not voiced[index]:
            index += 1
            continue
        start = index
        while index < len(voiced):
            if not voiced[index]:
                quiet = 0
                probe = index
                while probe < len(voiced) and not voiced[probe]:
                    quiet += 1
                    probe += 1
                if quiet >= 3:
                    break
                index = probe
            index += 1
        if (index - start) * HOP_MS >= MIN_RUN_MS:
            offsets.append(index * HOP_MS)
    return {"hop_ms": HOP_MS, "frames": len(rms_level), "duration_ms": len(samples) * 1000 // SAMPLE_RATE,
            "rms": rms_level, "low": low_level, "flux": flux, "voiced": voiced,
            "threshold": threshold, "floor": floor, "onsets": onsets, "offsets": offsets}


def _nearest(marks, value):
    """`marks`（已排序的毫秒）里离 `value` 最近的一个，返回 (距离, 值)；空表给 (None, None)。"""
    if not marks:
        return None, None
    at = bisect.bisect_left(marks, value)
    best = None
    for candidate in (at - 1, at):
        if 0 <= candidate < len(marks):
            distance = abs(marks[candidate] - value)
            if best is None or distance < best[0]:
                best = (distance, marks[candidate])
    return best


def _candidates(original, onsets, max_shift):
    """一行的候选句首：窗口内的每个人声起点 + "保持原时间"。

    返回值按时间升序；每项是 ``(时间, 离起点的距离, 是否原时间)``。
    """
    # 每个候选本身就是一个人声起点 → "离起点距离"是 0；
    # 只有"保持原时间"那一项才要算它离最近的起点有多远。
    marks = [(value, 0.0, False) for value in onsets if abs(value - original) <= max_shift]
    distance, _ = _nearest(onsets, original)
    marks.append((original, WOFF_CAP_MS if distance is None else min(distance, WOFF_CAP_MS), True))
    marks.sort(key=lambda item: item[0])
    return marks


def _bare(onsets, at):
    """这个位置是否落在"长段无人"的区间里（纯伴奏 / 间奏 / 长留白）。

    判据只有一条：**它夹在两个人声起点之间、而这两个点隔得很远**，
    同时它自己离最近的起点也够远。这样的位置把句首贴上去，要额外罚。
    """
    if not onsets:
        return True
    left = bisect.bisect_left(onsets, at)
    previous = onsets[left - 1] if left else None
    following = onsets[left] if left < len(onsets) else None
    distance, _ = _nearest(onsets, at)
    if distance is None or distance <= BARE_GAP_MS / 2:
        return False
    if previous is None or following is None:
        return True
    return following - previous >= BARE_GAP_MS


def _line_end(start, original_end, offsets, next_start, duration):
    """句尾：行内最后一段浊音的结束；找不到就保留原句尾。"""
    limit = min(duration, start + 20000)
    inside = [value for value in offsets if start + MIN_LINE_MS <= value <= limit]
    if next_start is not None and next_start - GAP_KEEP_MS > start + MIN_LINE_MS:
        limit = min(limit, next_start - GAP_KEEP_MS)
        inside = [value for value in inside if value <= limit]
    if inside:
        # 浊音结束时往往还拖着半个字，补一小段尾巴，但不超过窗口上限。
        return min(limit, inside[-1] + TAIL_MS // 2)
    if original_end is not None and start < original_end <= limit:
        return original_end
    return min(limit, start + 2000)


def fit_lines(doc, raw, field="main", max_shift=MAX_SHIFT_MS):
    """把整首歌词贴到人声起点序列上，返回**提案**（不改文档）。

    返回 ``{"document": 贴好的完整文档, "lines": [...], "notice": "...", ...}``。
    界面拿 ``document`` 做预览 / 应用 —— 服务端算，页面只管"预览还是应用"。
    """
    if field not in ("main", "translation"):
        raise ValueError("未知对齐轨道")
    if not 0 <= max_shift <= 20000:
        raise ValueError("搜索范围无效")
    # 先看歌词再看音频：没有歌词行时不必去解码，提示也更准。
    indexed = [(index, group) for index, group in enumerate(doc.groups)
               if getattr(group, field).text.strip()]
    if not indexed:
        raise ValueError("该轨道没有歌词行")
    samples = read_pcm(raw)
    info = analyse(samples)
    onsets = info["onsets"]
    if not onsets:
        raise ValueError("没有检测到可靠的人声起点：这首歌可能没有 vocal，或音量过低")
    duration = info["duration_ms"]

    originals = [int(getattr(group, field).start if getattr(group, field).start is not None
                    else group.start) for _, group in indexed]
    plans = [_candidates(value, onsets, max_shift) for value in originals]

    # ---- 单调路径（Viterbi）--------------------------------------------------
    # 代价单位统一成"毫秒当量"，几项可以直接相加。
    best = [None] * len(plans)
    back = [None] * len(plans)
    for i, candidates in enumerate(plans):
        row = []
        for at, off_cost, is_original in candidates:
            move = min(abs(at - originals[i]), 1500)
            cost = off_cost + W_MOVE * move + (KEEP_PENALTY_MS if is_original else 0)
            if _bare(onsets, at):
                cost += BARE_PENALTY_MS
            if i == 0:
                row.append((cost, None))
                continue
            pick = None
            for j, (previous_at, _, _) in enumerate(plans[i - 1]):
                if best[i - 1][j] is None:
                    continue
                if at - previous_at < MIN_LINE_MS:
                    continue            # 硬约束：后一行必须**明显**晚于前一行
                shape = W_SHAPE * abs((at - previous_at) - (originals[i] - originals[i - 1]))
                total = best[i - 1][j] + shape
                if pick is None or total < pick[0]:
                    pick = (total, j)
            row.append((None if pick is None else cost + pick[0], None if pick is None else pick[1]))
        best[i] = [item[0] for item in row]
        back[i] = [item[1] for item in row]

    # 末行取最优；再回溯。整条路径都不可行时（例如候选全挤在一起）退回原时间。
    reachable = [(value, j) for j, value in enumerate(best[-1]) if value is not None]
    chosen = [None] * len(plans)
    if reachable:
        _, at = min(reachable)
        for i in range(len(plans) - 1, -1, -1):
            chosen[i] = at
            at = back[i][at]
            if i and at is None:
                chosen = [None] * len(plans)
                break
    if any(value is None for value in chosen):
        # 路径接不上（例如某行窗口内一个候选都没有）→ **整首退回原时间**，
        # 不给单行编一个时间。
        for i, candidates in enumerate(plans):
            chosen[i] = min(range(len(candidates)), key=lambda k: candidates[k][1])

    document = deepcopy(doc)
    lines = []
    moved = kept = weak = 0
    for order, (index, group) in enumerate(indexed):
        candidates = plans[order]
        at, off_cost, is_original = candidates[chosen[order]]
        next_at = None
        if order + 1 < len(indexed):
            next_at = plans[order + 1][chosen[order + 1]][0]
        track = getattr(group, field)
        original_end = int(track.end) if track.end is not None else None
        end = _line_end(at, original_end, info["offsets"], next_at, duration)
        delta = at - originals[order]
        if is_original:
            confidence, reason = "kept", "没有找到更贴的声音起点，保持原时间"
            kept += 1
        else:
            hit = off_cost <= 1e-9
            confidence = "snapped" if hit else "weak"
            reason = ("已贴到人声起点（移动 %+d ms）" % delta)
            if not hit:
                reason = "贴到最近的可用起点（移动 %+d ms，置信偏低）" % delta
                weak += 1
            else:
                moved += 1
        if abs(delta) >= 500 and confidence == "snapped":
            reason += "；偏移较大，务必试听"
        target = getattr(document.groups[index], field)
        target.start = at
        target.end = end
        if field == "main":
            # **句首只有一个事实**：`group.start` 与 `main.start` 必须一起变。
            # 只改一个的话，时间轴（读 track）与表格（读 group）会各说各话。
            document.groups[index].start = at
        if target.start != originals[order] or track.end != end:
            # 逐字轴跟着整行一起走（否则字还在旧时间上，行内就散了）
            shift = at - originals[order]
            if shift:
                for word in target.words:
                    if word.start is not None:
                        word.start = max(0, word.start + shift)
        lines.append({"index": index, "old_start": originals[order], "old_end": original_end,
                      "start": at, "end": end, "moved_ms": delta,
                      "confidence": confidence, "reason": reason})

    notice = ("本机能量贴唱：从音频里找到 %d 个人声起点。"
              "%d 行贴到起点、%d 行置信偏低、%d 行保持原时间。"
              "%s" % (len(onsets), moved, weak, kept,
                      "打开「预览」可以播放着核对贴合情况，确认后再应用（可撤销）。"))
    return {"field": field, "duration_ms": duration, "hop_ms": info["hop_ms"],
            "threshold": round(info["threshold"], 4), "onsets": len(onsets),
            "offsets": len(info["offsets"]), "lines": lines,
            "moved": moved, "weak": weak, "kept": kept,
            "document": document.to_dict() if hasattr(document, "to_dict") else document,
            "notice": notice}
