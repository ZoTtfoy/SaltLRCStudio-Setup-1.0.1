"""Contract tests for optional ASR without pretending to run absent model weights.

ASR (faster-whisper / HeartTranscriptor) is an **optional** extra: the app
declares ``dependencies = []``, and ``saltlrc/transcriber.py`` is the only module
that imports a third-party package. These tests stub the *third-party* libraries
and assert the failure path is readable.
"""
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from test_audio import tone_audio
from saltlrc.transcriber import transcribe


class FakeArray(list):
    """Stands in for ``numpy.ndarray``: the code only does ``array / constant``."""

    def __truediv__(self, other):
        return FakeArray(value / other for value in self)


def numpy_stub():
    """A numpy stand-in good enough for ``np.asarray(samples, dtype=np.float32) / 32768``.

    The contract under test is *how we call* ``WhisperModel`` (local-only, int8,
    word timestamps).
    """
    return types.SimpleNamespace(float32='float32',
                                 asarray=lambda values, dtype=None: FakeArray(values))


class AdapterTests(unittest.TestCase):
    def test_faster_whisper_local_only_contract(self):
        seen = {}

        class Model:
            def __init__(self, path, **kwargs):
                seen.update(kwargs)

            def transcribe(self, audio, **kwargs):
                seen['options'] = kwargs
                word = types.SimpleNamespace(start=1.2, end=2.8, word='こんにちは')
                segment = types.SimpleNamespace(no_speech_prob=.1, words=[word])
                info = types.SimpleNamespace(language='ja', language_probability=.95)
                return iter([segment]), info

        with tempfile.TemporaryDirectory() as root:
            with patch.dict(sys.modules, {'numpy': numpy_stub(),
                                          'faster_whisper': types.SimpleNamespace(WhisperModel=Model)}):
                result = transcribe(tone_audio(), 'faster-whisper', root)
        self.assertTrue(seen['local_files_only'])
        self.assertEqual(seen['compute_type'], 'int8')
        self.assertTrue(seen['options']['word_timestamps'])
        self.assertEqual(result['language'], 'ja')
        self.assertEqual(result['segments'][0]['start'], 1.2)

    def test_a_missing_optional_dependency_is_refused_in_plain_chinese(self):
        """缺了可选依赖（numpy）抛出带中文原因的 `ValueError`，不是 `ModuleNotFoundError`。

        这条断言用 `sys.modules` 里放 `None` 让 import 失败，所以在"装过 /
        没装过可选依赖"的机器上都成立。
        """
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(sys.modules, {'numpy': None}):
                with self.assertRaisesRegex(ValueError, 'setup-ai') as caught:
                    transcribe(tone_audio(), 'faster-whisper', root)
        self.assertIn('numpy', str(caught.exception))

    def test_missing_weights_do_not_trigger_network_download(self):
        with self.assertRaisesRegex(ValueError, '模型尚未下载'):
            transcribe(tone_audio(), 'faster-whisper', '/nonexistent-salt-model')
