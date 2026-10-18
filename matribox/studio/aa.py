"""Anti-aliased shapes for Tk canvases.

Tk on Windows draws canvas items with GDI: polygons, ovals and thick outlines have no anti-aliasing (and a
`smooth` polygon used as a rounded rectangle grows little spikes at the corners when its outline is wider than
a pixel). Here shapes are rasterised into PhotoImages from signed distance fields: one evaluation per pixel
centre, coverage = clamp(0.5 - distance), so edges are smooth at any (fractional) size and stroke width.

    draw(canvas, x1, y1, x2, y2, shapes, bg)      shapes = [(kind, params in canvas coords, colour)], painted
                                                  in order over bg inside the box x1..x2, y1..y2
    kinds: 'rrect' (x1, y1, x2, y2, r), 'circle' (cx, cy, r), 'seg' (ax, ay, bx, by, half width),
           'poly' (x1, y1, x2, y2, ...) convex

Coverage is quantised to 16 levels; masks are cached per geometry (shapes relative to the image, positions
snapped to 1/4 px) and images per geometry + colours, so redrawing the same thing costs nothing.
"""
import math
import tkinter as tk
from collections import OrderedDict

LEVELS = 16
MAX_MASKS, MAX_IMAGES = 300, 900
_masks = OrderedDict()
_images = OrderedDict()


def _rgb(c):
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def _q(v):
    return round(v * 4) / 4


# ------------------------------------------------------------------------------------------------ distance fields
def _sdf(kind, a):
    if kind == 'rrect':
        x1, y1, x2, y2, r = a
        cx, cy, hw, hh = (x1 + x2) / 2, (y1 + y2) / 2, (x2 - x1) / 2, (y2 - y1) / 2
        r = max(0.0, min(r, hw, hh))
        def f(x, y):
            qx, qy = abs(x - cx) - hw + r, abs(y - cy) - hh + r
            return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r
        return f
    if kind == 'circle':
        cx, cy, r = a
        return lambda x, y: math.hypot(x - cx, y - cy) - r
    if kind == 'seg':
        ax, ay, bx, by, r = a
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy or 1e-9
        def f(x, y):
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / ll))
            return math.hypot(x - ax - t * dx, y - ay - t * dy) - r
        return f
    if kind == 'poly':
        pts = list(zip(a[0::2], a[1::2]))
        area = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))
        if area < 0:
            pts.reverse()
        edges = []
        for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
            nx, ny = y1 - y0, -(x1 - x0)                # outward normal of a counter-clockwise (y down) edge
            n = math.hypot(nx, ny) or 1e-9
            edges.append((nx / n, ny / n, x0, y0))
        return lambda x, y: max(nx * (x - x0) + ny * (y - y0) for nx, ny, x0, y0 in edges)
    raise ValueError(kind)


def _mask(w, h, geo):
    key = (w, h, geo)
    m = _masks.get(key)
    if m is not None:
        _masks.move_to_end(key)
        return m
    fs = [_sdf(k, a) for k, a in geo]
    # concentric rectangles / circles centred in the image: compute a quarter and mirror it
    sym = all((k == 'rrect' and abs(a[0] + a[2] - w) < 1e-6 and abs(a[1] + a[3] - h) < 1e-6) or
              (k == 'circle' and abs(2 * a[0] - w) < 1e-6 and abs(2 * a[1] - h) < 1e-6) for k, a in geo)
    W2, H2 = ((w + 1) // 2, (h + 1) // 2) if sym else (w, h)
    L = LEVELS
    rows = []
    for y in range(H2):
        py = y + 0.5
        row = []
        for x in range(W2):
            px = x + 0.5
            row.append(tuple(min(L, max(0, int((0.5 - f(px, py)) * L + 0.5))) for f in fs))
        if sym:
            row = row + row[:w - W2][::-1]
        rows.append(row)
    if sym:
        rows = rows + rows[:h - H2][::-1]
    _masks[key] = rows
    if len(_masks) > MAX_MASKS:
        _masks.popitem(last=False)
    return rows


def image(master, w, h, geo, colors, bg):
    """PhotoImage w x h: shapes geo (local coords) painted with colors over bg"""
    key = (w, h, geo, tuple(colors), bg)
    img = _images.get(key)
    if img is not None:
        _images.move_to_end(key)
        return img
    rows = _mask(w, h, geo)
    base = _rgb(bg)
    cl = [_rgb(c) for c in colors]
    pal = {}
    out = []
    for row in rows:
        s = []
        for t in row:
            v = pal.get(t)
            if v is None:
                r, g, b = base
                for cov, (lr, lg, lb) in zip(t, cl):
                    if cov:
                        a = cov / LEVELS
                        r, g, b = r + (lr - r) * a, g + (lg - g) * a, b + (lb - b) * a
                v = pal[t] = '#%02x%02x%02x' % (round(r), round(g), round(b))
            s.append(v)
        out.append('{' + ' '.join(s) + '}')
    img = tk.PhotoImage(master=master, width=w, height=h)
    if out:
        img.put(' '.join(out))
    _images[key] = img
    if len(_images) > MAX_IMAGES:
        _images.popitem(last=False)
    return img


def draw(c, x1, y1, x2, y2, shapes, bg=None, **kw):
    """paint shapes [(kind, params in canvas coords, colour)] inside the box; returns the canvas item"""
    bg = bg or c.cget('bg')
    ix, iy = math.floor(x1), math.floor(y1)
    w, h = max(1, math.ceil(x2) - ix), max(1, math.ceil(y2) - iy)
    geo = []
    colors = []
    for kind, a, col in shapes:
        if not col:
            continue
        loc = [v - (ix if k % 2 == 0 else iy) for k, v in enumerate(a)]
        if kind == 'circle':
            loc[2] = a[2]
        elif kind == 'seg':
            loc[4] = a[4]
        elif kind == 'rrect':
            loc[4] = a[4]
        geo.append((kind, tuple(_q(v) for v in loc)))
        colors.append(col)
    return c.create_image(ix, iy, image=image(c, w, h, tuple(geo), colors, bg), anchor='nw', **kw)


# ------------------------------------------------------------------------------------------------ helpers
def rrect(c, x1, y1, x2, y2, r, fill=None, outline=None, width=1.0, bg=None, **kw):
    """rounded rectangle: fill, optionally with an outline of the given width (drawn inside the box)"""
    shapes = []
    if outline and outline != fill:
        shapes.append(('rrect', (x1, y1, x2, y2, r), outline))
        if fill:
            shapes.append(('rrect', (x1 + width, y1 + width, x2 - width, y2 - width, max(0.0, r - width)), fill))
        else:
            shapes.append(('rrect', (x1 + width, y1 + width, x2 - width, y2 - width, max(0.0, r - width)),
                           bg or c.cget('bg')))
    elif fill:
        shapes.append(('rrect', (x1, y1, x2, y2, r), fill))
    return draw(c, x1, y1, x2, y2, shapes, bg, **kw)


def oval(c, cx, cy, r, fill=None, outline=None, width=1.0, bg=None, **kw):
    """circle centred on (cx, cy): fill, optionally with an outline drawn inside the radius"""
    shapes = []
    if outline and outline != fill:
        shapes.append(('circle', (cx, cy, r), outline))
        shapes.append(('circle', (cx, cy, r - width), fill or bg or c.cget('bg')))
    elif fill:
        shapes.append(('circle', (cx, cy, r), fill))
    return draw(c, cx - r, cy - r, cx + r, cy + r, shapes, bg, **kw)
