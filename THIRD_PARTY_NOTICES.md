# Open-source references and optional components

> Every entry below is an attribution, not a bundling claim: the optional components are
> installed separately, and no upstream licence file is redistributed with this project.

The SaltLRC parser, bilingual model, waveform renderer, matching algorithm and user interface are independently implemented. No upstream frontend code or artwork is copied.

| Project | Use in SaltLRC Studio | License / notice |
| --- | --- | --- |
| [King LRC Waveform Editor](https://github.com/TKCB/King-LRC-Waveform-Editor) | Interaction reference: waveform-backed lyric blocks, drag-to-time, zoom and multitrack synchronization. Reviewed commit `36c7f1e006a4abda3d9b2881c39d02bdfce68836`. No copied implementation. | MIT, Copyright (c) 2026 TKCB-Nm — licence text at the upstream repository (no licence file is redistributed here). |
| [HeartMuLa / heartlib](https://github.com/HeartMuLa/heartlib) and [HeartTranscriptor-oss](https://huggingface.co/HeartMuLa/HeartTranscriptor-oss) | Optional local lyric-transcription model adapter. The official standard Whisper architecture and example informed integration. Model loaded with Transformers directly; heartlib music generation/codecs are not bundled. Reviewed heartlib commit `ba0a7869a8cc44b58514289f34c8403c06e68ab8`. | Apache-2.0 — licence text at the upstream repository (no licence file is redistributed here). Model weights downloaded separately; verify their model card on download. |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Optional CPU INT8 / CUDA speech recognition and word timestamps; transitive CTranslate2/PyAV dependencies installed separately. | MIT — licence text at the upstream repository. |
| [langid.py](https://github.com/saffsd/langid.py) | Optional statistical text language identification. | BSD-3-Clause — licence text at the upstream repository. |
| [Transformers](https://github.com/huggingface/transformers) | Optional HeartTranscriptor inference and Whisper word-timestamp alignment. | Apache-2.0; dependency installed separately with its license. |
| [PyTorch](https://github.com/pytorch/pytorch) | Optional HeartTranscriptor tensor runtime. | BSD-style; dependency installed separately with its license. |
| [EasyLRC](https://lrc.isea.dev/) | Original v0.1 interaction reference for bilingual/word editing and preview. | No source or assets copied. |

No model weights, proprietary audio, credentials, dependency binaries, or external JavaScript/CDN assets are included in the project archive or repository backup. Network downloads are limited to the explicit setup script. Application inference is local-only.

An ASR timestamp reflects a model estimate, not verified human transcription. Matching coverage shown in the UI is text overlap, not calibrated acoustic confidence. The energy-only helper is original DSP code and is not an ASR fallback pretending to understand lyrics.
