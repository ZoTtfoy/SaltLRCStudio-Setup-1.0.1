import re
from copy import deepcopy


def japanese(text):
    return bool(re.search(r"[\u3040-\u30ff]", text))


def chinese_candidate(text):
    return bool(re.search(r"[\u3400-\u9fff]", text)) and not japanese(text)


def prefer_chinese(doc):
    for group in doc.groups:
        if japanese(group.main.text) and chinese_candidate(group.translation.text):
            group.main, group.translation = group.translation, group.main
            doc.notes.append("已将中文候选轨放到主歌词；时间轴随各自文字保留")
    return doc


def swap(group):
    group.main, group.translation = group.translation, group.main


def shift(doc, delta):
    if type(delta) is not int:
        raise ValueError("偏移量必须是整数毫秒")
    result = deepcopy(doc)
    for group in result.groups:
        group.start += delta
        for track in (group.main, group.translation):
            if track.start is not None:
                track.start += delta
            for word in track.words:
                word.start += delta
            if track.end is not None:
                track.end += delta
        times = [group.start] + [w.start for t in (group.main, group.translation) for w in t.words]
        times += [t.end for t in (group.main, group.translation) if t.end is not None]
        times += [t.start for t in (group.main, group.translation) if t.start is not None]
        if min(times) < 0:
            raise ValueError("偏移后出现负时间，未应用此次修改")
    return result
