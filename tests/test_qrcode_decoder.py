"""Decodes the encoder's output with an independent QR reader.

The structural tests in ``test_qrcode.py`` only prove the symbol has the right
*shape*. Whether the bits land in the right cells is a separate question, and it
is the only one that decides whether a phone can read the code. This module
renders each produced matrix to a PNG and feeds it to ``zxing-cpp``.

Two real bugs were caught this way and neither showed up structurally:

* the format-information block was written least-significant-bit first, so the
  symbol's declared mask did not match its own masking;
* the Reed-Solomon block split handed the remainder codewords to the *first*
  blocks instead of the last, which only breaks the versions where the data does
  not divide evenly.

``zxing-cpp`` and ``Pillow`` are verification-only dependencies -- they are not
needed to *produce* a QR code. When they are missing these tests skip rather
than fail, so the zero-dependency core still tests clean on a bare machine.
"""

import sys
import unittest

sys.path.insert(0, r"C:\Users\Administrator\WorkBuddy\2026-09-22-12-52-10\SaltLRCStudio")

from saltlrc.tools import qrcode

try:
    import zxingcpp
    from PIL import Image
    HAVE_DECODER = True
except ImportError:                     # pragma: no cover - environment dependent
    HAVE_DECODER = False


def render(modules, scale=6, border=4):
    size = len(modules)
    side = size + border * 2
    image = Image.new("L", (side, side), 255)
    pixels = image.load()
    for row in range(size):
        for col in range(size):
            if modules[row][col]:
                pixels[col + border, row + border] = 0
    return image.resize((side * scale, side * scale), Image.NEAREST)


def decode(modules, scale=6):
    results = zxingcpp.read_barcodes(render(modules, scale))
    return results[0].text if results else None


@unittest.skipUnless(HAVE_DECODER, "zxing-cpp / Pillow not installed")
class DecoderTests(unittest.TestCase):
    def test_every_version_and_level_at_capacity(self):
        """Fill each version exactly and confirm a decoder reads it back.

        Boundaries matter more than the middle here: the last codeword before
        padding, and the block splits, are where the two bugs above lived.
        """
        for level in ("L", "M"):
            for version in range(1, 11):
                if level not in qrcode.SPEC[version][1]:
                    continue
                capacity = qrcode._capacity_bytes(version, level)
                for length in (1, capacity // 2, capacity):
                    if length < 1:
                        continue
                    payload = "z" * length
                    encoded_version, mask, modules = qrcode._encode(payload, level)
                    if encoded_version != version:
                        continue
                    self.assertEqual(decode(modules), payload,
                                     "v%d %s len %d mask %d" %
                                     (version, level, length, mask))

    def test_realistic_login_url(self):
        # 128 hex characters of key, the shape a Netease scan-login URL has.
        url = "https://music.163.com/login?codekey=" + "a1b2c3d4" * 16
        version, mask, modules = qrcode._encode(url, "M")
        self.assertEqual(decode(modules), url)
        self.assertLessEqual(version, 10)

    def test_utf8_payload_survives_the_round_trip(self):
        text = "中文歌词测试 · 日本語のテスト"
        modules = qrcode.matrix(text, "M")
        self.assertEqual(decode(modules), text)

    def test_ascii_punctuation_round_trips(self):
        text = "abc, def; ghi! (test) [ok] {yes} 100%"
        modules = qrcode.matrix(text, "M")
        self.assertEqual(decode(modules), text)

    def test_whitespace_is_preserved(self):
        # Lyrics routinely carry leading and repeated spaces; a byte-mode
        # encoder must not trim or merge them.
        text = "  two  spaces  and a trailing one "
        modules = qrcode.matrix(text, "M")
        self.assertEqual(decode(modules), text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
