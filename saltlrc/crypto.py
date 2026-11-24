"""Pure-python AES-128 and the Netease request envelopes.

No third-party dependency: the app ships a bare Python runtime and must stay
installable offline. AES-128 is small enough to implement directly, and the
Netease RSA step is plain modular exponentiation.
"""

import base64
import hashlib
import json
import random

SBOX = bytes([
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16])

RCON = bytes([0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36, 0x6c, 0xd8, 0xab, 0x4d])


def _xtime(value):
    return (((value << 1) ^ 0x1b) & 0xff) if (value & 0x80) else ((value << 1) & 0xff)


def _expand_key(key):
    if len(key) not in (16, 24, 32):
        raise ValueError("AES key must be 16, 24 or 32 bytes")
    nk = len(key) // 4
    rounds = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (rounds + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            temp = [SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return words, rounds


def _encrypt_block(block, words, rounds):
    state = list(block)

    def add_round_key(rnd):
        for c in range(4):
            for r in range(4):
                state[c * 4 + r] ^= words[rnd * 4 + c][r]

    add_round_key(0)
    for rnd in range(1, rounds + 1):
        state = [SBOX[b] for b in state]
        rows = [[state[c * 4 + r] for c in range(4)] for r in range(4)]
        rows = [rows[r][r:] + rows[r][:r] for r in range(4)]
        if rnd != rounds:
            for c in range(4):
                a0, a1, a2, a3 = rows[0][c], rows[1][c], rows[2][c], rows[3][c]
                rows[0][c] = _xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3
                rows[1][c] = a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3
                rows[2][c] = a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3)
                rows[3][c] = (_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3)
        state = [rows[r][c] for c in range(4) for r in range(4)]
        add_round_key(rnd)
    return bytes(state)


def _pad(data):
    size = 16 - (len(data) % 16)
    return data + bytes([size]) * size


def ecb_encrypt(data, key):
    words, rounds = _expand_key(key)
    return b"".join(_encrypt_block(_pad(data)[i:i + 16], words, rounds)
                    for i in range(0, len(_pad(data)), 16))


def cbc_encrypt(data, key, iv):
    if len(iv) != 16:
        raise ValueError("AES IV must be 16 bytes")
    words, rounds = _expand_key(key)
    padded = _pad(data)
    previous = iv
    blocks = []
    for i in range(0, len(padded), 16):
        mixed = bytes(a ^ b for a, b in zip(padded[i:i + 16], previous))
        previous = _encrypt_block(mixed, words, rounds)
        blocks.append(previous)
    return b"".join(blocks)


# Netease constants, see the public reverse-engineering references.
NONCE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
WEAPI_IV = b"0102030405060708"
WEAPI_FIRST_KEY = b"0CoJUm6Qyw8W8jud"
RSA_PUBLIC = 0x010001
RSA_MODULUS = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e"
    "417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932"
    "575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7", 16)
EAPI_KEY = b"e82ckenh8dichen8"


def weapi(payload):
    """Envelope for music.163.com/weapi/* endpoints."""
    text = json.dumps(payload, separators=(",", ":"))
    secret = "".join(random.choice(NONCE_ALPHABET) for _ in range(16))
    first = base64.b64encode(cbc_encrypt(text.encode("utf-8"), WEAPI_FIRST_KEY, WEAPI_IV))
    params = base64.b64encode(cbc_encrypt(first, secret.encode("utf-8"), WEAPI_IV)).decode()
    reversed_secret = int.from_bytes(secret[::-1].encode("utf-8"), "big")
    enc_sec_key = pow(reversed_secret, RSA_PUBLIC, RSA_MODULUS)
    return {"params": params, "encSecKey": format(enc_sec_key, "x").zfill(256)}


def eapi(url, payload):
    """Envelope for interface3.music.163.com/eapi/* endpoints."""
    text = json.dumps(payload, separators=(",", ":"))
    digest = hashlib.md5(("nobody%suse%smd5forencrypt" % (url, text)).encode("utf-8")).hexdigest()
    body = "%s-36cd479b6b5-%s-36cd479b6b5-%s" % (url, text, digest)
    return {"params": ecb_encrypt(_pad(body.encode("utf-8")), EAPI_KEY).hex().upper()}
