import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from .acoustic import read_pcm
from .document import Document


def _default_models_dir():
    """Where optional ASR weights live.

    A packaged build is read-only and unpacks to a temporary folder, so models
    belong next to the executable (or wherever SALTLRC_MODELS points).
    """
    override = os.environ.get("SALTLRC_MODELS")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "models"
    return Path(__file__).resolve().parent.parent / "models"


def _worker_command(root):
    """Spawn the inference worker.

    A PyInstaller build cannot run ``python -m saltlrc.worker``, so the frozen
    executable exposes a ``--worker`` switch that dispatches to the same code.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", str(root)]
    return [sys.executable, "-m", "saltlrc.worker", str(root)]


class Jobs:
    def __init__(self, models=None, device="cpu"):
        self.models = Path(models or _default_models_dir()).resolve()
        self.device = device
        self.entries = {}
        self.lock = threading.Lock()

    def capabilities(self):
        return {"faster_whisper": importlib.util.find_spec("faster_whisper") is not None,
                "heart": all(importlib.util.find_spec(n) is not None for n in ("torch", "transformers")),
                "base_model": (self.models / "faster-whisper-base" / "model.bin").is_file(),
                "heart_model": (self.models / "HeartTranscriptor-oss" / "config.json").is_file(),
                "device": self.device, "acoustic": True}

    def start(self, data, raw):
        read_pcm(raw)
        doc = Document.from_dict(data["document"])
        engine = data.get("engine", "faster-whisper")
        if engine not in ("faster-whisper", "heart-transcriptor", "acoustic"):
            raise ValueError("未知分析引擎")
        field = data.get("field", "auto")
        if field not in ("auto", "main", "translation"):
            raise ValueError("未知对齐轨道")
        language = data.get("language") or None
        if language not in (None, "ja", "zh", "en", "ko", "fr", "de", "es"):
            raise ValueError("不支持的语言选项")
        capabilities = self.capabilities()
        if engine == "faster-whisper" and not (capabilities["faster_whisper"] and capabilities["base_model"]):
            raise ValueError("语音识别尚未就绪。请运行 setup-ai.ps1 安装依赖并下载模型；也可使用能量辅助微调")
        if engine == "heart-transcriptor" and not (capabilities["heart"] and capabilities["heart_model"]):
            raise ValueError("HeartTranscriptor 尚未就绪。请运行 setup-ai.ps1 -Engine heart")
        with self.lock:
            for identity in list(self.entries):
                self._status(identity)
            if any(j["state"] == "running" for j in self.entries.values()):
                raise ValueError("已有音频正在分析，请等待完成或取消")
            # Keep only the most recent 20 jobs; no audio retained after completion.
            while len(self.entries) >= 20:
                self.entries.pop(next(iter(self.entries)))
            folder = tempfile.TemporaryDirectory(prefix="saltlrc-")
            root = Path(folder.name)
            model_name = "faster-whisper-base" if engine == "faster-whisper" else "HeartTranscriptor-oss"
            config = {"document": doc.to_dict(), "engine": engine, "field": field, "language": language,
                      "max_shift": min(600000, max(1000, int(data.get("max_shift", 30000)))),
                      "radius": min(5000, max(100, int(data.get("radius", 1500)))),
                      "model_path": str(self.models / model_name), "device": self.device}
            (root / "input.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            (root / "audio.wav").write_bytes(raw)
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            try:
                process = subprocess.Popen(_worker_command(root),
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                                           cwd=Path(__file__).resolve().parent.parent, **kwargs)
            except Exception:
                folder.cleanup()
                raise
            identity = uuid.uuid4().hex
            self.entries[identity] = {"state": "running", "process": process, "folder": folder,
                                      "created": time.monotonic(), "engine": engine}
            return {"id": identity, "state": "running"}

    def _status(self, identity):
        if identity not in self.entries:
            raise ValueError("任务不存在或已过期")
        item = self.entries[identity]
        if item["state"] == "running":
            process = item["process"]
            if time.monotonic() - item["created"] > 1800:
                process.kill()
                process.wait()
                item.update(state="failed", error="分析超过 30 分钟，已终止；请分段处理或使用更小模型")
            elif process.poll() is not None:
                path = Path(item["folder"].name) / "result.json"
                payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"error": "分析进程异常退出"}
                item.update(payload)
                item["state"] = "failed" if "error" in payload else "completed"
            if item["state"] != "running":
                item["folder"].cleanup()
        return {k: v for k, v in item.items() if k not in ("process", "folder", "created")}

    def status(self, identity):
        with self.lock:
            return self._status(identity)

    def cancel(self, identity):
        with self.lock:
            item = self.entries.get(identity)
            if item and item["state"] == "running":
                item["process"].terminate()
                item["process"].wait(timeout=10)
                item["folder"].cleanup()
                item["state"] = "cancelled"
        return {"state": "cancelled"}

    def close(self):
        for identity in list(self.entries):
            self.cancel(identity)
