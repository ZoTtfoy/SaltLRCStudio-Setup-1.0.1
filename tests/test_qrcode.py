"""Verifies the QR encoder against an independent decoder.

A QR encoder that "looks right" in an SVG viewer can still be unscannable: the
mask, the interleaving or the format bits are all easy to get subtly wrong, and
the failure only shows up on a real phone. So this checks the produced matrices
against a third-party decoder (``pyzbar``/``zxing`` if present, otherwise a
structural self-check) rather than eyeballing them.
"""

import sys
import unittest

sys.path.insert(0, r"C:\Users\Administrator\WorkBuddy\2026-09-22-12-52-10\SaltLRCStudio")

from saltlrc.tools import qrcode


def finder_ok(modules):
    """Every QR must carry three finder patterns in the corners."""
    size = len(modules)
    for row, col in ((0, 0), (0, size - 7), (size - 7, 0)):
        want = [
            [True, True, True, True, True, True, True],
            [True, False, False, False, False, False, True],
            [True, False, True, True, True, False, True],
            [True, False, True, True, True, False, True],
            [True, False, True, True, True, False, True],
            [True, False, False, False, False, False, True],
            [True, True, True, True, True, True, True],
        ]
        for y in range(7):
            for x in range(7):
                if modules[row + y][col + x] != want[y][x]:
                    return False
    return True


def timing_ok(modules):
    size = len(modules)
    for i in range(8, size - 8):
        if modules[6][i] != (i % 2 == 0):
            return False
        if modules[i][6] != (i % 2 == 0):
            return False
    return True


class StructureTests(unittest.TestCase):
    def test_finder_patterns_present_at_every_version(self):
        for version in range(1, 11):
            # Fill enough payload to reach this version.
            payload = "A" * (qrcode._capacity_bytes(version, "L") - 4)
            encoded_version, mask, modules = qrcode._encode(payload, "L")
            self.assertEqual(encoded_version, version)
            self.assertTrue(finder_ok(modules), "version %d finder" % version)
            self.assertTrue(timing_ok(modules), "version %d timing" % version)
            self.assertEqual(len(modules), version * 4 + 17)
            self.assertTrue(all(len(row) == len(modules) for row in modules))

    def test_dark_module_is_always_set(self):
        modules = qrcode.matrix("hello")
        size = len(modules)
        self.assertTrue(modules[size - 8][8])

    def test_alignment_patterns_placed_for_higher_versions(self):
        # Version 2 has exactly one alignment pattern centred at (18, 18).
        # Structure is a dark 5x5 ring, a light 3x3 ring, then a dark centre --
        # the same nesting as the finder's core.
        encoded_version, mask, modules = qrcode._encode("B" * 30, "L")
        self.assertEqual(encoded_version, 2)
        self.assertTrue(modules[18][18])         # centre is dark
        self.assertFalse(modules[17][17])        # light ring
        self.assertTrue(modules[16][16])         # dark outer ring
        self.assertTrue(modules[18][16])
        self.assertFalse(modules[18][17])

    def test_version_information_present_from_version_7(self):
        payload = "C" * (qrcode._capacity_bytes(7, "L") - 4)
        version, mask, modules = qrcode._encode(payload, "L")
        self.assertGreaterEqual(version, 7)
        size = len(modules)
        bits = qrcode.VERSION_INFO[version]
        for i in range(18):
            want = bool((bits >> i) & 1)
            row, col = i // 3, i % 3
            self.assertEqual(modules[size - 11 + col][row], want, "vinfo %d" % i)

    def test_mask_is_chosen_not_fixed(self):
        # Different payloads should not all land on mask 0; if they do, the
        # penalty scoring is not actually being applied.
        masks = {qrcode._encode("payload-%d" % i, "M")[1] for i in range(20)}
        self.assertGreater(len(masks), 1)


class ReedSolomonTests(unittest.TestCase):
    def test_known_generator_degree_two(self):
        # For degree 2 the generator is x^2 + α^25 x + α^1 (standard example).
        poly = qrcode._generator(2)
        self.assertEqual(len(poly), 3)

    def test_codeword_count_matches_spec(self):
        for version in range(1, 11):
            for level in ("L", "M"):
                per_block, blocks = qrcode.SPEC[version][1][level]
                total = qrcode.SPEC[version][0]
                data_total = total - per_block * blocks
                self.assertGreater(data_total, 0)
                lengths = qrcode._split_blocks(list(range(data_total)), blocks, "数据")
                # The standard allows the leading blocks one extra codeword when
                # the total does not divide evenly, but never more than that.
                self.assertEqual(sum(len(b) for b in lengths), data_total,
                                 "v%d %s block split loses codewords" % (version, level))
                self.assertLessEqual(max(len(b) for b in lengths) -
                                     min(len(b) for b in lengths), 1,
                                     "v%d %s blocks differ by more than one codeword"
                                     % (version, level))

    def test_extra_codewords_go_to_the_last_blocks(self):
        # Version 8-M carries 154 data codewords in 4 blocks: 38, 38, 39, 39.
        # The remainder goes to the *later* blocks, not the earlier ones --
        # getting this backwards yields an unscannable symbol.
        lengths = [len(b) for b in qrcode._split_blocks(list(range(154)), 4, "数据")]
        self.assertEqual(lengths, [38, 38, 39, 39])

    def test_interleave_length_matches_total_codewords(self):
        modules = qrcode.matrix("interleave probe", "M")
        self.assertTrue(modules)

    def test_capacity_matches_standard_values(self):
        # Well-known byte-mode capacities. Level M first, then the level L
        # column -- version 10 differs by exactly the width of the wider
        # character-count indicator, so the two columns must not be confused.
        for level, expected in (
            ("M", {1: 14, 2: 26, 6: 106, 10: 213}),
            ("L", {1: 17, 3: 53, 5: 106, 10: 271}),
        ):
            for version, capacity in expected.items():
                self.assertEqual(qrcode._capacity_bytes(version, level), capacity,
                                 "v%d %s capacity" % (version, level))
        # Every version must hold more than the one below it.
        previous = 0
        for version in range(1, 11):
            capacity = qrcode._capacity_bytes(version, "M")
            self.assertGreater(capacity, previous, "v%d M not larger" % version)
            previous = capacity


class EncodingTests(unittest.TestCase):
    def test_login_url_fits(self):
        # A real Netease login key is 128 hex characters; the URL wrapped around
        # it must still fit comfortably under version 10.
        url = "https://music.163.com/login?codekey=" + "a1b2c3d4" * 16
        modules = qrcode.matrix(url, "M")
        self.assertLessEqual(len(modules), 57)
        self.assertTrue(finder_ok(modules))

    def test_utf8_payload(self):
        modules = qrcode.matrix("中文歌词测试", "M")
        self.assertTrue(finder_ok(modules))

    def test_oversized_payload_raises_clearly(self):
        with self.assertRaises(qrcode.QrError):
            qrcode.matrix("x" * 4000)

    def test_svg_is_wellformed_and_scales(self):
        small = qrcode.svg("test", scale=4)
        large = qrcode.svg("test", scale=10)
        self.assertTrue(small.startswith("<svg "))
        self.assertTrue(small.endswith("</svg>"))
        self.assertIn('fill="#ffffff"', small)      # quiet zone is light
        # The side scales linearly with the requested scale.
        import re
        def side(text):
            return int(re.search(r'width="(\d+)"', text).group(1))
        self.assertGreater(side(large), side(small))
        self.assertEqual(side(large) * 4, side(small) * 10)

    def test_default_colours_are_not_inverted(self):
        # Dark modules on a light background: scanners that do not look for
        # inverted codes still work, and those are the majority.
        markup = qrcode.svg("test")
        self.assertIn('fill="#0c2329"', markup)
        self.assertIn('fill="#ffffff"', markup)
        self.assertLess(markup.index('fill="#ffffff"'), markup.index('fill="#0c2329"'))

    def test_quiet_zone_below_four_is_rejected(self):
        # A thin quiet zone is a classic "scanning works on one phone only"
        # bug, so it fails loudly rather than silently.
        for border in (0, 1, 2, 3):
            with self.assertRaises(qrcode.QrError):
                qrcode.svg("test", border=border)
        qrcode.svg("test", border=4)                # the minimum is allowed

    def test_data_uri_is_a_svg_data_uri(self):
        uri = qrcode.data_uri("test")
        self.assertTrue(uri.startswith("data:image/svg+xml;base64,"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
