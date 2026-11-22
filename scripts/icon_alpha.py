#!/usr/bin/env python3
"""Give a Quick Look-rendered 1024px icon transparent corners.

Quick Look renders SVGs on white. The icon is a known rounded square
(x/y 64..960, radius 200), so alpha comes from that shape's coverage and the
white matte is removed from the anti-aliased edge. Standard library only.
"""
import math
import struct
import sys
import zlib


def read_png(path):
    data = open(path, 'rb').read()
    pos, chunks = 8, {}
    idat = b''
    while pos < len(data):
        length, kind = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b'IHDR': chunks['IHDR'] = body
        if kind == b'IDAT': idat += body
        pos += 12 + length
    width, height, depth, color = struct.unpack('>IIBB', chunks['IHDR'][:10])
    if depth != 8 or color not in (2, 6): raise SystemExit(f'unsupported PNG: depth {depth} color {color}')
    bpp = 3 if color == 2 else 4
    raw, stride, rows, prev = zlib.decompress(idat), width * bpp, [], bytearray(width * bpp)
    for y in range(height):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], prev[i - bpp] if i >= bpp else 0
            if f == 1: line[i] = (line[i] + a) & 255
            elif f == 2: line[i] = (line[i] + b) & 255
            elif f == 3: line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line); prev = line
    return width, height, bpp, rows


def coverage(x, y, scale):
    lo, hi, r = 64 * scale, 960 * scale, 200 * scale
    cx = min(max(x, lo + r), hi - r); cy = min(max(y, lo + r), hi - r)
    inside = lo <= x <= hi and lo <= y <= hi
    d = math.hypot(x - cx, y - cy) - r if (x < lo + r or x > hi - r) and (y < lo + r or y > hi - r) else \
        (-min(x - lo, hi - x, y - lo, hi - y) if inside else max(lo - x, x - hi, lo - y, y - hi))
    return min(1.0, max(0.0, 0.5 - d))


def main(src, dst):
    width, height, bpp, rows = read_png(src)
    scale = width / 1024
    out = bytearray()
    for y, line in enumerate(rows):
        out.append(0)
        for x in range(width):
            px = line[x * bpp:x * bpp + 3]
            a = coverage(x + 0.5, y + 0.5, scale)
            rgb = [0, 0, 0] if a == 0 else [min(255, max(0, round((v - (1 - a) * 255) / a))) for v in px]
            out += bytes(rgb) + bytes([round(a * 255)])
    def chunk(kind, body): return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)) + \
          chunk(b'IDAT', zlib.compress(bytes(out), 9)) + chunk(b'IEND', b'')
    open(dst, 'wb').write(png)


if __name__ == '__main__':
    main(*sys.argv[1:3])
