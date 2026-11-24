"""Isolated inference worker: terminated safely when a user cancels a job."""
import json
import sys
from pathlib import Path
from .document import Document
from .alignment import match_transcript, source_track
from .acoustic import onset_proposals
from .transcriber import transcribe


def run(directory):
    root = Path(directory)
    config = json.loads((root / "input.json").read_text(encoding="utf-8"))
    doc = Document.from_dict(config["document"])
    raw = (root / "audio.wav").read_bytes()
    try:
        if config["engine"] == "acoustic":
            field = config["field"] if config["field"] != "auto" else source_track(doc)
            result = onset_proposals(doc, raw, field, config["radius"])
        else:
            transcript = transcribe(raw, config["engine"], config["model_path"], config.get("language"), config["device"])
            result = match_transcript(doc, transcript, config["field"], config["max_shift"])
            result["transcript"] = transcript
        (root / "result.json").write_text(json.dumps({"result": result}, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        (root / "result.json").write_text(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), encoding="utf-8")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1]))
