"""Generate patches/route_icons.h: chain block icons for Split / Path B / Merge (original app patch).

Same geometry as the app's own blocks (measured from its screens): 32x44 (main screen), 36x50 and 54x76
(edit screen), rounded box with a 2 px (3 px for the big one) border, glyph in the upper part, label below.
2 bits per pixel with a palette (0 = transparent); the bypassed variant shares the pixels with a grey
palette. Also writes <build>/route_icons.png as a preview.
"""
import os, sys
from matribox import paths
from matribox.app.mkfont import G

HERE = os.path.dirname(os.path.abspath(__file__))
TRANSP, BORDER, FILL, INK = range(4)
ON = (0x000000, 0xff5a3410, 0xff1f1206, 0xffffa43a)       # ARGB, 0xff = opaque
OFF = (0x000000, 0xff3a3a3a, 0xff141414, 0xff808080)

# name, width, height, border, corner radius, glyph box (y0, y1), label top, label scale
SIZES = [('main', 32, 44, 2, 4, 7, 21, 27, 1),
         ('small', 36, 50, 2, 4, 9, 24, 31, 1),
         ('big', 54, 76, 3, 7, 15, 37, 46, 2)]
MODULES = [('split', 'SPL'), ('pathb', 'B'), ('merge', 'MRG')]


def box(w, h, bw, r):
    px = [[TRANSP] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            # distance outside the rounded corner
            cx = r - 1 - x if x < r else x - (w - r) if x >= w - r else -1
            cy = r - 1 - y if y < r else y - (h - r) if y >= h - r else -1
            if cx >= 0 and cy >= 0:
                d2 = (cx + 0.5) ** 2 + (cy + 0.5) ** 2
                if d2 > r * r:
                    continue
                inner = d2 <= (r - bw) ** 2
            else:
                inner = bw <= x < w - bw and bw <= y < h - bw
            px[y][x] = FILL if inner else BORDER
    return px


def line(px, x0, y0, x1, y1, t):
    n = max(abs(x1 - x0), abs(y1 - y0), 1)
    for i in range(n + 1):
        x = x0 + (x1 - x0) * i / n
        y = y0 + (y1 - y0) * i / n
        for dy in range(t):
            for dx in range(t):
                xi, yi = int(round(x)) + dx, int(round(y)) + dy
                if 0 <= yi < len(px) and 0 <= xi < len(px[0]) and px[yi][xi] != TRANSP:
                    px[yi][xi] = INK


def dot(px, cx, cy, r):
    for y in range(cy - r, cy + r + 1):
        for x in range(cx - r, cx + r + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r + r:
                px[y][x] = INK


def rect(px, x0, y0, x1, y1):
    """filled, inclusive"""
    for y in range(min(y0, y1), max(y0, y1) + 1):
        for x in range(min(x0, x1), max(x0, x1) + 1):
            if px[y][x] != TRANSP:
                px[y][x] = INK


def glyph(px, kind, w, y0, y1, s):
    """orthogonal strokes only, like the app's own icons"""
    t = 3 if s > 1 else 2                       # stroke width
    gw = (y1 - y0) * 4 // 3                     # glyph box, centred
    x0 = (w - gw) // 2
    x1 = x0 + gw - 1
    cy = (y0 + y1) // 2 - t // 2                # top of the middle stroke
    ya, yb = y0 + 1, y1 - t                     # tops of the two lanes
    hs = lambda xa, xb, y: rect(px, xa, y, xb, y + t - 1)          # horizontal stroke
    vs = lambda x, ya_, yb_: rect(px, x, ya_, x + t - 1, yb_ + t - 1)   # vertical stroke
    if kind == 'split':                         # -[=
        xm = x0 + gw // 3
        hs(x0, xm, cy)
        vs(xm, ya, yb)
        hs(xm, x1, ya)
        hs(xm, x1, yb)
    elif kind == 'merge':                       # =]-
        xm = x1 - gw // 3 - t + 1
        hs(x0, xm, ya)
        hs(x0, xm, yb)
        vs(xm, ya, yb)
        hs(xm, x1, cy)
    else:                                       # path B: in on lane A, step down, out on lane B
        xm = x0 + gw // 3
        hs(x0, xm, ya)
        vs(xm, ya, yb)
        hs(xm, x1, yb)
        for x in range(xm + 2 * t, x1 + 1, 2 * t):   # lane A continues dotted
            rect(px, x, ya, min(x + t - 1, x1), ya + t - 1)


def text(px, s, top, scale):
    cw = 6 * scale                              # 5 columns + 1 bold column, 1 spacing
    tw = len(s) * (cw + scale) - scale
    left = (len(px[0]) - tw) // 2
    for i, ch in enumerate(s):
        rows = G[ch]
        for ry, row in enumerate(rows):
            for rx, c in enumerate(row):
                if c != '#':
                    continue
                for bx in (0, 1):               # bold: each pixel doubled to the right
                    for sy in range(scale):
                        for sx in range(scale):
                            x = left + i * (cw + scale) + (rx + bx) * scale + sx
                            y = top + ry * scale + sy
                            if px[y][x] != TRANSP:
                                px[y][x] = INK


def icon(kind, label, size):
    name, w, h, bw, r, gy0, gy1, ly, s = size
    px = box(w, h, bw, r)
    glyph(px, kind, w, gy0, gy1, s)
    text(px, label, ly, s)
    return px


def pack2(px):
    w = len(px[0])
    bpl = (w + 3) // 4
    out = bytearray()
    for row in px:
        b = bytearray(bpl)
        for x, v in enumerate(row):
            b[x // 4] |= v << (6 - 2 * (x % 4))
        out += b
    return bpl, bytes(out)


def main():
    lines = ['/* generated by matribox/app/mkicons.py: Split / Path B / Merge block icons, 2 bpp emWin bitmaps */',
             '#pragma once', '#include <stdint.h>', '',
             'typedef struct { int NumEntries; char HasTrans; const uint32_t *pPalEntries; } GUI_LOGPALETTE;',
             'typedef struct { uint16_t XSize, YSize, BytesPerLine, BitsPerPixel; const uint8_t *pData;',
             '                 const GUI_LOGPALETTE *pPal; const void *pMethods; } GUI_BITMAP;', '',
             'static const uint32_t route_pal_on[4] = { %s };' % ', '.join('0x%08xu' % c for c in ON),
             'static const uint32_t route_pal_off[4] = { %s };' % ', '.join('0x%08xu' % c for c in OFF),
             'static const GUI_LOGPALETTE route_on = { 4, 1, route_pal_on };',
             'static const GUI_LOGPALETTE route_off = { 4, 1, route_pal_off };', '']
    preview = []
    for kind, label in MODULES:
        for size in SIZES:
            px = icon(kind, label, size)
            preview.append(px)
            bpl, data = pack2(px)
            n = '%s_%s' % (kind, size[0])
            lines.append('static const uint8_t %s_px[%d] = {' % (n, len(data)))
            for o in range(0, len(data), 16):
                lines.append('    ' + ', '.join('0x%02x' % b for b in data[o:o + 16]) + ',')
            lines.append('};')
            for st in ('on', 'off'):
                lines.append('static const GUI_BITMAP %s_%s = { %d, %d, %d, 2, %s_px, &route_%s, 0 };'
                             % (n, st, size[1], size[2], bpl, n, st))
            lines.append('')
    for st in ('on', 'off'):
        lines.append('/* [module: split, path b, merge][size: main 32x44, small 36x50, big 54x76] */')
        lines.append('static const GUI_BITMAP *const route_icons_%s[3][3] = {' % st)
        for kind, _ in MODULES:
            lines.append('    { %s },' % ', '.join('&%s_%s_%s' % (kind, s[0], st) for s in SIZES))
        lines.append('};')
    open(os.path.join(HERE, 'patches', 'route_icons.h'), 'w').write('\n'.join(lines) + '\n')

    # preview: on (top) and off (bottom), 2x
    from matribox.pngio import png
    S, W, H = 2, 3 * (32 + 36 + 54 + 12) * 2, 2 * 80 * 2
    img = bytearray(W * H * 3)
    x = 0
    for k, px in enumerate(preview):
        for row, pal in enumerate((ON, OFF)):
            for y, r in enumerate(px):
                for xx, v in enumerate(r):
                    c = pal[v] if v else 0
                    for sy in range(S):
                        for sx in range(S):
                            o = ((row * 80 + y) * S + sy) * W * 3 + ((x + xx) * S + sx) * 3
                            img[o:o + 3] = bytes(((c >> 16) & 255, (c >> 8) & 255, c & 255))
        x += len(px[0]) + 4
    os.makedirs(paths.BUILD_DIR, exist_ok=True)
    png(paths.build_path('route_icons.png'), bytes(img), W, H)
    print('patches/route_icons.h, %s' % paths.build_path('route_icons.png'))


if __name__ == '__main__':
    main()
