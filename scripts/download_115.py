#!/usr/bin/env python3
"""Standalone 115 reader: Python 3.9+, standard library only.

Run with no arguments for QR login + file browser:
    python3 download_115.py
Other examples:
    python3 download_115.py ls /software
    python3 download_115.py get /software/test.bin --output test.bin
    python3 download_115.py bench /software/test.bin --sample-mib 128 --rounds 3
    python3 download_115.py login --app alipaymini

No Docker, AList, biomed, proxy service, upload, delete or account password needed.
The script uses community-documented 115 client endpoints, NOT an official SDK.
Account cookies are stored on the executing machine, never embedded in this file.
QR login can replace another session of the same device type. Use an unused type.

Protocol reference: https://github.com/ChenyangGao/p115client
RSA component: p115rsacipher 0.0.1 (Python 3.9 compatibility adaptation).
Original source: https://pypi.org/project/p115rsacipher/0.0.1/
QR encoder: Project Nayuki qrcodegen v1.8.0 (MIT), embedded without dependencies.
https://github.com/nayuki/QR-Code-generator/tree/v1.8.0/python
"""
from __future__ import annotations

import argparse
import collections
import itertools
from base64 import b64decode, b64encode
from collections.abc import Iterator, Sized, Sequence
from datetime import datetime, timezone
import fcntl
import hashlib
from http.cookies import SimpleCookie
import http.client
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import shutil
import sys
import time
from typing import Any as Buffer, Callable, Dict, Final, List, Optional, Tuple, Union
import urllib.error
import urllib.parse
import urllib.request

# BEGIN vendored p115rsacipher 0.0.1; full upstream MIT license follows.
# MIT License
#
# Copyright (c) 2024 ChenyangGao <https://github.com/ChenyangGao>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

G_kts: Final = b"\xf0\xe5i\xae\xbf\xdc\xbf\x8a\x1aE\xe8\xbe}\xa6s\xb8\xde\x8f\xe7\xc4E\xda\x86\xc4\x9bd\x8b\x14j\xb4\xf1\xaa8\x015\x9e&i,\x86\x00kO\xa564b\xa6*\x96h\x18\xf2J\xfd\xbdk\x97\x8fM\x8f\x89\x13\xb7l\x8e\x93\xed\x0e\rH>\xd7/\x88\xd8\xfe\xfe~\x86P\x95O\xd1\xeb\x83&4\xdbf{\x9c~\x9dz\x812\xea\xb63\xde:\xa9Y4f;\xaa\xba\x81`H\xb9\xd5\x81\x9c\xf8l\x84w\xffTx&_\xbe\xe8\x1e6\x9f4\x80\\E,\x9bv\xd5\x1b\x8f\xcc\xc3\xb8\xf5"
RSA_e: Final = 0x8686980c0f5a24c4b9d43020cd2c22703ff3f450756529058b1cf88f09b8602136477198a6e2683149659bd122c33592fdb5ad47944ad1ea4d36c6b172aad6338c3bb6ac6227502d010993ac967d1aef00f0c8e038de2e4d3bc2ec368af2e9f10a6f1eda4f7262f136420c07c331b871bf139f74f3010e3c4fe57df3afb71683
RSA_n: Final = 0x10001

def to_bytes(value, length):
    return int.to_bytes(value, length, "big")

def from_bytes(value):
    return int.from_bytes(value, "big")


def acc_step(start: int, stop: int, step: int = 1) -> Iterator[tuple[int, int, int]]:
    for i in range(start + step, stop, step):
        yield start, i, step
        start = i
    if start != stop:
        yield start, stop, stop - start


def bytes_xor(v1: Buffer, v2: Buffer, /) -> bytes:
    return to_bytes(
        from_bytes(v1) ^ from_bytes(v2),
        len(v1 if isinstance(v1, Sized) else memoryview(v1)),
    )


def gen_key(rand_key: Buffer, sk_len: int, /) -> bytearray:
    xor_key = bytearray(sk_len)
    length = sk_len * (sk_len - 1)
    index = 0
    if not isinstance(rand_key, (bytes, bytearray, memoryview)):
        rand_key = memoryview(rand_key)
    for i in range(sk_len):
        x = (rand_key[i] + G_kts[index]) & 0xff
        xor_key[i] = G_kts[length] ^ x
        length -= sk_len
        index += sk_len
    return xor_key


def pad_pkcs1_v1_5(message: Buffer, /) -> int:
    length = len(message if isinstance(message, Sized) else memoryview(message))
    return from_bytes(b"\x00" + b"\x02" * (126 - length) + b"\x00" + message)


def xor(src: Buffer, key: Buffer, /) -> bytearray:
    src = memoryview(src)
    key = memoryview(key)
    secret = bytearray()
    i = len(src) & 0b11
    if i:
        secret += bytes_xor(src[:i], key[:i])
    for i, j, s in acc_step(i, len(src), len(key)):
        secret += bytes_xor(src[i:j], key[:s])
    return secret


def encrypt(data: str | Buffer, /) -> bytes:
    if isinstance(data, str):
        data = bytes(data, "utf-8")
    xor_text = bytearray(16)
    tmp = memoryview(xor(data, b"\x8d\xa5\xa5\x8d"))[::-1]
    xor_text += xor(tmp, b"x\x06\xadL3\x86]\x18L\x01?F")
    cipher_data = bytearray()
    view = memoryview(xor_text)
    for left, right, _ in acc_step(0, len(view), 117):
        cipher_data += to_bytes(pow(pad_pkcs1_v1_5(view[left:right]), RSA_n, RSA_e), 128)
    return b64encode(cipher_data)


def decrypt(cipher_data: str | Buffer, /) -> bytearray:
    cipher_data = memoryview(b64decode(cipher_data))
    data = bytearray()
    for left, right, _ in acc_step(0, len(cipher_data), 128):
        p = pow(from_bytes(cipher_data[left:right]), RSA_n, RSA_e)
        b = to_bytes(p, (p.bit_length() + 0b111) >> 3)
        data += memoryview(b)[b.index(0)+1:]
    m = memoryview(data)
    key_l = gen_key(m[:16], 12)
    tmp = memoryview(xor(m[16:], key_l))[::-1]
    return xor(tmp, b"\x8d\xa5\xa5\x8d")


# END vendored p115rsacipher

# BEGIN vendored Nayuki qrcodegen v1.8.0 (MIT); full upstream license below.
# Upstream SHA256: b089855caf16185c61421ea4927c1b213cf9468940d71fa8ab11ef83662dcc84
#
# QR Code generator library (Python)
#
# Copyright (c) Project Nayuki. (MIT License)
# https://www.nayuki.io/page/qr-code-generator-library
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
# - The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
# - The Software is provided "as is", without warranty of any kind, express or
#   implied, including but not limited to the warranties of merchantability,
#   fitness for a particular purpose and noninfringement. In no event shall the
#   authors or copyright holders be liable for any claim, damages or other
#   liability, whether in an action of contract, tort or otherwise, arising from,
#   out of or in connection with the Software or the use or other dealings in the
#   Software.
#



# ---- QR Code symbol class ----

class QrCode:
    """A QR Code symbol, which is a type of two-dimension barcode.
    Invented by Denso Wave and described in the ISO/IEC 18004 standard.
    Instances of this class represent an immutable square grid of dark and light cells.
    The class provides static factory functions to create a QR Code from text or binary data.
    The class covers the QR Code Model 2 specification, supporting all versions (sizes)
    from 1 to 40, all 4 error correction levels, and 4 character encoding modes.

    Ways to create a QR Code object:
    - High level: Take the payload data and call QrCode.encode_text() or QrCode.encode_binary().
    - Mid level: Custom-make the list of segments and call QrCode.encode_segments().
    - Low level: Custom-make the array of data codeword bytes (including
      segment headers and final padding, excluding error correction codewords),
      supply the appropriate version number, and call the QrCode() constructor.
    (Note that all ways require supplying the desired error correction level.)"""

    # ---- Static factory functions (high level) ----

    @staticmethod
    def encode_text(text: str, ecl: QrCode.Ecc) -> QrCode:
        """Returns a QR Code representing the given Unicode text string at the given error correction level.
        As a conservative upper bound, this function is guaranteed to succeed for strings that have 738 or fewer
        Unicode code points (not UTF-16 code units) if the low error correction level is used. The smallest possible
        QR Code version is automatically chosen for the output. The ECC level of the result may be higher than the
        ecl argument if it can be done without increasing the version."""
        segs: List[QrSegment] = QrSegment.make_segments(text)
        return QrCode.encode_segments(segs, ecl)


    @staticmethod
    def encode_binary(data: Union[bytes,Sequence[int]], ecl: QrCode.Ecc) -> QrCode:
        """Returns a QR Code representing the given binary data at the given error correction level.
        This function always encodes using the binary segment mode, not any text mode. The maximum number of
        bytes allowed is 2953. The smallest possible QR Code version is automatically chosen for the output.
        The ECC level of the result may be higher than the ecl argument if it can be done without increasing the version."""
        return QrCode.encode_segments([QrSegment.make_bytes(data)], ecl)


    # ---- Static factory functions (mid level) ----

    @staticmethod
    def encode_segments(segs: Sequence[QrSegment], ecl: QrCode.Ecc, minversion: int = 1, maxversion: int = 40, mask: int = -1, boostecl: bool = True) -> QrCode:
        """Returns a QR Code representing the given segments with the given encoding parameters.
        The smallest possible QR Code version within the given range is automatically
        chosen for the output. Iff boostecl is true, then the ECC level of the result
        may be higher than the ecl argument if it can be done without increasing the
        version. The mask number is either between 0 to 7 (inclusive) to force that
        mask, or -1 to automatically choose an appropriate mask (which may be slow).
        This function allows the user to create a custom sequence of segments that switches
        between modes (such as alphanumeric and byte) to encode text in less space.
        This is a mid-level API; the high-level API is encode_text() and encode_binary()."""

        if not (QrCode.MIN_VERSION <= minversion <= maxversion <= QrCode.MAX_VERSION) or not (-1 <= mask <= 7):
            raise ValueError("Invalid value")

        # Find the minimal version number to use
        for version in range(minversion, maxversion + 1):
            datacapacitybits: int = QrCode._get_num_data_codewords(version, ecl) * 8  # Number of data bits available
            datausedbits: Optional[int] = QrSegment.get_total_bits(segs, version)
            if (datausedbits is not None) and (datausedbits <= datacapacitybits):
                break  # This version number is found to be suitable
            if version >= maxversion:  # All versions in the range could not fit the given data
                msg: str = "Segment too long"
                if datausedbits is not None:
                    msg = f"Data length = {datausedbits} bits, Max capacity = {datacapacitybits} bits"
                raise DataTooLongError(msg)
        assert datausedbits is not None

        # Increase the error correction level while the data still fits in the current version number
        for newecl in (QrCode.Ecc.MEDIUM, QrCode.Ecc.QUARTILE, QrCode.Ecc.HIGH):  # From low to high
            if boostecl and (datausedbits <= QrCode._get_num_data_codewords(version, newecl) * 8):
                ecl = newecl

        # Concatenate all segments to create the data bit string
        bb = _BitBuffer()
        for seg in segs:
            bb.append_bits(seg.get_mode().get_mode_bits(), 4)
            bb.append_bits(seg.get_num_chars(), seg.get_mode().num_char_count_bits(version))
            bb.extend(seg._bitdata)
        assert len(bb) == datausedbits

        # Add terminator and pad up to a byte if applicable
        datacapacitybits = QrCode._get_num_data_codewords(version, ecl) * 8
        assert len(bb) <= datacapacitybits
        bb.append_bits(0, min(4, datacapacitybits - len(bb)))
        bb.append_bits(0, -len(bb) % 8)  # Note: Python's modulo on negative numbers behaves better than C family languages
        assert len(bb) % 8 == 0

        # Pad with alternating bytes until data capacity is reached
        for padbyte in itertools.cycle((0xEC, 0x11)):
            if len(bb) >= datacapacitybits:
                break
            bb.append_bits(padbyte, 8)

        # Pack bits into bytes in big endian
        datacodewords = bytearray([0] * (len(bb) // 8))
        for (i, bit) in enumerate(bb):
            datacodewords[i >> 3] |= bit << (7 - (i & 7))

        # Create the QR Code object
        return QrCode(version, ecl, datacodewords, mask)


    # ---- Private fields ----

    # The version number of this QR Code, which is between 1 and 40 (inclusive).
    # This determines the size of this barcode.
    _version: int

    # The width and height of this QR Code, measured in modules, between
    # 21 and 177 (inclusive). This is equal to version * 4 + 17.
    _size: int

    # The error correction level used in this QR Code.
    _errcorlvl: QrCode.Ecc

    # The index of the mask pattern used in this QR Code, which is between 0 and 7 (inclusive).
    # Even if a QR Code is created with automatic masking requested (mask = -1),
    # the resulting object still has a mask value between 0 and 7.
    _mask: int

    # The modules of this QR Code (False = light, True = dark).
    # Immutable after constructor finishes. Accessed through get_module().
    _modules: List[List[bool]]

    # Indicates function modules that are not subjected to masking. Discarded when constructor finishes.
    _isfunction: List[List[bool]]


    # ---- Constructor (low level) ----

    def __init__(self, version: int, errcorlvl: QrCode.Ecc, datacodewords: Union[bytes,Sequence[int]], msk: int) -> None:
        """Creates a new QR Code with the given version number,
        error correction level, data codeword bytes, and mask number.
        This is a low-level API that most users should not use directly.
        A mid-level API is the encode_segments() function."""

        # Check scalar arguments and set fields
        if not (QrCode.MIN_VERSION <= version <= QrCode.MAX_VERSION):
            raise ValueError("Version value out of range")
        if not (-1 <= msk <= 7):
            raise ValueError("Mask value out of range")

        self._version = version
        self._size = version * 4 + 17
        self._errcorlvl = errcorlvl

        # Initialize both grids to be size*size arrays of Boolean false
        self._modules    = [[False] * self._size for _ in range(self._size)]  # Initially all light
        self._isfunction = [[False] * self._size for _ in range(self._size)]

        # Compute ECC, draw modules
        self._draw_function_patterns()
        allcodewords: bytes = self._add_ecc_and_interleave(bytearray(datacodewords))
        self._draw_codewords(allcodewords)

        # Do masking
        if msk == -1:  # Automatically choose best mask
            minpenalty: int = 1 << 32
            for i in range(8):
                self._apply_mask(i)
                self._draw_format_bits(i)
                penalty = self._get_penalty_score()
                if penalty < minpenalty:
                    msk = i
                    minpenalty = penalty
                self._apply_mask(i)  # Undoes the mask due to XOR
        assert 0 <= msk <= 7
        self._mask = msk
        self._apply_mask(msk)  # Apply the final choice of mask
        self._draw_format_bits(msk)  # Overwrite old format bits

        del self._isfunction


    # ---- Accessor methods ----

    def get_version(self) -> int:
        """Returns this QR Code's version number, in the range [1, 40]."""
        return self._version

    def get_size(self) -> int:
        """Returns this QR Code's size, in the range [21, 177]."""
        return self._size

    def get_error_correction_level(self) -> QrCode.Ecc:
        """Returns this QR Code's error correction level."""
        return self._errcorlvl

    def get_mask(self) -> int:
        """Returns this QR Code's mask, in the range [0, 7]."""
        return self._mask

    def get_module(self, x: int, y: int) -> bool:
        """Returns the color of the module (pixel) at the given coordinates, which is False
        for light or True for dark. The top left corner has the coordinates (x=0, y=0).
        If the given coordinates are out of bounds, then False (light) is returned."""
        return (0 <= x < self._size) and (0 <= y < self._size) and self._modules[y][x]


    # ---- Private helper methods for constructor: Drawing function modules ----

    def _draw_function_patterns(self) -> None:
        """Reads this object's version field, and draws and marks all function modules."""
        # Draw horizontal and vertical timing patterns
        for i in range(self._size):
            self._set_function_module(6, i, i % 2 == 0)
            self._set_function_module(i, 6, i % 2 == 0)

        # Draw 3 finder patterns (all corners except bottom right; overwrites some timing modules)
        self._draw_finder_pattern(3, 3)
        self._draw_finder_pattern(self._size - 4, 3)
        self._draw_finder_pattern(3, self._size - 4)

        # Draw numerous alignment patterns
        alignpatpos: List[int] = self._get_alignment_pattern_positions()
        numalign: int = len(alignpatpos)
        skips: Sequence[Tuple[int,int]] = ((0, 0), (0, numalign - 1), (numalign - 1, 0))
        for i in range(numalign):
            for j in range(numalign):
                if (i, j) not in skips:  # Don't draw on the three finder corners
                    self._draw_alignment_pattern(alignpatpos[i], alignpatpos[j])

        # Draw configuration data
        self._draw_format_bits(0)  # Dummy mask value; overwritten later in the constructor
        self._draw_version()


    def _draw_format_bits(self, mask: int) -> None:
        """Draws two copies of the format bits (with its own error correction code)
        based on the given mask and this object's error correction level field."""
        # Calculate error correction code and pack bits
        data: int = self._errcorlvl.formatbits << 3 | mask  # errCorrLvl is uint2, mask is uint3
        rem: int = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits: int = (data << 10 | rem) ^ 0x5412  # uint15
        assert bits >> 15 == 0

        # Draw first copy
        for i in range(0, 6):
            self._set_function_module(8, i, _get_bit(bits, i))
        self._set_function_module(8, 7, _get_bit(bits, 6))
        self._set_function_module(8, 8, _get_bit(bits, 7))
        self._set_function_module(7, 8, _get_bit(bits, 8))
        for i in range(9, 15):
            self._set_function_module(14 - i, 8, _get_bit(bits, i))

        # Draw second copy
        for i in range(0, 8):
            self._set_function_module(self._size - 1 - i, 8, _get_bit(bits, i))
        for i in range(8, 15):
            self._set_function_module(8, self._size - 15 + i, _get_bit(bits, i))
        self._set_function_module(8, self._size - 8, True)  # Always dark


    def _draw_version(self) -> None:
        """Draws two copies of the version bits (with its own error correction code),
        based on this object's version field, iff 7 <= version <= 40."""
        if self._version < 7:
            return

        # Calculate error correction code and pack bits
        rem: int = self._version  # version is uint6, in the range [7, 40]
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits: int = self._version << 12 | rem  # uint18
        assert bits >> 18 == 0

        # Draw two copies
        for i in range(18):
            bit: bool = _get_bit(bits, i)
            a: int = self._size - 11 + i % 3
            b: int = i // 3
            self._set_function_module(a, b, bit)
            self._set_function_module(b, a, bit)


    def _draw_finder_pattern(self, x: int, y: int) -> None:
        """Draws a 9*9 finder pattern including the border separator,
        with the center module at (x, y). Modules can be out of bounds."""
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                xx, yy = x + dx, y + dy
                if (0 <= xx < self._size) and (0 <= yy < self._size):
                    # Chebyshev/infinity norm
                    self._set_function_module(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))


    def _draw_alignment_pattern(self, x: int, y: int) -> None:
        """Draws a 5*5 alignment pattern, with the center module
        at (x, y). All modules must be in bounds."""
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self._set_function_module(x + dx, y + dy, max(abs(dx), abs(dy)) != 1)


    def _set_function_module(self, x: int, y: int, isdark: bool) -> None:
        """Sets the color of a module and marks it as a function module.
        Only used by the constructor. Coordinates must be in bounds."""
        assert type(isdark) is bool
        self._modules[y][x] = isdark
        self._isfunction[y][x] = True


    # ---- Private helper methods for constructor: Codewords and masking ----

    def _add_ecc_and_interleave(self, data: bytearray) -> bytes:
        """Returns a new byte string representing the given data with the appropriate error correction
        codewords appended to it, based on this object's version and error correction level."""
        version: int = self._version
        assert len(data) == QrCode._get_num_data_codewords(version, self._errcorlvl)

        # Calculate parameter numbers
        numblocks: int = QrCode._NUM_ERROR_CORRECTION_BLOCKS[self._errcorlvl.ordinal][version]
        blockecclen: int = QrCode._ECC_CODEWORDS_PER_BLOCK  [self._errcorlvl.ordinal][version]
        rawcodewords: int = QrCode._get_num_raw_data_modules(version) // 8
        numshortblocks: int = numblocks - rawcodewords % numblocks
        shortblocklen: int = rawcodewords // numblocks

        # Split data into blocks and append ECC to each block
        blocks: List[bytes] = []
        rsdiv: bytes = QrCode._reed_solomon_compute_divisor(blockecclen)
        k: int = 0
        for i in range(numblocks):
            dat: bytearray = data[k : k + shortblocklen - blockecclen + (0 if i < numshortblocks else 1)]
            k += len(dat)
            ecc: bytes = QrCode._reed_solomon_compute_remainder(dat, rsdiv)
            if i < numshortblocks:
                dat.append(0)
            blocks.append(dat + ecc)
        assert k == len(data)

        # Interleave (not concatenate) the bytes from every block into a single sequence
        result = bytearray()
        for i in range(len(blocks[0])):
            for (j, blk) in enumerate(blocks):
                # Skip the padding byte in short blocks
                if (i != shortblocklen - blockecclen) or (j >= numshortblocks):
                    result.append(blk[i])
        assert len(result) == rawcodewords
        return result


    def _draw_codewords(self, data: bytes) -> None:
        """Draws the given sequence of 8-bit codewords (data and error correction) onto the entire
        data area of this QR Code. Function modules need to be marked off before this is called."""
        assert len(data) == QrCode._get_num_raw_data_modules(self._version) // 8

        i: int = 0  # Bit index into the data
        # Do the funny zigzag scan
        for right in range(self._size - 1, 0, -2):  # Index of right column in each column pair
            if right <= 6:
                right -= 1
            for vert in range(self._size):  # Vertical counter
                for j in range(2):
                    x: int = right - j  # Actual x coordinate
                    upward: bool = (right + 1) & 2 == 0
                    y: int = (self._size - 1 - vert) if upward else vert  # Actual y coordinate
                    if (not self._isfunction[y][x]) and (i < len(data) * 8):
                        self._modules[y][x] = _get_bit(data[i >> 3], 7 - (i & 7))
                        i += 1
                    # If this QR Code has any remainder bits (0 to 7), they were assigned as
                    # 0/false/light by the constructor and are left unchanged by this method
        assert i == len(data) * 8


    def _apply_mask(self, mask: int) -> None:
        """XORs the codeword modules in this QR Code with the given mask pattern.
        The function modules must be marked and the codeword bits must be drawn
        before masking. Due to the arithmetic of XOR, calling _apply_mask() with
        the same mask value a second time will undo the mask. A final well-formed
        QR Code needs exactly one (not zero, two, etc.) mask applied."""
        if not (0 <= mask <= 7):
            raise ValueError("Mask value out of range")
        masker: Callable[[int,int],int] = QrCode._MASK_PATTERNS[mask]
        for y in range(self._size):
            for x in range(self._size):
                self._modules[y][x] ^= (masker(x, y) == 0) and (not self._isfunction[y][x])


    def _get_penalty_score(self) -> int:
        """Calculates and returns the penalty score based on state of this QR Code's current modules.
        This is used by the automatic mask choice algorithm to find the mask pattern that yields the lowest score."""
        result: int = 0
        size: int = self._size
        modules: List[List[bool]] = self._modules

        # Adjacent modules in row having same color, and finder-like patterns
        for y in range(size):
            runcolor: bool = False
            runx: int = 0
            runhistory = collections.deque([0] * 7, 7)
            for x in range(size):
                if modules[y][x] == runcolor:
                    runx += 1
                    if runx == 5:
                        result += QrCode._PENALTY_N1
                    elif runx > 5:
                        result += 1
                else:
                    self._finder_penalty_add_history(runx, runhistory)
                    if not runcolor:
                        result += self._finder_penalty_count_patterns(runhistory) * QrCode._PENALTY_N3
                    runcolor = modules[y][x]
                    runx = 1
            result += self._finder_penalty_terminate_and_count(runcolor, runx, runhistory) * QrCode._PENALTY_N3
        # Adjacent modules in column having same color, and finder-like patterns
        for x in range(size):
            runcolor = False
            runy = 0
            runhistory = collections.deque([0] * 7, 7)
            for y in range(size):
                if modules[y][x] == runcolor:
                    runy += 1
                    if runy == 5:
                        result += QrCode._PENALTY_N1
                    elif runy > 5:
                        result += 1
                else:
                    self._finder_penalty_add_history(runy, runhistory)
                    if not runcolor:
                        result += self._finder_penalty_count_patterns(runhistory) * QrCode._PENALTY_N3
                    runcolor = modules[y][x]
                    runy = 1
            result += self._finder_penalty_terminate_and_count(runcolor, runy, runhistory) * QrCode._PENALTY_N3

        # 2*2 blocks of modules having same color
        for y in range(size - 1):
            for x in range(size - 1):
                if modules[y][x] == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                    result += QrCode._PENALTY_N2

        # Balance of dark and light modules
        dark: int = sum((1 if cell else 0) for row in modules for cell in row)
        total: int = size**2  # Note that size is odd, so dark/total != 1/2
        # Compute the smallest integer k >= 0 such that (45-5k)% <= dark/total <= (55+5k)%
        k: int = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        assert 0 <= k <= 9
        result += k * QrCode._PENALTY_N4
        assert 0 <= result <= 2568888  # Non-tight upper bound based on default values of PENALTY_N1, ..., N4
        return result


    # ---- Private helper functions ----

    def _get_alignment_pattern_positions(self) -> List[int]:
        """Returns an ascending list of positions of alignment patterns for this version number.
        Each position is in the range [0,177), and are used on both the x and y axes.
        This could be implemented as lookup table of 40 variable-length lists of integers."""
        ver: int = self._version
        if ver == 1:
            return []
        else:
            numalign: int = ver // 7 + 2
            step: int = 26 if (ver == 32) else \
                (ver * 4 + numalign * 2 + 1) // (numalign * 2 - 2) * 2
            result: List[int] = [(self._size - 7 - i * step) for i in range(numalign - 1)] + [6]
            return list(reversed(result))


    @staticmethod
    def _get_num_raw_data_modules(ver: int) -> int:
        """Returns the number of data bits that can be stored in a QR Code of the given version number, after
        all function modules are excluded. This includes remainder bits, so it might not be a multiple of 8.
        The result is in the range [208, 29648]. This could be implemented as a 40-entry lookup table."""
        if not (QrCode.MIN_VERSION <= ver <= QrCode.MAX_VERSION):
            raise ValueError("Version number out of range")
        result: int = (16 * ver + 128) * ver + 64
        if ver >= 2:
            numalign: int = ver // 7 + 2
            result -= (25 * numalign - 10) * numalign - 55
            if ver >= 7:
                result -= 36
        assert 208 <= result <= 29648
        return result


    @staticmethod
    def _get_num_data_codewords(ver: int, ecl: QrCode.Ecc) -> int:
        """Returns the number of 8-bit data (i.e. not error correction) codewords contained in any
        QR Code of the given version number and error correction level, with remainder bits discarded.
        This stateless pure function could be implemented as a (40*4)-cell lookup table."""
        return QrCode._get_num_raw_data_modules(ver) // 8 \
            - QrCode._ECC_CODEWORDS_PER_BLOCK    [ecl.ordinal][ver] \
            * QrCode._NUM_ERROR_CORRECTION_BLOCKS[ecl.ordinal][ver]


    @staticmethod
    def _reed_solomon_compute_divisor(degree: int) -> bytes:
        """Returns a Reed-Solomon ECC generator polynomial for the given degree. This could be
        implemented as a lookup table over all possible parameter values, instead of as an algorithm."""
        if not (1 <= degree <= 255):
            raise ValueError("Degree out of range")
        # Polynomial coefficients are stored from highest to lowest power, excluding the leading term which is always 1.
        # For example the polynomial x^3 + 255x^2 + 8x + 93 is stored as the uint8 array [255, 8, 93].
        result = bytearray([0] * (degree - 1) + [1])  # Start off with the monomial x^0

        # Compute the product polynomial (x - r^0) * (x - r^1) * (x - r^2) * ... * (x - r^{degree-1}),
        # and drop the highest monomial term which is always 1x^degree.
        # Note that r = 0x02, which is a generator element of this field GF(2^8/0x11D).
        root: int = 1
        for _ in range(degree):  # Unused variable i
            # Multiply the current product by (x - r^i)
            for j in range(degree):
                result[j] = QrCode._reed_solomon_multiply(result[j], root)
                if j + 1 < degree:
                    result[j] ^= result[j + 1]
            root = QrCode._reed_solomon_multiply(root, 0x02)
        return result


    @staticmethod
    def _reed_solomon_compute_remainder(data: bytes, divisor: bytes) -> bytes:
        """Returns the Reed-Solomon error correction codeword for the given data and divisor polynomials."""
        result = bytearray([0] * len(divisor))
        for b in data:  # Polynomial division
            factor: int = b ^ result.pop(0)
            result.append(0)
            for (i, coef) in enumerate(divisor):
                result[i] ^= QrCode._reed_solomon_multiply(coef, factor)
        return result


    @staticmethod
    def _reed_solomon_multiply(x: int, y: int) -> int:
        """Returns the product of the two given field elements modulo GF(2^8/0x11D). The arguments and result
        are unsigned 8-bit integers. This could be implemented as a lookup table of 256*256 entries of uint8."""
        if (x >> 8 != 0) or (y >> 8 != 0):
            raise ValueError("Byte out of range")
        # Russian peasant multiplication
        z: int = 0
        for i in reversed(range(8)):
            z = (z << 1) ^ ((z >> 7) * 0x11D)
            z ^= ((y >> i) & 1) * x
        assert z >> 8 == 0
        return z


    def _finder_penalty_count_patterns(self, runhistory: collections.deque) -> int:
        """Can only be called immediately after a light run is added, and
        returns either 0, 1, or 2. A helper function for _get_penalty_score()."""
        n: int = runhistory[1]
        assert n <= self._size * 3
        core: bool = n > 0 and (runhistory[2] == runhistory[4] == runhistory[5] == n) and runhistory[3] == n * 3
        return (1 if (core and runhistory[0] >= n * 4 and runhistory[6] >= n) else 0) \
             + (1 if (core and runhistory[6] >= n * 4 and runhistory[0] >= n) else 0)


    def _finder_penalty_terminate_and_count(self, currentruncolor: bool, currentrunlength: int, runhistory: collections.deque) -> int:
        """Must be called at the end of a line (row or column) of modules. A helper function for _get_penalty_score()."""
        if currentruncolor:  # Terminate dark run
            self._finder_penalty_add_history(currentrunlength, runhistory)
            currentrunlength = 0
        currentrunlength += self._size  # Add light border to final run
        self._finder_penalty_add_history(currentrunlength, runhistory)
        return self._finder_penalty_count_patterns(runhistory)


    def _finder_penalty_add_history(self, currentrunlength: int, runhistory: collections.deque) -> None:
        if runhistory[0] == 0:
            currentrunlength += self._size  # Add light border to initial run
        runhistory.appendleft(currentrunlength)


    # ---- Constants and tables ----

    MIN_VERSION: int =  1  # The minimum version number supported in the QR Code Model 2 standard
    MAX_VERSION: int = 40  # The maximum version number supported in the QR Code Model 2 standard

    # For use in _get_penalty_score(), when evaluating which mask is best.
    _PENALTY_N1: int =  3
    _PENALTY_N2: int =  3
    _PENALTY_N3: int = 40
    _PENALTY_N4: int = 10

    _ECC_CODEWORDS_PER_BLOCK: Sequence[Sequence[int]] = (
        # Version: (note that index 0 is for padding, and is set to an illegal value)
        # 0,  1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40    Error correction level
        (-1,  7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),  # Low
        (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),  # Medium
        (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),  # Quartile
        (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30))  # High

    _NUM_ERROR_CORRECTION_BLOCKS: Sequence[Sequence[int]] = (
        # Version: (note that index 0 is for padding, and is set to an illegal value)
        # 0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40    Error correction level
        (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4,  4,  4,  4,  4,  6,  6,  6,  6,  7,  8,  8,  9,  9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),  # Low
        (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5,  5,  8,  9,  9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),  # Medium
        (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8,  8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),  # Quartile
        (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81))  # High

    _MASK_PATTERNS: Sequence[Callable[[int,int],int]] = (
        (lambda x, y:  (x + y) % 2                  ),
        (lambda x, y:  y % 2                        ),
        (lambda x, y:  x % 3                        ),
        (lambda x, y:  (x + y) % 3                  ),
        (lambda x, y:  (x // 3 + y // 2) % 2        ),
        (lambda x, y:  x * y % 2 + x * y % 3        ),
        (lambda x, y:  (x * y % 2 + x * y % 3) % 2  ),
        (lambda x, y:  ((x + y) % 2 + x * y % 3) % 2),
    )


    # ---- Public helper enumeration ----

    class Ecc:
        ordinal: int  # (Public) In the range 0 to 3 (unsigned 2-bit integer)
        formatbits: int  # (Package-private) In the range 0 to 3 (unsigned 2-bit integer)

        """The error correction level in a QR Code symbol. Immutable."""
        # Private constructor
        def __init__(self, i: int, fb: int) -> None:
            self.ordinal = i
            self.formatbits = fb

        # Placeholders
        LOW     : QrCode.Ecc
        MEDIUM  : QrCode.Ecc
        QUARTILE: QrCode.Ecc
        HIGH    : QrCode.Ecc

    # Public constants. Create them outside the class.
    Ecc.LOW      = Ecc(0, 1)  # The QR Code can tolerate about  7% erroneous codewords
    Ecc.MEDIUM   = Ecc(1, 0)  # The QR Code can tolerate about 15% erroneous codewords
    Ecc.QUARTILE = Ecc(2, 3)  # The QR Code can tolerate about 25% erroneous codewords
    Ecc.HIGH     = Ecc(3, 2)  # The QR Code can tolerate about 30% erroneous codewords



# ---- Data segment class ----

class QrSegment:
    """A segment of character/binary/control data in a QR Code symbol.
    Instances of this class are immutable.
    The mid-level way to create a segment is to take the payload data
    and call a static factory function such as QrSegment.make_numeric().
    The low-level way to create a segment is to custom-make the bit buffer
    and call the QrSegment() constructor with appropriate values.
    This segment class imposes no length restrictions, but QR Codes have restrictions.
    Even in the most favorable conditions, a QR Code can only hold 7089 characters of data.
    Any segment longer than this is meaningless for the purpose of generating QR Codes."""

    # ---- Static factory functions (mid level) ----

    @staticmethod
    def make_bytes(data: Union[bytes,Sequence[int]]) -> QrSegment:
        """Returns a segment representing the given binary data encoded in byte mode.
        All input byte lists are acceptable. Any text string can be converted to
        UTF-8 bytes (s.encode("UTF-8")) and encoded as a byte mode segment."""
        bb = _BitBuffer()
        for b in data:
            bb.append_bits(b, 8)
        return QrSegment(QrSegment.Mode.BYTE, len(data), bb)


    @staticmethod
    def make_numeric(digits: str) -> QrSegment:
        """Returns a segment representing the given string of decimal digits encoded in numeric mode."""
        if not QrSegment.is_numeric(digits):
            raise ValueError("String contains non-numeric characters")
        bb = _BitBuffer()
        i: int = 0
        while i < len(digits):  # Consume up to 3 digits per iteration
            n: int = min(len(digits) - i, 3)
            bb.append_bits(int(digits[i : i + n]), n * 3 + 1)
            i += n
        return QrSegment(QrSegment.Mode.NUMERIC, len(digits), bb)


    @staticmethod
    def make_alphanumeric(text: str) -> QrSegment:
        """Returns a segment representing the given text string encoded in alphanumeric mode.
        The characters allowed are: 0 to 9, A to Z (uppercase only), space,
        dollar, percent, asterisk, plus, hyphen, period, slash, colon."""
        if not QrSegment.is_alphanumeric(text):
            raise ValueError("String contains unencodable characters in alphanumeric mode")
        bb = _BitBuffer()
        for i in range(0, len(text) - 1, 2):  # Process groups of 2
            temp: int = QrSegment._ALPHANUMERIC_ENCODING_TABLE[text[i]] * 45
            temp += QrSegment._ALPHANUMERIC_ENCODING_TABLE[text[i + 1]]
            bb.append_bits(temp, 11)
        if len(text) % 2 > 0:  # 1 character remaining
            bb.append_bits(QrSegment._ALPHANUMERIC_ENCODING_TABLE[text[-1]], 6)
        return QrSegment(QrSegment.Mode.ALPHANUMERIC, len(text), bb)


    @staticmethod
    def make_segments(text: str) -> List[QrSegment]:
        """Returns a new mutable list of zero or more segments to represent the given Unicode text string.
        The result may use various segment modes and switch modes to optimize the length of the bit stream."""

        # Select the most efficient segment encoding automatically
        if text == "":
            return []
        elif QrSegment.is_numeric(text):
            return [QrSegment.make_numeric(text)]
        elif QrSegment.is_alphanumeric(text):
            return [QrSegment.make_alphanumeric(text)]
        else:
            return [QrSegment.make_bytes(text.encode("UTF-8"))]


    @staticmethod
    def make_eci(assignval: int) -> QrSegment:
        """Returns a segment representing an Extended Channel Interpretation
        (ECI) designator with the given assignment value."""
        bb = _BitBuffer()
        if assignval < 0:
            raise ValueError("ECI assignment value out of range")
        elif assignval < (1 << 7):
            bb.append_bits(assignval, 8)
        elif assignval < (1 << 14):
            bb.append_bits(0b10, 2)
            bb.append_bits(assignval, 14)
        elif assignval < 1000000:
            bb.append_bits(0b110, 3)
            bb.append_bits(assignval, 21)
        else:
            raise ValueError("ECI assignment value out of range")
        return QrSegment(QrSegment.Mode.ECI, 0, bb)


    # Tests whether the given string can be encoded as a segment in numeric mode.
    # A string is encodable iff each character is in the range 0 to 9.
    @staticmethod
    def is_numeric(text: str) -> bool:
        return QrSegment._NUMERIC_REGEX.fullmatch(text) is not None


    # Tests whether the given string can be encoded as a segment in alphanumeric mode.
    # A string is encodable iff each character is in the following set: 0 to 9, A to Z
    # (uppercase only), space, dollar, percent, asterisk, plus, hyphen, period, slash, colon.
    @staticmethod
    def is_alphanumeric(text: str) -> bool:
        return QrSegment._ALPHANUMERIC_REGEX.fullmatch(text) is not None


    # ---- Private fields ----

    # The mode indicator of this segment. Accessed through get_mode().
    _mode: QrSegment.Mode

    # The length of this segment's unencoded data. Measured in characters for
    # numeric/alphanumeric/kanji mode, bytes for byte mode, and 0 for ECI mode.
    # Always zero or positive. Not the same as the data's bit length.
    # Accessed through get_num_chars().
    _numchars: int

    # The data bits of this segment. Accessed through get_data().
    _bitdata: List[int]


    # ---- Constructor (low level) ----

    def __init__(self, mode: QrSegment.Mode, numch: int, bitdata: Sequence[int]) -> None:
        """Creates a new QR Code segment with the given attributes and data.
        The character count (numch) must agree with the mode and the bit buffer length,
        but the constraint isn't checked. The given bit buffer is cloned and stored."""
        if numch < 0:
            raise ValueError()
        self._mode = mode
        self._numchars = numch
        self._bitdata = list(bitdata)  # Make defensive copy


    # ---- Accessor methods ----

    def get_mode(self) -> QrSegment.Mode:
        """Returns the mode field of this segment."""
        return self._mode

    def get_num_chars(self) -> int:
        """Returns the character count field of this segment."""
        return self._numchars

    def get_data(self) -> List[int]:
        """Returns a new copy of the data bits of this segment."""
        return list(self._bitdata)  # Make defensive copy


    # Package-private function
    @staticmethod
    def get_total_bits(segs: Sequence[QrSegment], version: int) -> Optional[int]:
        """Calculates the number of bits needed to encode the given segments at
        the given version. Returns a non-negative number if successful. Otherwise
        returns None if a segment has too many characters to fit its length field."""
        result = 0
        for seg in segs:
            ccbits: int = seg.get_mode().num_char_count_bits(version)
            if seg.get_num_chars() >= (1 << ccbits):
                return None  # The segment's length doesn't fit the field's bit width
            result += 4 + ccbits + len(seg._bitdata)
        return result


    # ---- Constants ----

    # Describes precisely all strings that are encodable in numeric mode.
    _NUMERIC_REGEX: re.Pattern = re.compile(r"[0-9]*")

    # Describes precisely all strings that are encodable in alphanumeric mode.
    _ALPHANUMERIC_REGEX: re.Pattern = re.compile(r"[A-Z0-9 $%*+./:-]*")

    # Dictionary of "0"->0, "A"->10, "$"->37, etc.
    _ALPHANUMERIC_ENCODING_TABLE: Dict[str,int] = {ch: i for (i, ch) in enumerate("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:")}


    # ---- Public helper enumeration ----

    class Mode:
        """Describes how a segment's data bits are interpreted. Immutable."""

        _modebits: int  # The mode indicator bits, which is a uint4 value (range 0 to 15)
        _charcounts: Tuple[int,int,int]  # Number of character count bits for three different version ranges

        # Private constructor
        def __init__(self, modebits: int, charcounts: Tuple[int,int,int]):
            self._modebits = modebits
            self._charcounts = charcounts

        # Package-private method
        def get_mode_bits(self) -> int:
            """Returns an unsigned 4-bit integer value (range 0 to 15) representing the mode indicator bits for this mode object."""
            return self._modebits

        # Package-private method
        def num_char_count_bits(self, ver: int) -> int:
            """Returns the bit width of the character count field for a segment in this mode
            in a QR Code at the given version number. The result is in the range [0, 16]."""
            return self._charcounts[(ver + 7) // 17]

        # Placeholders
        NUMERIC     : QrSegment.Mode
        ALPHANUMERIC: QrSegment.Mode
        BYTE        : QrSegment.Mode
        KANJI       : QrSegment.Mode
        ECI         : QrSegment.Mode

    # Public constants. Create them outside the class.
    Mode.NUMERIC      = Mode(0x1, (10, 12, 14))
    Mode.ALPHANUMERIC = Mode(0x2, ( 9, 11, 13))
    Mode.BYTE         = Mode(0x4, ( 8, 16, 16))
    Mode.KANJI        = Mode(0x8, ( 8, 10, 12))
    Mode.ECI          = Mode(0x7, ( 0,  0,  0))



# ---- Private helper class ----

class _BitBuffer(list):
    """An appendable sequence of bits (0s and 1s). Mainly used by QrSegment."""

    def append_bits(self, val: int, n: int) -> None:
        """Appends the given number of low-order bits of the given
        value to this buffer. Requires n >= 0 and 0 <= val < 2^n."""
        if (n < 0) or (val >> n != 0):
            raise ValueError("Value out of range")
        self.extend(((val >> i) & 1) for i in reversed(range(n)))


def _get_bit(x: int, i: int) -> bool:
    """Returns true iff the i'th bit of x is set to 1."""
    return (x >> i) & 1 != 0



class DataTooLongError(ValueError):
    """Raised when the supplied data does not fit any QR Code version. Ways to handle this exception include:
    - Decrease the error correction level if it was greater than Ecc.LOW.
    - If the encode_segments() function was called with a maxversion argument, then increase
      it if it was less than QrCode.MAX_VERSION. (This advice does not apply to the other
      factory functions because they search all versions up to QrCode.MAX_VERSION.)
    - Split the text data into better or optimal segments in order to reduce the number of bits required.
    - Change the text or binary data to be shorter.
    - Change the text to fit the character set of a particular segment mode (e.g. alphanumeric).
    - Propagate the error upward to the caller/user."""
    pass

# END vendored Nayuki qrcodegen

VERSION = "1.0.2"
MIB = 1024 * 1024
UA = "Mozilla/5.0 115StandaloneDownloader/1.0"
QR_BASE = "https://qrcodeapi.115.com"
API_HOSTS = {"qrcodeapi.115.com", "webapi.115.com", "proapi.115.com"}
CDN_SUFFIXES = ("115.com", "115cdn.com", "115cdn.net", "anxia.com", "aliyuncs.com")
SESSION = Path.home() / ".config" / "115-download" / "session.json"
NETWORK_ERRORS = (OSError, http.client.HTTPException)


class DownloadError(Exception):
    """A deliberately credential-free error safe to show on a terminal."""


def display(value):
    return "".join(c if c.isprintable() else "?" for c in str(value))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp-" + os.urandom(6).hex())
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, private=False):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise DownloadError("配置必须是普通文件。")
        if private and info.st_mode & 0o077:
            raise DownloadError("会话文件权限过宽；请将该文件权限设为 600。")
        return json.load(stream)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def direct_opener():
    # Do not inherit HTTP_PROXY / HTTPS_PROXY or connect to any local relay.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def validated_url(url, api=False):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise DownloadError("拒绝非 HTTPS 或非标准端口的下载地址。")
    valid = host in API_HOSTS if api else any(host == s or host.endswith("." + s) for s in CDN_SUFFIXES)
    if not valid:
        raise DownloadError("115 返回了未识别的下载域名，已停止，未转发账号信息。")
    return host


def normalize_cookie(cookie):
    if isinstance(cookie, dict):
        values = {str(k): str(v) for k, v in cookie.items()}
    elif isinstance(cookie, str):
        parsed = SimpleCookie()
        parsed.load(cookie)
        values = {key: item.value for key, item in parsed.items()}
    else:
        raise DownloadError("登录响应中没有可识别的会话。")
    if not all(values.get(key) for key in ("UID", "CID", "SEID")):
        raise DownloadError("登录会话不完整，需要重新扫码。")
    if any("\r" in v or "\n" in v for v in values.values()):
        raise DownloadError("会话内容格式不正确。")
    return "; ".join(key + "=" + values[key] for key in ("UID", "CID", "SEID", "KID") if key in values)


class Client:
    def __init__(self, cookie="", timeout=30):
        self.cookie = normalize_cookie(cookie) if cookie else ""
        self.timeout = timeout
        self.opener = direct_opener()
        self.last_api = 0.0

    def request(self, url, params=None, form=None, authenticated=True):
        validated_url(url, api=True)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        wait = self.last_api + 1.0 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if authenticated:
            if not self.cookie:
                raise DownloadError("尚未登录，请先运行 login。")
            headers["Cookie"] = self.cookie
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        self.last_api = time.monotonic()
        try:
            with self.opener.open(urllib.request.Request(url, data=body, headers=headers), timeout=self.timeout) as response:
                payload = response.read(8 * MIB + 1)
                if len(payload) > 8 * MIB:
                    raise DownloadError("115 接口响应异常大，已停止。")
                data = json.loads(payload)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise DownloadError("115 接口 HTTP %s；未改变登录会话。" % code) from None
        except NETWORK_ERRORS:
            raise DownloadError("连接 115 接口失败或超时，请稍后重试。") from None
        except (ValueError, UnicodeError):
            raise DownloadError("115 接口未返回有效 JSON，可能是网络验证页。") from None
        if not isinstance(data, dict):
            raise DownloadError("115 接口响应格式变化，已停止。")
        if data.get("state") is False:
            code = str(data.get("errno", data.get("errNo", data.get("code", "unknown"))))
            # Never echo API messages: they may contain cookies or signed URLs.
            safe_code = code if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", code) else "unknown"
            raise DownloadError("115 接口拒绝请求（错误码 %s）。不自动重新登录；如遇验证请先在官方 App 检查。" % safe_code)
        return data

    def listing(self, cid="0", offset=0, limit=50):
        payload = self.request("https://webapi.115.com/files", params={
            "cid": cid, "offset": offset, "limit": limit, "show_dir": 1,
            "cur": 1, "record_open_time": 0, "count_folders": 1,
        })
        entries = []
        for raw in payload.get("data", []):
            directory = "fid" not in raw and "file_id" not in raw
            if "is_directory" in raw:
                directory = bool(int(raw["is_directory"]))
            entry = {
                "id": str(raw.get("cid") if directory else raw.get("fid", raw.get("file_id", ""))),
                "name": str(raw.get("n", raw.get("file_name", raw.get("name", "")))),
                "directory": directory,
                "size": int(raw.get("s", raw.get("file_size", 0)) or 0),
                "sha1": str(raw.get("sha", raw.get("sha1", ""))).lower(),
                "pickcode": str(raw.get("pc", raw.get("pick_code", ""))),
            }
            entries.append(entry)
        return entries, int(payload.get("count", len(entries)))

    def resolve(self, path):
        parts = PurePosixPath(path).parts
        current = {"id": "0", "name": "/", "directory": True}
        for part in (p for p in parts if p != "/"):
            if part in (".", "..") or not current["directory"]:
                raise DownloadError("请使用网盘内的完整文件路径，不包含 ..。")
            matches = []
            offset = 0
            while True:
                entries, total = self.listing(current["id"], offset)
                matches.extend(e for e in entries if e["name"] == part)
                offset += len(entries)
                if not entries or offset >= total:
                    break
            if len(matches) != 1:
                raise DownloadError("路径不存在或存在同名项；请用交互浏览选择具体文件。")
            current = matches[0]
        return current

    def link(self, entry):
        if entry["directory"] or not entry.get("pickcode"):
            raise DownloadError("请选择可下载的普通文件。")
        uid = SimpleCookie(self.cookie)["UID"].value.split("_", 1)[0]
        clear = json.dumps({"pickcode": entry["pickcode"], "user_id": uid}, separators=(",", ":"))
        payload = self.request("https://proapi.115.com/app/chrome/downurl", form={"data": encrypt(clear).decode("ascii")})
        try:
            records = json.loads(decrypt(payload["data"]))
            record = records.get(entry["id"])
            if record is None and len(records) == 1:
                record = next(iter(records.values()))
            raw = record["url"]
            url = raw["url"] if isinstance(raw, dict) else raw
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme == "http":
                url = urllib.parse.urlunsplit(parsed._replace(scheme="https"))
            validated_url(url)
            size = int(record.get("file_size", entry["size"]))
            sha1 = str(record.get("sha1", entry.get("sha1", ""))).lower()
            if size != entry["size"] or (entry.get("sha1") and sha1 and sha1 != entry["sha1"]):
                raise DownloadError("文件在获取地址时发生变化；请重新选取。")
            # Account UID/CID/SEID cookies are never sent to a download CDN.
            return url
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DownloadError("115 下载地址响应格式异常，或文件当前不支持下载。") from None


def terminal_qr_lines(payload):
    """Two QR rows per character cell, including a four-module quiet zone."""
    qr = QrCode.encode_text(payload, QrCode.Ecc.MEDIUM)
    border = 4
    size = qr.get_size() + 2 * border
    matrix = [[qr.get_module(x - border, y - border) for x in range(size)] for y in range(size)]
    if len(matrix) % 2:
        matrix.append([False] * size)
    glyphs = " ▄▀█"  # foreground=black, background=white
    return ["".join(glyphs[2 * int(top) + int(bottom)] for top, bottom in zip(matrix[y], matrix[y + 1]))
            for y in range(0, len(matrix), 2)]


def print_terminal_qr(token, stream=None):
    stream = sys.stdout if stream is None else stream
    # Encode the actual scan payload, never the URL of the QR PNG image.
    payload = token.get("qrcode") or "https://115.com/scan/dg-" + str(token["uid"])
    if not isinstance(payload, str):
        print("二维码内容格式变化，请使用下方官方图片链接。", file=stream)
        return False
    try:
        parsed = urllib.parse.urlsplit(payload)
    except ValueError:
        print("二维码内容格式变化，请使用下方官方图片链接。", file=stream)
        return False
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("115.com", "qrcodeapi.115.com") or len(payload) > 512:
        print("二维码内容格式变化，请使用下方官方图片链接。", file=stream)
        return False
    try:
        lines = terminal_qr_lines(payload)
    except (ValueError, OverflowError):
        print("字符二维码生成失败，请使用下方官方图片链接。", file=stream)
        return False
    try:
        "".join(lines).encode(getattr(stream, "encoding", None) or "utf-8")
    except (LookupError, UnicodeError):
        print("当前终端不支持块字符，请使用下方官方图片链接。", file=stream)
        return False
    is_tty = bool(getattr(stream, "isatty", lambda: False)())
    if is_tty and os.environ.get("TERM", "") == "dumb":
        print("当前终端不支持固定黑白显示，请使用下方官方图片链接。", file=stream)
        return False
    if is_tty and shutil.get_terminal_size(fallback=(80, 24)).columns <= len(lines[0]):
        print("终端过窄，请加宽窗口或使用下方官方图片链接（至少 %d 列）。" % (len(lines[0]) + 1), file=stream)
        return False
    print("用 115 手机 App 扫描下方字符二维码并确认登录：", file=stream)
    # Fixed black/white colors avoid a dark terminal inverting the QR code.
    color = is_tty and os.environ.get("TERM", "") != "dumb"
    for line in lines:
        print(("\x1b[30;107m" + line + "\x1b[0m") if color else line, file=stream)
    stream.flush()
    return True


def login(session, app):
    print("扫码将使用设备类型 %s；同类型的旧会话可能失效。" % app)
    print("你的既有备份使用 tv；新脚本默认 alipaymini，请确保该类型未被其他任务使用。")
    if input("按回车开始扫码，输入 q 取消：").strip().lower() == "q":
        return None
    # The status endpoint can long-poll for about 30 seconds before replying.
    client = Client(timeout=40)
    token = client.request(QR_BASE + "/api/1.0/web/1.0/token/", authenticated=False)["data"]
    qr_url = QR_BASE + "/api/1.0/web/1.0/qrcode?" + urllib.parse.urlencode({"uid": token["uid"]})
    print_terminal_qr(token)
    print("备用：在电脑浏览器打开下面的 115 官方二维码图片链接：")
    print(qr_url)
    print("等待扫码（最多 3 分钟）……", flush=True)
    end = time.monotonic() + 180
    state = None
    failures = 0
    while time.monotonic() < end:
        time.sleep(2)
        try:
            result = client.request(QR_BASE + "/get/status/", params={k: token[k] for k in ("uid", "time", "sign")}, authenticated=False)
            data = result.get("data")
            if isinstance(data, dict) and data.get("status") is None:
                # No state transition yet. Only an explicit status=2 can login.
                continue
            if not isinstance(data, dict):
                raise DownloadError("二维码状态响应格式异常。")
            try:
                status = int(data["status"])
            except (TypeError, ValueError):
                raise DownloadError("二维码状态响应格式异常。") from None
            failures = 0
        except DownloadError:
            failures += 1
            if failures >= 3:
                raise DownloadError("二维码状态连续查询失败，请稍后重新运行 login。") from None
            continue
        if status == 1 and state != 1:
            print("已扫描，请在手机上确认。", flush=True)
        state = status
        if status == 2:
            result = client.request(QR_BASE + "/app/1.0/" + app + "/1.0/login/qrcode/", form={"account": token["uid"]}, authenticated=False)
            data = result.get("data")
            if not isinstance(data, dict) or not data.get("cookie"):
                raise DownloadError("扫码已确认，但 115 未返回该设备类型的会话；请检查官方 App 提示或选其他未占用的设备类型。")
            cookie = normalize_cookie(data["cookie"])
            write_json(session, {"version": 1, "app": app, "cookie": cookie, "saved_at": utc_now()})
            print("登录成功，会话仅保存在 %s（权限 600）。" % display(session))
            return Client(cookie)
        if status in (-1, -2):
            raise DownloadError("二维码已过期或被取消，请重新运行 login。")
    raise DownloadError("等待扫码超时；未创建登录会话。")


def browse(client):
    stack = [("0", "/")]
    offset = 0
    while True:
        entries, total = client.listing(stack[-1][0], offset)
        print("\n目录：%s  第 %d–%d 项，共 %d 项" % (display("/".join(x[1] for x in stack).replace("//", "/")), offset + 1 if total else 0, offset + len(entries), total))
        for index, entry in enumerate(entries, 1):
            size = "目录" if entry["directory"] else "%.2f MiB" % (entry["size"] / MIB)
            print("%3d  %-12s %s" % (index, size, display(entry["name"])))
        choice = input("输入序号；n 下一页，p 上一页，u 上级，q 退出：").strip().lower()
        if choice == "q":
            return None
        if choice == "u":
            if len(stack) > 1:
                stack.pop()
            offset = 0
        elif choice == "n" and offset + len(entries) < total:
            offset += len(entries)
        elif choice == "p":
            offset = max(0, offset - 50)
        elif choice.isdigit() and 1 <= int(choice) <= len(entries):
            entry = entries[int(choice) - 1]
            if entry["directory"]:
                stack.append((entry["id"], entry["name"]))
                offset = 0
            else:
                return entry


def file_fingerprint(entry):
    return {key: entry.get(key) for key in ("id", "pickcode", "size", "sha1")}


def safe_name(name):
    name = str(name).replace("\\", "_").replace("/", "_")
    name = "".join(c if c.isprintable() else "_" for c in name)
    if name in ("", ".", ".."):
        raise DownloadError("远端文件名无效，请用 --output 指定本地文件。")
    return name


def open_download(opener, url, offset, length, total, timeout=30):
    # Only the fixed UA and Range are sent; never any 115 account cookie/token.
    headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
    ranged = offset > 0 or length != total
    if ranged:
        headers["Range"] = "bytes=%d-%d" % (offset, offset + length - 1)
    for _ in range(5):
        host = validated_url(url)
        try:
            response = opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location", "")
                exc.close()
                if not location:
                    raise DownloadError("下载跳转缺少地址。") from None
                url = urllib.parse.urljoin(url, location)
                continue
            raise
        try:
            content_range = response.headers.get("Content-Range")
            if ranged:
                expected = "bytes %d-%d/%d" % (offset, offset + length - 1, total)
                if response.status != 206 or content_range != expected:
                    raise DownloadError("服务器未按指定位置续传；已停止，保留原有部分文件。")
            elif response.status != 200:
                raise DownloadError("完整下载响应状态异常。")
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) != length:
                raise DownloadError("服务器返回的文件大小与选取版本不一致。")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise DownloadError("服务器返回压缩传输，无法保证断点位置。")
            return response, host
        except BaseException:
            response.close()
            raise
    raise DownloadError("下载跳转次数过多，已停止。")


def transfer(client, entry, stream, start, end, rate=0, attempts=3):
    begun = time.monotonic()
    position = start
    first_byte = None
    body_seconds = 0.0
    hosts = set()
    last_progress = 0.0
    failure = None
    retries_used = 0
    opener = direct_opener()
    for attempt in range(attempts):
        if position >= end:
            break
        response = None
        body_start = None
        try:
            url = client.link(entry)  # Refresh links when retrying; never persist them.
            response, host = open_download(opener, url, position, end - position, entry["size"])
            hosts.add(host)
            attempt_start = time.monotonic()
            attempt_bytes = 0
            while position < end:
                block = response.read(1 if body_start is None else min(256 * 1024, end - position))
                if not block:
                    raise OSError("early_eof")
                if body_start is None:
                    body_start = time.monotonic()
                    if first_byte is None:
                        first_byte = body_start
                if stream is not None:
                    try:
                        stream.write(block)
                    except OSError:
                        raise DownloadError("本地文件写入失败，请检查磁盘空间和权限；已停止网络重试。") from None
                position += len(block)
                attempt_bytes += len(block)
                if rate:
                    delay = attempt_start + attempt_bytes / (rate * MIB) - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                now = time.monotonic()
                if now - last_progress >= 1:
                    speed = (position - start) / MIB / max(now - begun, 0.001)
                    print("\r已传输 %.2f / %.2f MiB；本次平均 %.2f MiB/s" % ((position - start) / MIB, (end - start) / MIB, speed), end="", file=sys.stderr, flush=True)
                    last_progress = now
            failure = None
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            failure = "下载节点 HTTP %d" % code
            if code not in (401, 403, 408, 429, 500, 502, 503, 504):
                raise DownloadError(failure) from None
        except NETWORK_ERRORS:
            failure = "下载连接中断或超时"
        finally:
            if body_start is not None:
                body_seconds += time.monotonic() - body_start
            if response is not None:
                response.close()
            if stream is not None:
                stream.flush()
        if position >= end:
            break
        if attempt + 1 < attempts:
            retries_used += 1
            delay = (5, 15, 30)[min(attempt, 2)]
            print("\n%s；%d 秒后从已收字节继续（%d/%d）。" % (failure, delay, attempt + 2, attempts), file=sys.stderr, flush=True)
            time.sleep(delay)
    print(file=sys.stderr)
    elapsed = time.monotonic() - begun
    if position != end:
        raise DownloadError("%s；达到重试上限，部分文件已保留。重跑相同命令可以继续。" % failure)
    return {
        "started_offset": start, "received_bytes": position - start,
        "elapsed_seconds": round(elapsed, 4),
        "first_byte_seconds": round(first_byte - begun, 4) if first_byte else None,
        "average_mib_s": round((position - start) / MIB / max(elapsed, .001), 4),
        "body_mib_s": round((position - start) / MIB / max(body_seconds, .001), 4),
        "download_hosts": sorted(hosts), "retries": retries_used,
    }


def hash_file(path):
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(MIB), b""):
            sha1.update(block)
            sha256.update(block)
    return sha1.hexdigest(), sha256.hexdigest()


def download(client, entry, output, rate=0, attempts=3, expected_sha256=None):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".part")
    metadata = partial.with_name(partial.name + ".json")
    fingerprint = file_fingerprint(entry)
    if output.exists() or output.is_symlink():
        raise DownloadError("目标文件已存在；请换一个 --output 文件名，程序不会覆盖。")
    if partial.exists() or partial.is_symlink():
        if not metadata.exists() or read_json(metadata) != fingerprint:
            raise DownloadError("已有部分文件与当前文件版本不匹配；请换一个输出文件名。")
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    else:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(partial, flags, 0o600), "r+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DownloadError("另一个下载进程正在写入同一个文件。") from None
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise DownloadError("部分文件不是普通文件。")
        write_json(metadata, fingerprint)
        start = stream.seek(0, os.SEEK_END)
        if start > entry["size"]:
            raise DownloadError("部分文件大于远端文件，拒绝续传。")
        stats = transfer(client, entry, stream, start, entry["size"], rate, attempts)
        stream.flush()
        os.fsync(stream.fileno())
        print("正在校验完整文件……", flush=True)
        sha1, sha256 = hash_file(partial)
        cloud_sha1 = entry.get("sha1", "")
        if cloud_sha1 and sha1.lower() != cloud_sha1.lower():
            raise DownloadError("SHA1 与 115 文件记录不一致；保留 .part 供排查，未发布为完整文件。")
        if expected_sha256 and sha256.lower() != expected_sha256.lower():
            raise DownloadError("SHA256 与给定值不一致；保留 .part，未发布为完整文件。")
        # Atomic no-clobber publication, even when another process creates output.
        try:
            os.link(partial, output)
        except FileExistsError:
            raise DownloadError("下载期间目标文件被其他程序创建，已保留 .part。") from None
        partial.unlink()
        metadata.unlink(missing_ok=True)
    report = dict(version=VERSION, finished_at=utc_now(), mode="download", file=entry["name"],
                  size=entry["size"], limit_mib_s=rate or None, sha1=sha1, sha256=sha256,
                  cloud_sha1_verified=bool(cloud_sha1), expected_sha256_verified=bool(expected_sha256), **stats)
    report_path = output.with_name(output.name + ".download.json")
    write_json(report_path, report)
    print("完成：%s\n平均 %.2f MiB/s；正文 %.2f MiB/s\nSHA256: %s" % (display(output), stats["average_mib_s"], stats["body_mib_s"], sha256))
    print("115 SHA1：%s；报告：%s" % ("一致" if cloud_sha1 else "接口未提供，仅核验大小并计算本地哈希", display(report_path)))
    return report


def benchmark(client, entry, sample_mib=128, rounds=3, rate=0):
    size = min(entry["size"], int(sample_mib * MIB))
    if not size:
        raise DownloadError("空文件不能用于测速。")
    print("单路、不保存正文；每轮 %.2f MiB，共 %d 轮。" % (size / MIB, rounds))
    results = []
    for round_number in range(1, rounds + 1):
        print("第 %d/%d 轮" % (round_number, rounds), flush=True)
        result = transfer(client, entry, None, 0, size, rate, attempts=1)
        results.append(result)
        print("平均 %.2f MiB/s；正文 %.2f MiB/s" % (result["average_mib_s"], result["body_mib_s"]))
    report = dict(version=VERSION, finished_at=utc_now(), mode="benchmark", file=entry["name"],
                  file_bytes=entry["size"], sample_bytes=size, limit_mib_s=rate or None,
                  full_integrity_verified=False, results=results)
    path = Path("115-speed-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    write_json(path, report)
    print("报告：%s（测速不等于完整文件校验）。" % path)
    return report


def positive_float(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("必须为正数")
    return result


def parser():
    root = argparse.ArgumentParser(description="115 直连下载与测速；Python 3.9+，无需额外依赖。无参数进入扫码/文件选择。", epilog="账号只保存在当前服务器；不会自动重新登录或访问任何 AList/biomed 服务。")
    root.add_argument("--session", type=Path, default=SESSION, help="本机会话文件，默认 ~/.config/115-download/session.json")
    root.add_argument("--version", action="version", version=VERSION)
    commands = root.add_subparsers(dest="command")
    auth = commands.add_parser("login", help="扫码登录（同设备类型可能替换旧会话）")
    auth.add_argument("--app", default="alipaymini", choices=("alipaymini", "wechatmini", "web", "qandroid", "qandriod", "os_linux", "tv"))
    listing = commands.add_parser("ls", help="查看一个目录，按页获取")
    listing.add_argument("path", nargs="?", default="/")
    listing.add_argument("--page", type=int, default=1)
    for name, help_text in (("get", "下载文件并测速、校验，支持重跑续传"), ("bench", "仅读取文件片段测速，不保存正文")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("path", help="115 内完整路径，例如 /software/test.bin")
        sub.add_argument("--limit-mib-s", type=positive_float, default=0, help="可选单路限速，默认不限速")
        if name == "get":
            sub.add_argument("--output", "-o", type=Path)
            sub.add_argument("--sha256", help="可选，发布方提供的 SHA256")
            sub.add_argument("--attempts", type=int, choices=range(1, 6), default=3)
        else:
            sub.add_argument("--sample-mib", type=positive_float, default=128)
            sub.add_argument("--rounds", type=int, choices=range(1, 6), default=3)
    return root


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    if args.command == "login":
        login(args.session, args.app)
        return 0
    if args.session.exists():
        client = Client(read_json(args.session, private=True)["cookie"])
    elif args.command is None and sys.stdin.isatty():
        client = login(args.session, "alipaymini")
        if client is None:
            return 0
    else:
        raise DownloadError("尚未登录。先运行：python3 download_115.py login")
    if args.command is None:
        if not sys.stdin.isatty():
            raise DownloadError("非交互环境请使用 get、ls 或 bench 子命令。")
        entry = browse(client)
        if entry is None:
            return 0
        print("已选择 %s（%.2f MiB）" % (display(entry["name"]), entry["size"] / MIB))
        action = input("1 下载完整文件并校验，2 仅测速（默认，每轮最多 128 MiB），q 退出：").strip()
        if action == "q":
            return 0
        if action == "1":
            download(client, entry, safe_name(entry["name"]))
        elif action in ("", "2"):
            benchmark(client, entry)
        else:
            raise DownloadError("未选择有效操作，未开始下载。")
        return 0
    entry = client.resolve(args.path)
    if args.command == "ls":
        if not entry["directory"] or args.page < 1:
            raise DownloadError("ls 需要目录路径，页码从 1 开始。")
        entries, total = client.listing(entry["id"], (args.page - 1) * 50)
        for item in entries:
            print("%s\t%s\t%s" % ("DIR" if item["directory"] else "FILE", item["size"], display(item["name"])))
        print("共 %d 项；当前第 %d 页。" % (total, args.page))
    elif args.command == "get":
        if args.sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", args.sha256):
            raise DownloadError("--sha256 必须是 64 位十六进制值。")
        if entry["directory"]:
            raise DownloadError("get 当前仅支持单文件，请选择目录内的具体文件。")
        download(client, entry, args.output or safe_name(entry["name"]), args.limit_mib_s, args.attempts, args.sha256)
    elif args.command == "bench":
        if entry["directory"]:
            raise DownloadError("bench 需要具体文件。")
        benchmark(client, entry, args.sample_mib, args.rounds, args.limit_mib_s)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断；下载产生的 .part 已保留，重跑相同命令可续传。", file=sys.stderr)
        raise SystemExit(130)
    except DownloadError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        print("错误：本地文件、网络或接口响应异常；未输出可能含凭据的原始响应。", file=sys.stderr)
        raise SystemExit(1)
