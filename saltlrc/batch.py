import json
from pathlib import Path
from . import audioformats
from .parser import parse
from .repair import decode
from .exporter import export
from .validator import validate

LYRICS = {".lrc", ".spl", ".spw", ".lar"}
# 唯一权威：saltlrc/audioformats.py
AUDIO = audioformats.extensions()


def run_batch(source, output, salt=True, encoding=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_dir():
        raise ValueError("输入目录不存在")
    if output == source or source.is_relative_to(output):
        raise ValueError("输出目录不能等于输入目录或成为其父目录")
    files = sorted(p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in LYRICS and not p.resolve().is_relative_to(output))
    output.mkdir(parents=True, exist_ok=True)
    report = []
    for path in files:
        row = {"file": str(path.relative_to(source)), "status": "failed", "audio": [], "errors": [], "warnings": []}
        try:
            row["audio"] = [p.name for p in path.parent.iterdir() if p.stem.casefold() == path.stem.casefold() and p.suffix.lower() in AUDIO]
            if encoding:
                text, note = path.read_bytes().decode(encoding), encoding
            else:
                text, note = decode(path.read_bytes())
            doc = parse(text)
            doc.notes.append(note)
            if not row["audio"]:
                doc.notes.append("未找到同目录同名音频，未执行音频校验")
            row.update(validate(doc, salt))
            content = export(doc, salt)
            target = output / path.relative_to(source).with_suffix(".lrc")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
            row["status"] = "exported"
            row["output"] = str(target)
        except (ValueError, OSError, UnicodeError, LookupError) as error:
            row["errors"] = list(dict.fromkeys(row["errors"] + [str(error)]))
        report.append(row)
    # Exclusive report creation keeps earlier reports intact too.
    index = 1
    while True:
        target = output / f"batch-report-{index}.json"
        try:
            with target.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
            break
        except FileExistsError:
            index += 1
    return report
