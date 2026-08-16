"""Generate the Only Me & You app icons — pure stdlib, no Pillow.

This repo deliberately keeps its dependency list tiny, so the icons are
rasterized by hand: a pastel pink→violet gradient with two overlapping
hearts (me & you), written out as PNGs with zlib + struct. Run once and
commit the results:

    python3 -m onlymeyou.tools.make_icons
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parents[1] / "static" / "icons"

GRAD_TOP = (255, 153, 178)      # blush pink
GRAD_BOTTOM = (167, 139, 250)   # soft violet


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path: Path, size: int, pixels: bytearray) -> None:
    raw = b"".join(
        b"\x00" + bytes(pixels[y * size * 4:(y + 1) * size * 4])
        for y in range(size)
    )
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def _heart_hit(px: float, py: float, cx: float, cy: float, s: float,
               rot_deg: float) -> bool:
    """Point-in-heart via the classic implicit curve (x²+y²-1)³ = x²y³."""
    u = (px - cx) / s
    v = (cy - py) / s  # flip: screen y grows downward
    r = math.radians(rot_deg)
    ur = u * math.cos(r) - v * math.sin(r)
    vr = u * math.sin(r) + v * math.cos(r)
    vr += 0.25  # recentre: the curve's visual middle sits below its origin
    a = ur * ur + vr * vr - 1.0
    return a * a * a - ur * ur * vr * vr * vr <= 0.0


def render(size: int, maskable: bool = False) -> bytearray:
    scale = 0.78 if maskable else 1.0
    hearts = [  # (cx, cy, half-width, rotation, alpha) back first
        (0.5 + (0.615 - 0.5) * scale, 0.5 + (0.400 - 0.5) * scale, 0.230 * scale, -16.0, 0.62),
        (0.5 + (0.415 - 0.5) * scale, 0.5 + (0.540 - 0.5) * scale, 0.300 * scale, 10.0, 1.00),
    ]
    px = bytearray(size * size * 4)
    sub = 3  # 3×3 supersampling for soft edges
    step = 1.0 / (size * sub)
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2.0 * size)
            bg = [GRAD_TOP[i] + (GRAD_BOTTOM[i] - GRAD_TOP[i]) * t for i in range(3)]
            # gentle highlight toward the top-left, like morning light
            d = math.hypot(x / size - 0.32, y / size - 0.25)
            glow = max(0.0, 1.0 - d / 1.1) * 22
            color = [min(255.0, c + glow) for c in bg]
            for cx, cy, s, rot, alpha in hearts:
                hits = 0
                for sy in range(sub):
                    for sx in range(sub):
                        fx = (x * sub + sx + 0.5) * step
                        fy = (y * sub + sy + 0.5) * step
                        if _heart_hit(fx, fy, cx, cy, s, rot):
                            hits += 1
                if hits:
                    a = alpha * hits / (sub * sub)
                    color = [c + (255.0 - c) * a for c in color]
            i = (y * size + x) * 4
            px[i:i + 4] = bytes((int(color[0]), int(color[1]), int(color[2]), 255))
    return px


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("icon-512.png", 512, False),
        ("icon-192.png", 192, False),
        ("icon-180.png", 180, False),
        ("icon-512-maskable.png", 512, True),
    ]
    for name, size, maskable in jobs:
        write_png(OUT_DIR / name, size, render(size, maskable))
        print(f"  🖼  {name} ({size}×{size})")


if __name__ == "__main__":
    main()
