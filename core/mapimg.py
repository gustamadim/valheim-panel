"""Minimal PNG decode/encode and map-layer decoding (stdlib only).

The panel image has no PIL and no numpy, so this module does the byte work with
`zlib` and `array`. It is deliberately narrow: it handles exactly the PNGs the
ValheimUI agent produces (8-bit, colour types 2/4/6, all filter types) and emits
filter-0 RGB/RGBA.

Layer encoding (from the agent's contract, ARCHITECTURE.md §20):

    R = terrain mask nibble (high) | biome code (low)
    G:B = height as (h + 200) * 32, big endian
    A = forest factor * 100

    biome code: 1 Meadows, 2 Black Forest, 3 Swamp, 4 Mountain, 5 Plains,
                6 Mistlands, 7 Ashlands, 8 Deep North, 9 Ocean, 15 off-world

Fog is a separate grey+alpha PNG where **255 = unexplored** in both channels.
"""
from __future__ import annotations

import struct
import zlib
from array import array

PNG_SIG = b"\x89PNG\r\n\x1a\n"


class PngError(RuntimeError):
    pass


# ----------------------------------------------------------------------
# decode
# ----------------------------------------------------------------------
def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data: bytes) -> tuple[int, int, int, list[bytes]]:
    """Return (width, height, channels, unfiltered rows)."""
    if data[:8] != PNG_SIG:
        raise PngError("not a PNG")

    pos = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()
    palette = None
    trns = None

    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
            if chunk[10] != 0 or chunk[11] != 0:
                raise PngError("interlaced PNG not supported")
        elif ctype == b"PLTE":
            palette = chunk
        elif ctype == b"tRNS":
            trns = chunk
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break

    if width is None:
        raise PngError("missing IHDR")
    if bit_depth != 8:
        raise PngError(f"unsupported bit depth {bit_depth}")

    if color_type == 3 and palette is not None:
        # Palette: expand to RGB(A) rows.
        channels = 3 if trns is None else 4
        stride = width
    else:
        channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
        if channels is None:
            raise PngError(f"unsupported colour type {color_type}")
        stride = width * channels

    raw = zlib.decompress(bytes(idat))
    rows: list[bytes] = []
    prev = bytearray(stride)
    i = 0
    for _ in range(height):
        if i >= len(raw):
            raise PngError("truncated image data")
        ftype = raw[i]
        i += 1
        line = bytearray(raw[i:i + stride])
        if len(line) < stride:
            raise PngError("truncated scanline")
        i += stride

        if ftype == 0:
            pass
        elif ftype == 1:
            for x in range(channels, stride):
                line[x] = (line[x] + line[x - channels]) & 0xFF
        elif ftype == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ftype == 3:
            for x in range(stride):
                left = line[x - channels] if x >= channels else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif ftype == 4:
            for x in range(stride):
                a = line[x - channels] if x >= channels else 0
                b = prev[x]
                c = prev[x - channels] if x >= channels else 0
                line[x] = (line[x] + _paeth(a, b, c)) & 0xFF
        else:
            raise PngError(f"bad filter type {ftype}")

        if color_type == 3 and palette is not None:
            out = bytearray(width * channels)
            for px in range(width):
                idx = line[px]
                base = idx * 3
                out[px * channels] = palette[base]
                out[px * channels + 1] = palette[base + 1]
                out[px * channels + 2] = palette[base + 2]
                if channels == 4:
                    out[px * channels + 3] = trns[idx] if idx < len(trns) else 255
            rows.append(bytes(out))
        else:
            rows.append(bytes(line))
        prev = line

    return width, height, channels, rows


# ----------------------------------------------------------------------
# encode
# ----------------------------------------------------------------------
def encode_png(width: int, height: int, channels: int, rows: list[bytes]) -> bytes:
    """Encode 8-bit rows with filter 0 (simple, predictable, fast enough)."""
    color_type = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    raw = bytearray()
    for row in rows:
        raw.append(0)
        raw += row
    compressed = zlib.compress(bytes(raw), 6)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return PNG_SIG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


# ----------------------------------------------------------------------
# layer / fog decoding
# ----------------------------------------------------------------------
class Layers:
    """Decoded terrain layers at their native resolution."""

    __slots__ = ("width", "height", "biome", "height_raw", "forest", "mask")

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.biome = array("B", bytes(width * height))
        self.height_raw = array("H", bytes(width * height * 2))
        self.forest = array("B", bytes(width * height))
        self.mask = array("B", bytes(width * height))

    def at(self, x: int, y: int) -> tuple[int, int, int, int]:
        i = y * self.width + x
        return (self.biome[i], self.height_raw[i], self.forest[i], self.mask[i])

    def height_m(self, x: int, y: int) -> float:
        return self.height_raw[y * self.width + x] / 32.0 - 200.0


def decode_layers(png: bytes) -> Layers:
    w, h, ch, rows = decode_png(png)
    if ch != 4:
        raise PngError(f"layers PNG should be RGBA, got {ch} channels")

    layers = Layers(w, h)
    biome = layers.biome
    hraw = layers.height_raw
    forest = layers.forest
    mask = layers.mask

    for y, row in enumerate(rows):
        base = y * w
        # Slice per channel via memoryview stepping: avoids a per-pixel tuple.
        r = row[0::4]
        g = row[1::4]
        b = row[2::4]
        a = row[3::4]
        for x in range(w):
            rv = r[x]
            biome[base + x] = rv & 0x0F
            mask[base + x] = rv >> 4
            hraw[base + x] = (g[x] << 8) | b[x]
            forest[base + x] = a[x]
    return layers


def decode_fog(png: bytes) -> tuple[int, int, array]:
    """Return (width, height, grey) where 255 means unexplored."""
    w, h, ch, rows = decode_png(png)
    grey = array("B", bytes(w * h))
    for y, row in enumerate(rows):
        base = y * w
        if ch >= 2:
            src = row[0::ch]
        else:
            src = row
        grey[base:base + w] = array("B", src[:w])
    return w, h, grey
