"""Pure-stdlib QR encoder, sized for login QR codes.

Implements the encoder with no third-party dependency.

Scope: byte mode, error-correction level L/M, versions 1–10. A login URL is
~120 bytes and lands around version 6 at level M. Requests beyond version 10
raise instead of silently truncating.

References: ISO/IEC 18004. The tables below (capacity, alignment centres,
level bits) are the standard's own.
"""

import re

# Error-correction levels: the two-bit indicator plus the generator degree.
# Only L and M are reachable from the login URL sizes we handle.
LEVELS = {
    "L": {"bits": 0b01, "poly": 0b001011100},
    "M": {"bits": 0b00, "poly": 0b010110111},
    "Q": {"bits": 0b11, "poly": 0b111011101},
    "H": {"bits": 0b10, "poly": 0b101110011},
}

# Total codewords (data + error correction) per version, and the number of
# error-correction codewords per block, for levels L and M only.
# (version): (total_codewords, {level: (ec_per_block, blocks)})
SPEC = {
    1:  (26,   {"L": (7, 1),   "M": (10, 1)}),
    2:  (44,   {"L": (10, 1),  "M": (16, 1)}),
    3:  (70,   {"L": (15, 1),  "M": (26, 1)}),
    4:  (100,  {"L": (20, 1),  "M": (18, 2)}),
    5:  (134,  {"L": (26, 1),  "M": (24, 2)}),
    6:  (172,  {"L": (18, 2),  "M": (16, 4)}),
    7:  (196,  {"L": (20, 2),  "M": (18, 4)}),
    8:  (242,  {"L": (24, 2),  "M": (22, 4)}),
    9:  (292,  {"L": (30, 2),  "M": (22, 5)}),
    10: (346,  {"L": (18, 4),  "M": (26, 5)}),
}

# The standard does not split the data codewords perfectly evenly: when the
# total does not divide by the block count, the block lengths differ.
# Version 8-M (78 data codewords in 4 blocks) is the first case in this range.
GROUP_RULE = "first blocks take the remainder"

# Alignment pattern centres per version (empty for version 1).
ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

# The version information block only appears from version 7 up.
VERSION_INFO = {
    7: 0x07C94, 8: 0x085BC, 9: 0x09A99, 10: 0x0A4D3,
}


class QrError(ValueError):
    """Raised when the payload cannot be encoded at the requested level."""


def _data_codewords(version, level):
    """Total data (non-EC) codewords for this version/level."""
    total, per_level = SPEC[version]
    per_block, blocks = per_level[level]
    return total - per_block * blocks


def _length_bits(version):
    """Byte-mode character-count indicator width.

    The standard widens it at version 10: versions 1-9 use 8 bits, 10+ use 16.
    """
    return 16 if version >= 10 else 8


def _capacity_bytes(version, level):
    """How many payload bytes fit in the data codewords."""
    # 4 bits of mode + the character count, then 8 bits per byte.
    header_bits = 4 + _length_bits(version)
    return (_data_codewords(version, level) * 8 - header_bits) // 8


def _pick_version(length, level):
    for version in range(1, 11):
        if level not in SPEC[version][1]:
            continue
        if length <= _capacity_bytes(version, level):
            return version
    raise QrError("内容过长（%d 字节），无法用版本 1-10 的二维码编码" % length)


def _append(buffer, value, length):
    for shift in range(length - 1, -1, -1):
        buffer["bits"].append((value >> shift) & 1)


def _encode_data(payload, version, level):
    """Mode + length + data + terminator + padding, as a bit list."""
    buffer = {"bits": []}
    _append(buffer, 0b0100, 4)                       # byte mode
    _append(buffer, len(payload), _length_bits(version))
    for byte in payload:
        _append(buffer, byte, 8)

    capacity = _data_codewords(version, level) * 8
    # Terminator: up to four zero bits, then pad to a byte boundary.
    _append(buffer, 0, min(4, capacity - len(buffer["bits"])))
    while len(buffer["bits"]) % 8:
        buffer["bits"].append(0)
    # Pad bytes alternate 0xEC / 0x11 by the standard.
    pad = [0xEC, 0x11]
    index = 0
    while len(buffer["bits"]) < capacity:
        _append(buffer, pad[index % 2], 8)
        index += 1
    return buffer["bits"]


def _to_codewords(bits):
    return [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]


def _gf_multiply(a, b):
    result = 0
    while b:
        if b & 1:
            result ^= a
        b >>= 1
        a <<= 1
        if a & 0x100:
            a ^= 0x11D          # primitive polynomial for QR's GF(256)
    return result


def _generator(degree):
    poly = [1]
    for i in range(degree):
        # Multiply the running polynomial by (x - α^i).
        nxt = [0] * (len(poly) + 1)
        for j, coefficient in enumerate(poly):
            nxt[j] ^= _gf_multiply(coefficient, 1)
            nxt[j + 1] ^= _gf_multiply(coefficient, _gf_power(i))
        poly = nxt
    return poly


_GF_EXP = None


def _gf_power(exponent):
    """α^exponent in GF(256), built once."""
    global _GF_EXP
    if _GF_EXP is None:
        table = [0] * 512
        value = 1
        for i in range(255):
            table[i] = value
            value = _gf_multiply(value, 2)
        for i in range(255, 512):
            table[i] = table[i - 255]
        _GF_EXP = table
    return _GF_EXP[exponent % 255]


def _reed_solomon(data, ec_count):
    generator = _generator(ec_count)
    remainder = list(data) + [0] * ec_count
    for i in range(len(data)):
        factor = remainder[i]
        if factor == 0:
            continue
        for j, coefficient in enumerate(generator):
            remainder[i + j] ^= _gf_multiply(coefficient, factor)
    return remainder[len(data):]


def _split_blocks(codewords, blocks, kind):
    """Split a codeword list into ``blocks`` groups.

    The standard does not require an even split: when the total is not a
    multiple of the block count, the *later* blocks take one extra codeword
    (version 8-M is the first such case in our range, with lengths
    38/38/39/39 rather than an even 38.5 apiece).
    """
    count = len(codewords)
    if count % blocks:
        short = count // blocks
        extra = count - short * blocks
        lengths = [short] * (blocks - extra) + [short + 1] * extra
    else:
        lengths = [count // blocks] * blocks
    if sum(lengths) != count:
        raise QrError("内部错误：%s 码字无法按 %d 块划分" % (kind, blocks))
    out = []
    start = 0
    for length in lengths:
        out.append(codewords[start:start + length])
        start += length
    return out


def _interleave(blocks_data, blocks_ec, per_block_ec, data_lengths):
    """Interleave data then EC codewords for placement.

    The standard interleaves so a burst error damages a bit of every block,
    which the per-block correction can still repair.
    """
    out = []
    for index in range(max(data_lengths)):
        for block, length in zip(blocks_data, data_lengths):
            if index < length:
                out.append(block[index])
    for index in range(per_block_ec):
        for block in blocks_ec:
            out.append(block[index])
    return out


def _matrix(version):
    size = version * 4 + 17
    # None means "not yet set"; False/True are modules. Reserving a sentinel
    # keeps the function patterns from being overwritten by later data.
    return [[None] * size for _ in range(size)]


def _place_finder(matrix, row, col):
    for y in range(-1, 8):
        for x in range(-1, 8):
            ry, rx = row + y, col + x
            if not (0 <= ry < len(matrix) and 0 <= rx < len(matrix)):
                continue
            if 0 <= y <= 6 and 0 <= x <= 6:
                edge = y in (0, 6) or x in (0, 6)
                core = 2 <= y <= 4 and 2 <= x <= 4
                matrix[ry][rx] = bool(edge or core)
            else:
                matrix[ry][rx] = False       # separator


def _place_patterns(matrix, version):
    size = len(matrix)
    _place_finder(matrix, 0, 0)
    _place_finder(matrix, 0, size - 7)
    _place_finder(matrix, size - 7, 0)
    # Timing patterns.
    for i in range(8, size - 8):
        matrix[6][i] = (i % 2 == 0)
        matrix[i][6] = (i % 2 == 0)
    # Alignment patterns, skipping the three finder corners.
    centres = ALIGNMENT[version]
    for row in centres:
        for col in centres:
            near_finder = ((row <= 8 and col <= 8) or
                           (row <= 8 and col >= size - 9) or
                           (row >= size - 9 and col <= 8))
            if near_finder:
                continue
            for y in range(-2, 3):
                for x in range(-2, 3):
                    edge = max(abs(y), abs(x))
                    matrix[row + y][col + x] = edge != 1
    # Reserve the format area (values are written later by _place_format).
    for i in range(9):
        if matrix[8][i] is None:
            matrix[8][i] = False
        if matrix[i][8] is None:
            matrix[i][8] = False
    for i in range(8):
        matrix[8][size - 1 - i] = False
        matrix[size - 1 - i][8] = False
    matrix[size - 8][8] = True               # the always-dark module


def _reserved(version, size):
    """Positions owned by function patterns and format information.

    Data placement and masking both skip these cells.
    """
    taken = [[False] * size for _ in range(size)]

    def mark(row, col):
        if 0 <= row < size and 0 <= col < size:
            taken[row][col] = True

    for row, col in ((0, 0), (0, size - 7), (size - 7, 0)):
        for y in range(-1, 8):
            for x in range(-1, 8):
                mark(row + y, col + x)
    # Timing patterns.
    for i in range(size):
        mark(6, i)
        mark(i, 6)
    # Alignment patterns, skipping the three finder corners.
    for row in ALIGNMENT[version]:
        for col in ALIGNMENT[version]:
            near_finder = ((row <= 8 and col <= 8) or
                           (row <= 8 and col >= size - 9) or
                           (row >= size - 9 and col <= 8))
            if near_finder:
                continue
            for y in range(-2, 3):
                for x in range(-2, 3):
                    mark(row + y, col + x)
    # Format information, both copies.
    for i in range(9):
        mark(8, i)
        mark(i, 8)
    for i in range(8):
        mark(8, size - 1 - i)
        mark(size - 1 - i, 8)
    mark(size - 8, 8)
    # Version information block, versions 7 and up.
    if version >= 7:
        for i in range(6):
            for j in range(3):
                mark(size - 11 + j, i)
                mark(i, size - 11 + j)
    return taken


def _place_data(matrix, version, codewords):
    size = len(matrix)
    taken = _reserved(version, size)
    bits = []
    for codeword in codewords:
        for shift in range(7, -1, -1):
            bits.append((codeword >> shift) & 1)
    index = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:                 # the vertical timing pattern column
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for offset in (0, 1):
                c = col - offset
                if taken[row][c]:
                    continue
                value = bits[index] if index < len(bits) else 0
                index += 1
                # Unmasked bit for now; the winning mask is applied afterwards.
                matrix[row][c] = bool(value)
        upward = not upward
        col -= 2
    return matrix


MASK_FUNCTIONS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _apply_mask(matrix, version, mask):
    size = len(matrix)
    taken = _reserved(version, size)
    function = MASK_FUNCTIONS[mask]
    for row in range(size):
        for col in range(size):
            if taken[row][col]:
                continue
            if function(row, col):
                matrix[row][col] = not matrix[row][col]


def _place_format(matrix, version, level, mask):
    size = len(matrix)
    data = (LEVELS[level]["bits"] << 3) | mask
    # BCH(15,5) with the standard generator 0b10100110111.
    value = data << 10
    for i in range(4, -1, -1):
        if value & (1 << (i + 10)):
            value ^= 0b10100110111 << i
    combined = ((data << 10) | value) ^ 0b101010000010010

    for i in range(15):
        # Bit i is counted from the *most* significant end: the standard lays
        # the format string out starting with its top bit next to the finder.
        bit = bool((combined >> (14 - i)) & 1)
        # Copy 1: around the top-left finder, skipping the timing row/column.
        if i < 6:
            matrix[8][i] = bit
        elif i == 6:
            matrix[8][7] = bit
        elif i == 7:
            matrix[8][8] = bit
        elif i == 8:
            matrix[7][8] = bit
        else:
            matrix[14 - i][8] = bit
        # Copy 2: seven bits up the bottom-left column, eight along the
        # top-right row. It stops at row size-7; row size-8 is the always-dark
        # module.
        if i < 7:
            matrix[size - 1 - i][8] = bit
        else:
            matrix[8][size - 15 + i] = bit
    matrix[size - 8][8] = True               # keep the always-dark module dark
    if version >= 7:
        bits = VERSION_INFO[version]
        for i in range(18):
            # The version block runs the other way from the format block: it is
            # laid out least-significant-bit first.
            bit = bool((bits >> i) & 1)
            row, col = i // 3, i % 3
            matrix[size - 11 + col][row] = bit
            matrix[row][size - 11 + col] = bit


def _penalty(matrix):
    """The standard's four penalty rules, scored for mask selection."""
    size = len(matrix)
    score = 0
    # Rule 1: runs of five or more same-coloured modules.
    for line in list(matrix) + [list(col) for col in zip(*matrix)]:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    # Rule 2: 2x2 blocks of one colour.
    for row in range(size - 1):
        for col in range(size - 1):
            if (matrix[row][col] == matrix[row][col + 1] ==
                    matrix[row + 1][col] == matrix[row + 1][col + 1]):
                score += 3
    # Rule 3: the finder-like 1:1:3:1:1 pattern.
    pattern_a = [True, False, True, True, True, False, True, False, False, False, False]
    pattern_b = [False, False, False, False, True, False, True, True, True, False, True]
    for line in list(matrix) + [list(col) for col in zip(*matrix)]:
        for i in range(size - 10):
            window = line[i:i + 11]
            if window == pattern_a or window == pattern_b:
                score += 40
    # Rule 4: deviation from a 50% dark ratio.
    dark = sum(1 for row in matrix for cell in row if cell)
    percent = dark * 100 / (size * size)
    score += 10 * (abs(percent - 50) // 5)
    return score


def _encode(payload, level="M"):
    bytes_payload = payload.encode("utf-8")
    version = _pick_version(len(bytes_payload), level)
    bits = _encode_data(bytes_payload, version, level)
    data_codewords = _to_codewords(bits)
    per_block_ec, blocks = SPEC[version][1][level]
    blocks_data = _split_blocks(data_codewords, blocks, "数据")
    data_lengths = [len(block) for block in blocks_data]
    ec_codewords = []
    for block in blocks_data:
        ec_codewords += _reed_solomon(block, per_block_ec)
    blocks_ec = _split_blocks(ec_codewords, blocks, "纠错")
    final = _interleave(blocks_data, blocks_ec, per_block_ec, data_lengths)
    if len(final) != SPEC[version][0]:
        raise QrError("内部错误：最终码字数 %d 与版本 %d 不符" % (len(final), version))

    best = None
    for mask in range(8):
        matrix = _matrix(version)
        _place_patterns(matrix, version)
        matrix = _place_data(matrix, version, final)
        _apply_mask(matrix, version, mask)
        _place_format(matrix, version, level, mask)
        score = _penalty(matrix)
        if best is None or score < best[0]:
            best = (score, mask, matrix)
    return version, best[1], best[2]


def matrix(text, level="M"):
    """QR module matrix as a list of boolean rows (True = dark)."""
    return _encode(text, level)[2]


def svg(text, level="M", scale=6, border=4, dark="#0c2329", light="#ffffff"):
    """Render a QR code as a standalone SVG document string.

    The default is dark modules on a light background, as the standard
    specifies. An *inverted* code (light modules on dark) is readable only by
    scanners that explicitly look for it.

    ``border`` is the quiet zone in modules and must stay at least 4.
    """
    if border < 4:
        raise QrError("静区（border）至少需要 4 个模块，否则很多扫码器无法识别")
    modules = matrix(text, level)
    size = len(modules)
    side = (size + border * 2) * scale
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" shape-rendering="crispEdges">' % (side, side, side, side),
        '<rect width="%d" height="%d" fill="%s"/>' % (side, side, light),
    ]
    for row, line in enumerate(modules):
        col = 0
        while col < size:
            if not line[col]:
                col += 1
                continue
            start = col
            while col < size and line[col]:
                col += 1
            x = (border + start) * scale
            y = (border + row) * scale
            parts.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s"/>'
                         % (x, y, (col - start) * scale, scale, dark))
    parts.append("</svg>")
    return "".join(parts)


def data_uri(text, level="M", scale=6, border=4, dark="#0c2329", light="#ffffff"):
    """Same QR, inlined as a data URI."""
    import base64

    markup = svg(text, level, scale, border, dark, light)
    encoded = base64.b64encode(markup.encode("utf-8")).decode("ascii")
    return "data:image/svg+xml;base64," + encoded


def _rgb(colour):
    """Parse ``#rgb`` / ``#rrggbb`` into an (r, g, b) tuple."""
    value = (colour or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    if len(value) != 6:
        raise QrError("颜色必须是 #rgb 或 #rrggbb 形式：%r" % colour)
    try:
        return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        raise QrError("颜色必须是 #rgb 或 #rrggbb 形式：%r" % colour) from None


def png(text, level="M", scale=6, border=4, dark="#0c2329", light="#ffffff"):
    """Render a QR code as PNG bytes.

    Written directly: one zlib stream, no interlacing, no third-party
    dependency.

    ``scale`` is in pixels per module; the quiet zone follows the same rule as
    the SVG output.
    """
    import struct
    import zlib

    if border < 4:
        raise QrError("静区（border）至少需要 4 个模块，否则很多扫码器无法识别")
    modules = matrix(text, level)
    size = len(modules)
    side = (size + border * 2) * scale
    dark_rgb, light_rgb = _rgb(dark), _rgb(light)

    raw = bytearray()
    for row in range(side):
        raw.append(0)                                   # filter type 0 (None)
        module_row = (row // scale) - border
        inside_row = 0 <= module_row < size
        for col in range(side):
            module_col = (col // scale) - border
            is_dark = (inside_row and 0 <= module_col < size
                       and modules[module_row][module_col])
            raw += bytes(dark_rgb if is_dark else light_rgb)

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)   # 8-bit truecolour
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def png_data_uri(text, level="M", scale=6, border=4, dark="#0c2329", light="#ffffff"):
    """The PNG above as a data URI, for an ``<img src>`` in a webview.

    Used ahead of :func:`data_uri` when the caller can accept PNG.
    """
    import base64

    encoded = base64.b64encode(png(text, level, scale, border, dark, light)).decode("ascii")
    return "data:image/png;base64," + encoded
