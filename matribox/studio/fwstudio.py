"""Matribox Studio: monta um firmware do Matribox II Pro (QME-200) escolhendo os efeitos.

Parte de um firmware base (por padrão o nosso, com cadeia paralela + GUI de duas fileiras), permite tirar
efeitos que já estão nele e acrescentar módulos das bibliotecas (pastas de manifestos que o usuário cadastra
em "Bibliotecas"), e gera o .bin (e, opcionalmente, o alg_data.json do editor de PC). A lógica fica em
fwstudio_core.py.

  python -m matribox studio
"""
import ctypes, functools, glob, json, math, os, subprocess, sys, threading, time, zlib
import tkinter as tk
from tkinter import ttk, filedialog, font as tkfont

from matribox.studio import fwstudio_core as C
from matribox.firmware import fwtool
from matribox.studio import aa

APPNAME = 'Matribox Studio'
APP_KINDS = {'patched': 'app com GUI paralela', 'original': 'app original', 'own': 'app próprio'}
APP_SHORT = {'patched': 'GUI paralela', 'original': 'app original', 'own': 'app próprio'}
SESSION = os.path.join(os.environ.get('APPDATA', C.ROOT), 'MatriboxStudio', 'session.json')
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')     # IBM Plex (OFL), optional


# ------------------------------------------------------------------------------------------------ colours
def mix(c1, c2, t):
    """c1 moved towards c2 by t (t = 1 - opacity of c1 over c2)"""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


@functools.lru_cache(maxsize=None)
def oklch(L, c, h):
    """OKLCH -> sRGB hex (the design's colour space)"""
    hr = math.radians(h)
    A, B = c * math.cos(hr), c * math.sin(hr)
    l_ = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3
    m_ = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3
    s_ = (L - 0.0894841775 * A - 1.2914855480 * B) ** 3
    rgb = (4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
           -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
           -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_)
    g = lambda x: 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
    return '#%02x%02x%02x' % tuple(round(255 * min(1.0, max(0.0, g(v)))) for v in rgb)


T = dict(
    bg='#2a2e33', panel='#2f3338', card='#2c3035', raised='#363a40', hover='#3a3f45', seg_on='#4a4f57',
    input='#272b30', thead='#33373d', tile='#33373d', rowline='#383c42', foot='#26292d', track='#3d4248',
    chain='#272b30', line='#40454b', line2='#484d54', cardline='#42474d', outline='#555a62', focus='#636970',
    wire='#555a62', text='#e7e9ec', bright='#f4f5f7', soft='#c6cbd1', soft2='#c9ced5', muted='#a9afb6',
    faint='#8f959c', dim='#6b7178', accent='#f28b2c', accent_hi='#ff9c42', accent_ink='#1a0f05',
    green='#4fc883', green_soft='#7fd6a0', red='#f07a6a', red_hi='#e5604f', amber='#f2c46b', sel='#42474e',
    base_bar='#7f8fb0', disabled='#4a4f56',
)
T.update(accent_lo=mix(T['accent'], T['panel'], .87), teal=T['green'], teal_lo=mix(T['green'], T['panel'], .88),
         green_lo=mix(T['green'], T['panel'], .88), green_line=mix(T['green'], T['panel'], .62),
         red_lo=mix(T['red'], T['panel'], .88), red_line=mix(T['red'], T['panel'], .62),
         amber_lo=mix(T['amber'], T['panel'], .9))

MENU_HUE = {0: 250, 1: 290, 2: 335, 3: 55, 4: 25, 5: 80, 6: 105, 7: 140, 8: 175, 9: 220, 10: 265, 11: 315}
FIXED_LABELS = {5: 'Cab', 6: 'IR', 11: 'Clone', 12: 'Loop', 13: 'Send', 14: 'Return', 15: 'Vol'}


def menu_color(m, L=.76, c=.13):
    """the menu's hue (design); Loop / Send / Return / Volume are neutral"""
    h = MENU_HUE.get(m)
    return oklch(L, .015, 250) if h is None else oklch(L, c, h)


ORIGIN_COLORS = {C.FACTORY: T['base_bar']}      # + the colours libraries declare (library.json), set on load
EXTRA_COLORS = [oklch(.74, .12, h) for h in (230, 350, 85, 160, 300, 30, 195, 265, 130, 55, 320, 105)]
ICO = dict(search='', add='', remove='', check='', lock='', save='',
           open='', build='', reset='', warn='', info='', folder='',
           chip='', close='', filter='', edit='', library='', all='',
           more='', chev='', refresh='')


def origin_color(o):
    """declared colour, else one picked from the name (stable across runs)"""
    return ORIGIN_COLORS.get(o) or EXTRA_COLORS[zlib.crc32(o.encode('utf-8')) % len(EXTRA_COLORS)]


def preset_label(i):
    """the editor's numbering (qmepreset.preset_label): 4 presets per bank, 01-A .. 60-D"""
    return '%02d-%s' % (i // 4 + 1, 'ABCD'[i % 4])


def load_fonts():
    """IBM Plex from studio/fonts for this process only (Windows); without it Segoe UI / Cascadia are used"""
    if sys.platform != 'win32':
        return
    for f in glob.glob(os.path.join(FONT_DIR, '*.ttf')):
        try:
            ctypes.windll.gdi32.AddFontResourceExW(f, 0x10, 0)      # FR_PRIVATE
        except Exception:
            pass


class Fonts:
    """sizes in pixels at 96 dpi, scaled (the design's CSS px)"""
    def __init__(self, root, S):
        fam = set(tkfont.families(root))
        pick = lambda *names: next((f for f in names if f in fam), None)
        sans = pick('IBM Plex Sans', 'Segoe UI Variable Text', 'Segoe UI', 'Helvetica') or 'TkDefaultFont'
        med = pick('IBM Plex Sans Medm', 'IBM Plex Sans Medium', 'Segoe UI Variable Text Semibold') or sans
        semi = pick('IBM Plex Sans SmBld', 'IBM Plex Sans SemiBold', 'Segoe UI Semibold') or sans
        mono = pick('IBM Plex Mono', 'Cascadia Mono', 'Consolas', 'Courier New') or 'TkFixedFont'
        mono_m = pick('IBM Plex Mono Medium', 'Cascadia Mono SemiBold') or mono
        self.icons = pick('Segoe Fluent Icons', 'Segoe MDL2 Assets')
        f = lambda family, px: tkfont.Font(root, family=family, size=-max(1, round(px * S)))
        self.body, self.medium, self.semi = f(sans, 13.5), f(med, 13.5), f(semi, 13.5)
        self.small, self.small_m = f(sans, 12.5), f(med, 12.5)
        self.xs = f(sans, 12)
        self.tiny = f(semi, 12)              # uppercase section labels
        self.th = f(semi, 11.5)              # table headings
        self.badge = f(semi, 10.5)
        self.h3, self.cap = f(semi, 15), f(semi, 14)
        self.h2, self.h_name, self.h_block, self.h1 = f(semi, 19), f(semi, 21), f(semi, 22), f(semi, 26)
        self.mono, self.mono_xs, self.mono_s = f(mono, 12.5), f(mono, 11.5), f(mono_m, 11)
        self.mono_m, self.mono_b = f(mono_m, 13), f(pick('IBM Plex Mono SemiBold') or mono_m, 15)
        self.icon = f(self.icons or sans, 13)
        self.icon_big = f(self.icons or sans, 17)
        self.icon_s = f(self.icons or sans, 10)


def rrect(c, x1, y1, x2, y2, r, fill=None, outline=None, width=1, bg=None, **kw):
    """anti-aliased rounded rectangle (aa.py); bg = colour under it (default: the canvas background)"""
    return aa.rrect(c, x1, y1, x2, y2, r, fill=fill, outline=outline, width=width, bg=bg, **kw)


def fit_text(font, text, width):
    """text cut with an ellipsis to fit width pixels"""
    if font.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        m = (lo + hi + 1) // 2
        if font.measure(text[:m] + '…') <= width:
            lo = m
        else:
            hi = m - 1
    return text[:lo].rstrip() + '…' if lo else ''


def draw_switch(c, S, on, color, bg, x=0, y=0):
    """the design's 34 x 20 toggle: track + white knob"""
    p = lambda v: round(v * S)
    w, h = p(34), p(20)
    track = (color or T['accent']) if on else T['outline']
    r = (h - 2 * p(2)) / 2
    kx = x + w - p(2) - r if on else x + p(2) + r
    aa.draw(c, x, y, x + w, y + h, [('rrect', (x, y, x + w, y + h, h / 2), track),
                                    ('circle', (kx, y + h / 2, r), T['bright'])], bg)


def lw_of(S):
    return 1 if S < 1.75 else 2


# ------------------------------------------------------------------------------------------------ widgets
class Btn(tk.Canvas):
    """flat button on a canvas (anti-aliased corners). kind: primary, ghost (outlined), subtle, danger, teal,
    seg / seg_on (inside a segmented control)"""
    STYLES = {   # fill, border, ink, hover fill, hover ink
        'primary': ('accent', None, 'accent_ink', 'accent_hi', 'accent_ink'),
        'ghost': (None, 'outline', 'soft2', 'hover', 'text'),
        'subtle': (None, None, 'muted', 'hover', 'text'),
        'danger': (None, 'red_line', 'red', 'red_lo', 'red'),
        'teal': (None, 'green_line', 'green_soft', 'green_lo', 'green_soft'),
        'seg': (None, None, 'muted', None, 'text'),
        'seg_on': ('seg_on', None, 'text', 'seg_on', 'text'),
    }

    def __init__(self, parent, app, text='', icon=None, command=None, kind='ghost', height=34, padx=14,
                 width=None, tip=None, font=None, radius=None):
        self.app, self.text, self.icon, self.command, self.kind = app, text, icon, command, kind
        self.font, self.radius = font, radius
        self.enabled, self.hover, self.down = True, False, False
        self.pbg = parent.cget('bg')
        S = app.S
        self.h = round(height * S)
        self.padx = round(padx * S)
        super().__init__(parent, height=self.h, highlightthickness=0, bd=0, bg=self.pbg, cursor='hand2')
        self.fixed_w = round(width * S) if width else None
        self._size()
        for ev, fn in (('<Enter>', self._enter), ('<Leave>', self._leave), ('<ButtonPress-1>', self._press),
                       ('<ButtonRelease-1>', self._release)):
            self.bind(ev, fn)
        if tip:
            Tooltip(self, app, tip)

    def _font(self):
        return self.font or (self.app.F.semi if self.kind == 'primary' else self.app.F.medium)

    def _size(self):
        f, fi = self._font(), self.app.F.icon
        w = self.padx * 2
        if self.text:
            w += f.measure(self.text)
        if self.icon and self.app.F.icons:
            w += fi.measure(self.icon) + (round(8 * self.app.S) if self.text else 0)
        self.w = self.fixed_w or w
        self.configure(width=self.w)
        self.draw()

    def config_btn(self, text=None, kind=None, icon=None, enabled=None):
        if text is not None: self.text = text
        if kind is not None: self.kind = kind
        if icon is not None: self.icon = icon
        if enabled is not None:
            self.enabled = enabled
            self.configure(cursor='hand2' if enabled else 'arrow')
        self._size()

    def draw(self):
        self.delete('all')
        fill, border, ink, hfill, hink = self.STYLES[self.kind]
        c, b, ink = T.get(fill), T.get(border), T[ink]
        if self.hover and self.enabled:
            c, ink = T.get(hfill) or c, T[hink]
        if self.down and self.enabled and c:
            c = mix(c, '#000000', .1)
        if not self.enabled:
            ink = T['faint'] if self.kind != 'subtle' else T['dim']
            b = T['line2'] if b else None
            if self.kind == 'primary':
                c = T['disabled']
            elif self.kind == 'seg_on':
                c = T['raised']
        r = round((self.radius if self.radius is not None else 6 if self.kind.startswith('seg') else 8) * self.app.S)
        if c or b:
            rrect(self, 0, 0, self.w, self.h, r, fill=c, outline=b, width=lw_of(self.app.S), bg=self.pbg)
        parts = []
        if self.icon and self.app.F.icons:
            parts.append((self.icon, self.app.F.icon))
        if self.text:
            parts.append((self.text, self._font()))
        gap = round(8 * self.app.S) if len(parts) == 2 else 0
        total = sum(f.measure(t) for t, f in parts) + gap
        x0 = (self.w - total) / 2
        for t, f in parts:
            self.create_text(x0, self.h / 2, text=t, font=f, fill=ink, anchor='w')
            x0 += f.measure(t) + gap

    def _enter(self, e): self.hover = True; self.draw()

    def _leave(self, e): self.hover = self.down = False; self.draw()

    def _press(self, e): self.down = True; self.draw()

    def _release(self, e):
        was = self.down
        self.down = False; self.draw()
        if was and self.enabled and self.command and 0 <= e.x < self.w and 0 <= e.y < self.h:
            self.command()


class Tooltip:
    def __init__(self, w, app, text):
        self.w, self.app, self.text, self.tip, self.job = w, app, text, None, None
        w.bind('<Enter>', self._sched, add='+')
        w.bind('<Leave>', self._hide, add='+')
        w.bind('<ButtonPress>', self._hide, add='+')

    def _sched(self, e):
        self.job = self.w.after(550, self._show)

    def _show(self):
        self.job = None
        if self.tip or not self.text:
            return
        t = self.text() if callable(self.text) else self.text
        if not t:
            return
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.configure(bg=T['line2'])
        tk.Label(self.tip, text=t, font=self.app.F.small, bg=T['raised'], fg=T['text'], justify='left',
                 padx=10, pady=6, wraplength=round(360 * self.app.S)).pack(padx=1, pady=1)
        x = self.w.winfo_pointerx() + 8 if callable(self.text) else self.w.winfo_rootx()
        y = self.w.winfo_rooty() + self.w.winfo_height() + 6
        self.tip.wm_geometry('+%d+%d' % (x, y))

    def _hide(self, e=None):
        if self.job:
            self.w.after_cancel(self.job); self.job = None
        if self.tip:
            self.tip.destroy(); self.tip = None


class Card(tk.Canvas):
    """rounded panel with a 1 px border; widgets go in .body, inset so they clear the corners.
    fit: 'both' (the body follows the card), 'x' (the card's height follows the body), 'none' (both sizes do)"""
    def __init__(self, parent, app, bg, border=None, radius=12, fit='x', top_only=False, under=None, inset=None):
        self.under = under or parent.cget('bg')
        super().__init__(parent, bg=self.under, highlightthickness=0, bd=0, width=1, height=1)
        self.app, self.fill, self.border, self.fit, self.top_only = app, bg, border, fit, top_only
        self.rp = round(radius * app.S)
        self.lw = lw_of(app.S)
        self.i = inset if inset is not None else max(self.lw, math.ceil(self.rp * .3))
        self.body = tk.Frame(self, bg=bg)
        self.win = self.create_window(self.i, self.i, window=self.body, anchor='nw')
        self.bind('<Configure>', self._draw)
        if fit != 'both':
            self.body.bind('<Configure>', self._on_body, add='+')
            self.after_idle(self._on_body)          # a 1 px canvas does not map the body: size it once

    def _on_body(self, e=None):
        i2 = 2 * self.i
        if not self.winfo_exists():
            return
        if self.fit == 'none':
            self.configure(width=self.body.winfo_reqwidth() + i2)
        self.configure(height=self.body.winfo_reqheight() + i2)

    def set_border(self, color=None, fill=None):
        self.border = color
        if fill and fill != self.fill:
            self.fill = fill
            self.body.configure(bg=fill)
        self._draw()

    def _draw(self, e=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2 or h < 2:
            return
        self.delete('bg')
        i = self.i
        if self.fit in ('x', 'both'):
            self.itemconfigure(self.win, width=max(1, w - 2 * i))
        if self.fit == 'both':
            self.itemconfigure(self.win, height=max(1, h - 2 * i))
        r = max(1, min(self.rp, w // 2, h // 2))
        f, b, lw = self.fill, self.border, self.lw
        bot = 0 if self.top_only else r
        rect = lambda x1, y1, x2, y2, c: self.create_rectangle(x1, y1, x2, y2, fill=c, outline='', tags='bg')
        rect(0, r, w, h - bot, f)
        rect(r, 0, w - r, h, f)
        if b:
            rect(r, 0, w - r, lw, b)
            if not self.top_only:
                rect(r, h - lw, w - r, h, b)
            rect(0, r, lw, h - bot, b)
            rect(w - lw, r, w, h - bot, b)
        R = 4 * r
        corners = [(0, 0, 0, 0), (w - r, 0, w - R, 0)]
        if not self.top_only:
            corners += [(0, h - r, 0, h - R), (w - r, h - r, w - R, h - R)]
        for x, y, rx, ry in corners:
            shapes = [('rrect', (rx, ry, rx + R, ry + R, r), b or f)]
            if b:
                shapes.append(('rrect', (rx + lw, ry + lw, rx + R - lw, ry + R - lw, max(0, r - lw)), f))
            aa.draw(self, x, y, x + r, y + r, shapes, self.under, tags='bg')
        self.tag_lower('bg')


def magnifier(parent, app, bg, size=14):
    p = lambda v: round(v * app.S)
    c = tk.Canvas(parent, width=p(size), height=p(size), bg=bg, highlightthickness=0)
    k, sw = size / 14, max(1.0, 1.5 * app.S) / 2
    aa.draw(c, 0, 0, p(size), p(size), [('circle', (p(6 * k), p(6 * k), p(4.5 * k) + sw), T['faint']),
                                        ('circle', (p(6 * k), p(6 * k), p(4.5 * k) - sw), bg),
                                        ('seg', (p(9.6 * k), p(9.6 * k), p(13 * k), p(13 * k), sw), T['faint'])], bg)
    return c


class Field(Card):
    """text input: rounded border (lighter while focused), optional magnifier, placeholder and a key hint"""
    def __init__(self, parent, app, var, placeholder='', search=True, height=36, bg=None, kbd=None, radius=8,
                 font=None, width=None, under=None):
        bg = bg or T['input']
        super().__init__(parent, app, bg, T['line2'], radius, fit='x', under=under)
        p = lambda v: round(v * app.S)
        F = app.F
        font = font or F.body
        b = self.body
        b.configure(height=p(height) - 2 * self.i)
        b.pack_propagate(False)
        if width:
            self.configure(width=p(width))
        x = p(12) - self.i
        if search:
            magnifier(b, app, bg, 14 if height >= 34 else 13).pack(side='left', padx=(x, 0))
            x = p(8)
        if kbd:
            tk.Label(b, text=kbd, font=F.mono_s, bg=bg, fg=T['faint'], padx=p(6), pady=0, highlightthickness=1,
                     highlightbackground=T['line2']).pack(side='right', padx=(p(8), p(12) - self.i))
        self.var = var
        self.entry = tk.Entry(b, textvariable=var, font=font, bg=bg, fg=T['text'], bd=0, relief='flat',
                              insertbackground=T['text'], highlightthickness=0, disabledbackground=bg,
                              selectbackground=T['seg_on'], selectforeground=T['text'])
        self.entry.pack(side='left', fill='x', expand=True, padx=(x, 0 if kbd else p(12) - self.i))
        self.ph = tk.Label(b, text=placeholder, font=font, bg=bg, fg=T['faint'], cursor='xterm', padx=0, pady=0)
        self.ph.bind('<Button-1>', lambda e: self.entry.focus_set())
        b.bind('<Button-1>', lambda e: self.entry.focus_set())
        var.trace_add('write', lambda *a: self._ph())
        self.entry.bind('<FocusIn>', lambda e: self.set_border(T['focus']), add='+')
        self.entry.bind('<FocusOut>', lambda e: self.set_border(T['line2']), add='+')
        self.after_idle(self._ph)

    def _ph(self):
        try:
            if self.var.get():
                self.ph.place_forget()
            else:
                self.ph.place(in_=self.entry, x=1, rely=.5, anchor='w')
        except tk.TclError:
            pass


class PopupMenu:
    """dark drop-down list: items [(text, callback[, enabled])] and None for a separator"""
    def __init__(self, app, items, x, y, width=None, current=None, above=None):
        self.app = app
        p = lambda v: round(v * app.S)
        F = app.F
        top = self.top = tk.Toplevel(app.root)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg=T['line2'])
        fr = tk.Frame(top, bg=T['raised'])
        fr.pack(padx=1, pady=1, fill='both')
        tk.Frame(fr, bg=T['raised'], height=p(4)).pack(fill='x')
        for it in items:
            if it is None:
                tk.Frame(fr, bg=T['line2'], height=1).pack(fill='x', pady=p(4))
                continue
            text, cmd = it[0], it[1]
            enabled = it[2] if len(it) > 2 else True
            row = tk.Frame(fr, bg=T['raised'], cursor='hand2' if enabled else '')
            row.pack(fill='x', padx=p(4))
            mark = tk.Label(row, text='✓' if current is not None and text == current else '', width=2,
                            font=F.small, bg=T['raised'], fg=T['accent'])
            mark.pack(side='left', padx=(p(4), 0))
            lb = tk.Label(row, text=text, font=F.body, bg=T['raised'], fg=T['text'] if enabled else T['dim'],
                          anchor='w', padx=p(4), pady=p(6))
            lb.pack(side='left', fill='x', expand=True, padx=(0, p(14)))
            ws = (row, mark, lb)
            if enabled:
                for w in ws:
                    w.bind('<Enter>', lambda e, ws=ws: [x.configure(bg=T['seg_on']) for x in ws])
                    w.bind('<Leave>', lambda e, ws=ws: [x.configure(bg=T['raised']) for x in ws])
                    w.bind('<ButtonRelease-1>', lambda e, cmd=cmd: self._pick(cmd))
        tk.Frame(fr, bg=T['raised'], height=p(4)).pack(fill='x')
        top.update_idletasks()
        w = max(width or 0, top.winfo_reqwidth())
        h = top.winfo_reqheight()
        sw, sh = app.root.winfo_screenwidth(), app.root.winfo_screenheight()
        if y + h > sh - p(40) and above is not None:
            y = above - h
        x = min(x, sw - w - p(8))
        top.geometry('%dx%d+%d+%d' % (w, h, x, max(0, y)))
        top.deiconify()
        top.lift()
        # inside a modal dialog the dialog's grab sends the focus straight back to it (FocusOut closed the list
        # before any click): the list takes the grab while open, and gives it back on close
        self.prev_grab = app.root.grab_current()
        if self.prev_grab is not None:
            top.grab_set()
            top.bind('<ButtonPress>', self._outside)       # with the grab, clicks elsewhere arrive here
        top.bind('<FocusOut>', lambda e: self.close() if e.widget is top else None)
        top.bind('<Escape>', lambda e: self.close())
        top.after(10, top.focus_force)

    def _outside(self, e):
        t = self.top
        if not (0 <= e.x_root - t.winfo_rootx() < t.winfo_width() and 0 <= e.y_root - t.winfo_rooty() < t.winfo_height()):
            self.close()

    def _pick(self, cmd):
        self.close()
        if cmd:
            cmd()

    def close(self):
        try:
            self.top.destroy()
        except tk.TclError:
            return
        prev, self.prev_grab = self.prev_grab, None
        if prev is not None:
            try:
                prev.grab_set()
                prev.focus_set()
            except tk.TclError:
                pass


class Dropdown(Card):
    """read-only select: the value and a chevron; a click opens the list"""
    def __init__(self, parent, app, values, value, command, width=180, height=36, bg=None, font=None, under=None):
        bg = bg or T['input']
        super().__init__(parent, app, bg, T['line2'], 8 if height >= 30 else 7, fit='x', under=under)
        p = lambda v: round(v * app.S)
        self.values, self.value, self.command = list(values), value, command
        b = self.body
        b.configure(height=p(height) - 2 * self.i, cursor='hand2')
        b.pack_propagate(False)
        if width:
            self.configure(width=p(width))
        F = app.F
        self.lbl = tk.Label(b, text=value, font=font or F.body, bg=bg, fg=T['text'], anchor='w', cursor='hand2')
        self.lbl.pack(side='left', fill='x', expand=True, padx=(p(10) - self.i, 0))
        self.chev = tk.Label(b, text=ICO['chev'] if F.icons else '▾', font=F.icon_s, bg=bg, fg=T['faint'],
                             cursor='hand2')
        self.chev.pack(side='right', padx=(p(6), p(10) - self.i))
        for w in (self, b, self.lbl, self.chev):
            w.bind('<ButtonRelease-1>', lambda e: self.open())
            w.bind('<Enter>', lambda e: self.set_border(T['focus']))
            w.bind('<Leave>', lambda e: self.set_border(T['line2']))

    def open(self):
        y = self.winfo_rooty()
        PopupMenu(self.app, [(v, lambda v=v: self._pick(v)) for v in self.values], self.winfo_rootx(),
                  y + self.winfo_height() + round(4 * self.app.S), width=self.winfo_width(), current=self.value,
                  above=y - round(4 * self.app.S))

    def _pick(self, v):
        self.set(v)
        self.command(v)

    def set(self, v):
        self.value = v
        self.lbl.configure(text=v)

    def set_values(self, values):
        self.values = list(values)


class SegBar(tk.Canvas):
    """segmented control: items [(key, label)] or [(key, label, count, count colour)]; fill: equal widths"""
    def __init__(self, parent, app, items, active, command, height=34, radius=8, inner=5, font=None, pad=12,
                 fill=False, margin=3):
        self.pbg = parent.cget('bg')
        super().__init__(parent, height=round(height * app.S), bg=self.pbg, highlightthickness=0, width=1,
                         cursor='hand2')
        self.app, self.items, self.active, self.command = app, items, active, command
        self.r, self.ir, self.font, self.pad, self.fill, self.m = radius, inner, font or app.F.medium, pad, fill, margin
        self.hover = None
        self.spans = []
        self.bind('<Configure>', lambda e: self.draw())
        self.bind('<Motion>', self._motion)
        self.bind('<Leave>', lambda e: self._set_hover(None))
        self.bind('<ButtonRelease-1>', self._click)
        self._measure()

    def _part(self, it):
        label = it[1]
        count = it[2] if len(it) > 2 else None
        return label, (None if count is None else str(count))

    def _measure(self):
        p = lambda v: round(v * self.app.S)
        F = self.app.F
        self.widths = []
        for it in self.items:
            label, count = self._part(it)
            w = self.font.measure(label) + 2 * p(self.pad)
            if count is not None:
                w += p(6) + F.mono_s.measure(count)
            self.widths.append(w)
        if not self.fill:
            self.configure(width=sum(self.widths) + 2 * p(self.m) + p(2) * (len(self.items) - 1))
        self.draw()

    def set(self, items=None, active=None):
        if items is not None:
            self.items = items
        if active is not None:
            self.active = active
        self._measure()

    def draw(self):
        self.delete('all')
        S = self.app.S
        p = lambda v: round(v * S)
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        F = self.app.F
        rrect(self, 0, 0, w, h, p(self.r), fill=T['raised'], outline=T['line2'], width=lw_of(S), bg=self.pbg)
        m, gap = p(self.m), p(2)
        n = len(self.items)
        widths = [(w - 2 * m - gap * (n - 1)) / n] * n if self.fill else self.widths
        x = m
        self.spans = []
        for it, sw in zip(self.items, widths):
            key = it[0]
            label, count = self._part(it)
            on = key == self.active
            if on:
                rrect(self, x, m, x + sw, h - m, p(self.ir), fill=T['seg_on'], bg=T['raised'])
            ink = T['text'] if on or self.hover == key else T['muted']
            tw = self.font.measure(label) + (p(6) + F.mono_s.measure(count) if count is not None else 0)
            tx = x + (sw - tw) / 2
            self.create_text(tx, h / 2, text=label, font=self.font, fill=ink, anchor='w')
            if count is not None:
                cc = it[3] if len(it) > 3 and it[3] else T['faint']
                self.create_text(tx + self.font.measure(label) + p(6), h / 2 + p(.5), text=count, font=F.mono_s,
                                 fill=cc, anchor='w')
            self.spans.append((x, x + sw, key))
            x += sw + gap

    def _key_at(self, x):
        for x1, x2, key in self.spans:
            if x1 <= x <= x2:
                return key
        return None

    def _set_hover(self, key):
        if key != self.hover:
            self.hover = key
            self.draw()

    def _motion(self, e):
        self._set_hover(self._key_at(e.x))

    def _click(self, e):
        key = self._key_at(e.x)
        if key is not None and 0 <= e.y <= self.winfo_height():
            self.command(key)


class Pill(tk.Canvas):
    """small tinted label (menu codes, states, origins)"""
    def __init__(self, parent, app, text, color, bg=None, icon=None, font=None, tint=.85, radius=5, height=22,
                 padx=8, ink=None):
        self.app = app
        f = font or app.F.badge
        S = app.S
        pad = round(padx * S)
        under = bg or parent.cget('bg')
        w = f.measure(text) + 2 * pad + (app.F.icon_s.measure(icon) + round(5 * S) if icon and app.F.icons else 0)
        h = round(height * S)
        super().__init__(parent, width=w, height=h, bg=under, highlightthickness=0, bd=0)
        lo = mix(color, under, tint)
        rrect(self, 0, 0, w, h, round(radius * S), fill=lo, bg=under)
        x = pad
        if icon and app.F.icons:
            self.create_text(x, h / 2, text=icon, font=app.F.icon_s, fill=ink or color, anchor='w')
            x += app.F.icon_s.measure(icon) + round(5 * S)
        self.create_text(x, h / 2, text=text, font=f, fill=ink or color, anchor='w')


class Switch(tk.Frame):
    """toggle switch + label bound to a BooleanVar"""
    def __init__(self, parent, app, text, var, command=None, color=None):
        bg = parent.cget('bg')
        super().__init__(parent, bg=bg, cursor='hand2')
        S = app.S
        self.var, self.command, self.S, self.color, self.bg = var, command, S, color, bg
        self.c = tk.Canvas(self, width=round(34 * S), height=round(20 * S), bg=bg, highlightthickness=0)
        self.c.pack(side='left')
        self.lbl = tk.Label(self, text=text, font=app.F.body, bg=bg, fg=T['text'])
        self.lbl.pack(side='left', padx=(round(10 * S), 0))
        for w in (self, self.c, self.lbl):
            w.bind('<Button-1>', self._click)
        self.draw()

    def _click(self, e):
        self.var.set(not self.var.get())
        self.draw()
        if self.command:
            self.command()

    def draw(self):
        self.c.delete('all')
        draw_switch(self.c, self.S, self.var.get(), self.color, self.bg)


class ToggleRow(Card):
    """the design's on/off row: label on the left, switch on the right, the whole row clickable"""
    def __init__(self, parent, app, text, on, command, color=None, enabled=True, height=40):
        super().__init__(parent, app, T['tile'], T['line2'], 8, fit='x')
        S = app.S
        p = lambda v: round(v * S)
        self.S, self.on, self.color, self.command, self.enabled = S, on, color, command, enabled
        b = self.body
        b.configure(height=p(height) - 2 * self.i, cursor='hand2' if enabled else '')
        b.pack_propagate(False)
        self.lbl = tk.Label(b, text=text, font=app.F.body, bg=T['tile'], fg=T['text'] if enabled else T['muted'])
        self.lbl.pack(side='left', padx=(p(12) - self.i, 0))
        self.sw = tk.Canvas(b, width=p(34), height=p(20), bg=T['tile'], highlightthickness=0)
        self.sw.pack(side='right', padx=(0, p(12) - self.i))
        for w in (self, b, self.lbl, self.sw):
            w.bind('<ButtonRelease-1>', lambda e: self.command() if self.enabled and self.command else None)
        self._sw()

    def _sw(self):
        self.sw.delete('all')
        draw_switch(self.sw, self.S, self.on, self.color if self.enabled else T['dim'], T['tile'])

    def set(self, text, on):
        self.on = on
        self.lbl.configure(text=text)
        self._sw()


class ChipFlow(tk.Canvas):
    """chips that wrap to the width: kind 'outline' (bordered) or 'fill' (mono, filled); tips: hover texts"""
    def __init__(self, parent, app, chips, kind='outline', tips=None):
        self.pbg = parent.cget('bg')
        super().__init__(parent, height=1, width=1, bg=self.pbg, highlightthickness=0)
        self.app, self.chips, self.kind, self.tips = app, chips, kind, tips
        self.boxes = []
        self.bind('<Configure>', lambda e: self._layout(e.width))
        if tips:
            Tooltip(self, app, self._tip)

    def _layout(self, w):
        self.delete('all')
        S = self.app.S
        p = lambda v: round(v * S)
        F = self.app.F
        f = F.small if self.kind == 'outline' else F.mono_xs
        pad, h, gap = (p(9), p(25), p(6)) if self.kind == 'outline' else (p(8), p(23), p(6))
        x = y = 0
        self.boxes = []
        for k, t in enumerate(self.chips):
            tw = f.measure(t) + 2 * pad
            if x and x + tw > w:
                x, y = 0, y + h + gap
            if self.kind == 'outline':
                rrect(self, x, y, x + tw, y + h, p(6), outline=T['line2'], width=lw_of(S), bg=self.pbg)
            else:
                rrect(self, x, y, x + tw, y + h, p(5), fill=T['hover'], bg=self.pbg)
            self.create_text(x + tw / 2, y + h / 2, text=t, font=f, fill=T['soft2'])
            self.boxes.append((x, y, x + tw, y + h, k))
            x += tw + gap
        nh = (y + h) if self.chips else 1
        if int(self.cget('height')) != nh:
            self.configure(height=nh)

    def _tip(self):
        x = self.winfo_pointerx() - self.winfo_rootx()
        y = self.winfo_pointery() - self.winfo_rooty()
        for x1, y1, x2, y2, k in self.boxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return self.tips[k]
        return None


class RowButton(tk.Canvas):
    """list row (origins): rounded hover / active background, dot, text and a right-aligned stat"""
    def __init__(self, parent, app, text, right='', dot=None, active=False, command=None, height=36):
        self.pbg = parent.cget('bg')
        super().__init__(parent, height=round(height * app.S), width=1, bg=self.pbg, highlightthickness=0,
                         cursor='hand2')
        self.app, self.text, self.right, self.dot, self.active, self.command = app, text, right, dot, active, command
        self.hover = False
        self.bind('<Configure>', lambda e: self.draw())
        self.bind('<Enter>', lambda e: self._hov(True))
        self.bind('<Leave>', lambda e: self._hov(False))
        self.bind('<ButtonRelease-1>', lambda e: command() if command else None)

    def _hov(self, v):
        self.hover = v
        self.draw()

    def draw(self):
        self.delete('all')
        S, F = self.app.S, self.app.F
        p = lambda v: round(v * S)
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        fill = T['line'] if self.active else T['hover'] if self.hover else None
        if fill:
            rrect(self, 0, 0, w, h, p(7), fill=fill, bg=self.pbg)
        bg = fill or self.pbg
        x = p(10)
        if self.dot:
            aa.oval(self, x + p(4), h / 2, p(4), fill=self.dot, bg=bg)
            x += p(22)
        rw = F.mono_xs.measure(self.right) + p(8) if self.right else 0
        self.create_text(x, h / 2, text=fit_text(F.body, self.text, w - x - p(10) - rw), font=F.body,
                         fill=T['text'], anchor='w')
        if self.right:
            self.create_text(w - p(10), h / 2, text=self.right, font=F.mono_xs, fill=T['muted'], anchor='e')


class ScrollFrame(tk.Frame):
    def __init__(self, parent, app, bg):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.sb = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview, style='Dark.Vertical.TScrollbar')
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor='nw')
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.inner.bind('<Configure>', lambda e: self._update())
        self.canvas.bind('<Configure>', lambda e: (self.canvas.itemconfigure(self.win, width=e.width), self._update()))

    def _update(self):
        ih, ch = self.inner.winfo_reqheight(), self.canvas.winfo_height()
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), ih))
        need = ih > ch + 1
        if need and not self.sb.winfo_manager():
            self.sb.pack(side='right', fill='y', before=self.canvas)
        elif not need and self.sb.winfo_manager():
            self.sb.pack_forget()
            self.canvas.yview_moveto(0)

    def _wheel(self, e):
        if self.inner.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-e.delta / 120) * 3, 'units')


# ------------------------------------------------------------------------------------------------ effects table
class FxTable(tk.Frame):
    """the effects list: a canvas that draws only the visible rows (check box, badges, origin dots), with the
    rounded top border of the design. Callbacks: on_select(), on_toggle(items), on_space(), on_set(value),
    on_sort(key)."""
    COLS = (('check', '', 44, 0), ('name', 'Efeito', 180, 2), ('origin', 'Origem', 130, 1.2), ('tab', 'Aba', 100, 1),
            ('menu', 'Menu', 64, 0), ('size', 'Tamanho', 84, 0), ('presets', 'Presets', 72, 0))
    RIGHT = ('size', 'presets')

    def __init__(self, parent, app, info, under):
        super().__init__(parent, bg=under)
        S = app.S
        self.p = lambda v: round(v * S)
        p = self.p
        self.app, self.info, self.under = app, info, under
        self.items, self.index, self.sel = [], {}, []
        self.anchor = self.focus = self.hover = None
        self.top = 0
        self.sort = (None, False)
        self.empty = ''
        self.on_select = self.on_toggle = self.on_space = self.on_set = self.on_sort = None
        self.rh, self.hh, self.r = p(40), p(36), p(12)
        self.lw = lw_of(S)
        self.head = tk.Canvas(self, height=self.hh, bg=under, highlightthickness=0)
        self.head.pack(fill='x')
        self.head.bind('<Configure>', lambda e: self._draw_head())
        self.head.bind('<ButtonRelease-1>', self._head_click)
        body = tk.Frame(self, bg=T['card'])
        body.pack(fill='both', expand=True)
        tk.Frame(body, bg=T['cardline'], width=self.lw).pack(side='left', fill='y')
        tk.Frame(body, bg=T['cardline'], width=self.lw).pack(side='right', fill='y')
        self.sb = ttk.Scrollbar(body, orient='vertical', command=self._yview, style='Card.Vertical.TScrollbar')
        self.cv = tk.Canvas(body, bg=T['card'], highlightthickness=0, takefocus=1)
        self.cv.pack(side='left', fill='both', expand=True)
        cv = self.cv
        cv.bind('<Configure>', lambda e: self.redraw())
        cv.bind('<Button-1>', self._click)
        cv.bind('<Double-Button-1>', self._double)
        cv.bind('<Motion>', self._motion)
        cv.bind('<Leave>', lambda e: self._set_hover(None))
        cv.bind('<MouseWheel>', lambda e: self.scroll(-3 if e.delta > 0 else 3))
        for k in ('Up', 'Down', 'Prior', 'Next', 'Home', 'End'):
            cv.bind('<%s>' % k, lambda e, k=k: self.key(k, e.state & 1))
            cv.bind('<Shift-%s>' % k, lambda e, k=k: self.key(k, True))
        cv.bind('<space>', lambda e: self.key('space'))
        cv.bind('<Delete>', lambda e: self.key('Delete'))
        cv.bind('<Insert>', lambda e: self.key('Insert'))
        cv.bind('<Escape>', lambda e: self.key('Escape'))
        cv.bind('<Control-a>', lambda e: self.key('all'))

    # ------------------------------------------------------------------ data
    def set_items(self, items):
        self.items = items
        self.index = {it.key: k for k, it in enumerate(items)}
        self.sel = [k for k in self.sel if k in self.index]
        self.redraw()

    def selection(self):
        return [k for k in self.sel if k in self.index]

    def set_selection(self, keys, notify=True):
        self.sel = [k for k in keys if k in self.index]
        self.focus = self.sel[-1] if self.sel else None
        self.anchor = self.focus
        self.redraw()
        if notify and self.on_select:
            self.on_select()

    def see(self, key):
        k = self.index.get(key)
        if k is None:
            return
        h = self.cv.winfo_height()
        y = k * self.rh
        if y < self.top:
            self.top = y
        elif y + self.rh > self.top + h:
            self.top = y + self.rh - h

    # ------------------------------------------------------------------ geometry
    def _cols(self, w):
        p = self.p
        fixed = sum(p(c[2]) for c in self.COLS if not c[3]) + p(12)
        flex = [c for c in self.COLS if c[3]]
        avail = w - fixed
        tw = sum(c[3] for c in flex)
        x, out = 0, {}
        for key, _t, mn, wt in self.COLS:
            cw = max(p(mn), avail * wt / tw) if wt else p(mn)
            out[key] = (x, x + cw)
            x += cw
        return out

    def _yview(self, *a):
        total, h = len(self.items) * self.rh, self.cv.winfo_height()
        if a[0] == 'moveto':
            self.top = float(a[1]) * total
        elif a[0] == 'scroll':
            self.top += int(a[1]) * (self.rh if a[2] == 'units' else max(self.rh, h - self.rh))
        self.redraw()

    def scroll(self, rows):
        self.top += rows * self.rh
        self.redraw()

    # ------------------------------------------------------------------ drawing
    def _draw_head(self):
        c, p, F = self.head, self.p, self.app.F
        c.delete('all')
        w, h = c.winfo_width(), self.hh
        if w < 20:
            return
        r, lw, f, b = self.r, self.lw, T['thead'], T['cardline']
        c.create_rectangle(r, 0, w - r, h, fill=f, outline='')
        c.create_rectangle(0, r, w, h, fill=f, outline='')
        c.create_rectangle(r, 0, w - r, lw, fill=b, outline='')
        c.create_rectangle(0, r, lw, h, fill=b, outline='')
        c.create_rectangle(w - lw, r, w, h, fill=b, outline='')
        c.create_rectangle(0, h - lw, w, h, fill=b, outline='')
        R = 4 * r
        for x, rx in ((0, 0), (w - r, w - R)):
            aa.draw(c, x, 0, x + r, r, [('rrect', (rx, 0, rx + R, R, r), b),
                                        ('rrect', (rx + lw, lw, rx + R - lw, R - lw, r - lw), f)], self.under)
        cols = self._cols(self.cv.winfo_width() or w)
        key, rev = self.sort
        self.head_hits = []
        for k, title, _mn, _wt in self.COLS:
            if not title:
                continue
            x1, x2 = cols[k]
            x1 += self.lw
            x2 += self.lw
            t = title.upper() + ((' ↓' if rev else ' ↑') if key == k else '')
            if k in self.RIGHT:
                c.create_text(x2, h / 2, text=t, font=F.th, fill=T['muted'], anchor='e')
            else:
                c.create_text(x1, h / 2, text=t, font=F.th, fill=T['muted'], anchor='w')
            self.head_hits.append((x1, x2, k))

    def _head_click(self, e):
        for x1, x2, k in getattr(self, 'head_hits', []):
            if x1 <= e.x <= x2 and self.on_sort:
                self.on_sort(k)
                return

    def redraw(self):
        cv, p, F = self.cv, self.p, self.app.F
        cv.delete('all')
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 20:
            return
        rh = self.rh
        total = len(self.items) * rh
        self.top = max(0, min(self.top, max(0, total - h)))
        first = int(self.top // rh)
        last = min(len(self.items), int((self.top + h) // rh) + 1)
        cols = self._cols(w)
        sel = set(self.sel)
        for k in range(first, last):
            self._row(k, k * rh - self.top, cols, w, self.items[k].key in sel)
        if not self.items and self.empty:
            cv.create_text(w / 2, p(60), text=self.empty, font=F.body, fill=T['faint'])
        if total > h:
            self.sb.set(self.top / total, (self.top + h) / total)
            if not self.sb.winfo_manager():
                self.sb.pack(side='right', fill='y', before=cv)
        elif self.sb.winfo_manager():
            self.sb.pack_forget()
        self._draw_head()

    def _row(self, k, y, cols, w, selected):
        cv, p, F = self.cv, self.p, self.app.F
        it = self.items[k]
        d = self.info(it)
        rh = self.rh
        bg = T['sel'] if selected else T['rowline'] if k == self.hover else T['card']
        if bg != T['card']:
            cv.create_rectangle(0, y, w, y + rh, fill=bg, outline='')
        cv.create_rectangle(0, y + rh - self.lw, w, y + rh, fill=T['rowline'], outline='')
        if selected:
            cv.create_rectangle(0, y, p(2), y + rh, fill=T['accent'], outline='')
        cy = y + rh / 2
        x1, x2 = cols['check']
        self._check(round((x1 + x2) / 2), round(cy), d['state'], bg)
        # name + badge
        x1, x2 = cols['name']
        room = x2 - x1 - p(10)
        badge = d.get('badge')
        bw = F.badge.measure(badge[0]) + p(12) + p(8) if badge else 0
        name = fit_text(F.body, d['name'], room - bw)
        cv.create_text(x1, cy, text=name, font=F.body, fill=d['ncolor'], anchor='w')
        if badge:
            bx = x1 + F.body.measure(name) + p(8)
            text, fg = badge
            tw = F.badge.measure(text) + p(12)
            rrect(cv, bx, cy - p(9), bx + tw, cy + p(9), p(4), fill=mix(fg, bg, .86), bg=bg)
            cv.create_text(bx + tw / 2, cy, text=text, font=F.badge, fill=fg)
        # origin
        x1, x2 = cols['origin']
        aa.oval(cv, x1 + p(3.5), cy, p(3.5), fill=d['ocolor'], bg=bg)
        cv.create_text(x1 + p(14), cy, text=fit_text(F.body, d['origin'], x2 - x1 - p(22)), font=F.body,
                       fill=T['soft'], anchor='w')
        x1, x2 = cols['tab']
        cv.create_text(x1, cy, text=fit_text(F.body, d['tab'], x2 - x1 - p(8)), font=F.body, fill=T['muted'],
                       anchor='w')
        x1, x2 = cols['menu']
        cv.create_text(x1, cy, text=d['menu'], font=F.mono_s, fill=d['mcolor'], anchor='w')
        cv.create_text(cols['size'][1], cy, text=d['size'], font=F.mono, fill=T['soft'], anchor='e')
        cv.create_text(cols['presets'][1], cy, text=d['presets'], font=F.mono, fill=d['pcolor'], anchor='e')

    def _check(self, cx, cy, state, bg):
        p = self.p
        s = p(18)
        x1, y1 = cx - s // 2, cy - s // 2
        x2, y2 = x1 + s, y1 + s
        r = p(5)
        lw = self.lw
        box = (x1, y1, x2, y2, r)
        inner = (x1 + lw, y1 + lw, x2 - lw, y2 - lw, r - lw)
        mark = lambda col: [('seg', (x1 + s * .27, y1 + s * .53, x1 + s * .43, y1 + s * .69, max(.9, s / 13)), col),
                            ('seg', (x1 + s * .43, y1 + s * .69, x1 + s * .74, y1 + s * .34, max(.9, s / 13)), col)]
        if state in ('on', 'new'):
            shapes = [('rrect', box, T['accent'])] + mark(T['accent_ink'])
        elif state == 'locked':
            shapes = [('rrect', box, T['line2'])] + mark(T['faint'])
        elif state == 'bad':
            shapes = [('rrect', box, T['line2']), ('rrect', inner, bg)]
        else:
            shapes = [('rrect', box, T['focus']), ('rrect', inner, bg)]
        aa.draw(self.cv, x1, y1, x2, y2, shapes, bg)

    # ------------------------------------------------------------------ input
    def _row_at(self, y):
        k = int((y + self.top) // self.rh)
        return k if 0 <= k < len(self.items) else None

    def _set_hover(self, k):
        if k != self.hover:
            self.hover = k
            self.redraw()

    def _motion(self, e):
        self._set_hover(self._row_at(e.y))

    def _click(self, e):
        self.cv.focus_set()
        k = self._row_at(e.y)
        if k is None:
            return
        it = self.items[k]
        if e.x < self._cols(self.cv.winfo_width())['check'][1]:
            if self.on_toggle:
                self.on_toggle([it])
            return
        key = it.key
        if e.state & 1 and self.anchor in self.index:
            a, b = sorted((self.index[self.anchor], k))
            self.sel = [x.key for x in self.items[a:b + 1]]
        elif e.state & 4:
            if key in self.sel:
                self.sel.remove(key)
            else:
                self.sel.append(key)
            self.anchor = key
        else:
            self.sel = [key]
            self.anchor = key
        self.focus = key
        self.redraw()
        if self.on_select:
            self.on_select()

    def _double(self, e):
        k = self._row_at(e.y)
        if k is not None and e.x >= self._cols(self.cv.winfo_width())['check'][1] and self.on_toggle:
            self.on_toggle([self.items[k]])
        return 'break'

    def key(self, k, shift=False):
        if k == 'space':
            self.on_space and self.on_space()
            return 'break'
        if k in ('Delete', 'Insert'):
            self.on_set and self.on_set(k == 'Insert')
            return 'break'
        if k == 'Escape':
            self.set_selection([])
            return 'break'
        if k == 'all':
            self.set_selection([it.key for it in self.items])
            return 'break'
        if not self.items:
            return 'break'
        n = len(self.items)
        page = max(1, self.cv.winfo_height() // self.rh - 1)
        cur = self.index.get(self.focus, -1)
        j = {'Up': cur - 1 if cur >= 0 else 0, 'Down': cur + 1, 'Prior': cur - page, 'Next': cur + page,
             'Home': 0, 'End': n - 1}[k]
        j = max(0, min(n - 1, j))
        key = self.items[j].key
        if shift and self.anchor in self.index:
            a, b = sorted((self.index[self.anchor], j))
            self.sel = [x.key for x in self.items[a:b + 1]]
        else:
            self.sel = [key]
            self.anchor = key
        self.focus = key
        self.see(key)
        self.redraw()
        if self.on_select:
            self.on_select()
        return 'break'


# ------------------------------------------------------------------------------------------------ app
class App:
    VIEWS = (('all', 'Todos'), ('in', 'No firmware'), ('factory', 'Firmware base'), ('library', 'Biblioteca'),
             ('changes', 'Alterações'))

    def __init__(self, root):
        self.root = root
        self.S = max(1.0, root.winfo_fpixels('1i') / 96)
        self.F = Fonts(root, self.S)
        self.project = None
        self.menu = None              # None = todos (fora os fixos), 'fixed', ou o número do menu
        self.filter = 'all'
        self.origin = 'Todas as origens'
        self.sort = (None, False)
        self.current = None           # item shown in the details panel
        self.side_hover = None
        self.session = self._load_session()
        if 'libraries' not in self.session:          # older sessions: the folders their profile used
            prof = self.session.get('profile') or {}
            self.session['libraries'] = [list(s) for s in prof.get('sources', [])] or self._example_libraries()
        root.title(APPNAME)
        root.configure(bg=T['bg'])
        w, h = round(1440 * self.S), round(880 * self.S)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(w, sw - 40), min(h, sh - round(120 * self.S))
        root.geometry('%dx%d+%d+%d' % (w, h, (sw - w) // 2, max(0, (sh - h) // 2 - round(30 * self.S))))
        root.minsize(round(1180 * self.S), round(660 * self.S))
        self._style()
        self._layout()
        self._bindings()
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.after(30, self._startup)

    def p(self, v):
        return round(v * self.S)

    # ------------------------------------------------------------------ setup
    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use('clam')
        S, F = self.S, self.F
        s.configure('Fx.Treeview', background=T['card'], fieldbackground=T['card'], foreground=T['text'],
                    rowheight=round(36 * S), borderwidth=0, relief='flat', font=F.body, indent=0)
        s.map('Fx.Treeview', background=[('selected', T['sel'])], foreground=[('selected', T['text'])])
        s.layout('Fx.Treeview', [('Fx.Treeview.treearea', {'sticky': 'nswe'})])
        for name in ('Treeview.Item', 'Fx.Treeview.Item'):
            s.layout(name, [('Treeitem.padding', {'sticky': 'nswe', 'children': [
                ('Treeitem.image', {'side': 'left', 'sticky': ''}),
                ('Treeitem.text', {'side': 'left', 'sticky': ''})]})])
        s.configure('Fx.Treeview.Heading', background=T['thead'], foreground=T['muted'], relief='flat',
                    font=F.th, borderwidth=0, padding=(round(10 * S), round(9 * S)))
        s.map('Fx.Treeview.Heading', background=[('active', T['hover'])], foreground=[('active', T['text'])])
        s.layout('Fx.Treeview.Heading', [('Treeheading.cell', {'sticky': 'nswe'}), ('Treeheading.padding', {
            'sticky': 'nswe', 'children': [('Treeheading.text', {'sticky': 'w'})]})])
        s.configure('Par.Treeview', background=T['panel'], fieldbackground=T['panel'], foreground=T['soft'],
                    rowheight=round(26 * S), borderwidth=0, font=F.small)
        s.map('Par.Treeview', background=[('selected', T['panel'])], foreground=[('selected', T['soft'])])
        s.layout('Par.Treeview', [('Par.Treeview.treearea', {'sticky': 'nswe'})])
        for pre, trough in (('Dark', T['panel']), ('Card', T['card']), ('Chain', T['chain'])):
            for o in ('Vertical', 'Horizontal'):
                st = '%s.%s.TScrollbar' % (pre, o)
                s.configure(st, troughcolor=trough, background=T['seg_on'], bordercolor=trough,
                            lightcolor=T['seg_on'], darkcolor=T['seg_on'], arrowcolor=T['muted'], gripcount=0,
                            arrowsize=round(10 * S))
                s.map(st, background=[('active', T['focus'])], lightcolor=[('active', T['focus'])],
                      darkcolor=[('active', T['focus'])])
                s.layout(st, [('%s.Scrollbar.trough' % o, {'sticky': 'ns' if o == 'Vertical' else 'ew',
                                                           'children': [('%s.Scrollbar.thumb' % o,
                                                                         {'expand': '1', 'sticky': 'nswe'})]})])
        s.configure('Dark.TCombobox', fieldbackground=T['input'], background=T['input'], foreground=T['text'],
                    arrowcolor=T['faint'], bordercolor=T['line2'], lightcolor=T['input'], darkcolor=T['input'],
                    selectbackground=T['input'], selectforeground=T['text'], padding=(round(8 * S), round(5 * S)),
                    insertcolor=T['text'])
        s.map('Dark.TCombobox', fieldbackground=[('readonly', T['input']), ('focus', T['input'])],
              foreground=[('readonly', T['text'])], bordercolor=[('focus', T['focus'])],
              background=[('active', T['raised'])], arrowcolor=[('active', T['text'])])
        self.root.option_add('*TCombobox*Listbox.background', T['raised'])
        self.root.option_add('*TCombobox*Listbox.foreground', T['text'])
        self.root.option_add('*TCombobox*Listbox.selectBackground', T['seg_on'])
        self.root.option_add('*TCombobox*Listbox.selectForeground', T['text'])
        self.root.option_add('*TCombobox*Listbox.font', F.body)
        self.root.option_add('*TCombobox*Listbox.borderWidth', 0)
        s.configure('Dark.TCheckbutton', background=T['panel'], foreground=T['text'], font=F.body,
                    indicatorbackground=T['raised'], indicatorforeground=T['accent'], bordercolor=T['line2'],
                    focuscolor=T['panel'], upperbordercolor=T['line2'], lowerbordercolor=T['line2'])
        s.map('Dark.TCheckbutton', background=[('active', T['panel'])],
              indicatorbackground=[('selected', T['raised']), ('active', T['raised'])])

    def _layout(self):
        S, F, p = self.S, self.F, self.p
        root = self.root
        # header -------------------------------------------------------
        hdr = tk.Frame(root, bg=T['panel'], height=p(60))
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        tk.Frame(root, bg=T['line'], height=1).pack(fill='x')
        hdr.columnconfigure(0, weight=1, uniform='side')
        hdr.columnconfigure(2, weight=1, uniform='side')
        hdr.rowconfigure(0, weight=1)
        brand = tk.Frame(hdr, bg=T['panel'])
        brand.grid(row=0, column=0, sticky='w', padx=(p(20), 0))
        logo = tk.Canvas(brand, width=p(30), height=p(30), bg=T['panel'], highlightthickness=0)
        logo.pack(side='left')
        rrect(logo, 0, 0, p(30), p(30), p(8), fill=T['accent'], bg=T['panel'])
        logo.create_text(p(15), p(15), text='M', font=F.mono_b, fill=T['accent_ink'])
        tk.Label(brand, text=APPNAME, font=F.h3, bg=T['panel'], fg=T['text']).pack(side='left', padx=(p(12), 0))
        self.nav = SegBar(hdr, self, [('fw', 'Firmware'), ('pedal', 'Pedal e presets'), ('nam', 'Capturas NAM'),
                                      ('ir', 'IRs'), ('global', 'Global'), ('tools', 'Ferramentas')], 'fw',
                          self.set_mode, height=38, radius=9,
                          inner=6, pad=16)
        self.nav.grid(row=0, column=1)
        self.hdr_right = tk.Frame(hdr, bg=T['panel'])
        self.hdr_right.grid(row=0, column=2, sticky='e', padx=(0, p(20)))
        self.actions = {}
        fa = self.actions['fw'] = tk.Frame(self.hdr_right, bg=T['panel'])
        self.btn_build = Btn(fa, self, 'Gerar firmware', None, self.open_build, 'primary', height=34, padx=16,
                             tip='Monta o .bin com a seleção atual (Ctrl+G)')
        self.btn_build.pack(side='right')
        Btn(fa, self, 'Salvar perfil', None, self.save_profile, 'ghost', height=34, padx=14,
            tip='Salva a seleção num arquivo .json (Ctrl+S)').pack(side='right', padx=(0, p(8)))
        Btn(fa, self, 'Atualizar pedal', None, self.open_update, 'ghost', height=34, padx=14,
            tip='Grava um firmware .bin no pedal pela USB, como o editor original').pack(side='right', padx=(0, p(8)))
        self.btn_more = Btn(fa, self, '' if F.icons else '⋯', ICO['more'], self.more_menu, 'subtle', height=34,
                            padx=9, tip='Abrir perfil, trocar a base, bibliotecas, restaurar')
        self.btn_more.pack(side='right', padx=(0, p(6)))
        # footer (all modes) -------------------------------------------
        foot = tk.Frame(root, bg=T['foot'], height=p(30))
        foot.pack(side='bottom', fill='x')
        foot.pack_propagate(False)
        tk.Frame(root, bg=T['line'], height=1).pack(side='bottom', fill='x')
        self.f_base = tk.Label(foot, text='', font=F.mono_xs, bg=T['foot'], fg=T['muted'], cursor='hand2')
        self.f_base.pack(side='left', padx=(p(20), 0))
        self.f_base.bind('<Button-1>', lambda e: self.choose_base())
        self.f_base.bind('<Enter>', lambda e: self.f_base.configure(fg=T['text']))
        self.f_base.bind('<Leave>', lambda e: self.f_base.configure(fg=T['muted']))
        Tooltip(self.f_base, self, 'Trocar o firmware base')
        self.f_info = []
        for _ in range(3):
            lb = tk.Label(foot, text='', font=F.xs, bg=T['foot'], fg=T['faint'])
            lb.pack(side='left', padx=(p(16), 0))
            self.f_info.append(lb)
        self.f_dev = tk.Label(foot, text='', font=F.xs, bg=T['foot'], fg=T['faint'])
        self.f_dev.pack(side='right', padx=(0, p(20)))

        # firmware view ------------------------------------------------
        self.fw_view = tk.Frame(root, bg=T['bg'])
        self.fw_view.pack(fill='both', expand=True)
        self.pedal_view = self.nam_view = self.ir_view = self.global_view = self.tools_view = None
        self.mode = 'fw'
        self.actions['fw'].pack(side='right')
        self._capacity(self.fw_view)
        body = tk.Frame(self.fw_view, bg=T['bg'])
        body.pack(fill='both', expand=True)
        side = tk.Frame(body, bg=T['panel'], width=p(236))
        side.pack(side='left', fill='y')
        side.pack_propagate(False)
        tk.Frame(body, bg=T['line'], width=1).pack(side='left', fill='y')
        self.side_cv = tk.Canvas(side, bg=T['panel'], highlightthickness=0, cursor='hand2')
        self.side_cv.pack(fill='both', expand=True, padx=p(10), pady=p(16))
        self.side_cv.bind('<Configure>', lambda e: self.refresh_sidebar())
        self.side_cv.bind('<Motion>', self._side_motion)
        self.side_cv.bind('<Leave>', lambda e: self._side_set_hover(None))
        self.side_cv.bind('<ButtonRelease-1>', self._side_click)
        self.det_outer = tk.Frame(body, bg=T['panel'], width=p(340))
        self.det_outer.pack(side='right', fill='y')
        self.det_outer.pack_propagate(False)
        tk.Frame(body, bg=T['line'], width=1).pack(side='right', fill='y')
        self.det = ScrollFrame(self.det_outer, self, T['panel'])
        self.det.pack(fill='both', expand=True)
        center = tk.Frame(body, bg=T['bg'])
        center.pack(side='left', fill='both', expand=True, padx=p(20), pady=(p(16), 0))
        tb = tk.Frame(center, bg=T['bg'])
        tb.pack(fill='x')
        self.q = tk.StringVar()
        self.search_f = Field(tb, self, self.q, 'Buscar por nome, aba, origem ou id', kbd='Ctrl F')
        self.search_f.pack(side='left', fill='x', expand=True)
        self.search = self.search_f.entry
        self.q.trace_add('write', lambda *a: self.refresh_table())
        self.origin_dd = Dropdown(tb, self, ['Todas as origens'], self.origin, self._set_origin, width=190)
        self.origin_dd.pack(side='left', padx=(p(10), 0))
        seg = tk.Frame(center, bg=T['bg'])
        seg.pack(fill='x', pady=p(12))
        self.btn_none = Btn(seg, self, 'Desmarcar visíveis', None, lambda: self.bulk(False), 'ghost', height=30,
                            padx=12, font=F.small, radius=7, tip='Tira do firmware todos os efeitos da lista')
        self.btn_none.pack(side='right')
        self.btn_all = Btn(seg, self, 'Marcar visíveis', None, lambda: self.bulk(True), 'ghost', height=30,
                           padx=12, font=F.small, radius=7, tip='Inclui no firmware todos os efeitos da lista')
        self.btn_all.pack(side='right', padx=(0, p(6)))
        self.views = SegBar(seg, self, [(k, t, 0) for k, t in self.VIEWS], self.filter, self._set_filter,
                            height=34, radius=8, inner=5, pad=11, font=F.medium)
        self.views.pack(side='left')
        self.count_lbl = tk.Label(seg, text='', font=F.small, bg=T['bg'], fg=T['faint'], anchor='e')
        self.count_lbl.pack(side='right', fill='x', expand=True, padx=(p(12), p(12)))
        self.table = FxTable(center, self, self._row_info, T['bg'])
        self.table.pack(fill='both', expand=True)
        self.table.on_select = self._on_select
        self.table.on_toggle = self.toggle
        self.table.on_space = self.toggle_selected
        self.table.on_set = self.set_selected
        self.table.on_sort = self._sort
        self.table.empty = 'Nenhum efeito com esses filtros.'
        self.toast = None

    def _capacity(self, parent):
        """the bottom section of the firmware view: space used in the DSP library"""
        F, p = self.F, self.p
        bg = T['panel']
        cap = tk.Frame(parent, bg=bg)
        cap.pack(side='bottom', fill='x')
        tk.Frame(parent, bg=T['line'], height=1).pack(side='bottom', fill='x')
        fin = tk.Frame(cap, bg=bg)
        fin.pack(fill='x', padx=p(20), pady=(p(14), p(16)))
        top = tk.Frame(fin, bg=bg)
        top.pack(fill='x')
        tk.Label(top, text='BIBLIOTECA DSP', font=F.tiny, bg=bg, fg=T['muted']).pack(side='left')
        self.cap_lbl = tk.Label(top, text='', font=F.cap, bg=bg, fg=T['text'])
        self.cap_lbl.pack(side='left', padx=(p(14), 0))
        self.pct_lbl = tk.Label(top, text='', font=F.mono, bg=bg, fg=T['muted'])
        self.pct_lbl.pack(side='left', padx=(p(14), 0))
        st = tk.Frame(top, bg=bg, cursor='hand2')
        st.pack(side='right')
        self.st_dot = tk.Canvas(st, width=p(7), height=p(7), bg=bg, highlightthickness=0)
        self.st_dot.pack(side='left')
        self.st_lbl = tk.Label(st, text='', font=F.medium, bg=bg, fg=T['green'])
        self.st_lbl.pack(side='left', padx=(p(7), 0))
        for w in (st, self.st_dot, self.st_lbl):
            w.bind('<Button-1>', lambda e: self.show_issues())
        Tooltip(st, self, 'Ver a verificação da seleção')
        self.aff_lbl = tk.Label(top, text='', font=F.small, bg=bg, fg=T['amber'])
        self.aff_lbl.pack(side='right', padx=(0, p(14)))
        self.delta_lbl = tk.Label(top, text='', font=F.mono_xs, bg=bg, fg=T['muted'])
        self.delta_lbl.pack(side='right', padx=(0, p(14)))
        self.bar = tk.Canvas(fin, height=p(10), bg=bg, highlightthickness=0)
        self.bar.pack(fill='x', pady=p(10))
        self.bar.bind('<Configure>', lambda e: self.draw_bar())
        leg = tk.Frame(fin, bg=bg)
        leg.pack(fill='x')
        self.legend = []
        for color, txt in ((T['base_bar'], 'Base mantida'), (T['green'], 'Adicionados'), (None, 'Livre')):
            sw = tk.Canvas(leg, width=p(8), height=p(8), bg=bg, highlightthickness=0)
            if color:
                rrect(sw, 0, 0, p(8), p(8), p(2), fill=color)
            else:
                rrect(sw, 0, 0, p(8), p(8), p(2), outline=T['focus'], width=lw_of(self.S))
            sw.pack(side='left')
            tk.Label(leg, text=txt, font=F.xs, bg=bg, fg=T['muted']).pack(side='left', padx=(p(6), p(6)))
            v = tk.Label(leg, text='', font=F.mono_xs, bg=bg, fg=T['soft2'])
            v.pack(side='left', padx=(0, p(14)))
            self.legend.append(v)
        self.index_lbl = tk.Label(leg, text='', font=F.mono_xs, bg=bg, fg=T['soft2'])
        self.index_lbl.pack(side='right')
        tk.Label(leg, text='Índice', font=F.xs, bg=bg, fg=T['muted']).pack(side='right', padx=(0, p(6)))

    def actions_frame(self, mode):
        """the header's right-hand slot for a view's actions (shown while that view is)"""
        f = self.actions.get(mode)
        if f is None:
            f = self.actions[mode] = tk.Frame(self.hdr_right, bg=T['panel'])
        return f

    def set_device(self, text):
        """footer, right: the pedal's MIDI port while connected"""
        self.f_dev.configure(text=text)

    def _bindings(self):
        r = self.root
        fw = lambda f: (lambda e: f() if self.mode == 'fw' else None)
        r.bind('<Control-f>', fw(lambda: (self.search.focus_set(), self.search.select_range(0, 'end'))))
        r.bind('<Control-s>', fw(self.save_profile))
        r.bind('<Control-o>', fw(self.open_profile))
        r.bind('<Control-g>', fw(self.open_build))
        r.bind('<Escape>', self._escape)
        for k in ('Up', 'Down', 'Prior', 'Next', 'space', 'Delete', 'Insert'):
            r.bind('<%s>' % k, lambda e, k=k: self._table_key(e, k))
        r.bind_all('<MouseWheel>', self._wheel)

    def _typing(self):
        w = self.root.focus_get()
        return isinstance(w, (tk.Entry, ttk.Combobox, tk.Text))

    def _table_key(self, e, k):
        """list keys work without clicking the list first (the design's ↑ ↓ / Espaço / Esc)"""
        if self.mode != 'fw' or self._typing() or e.widget is self.table.cv:
            return
        if not isinstance(e.widget, (tk.Tk, tk.Frame, tk.Canvas, tk.Label)):
            return
        return self.table.key(k, e.state & 1)

    def _escape(self, e):
        if self.mode != 'fw':
            return
        if self.root.focus_get() == self.search:
            if self.q.get():
                self.q.set('')
            else:
                self.root.focus_set()
        elif not self._typing():
            self.table.set_selection([])

    def _wheel(self, e):
        w = self.root.winfo_containing(e.x_root, e.y_root)
        if w is None:
            return
        for d in (self.det, self.pedal_view and self.pedal_view.det, self.nam_view and self.nam_view.det,
                  self.ir_view and self.ir_view.det, self.global_view and self.global_view.det):
            if d and str(w).startswith(str(d)):
                d._wheel(e)

    def set_mode(self, mode):
        """'fw': compose a firmware; 'pedal': presets and signal chains (pedalview.py); 'nam': NAM captures in
        the Clone slots (namview.py, shares the pedal view's MIDI port); 'ir': cabinet IRs in the User IR slots
        (irview.py, same port); 'global': the global EQ and settings (globalview.py, same port); 'tools': the
        firmware ports and other tools that were command-line scripts (toolsview.py)"""
        self.mode = mode
        self.nav.set(active=mode)
        if mode in ('pedal', 'nam', 'ir', 'global') and self.pedal_view is None:
            from matribox.studio import pedalview
            self.pedal_view = pedalview.PedalView(self.root, self, sys.modules[__name__])
        if mode == 'nam' and self.nam_view is None:
            from matribox.studio import namview
            self.nam_view = namview.NamView(self.root, self, sys.modules[__name__], self.pedal_view)
        if mode == 'ir' and self.ir_view is None:
            from matribox.studio import irview
            self.ir_view = irview.IrView(self.root, self, sys.modules[__name__], self.pedal_view)
        if mode == 'global' and self.global_view is None:
            from matribox.studio import globalview
            self.global_view = globalview.GlobalView(self.root, self, sys.modules[__name__], self.pedal_view)
        if mode == 'tools' and self.tools_view is None:
            from matribox.studio import toolsview
            self.tools_view = toolsview.ToolsView(self.root, self, sys.modules[__name__])
        views = {'fw': self.fw_view, 'pedal': self.pedal_view, 'nam': self.nam_view, 'ir': self.ir_view,
                 'global': self.global_view, 'tools': self.tools_view}
        for key, v in views.items():
            if v is not None and key != mode:
                v.pack_forget()
        acts = 'pedal' if mode in ('nam', 'ir', 'global') else mode     # these share the pedal's connection chip
        for key, a in self.actions.items():
            if key != acts:
                a.pack_forget()
        views[mode].pack(fill='both', expand=True)
        if acts in self.actions:
            self.actions[acts].pack(side='right')
        if mode in ('nam', 'ir', 'global', 'tools'):
            views[mode].refresh()
        self.session['mode'] = mode

    def more_menu(self):
        b = self.btn_more
        PopupMenu(self, [('Abrir perfil…', self.open_profile), ('Trocar firmware base…', self.choose_base),
                         ('Gerar base do original…', lambda: MakeBaseDialog(self)),
                         ('Bibliotecas…', self.manage_libraries), None,
                         ('Restaurar a seleção da base', self.reset, bool(self.project))],
                  b.winfo_rootx(), b.winfo_rooty() + b.winfo_height() + self.p(6))

    # ------------------------------------------------------------------ session / project
    def _load_session(self):
        try:
            return json.load(open(SESSION, encoding='utf-8'))
        except Exception:
            return {}

    @staticmethod
    def _example_libraries():
        """first run: the SDK's example catalogs (folders with a library.json and built modules)"""
        ex = C.paths.EXAMPLES
        dirs = sorted(d for d in (os.listdir(ex) if os.path.isdir(ex) else [])
                      if os.path.exists(os.path.join(ex, d, C.LIBRARY_META)) and glob.glob(os.path.join(ex, d, '*.axf')))
        return [[C.rel(os.path.join(ex, d)), C.library_meta(os.path.join(ex, d)).get('name', d)] for d in dirs]

    def _save_session(self):
        try:
            os.makedirs(os.path.dirname(SESSION), exist_ok=True)
            d = dict(self.session)
            if self.project:
                d['profile'] = self.project.to_profile()
            json.dump(d, open(SESSION, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
        except Exception:
            pass

    def _startup(self):
        dark_titlebar(self.root)
        last_mode = self.session.get('mode', 'fw')
        self.set_mode('fw')
        self.root.after(50, lambda: self.set_mode(last_mode) if last_mode in ('pedal', 'nam', 'ir', 'global',
                                                                              'tools') else None)
        prof = self.session.get('profile')
        base = C.absp(prof['base']) if prof and prof.get('base') else C.DEFAULT_BASE
        if not os.path.exists(base):
            base = C.DEFAULT_BASE
        if not os.path.exists(base):
            base = C.ORIGINAL_FW
        if not os.path.exists(base):
            self.toast_msg('Nenhum firmware base encontrado — use "Trocar firmware base"', 'warn')
            return
        if not self.load_project(base):
            return
        if prof and C.absp(prof.get('base', '')) == os.path.abspath(base):
            miss = self.project.apply_profile(prof)
            self.refresh_all()
            changed = sum(1 for it in self.project.items if self._state(it) in ('new', 'removed'))
            if changed:
                self.toast_msg('Sessão anterior restaurada: %d alterações%s' % (
                    changed, ', %d itens não encontrados' % len(miss) if miss else ''), 'info')

    def libraries(self):
        """the user's library folders: [(folder, name)]"""
        return [(d, n) for d, n in self.session.get('libraries', [])]

    def load_project(self, base):
        try:
            self.project = C.Project(base, self.libraries())
        except Exception as ex:
            self.toast_msg('Não foi possível ler %s: %s' % (os.path.basename(base), ex), 'error')
            return False
        ORIGIN_COLORS.update(self.project.lib_colors)
        self.rank = {id(it): k for k, it in enumerate(self.project.items)}
        self.by_key = {it.key: it for it in self.project.items}
        origins = sorted({it.origin for it in self.project.items}, key=lambda o: (o != C.FACTORY, o))
        self.origin_dd.set_values(['Todas as origens'] + origins)
        if self.origin not in self.origin_dd.values:
            self.origin = 'Todas as origens'
        self.origin_dd.set(self.origin)
        self.current = None
        self.table.set_selection([], notify=False)
        self.refresh_all()
        return True

    def refresh_base_info(self):
        P = self.project
        if not P:
            return
        nlib = sum(1 for it in P.catalog if not it.problem)
        self.f_base.configure(text='Base: %s' % os.path.basename(P.base_path))
        labels = {f: l for f, l, _ in C.APP_PATCHES}
        self.f_info[0].configure(text=' + '.join(labels[f] for f in P.patches) or APP_SHORT.get(P.app, 'app ?'))
        self.f_info[1].configure(text='%d efeitos no firmware' % len(P.selected()))
        self.f_info[2].configure(text='%d módulos na biblioteca' % nlib if P.sources else 'nenhuma biblioteca cadastrada')

    # ------------------------------------------------------------------ helpers
    def _state(self, it):
        if it.problem:
            return 'bad'
        if it.locked:
            return 'locked'
        if it.kind == 'base':
            return 'on' if it.selected else 'removed'
        return 'new' if it.selected else 'out'

    def _in_menu(self, it, key):
        if key is None or key == 'fixed':
            return (it.menu in C.FIXED_MENUS) == (key == 'fixed')
        return it.menu == key

    def _in_view(self, it, f):
        if f == 'in':
            return it.selected
        if f == 'factory':
            return it.kind == 'base'
        if f == 'library':
            return it.kind == 'lib'
        if f == 'changes':
            return self._state(it) in ('new', 'removed') or bool(it.edited)
        return True

    def _pre(self):
        """items of the chosen origin matching the search"""
        q = self.q.get().strip().lower()
        out = []
        for it in self.project.items:
            if self.origin != 'Todas as origens' and it.origin != self.origin:
                continue
            if q:
                hay = ' '.join((it.name, it.category, it.origin, '%08x' % it.id, '%#x' % it.id, it.source,
                                C.MENUS.get(it.menu, ''), C.MENU_SHORT.get(it.menu, ''))).lower()
                if not all(w in hay for w in q.split()):
                    continue
            out.append(it)
        return out

    def _visible(self, pre=None):
        pre = self._pre() if pre is None else pre
        out = [it for it in pre if self._in_menu(it, self.menu) and self._in_view(it, self.filter)]
        key, rev = self.sort
        if key is None:
            out.sort(key=lambda it: (it.menu, self.rank[id(it)]))
        else:
            f = dict(name=lambda it: it.name.lower(), origin=lambda it: (it.origin, it.name.lower()),
                     tab=lambda it: (it.category.lower(), it.name.lower()), menu=lambda it: (it.menu, self.rank[id(it)]),
                     size=lambda it: it.size, presets=lambda it: it.presets)[key]
            out.sort(key=f, reverse=rev)
        return out

    def _row_info(self, it):
        st = self._state(it)
        badge = ('entra', T['green_soft']) if st == 'new' else ('sai', T['red']) if st == 'removed' else \
            ('indisponível', T['amber']) if st == 'bad' else None
        n = it.presets if it.kind == 'base' else 0
        return dict(state=st, name=it.name + ('  ✎' if it.edited else ''), badge=badge,
                    ncolor=T['muted'] if st in ('removed', 'out', 'locked') else T['faint'] if st == 'bad' else T['text'],
                    origin=it.origin, ocolor=origin_color(it.origin), tab=it.category,
                    menu=C.MENU_SHORT.get(it.menu, str(it.menu)), mcolor=menu_color(it.menu),
                    size=C.fmt_size(it.size) if it.size else '—', presets=str(n) if n else '—',
                    pcolor=T['amber'] if st == 'removed' and n else T['soft'] if n else T['dim'])

    # ------------------------------------------------------------------ refresh
    def refresh_all(self):
        self.refresh_table()
        self.refresh_footer()
        self.refresh_details()

    def refresh_table(self):
        if not self.project:
            return
        pre = self._pre()
        vis = self._visible(pre)
        self.table.sort = self.sort
        self.table.set_items(vis)
        n_on = sum(1 for it in vis if it.selected)
        self.count_lbl.configure(text='%d itens · %d no firmware' % (len(vis), n_on))
        in_menu = [it for it in pre if self._in_menu(it, self.menu)]
        items = []
        for k, t in self.VIEWS:
            n = sum(1 for it in in_menu if self._in_view(it, k))
            items.append((k, t, n, T['accent'] if k == 'changes' and n else None))
        self.views.set(items, self.filter)
        self._pre_cache = pre
        self.refresh_sidebar()

    def update_rows(self, items):
        self.table.redraw()

    def refresh_sidebar(self):
        c = self.side_cv
        c.delete('all')
        P = self.project
        w = c.winfo_width()
        if not P or w < 20:
            return
        F, p = self.F, self.p
        bg = T['panel']
        pre = getattr(self, '_pre_cache', None) or self._pre()
        in_view = [it for it in pre if self._in_view(it, self.filter)]
        self.side_hits = []
        y = 0
        c.create_text(p(10), y, text='MENUS', font=F.tiny, fill=T['muted'], anchor='nw')
        y += F.tiny.metrics('linespace') + p(8) + p(4)
        rows = [(None, '', 'Todos os menus', sum(1 for it in in_view if it.menu not in C.FIXED_MENUS))]
        rows += [(m, C.MENU_SHORT.get(m, ''), C.MENUS.get(m, str(m)), sum(1 for it in in_view if it.menu == m))
                 for m in range(P.ncat) if m not in C.FIXED_MENUS]
        rh, gap = p(34), p(4)
        for key, code, label, n in rows:
            active = self.menu == key
            hov = self.side_hover == key and not active
            fill = T['line'] if active else T['raised'] if hov else None
            if fill:
                rrect(c, 0, y, w, y + rh, p(7), fill=fill, bg=bg)
            cy = y + rh / 2
            c.create_text(p(10), cy, text=code, font=F.mono_s, fill=T['faint'] if key is None else menu_color(key),
                          anchor='w')
            nt = str(n)
            room = w - p(48) - p(10) - F.mono_xs.measure(nt) - p(8)
            c.create_text(p(48), cy, text=fit_text(F.body, label, room), font=F.body,
                          fill=T['bright'] if active else T['soft'], anchor='w')
            c.create_text(w - p(10), cy, text=nt, font=F.mono_xs, fill=T['faint'], anchor='e')
            self.side_hits.append((y, y + rh, key))
            y += rh + gap
        y += p(10)
        c.create_rectangle(p(10), y, w - p(10), y + 1, fill=T['line'], outline='')
        y += p(14)
        y0 = y
        c.create_text(p(10), y, text='SEMPRE INCLUÍDOS', font=F.tiny, fill=T['muted'], anchor='nw')
        y += F.tiny.metrics('linespace') + p(8)
        active = self.menu == 'fixed'
        x = p(10)
        ch = p(22)
        for m in sorted(C.FIXED_MENUS):
            if m >= P.ncat:
                continue
            t = FIXED_LABELS.get(m, C.MENU_SHORT.get(m, str(m)))
            tw = F.xs.measure(t) + p(16)
            if x + tw > w - p(10):
                x, y = p(10), y + ch + p(4)
            rrect(c, x, y, x + tw, y + ch, p(5), outline=T['accent'] if active else T['line2'],
                  fill=T['accent_lo'] if active else None, width=lw_of(self.S), bg=bg)
            c.create_text(x + tw / 2, y + ch / 2, text=t, font=F.xs, fill=T['text'] if active else T['muted'])
            x += tw + p(4)
        y += ch
        self.side_hits.append((y0, y, 'fixed'))
        hint = 'Clique para ver os fixos' if not active else 'Mostrando os menus fixos'
        c.create_text(p(10), y + p(10), text=hint, font=F.xs, fill=T['dim'], anchor='nw')

    def _side_key(self, y):
        for y1, y2, key in getattr(self, 'side_hits', []):
            if y1 <= y <= y2:
                return key, True
        return None, False

    def _side_set_hover(self, key):
        if key != self.side_hover:
            self.side_hover = key
            self.refresh_sidebar()

    def _side_motion(self, e):
        key, hit = self._side_key(e.y)
        self._side_set_hover(key if hit else '-')

    def _side_click(self, e):
        key, hit = self._side_key(e.y)
        if not hit:
            return
        self.menu = None if key == 'fixed' and self.menu == 'fixed' else key
        self.refresh_table()

    def refresh_footer(self):
        P = self.project
        if not P:
            return
        self.refresh_base_info()
        total, idx, base, added = P.lib_size()
        self._cap = (total, base, added, idx)
        M = fwtool.MAX_LIBRARY
        pct = 100 * total / M
        over = total > M
        self.cap_lbl.configure(text='%s de %s' % (C.fmt_size(total), C.fmt_size(M)),
                               fg=T['red'] if over else T['text'])
        self.pct_lbl.configure(text=('%.1f%%' % pct).replace('.', ','), fg=T['red'] if over else T['muted'])
        free = M - total
        add = sum(1 for it in P.catalog if it.selected)
        rem = [it for it in P.base_items if not it.selected]
        self.delta_lbl.configure(text='+%d / −%d efeitos' % (add, len(rem)))
        hit = {r for it in rem for r in it.preset_ids}
        self.aff_lbl.configure(text='%d preset%s com efeitos removidos' % (len(hit), 's' if len(hit) != 1 else '')
                               if hit else '')
        self.legend[0].configure(text=C.fmt_size(base + idx))
        self.legend[1].configure(text=C.fmt_size(added))
        self.legend[2].configure(text=C.fmt_size(max(free, 0)))
        self.index_lbl.configure(text='%d / %d bytes' % (idx, fwtool.MAX_INDEX),
                                 fg=T['red'] if idx > fwtool.MAX_INDEX else T['soft2'])
        iss = P.issues()
        ne = sum(1 for l, t in iss if l == 'erro')
        nw = len(iss) - ne
        if over:
            text, col = 'Excede em %s' % C.fmt_size(-free), T['red']
        elif ne:
            text, col = '%d erro%s' % (ne, 's' if ne > 1 else ''), T['red']
        elif nw:
            text, col = '%d aviso%s' % (nw, 's' if nw > 1 else ''), T['amber']
        else:
            text, col = 'Tudo certo', T['green']
        self.st_lbl.configure(text=text, fg=col)
        self.st_dot.delete('all')
        aa.oval(self.st_dot, self.p(7) / 2, self.p(7) / 2, self.p(7) / 2, fill=col)
        self.btn_build.config_btn(enabled=not ne and not over,
                                  text='Libere espaço para gerar' if over else 'Gerar firmware')
        self.draw_bar()

    def draw_bar(self):
        c = self.bar
        c.delete('all')
        if not getattr(self, '_cap', None):
            return
        total, base, added, idx = self._cap
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10:
            return
        M = fwtool.MAX_LIBRARY
        scale = w / max(M, total)
        xb = (base + idx) * scale
        xa = xb + added * scale
        r = h / 2
        shapes = [('rrect', (0, 0, w, h, r), T['track'])]       # one image: the segments overlap
        if xa > 1:
            shapes.append(('rrect', (0, 0, max(xa, 2 * r), h, r), T['green']))
        if xb > 1:
            shapes.append(('rrect', (0, 0, max(xb, 2 * r), h, r), T['base_bar']))
        if total > M:
            xm = M * scale
            shapes.append(('rrect', (xm, 0, w, h, r), T['red_hi']))
        aa.draw(c, 0, 0, w, h, shapes, T['panel'])
        if total > M:
            c.create_line(xm, -2, xm, h + 2, fill=T['text'], width=2)

    # ------------------------------------------------------------------ details panel
    def _on_select(self):
        sel = self.table.selection()
        if len(sel) == 1:
            self.current = self.by_key.get(sel[0])
        elif not sel:
            self.current = None
        else:
            self.current = [self.by_key[k] for k in sel if k in self.by_key]
        self.refresh_details()

    def clear_selection(self):
        self.table.set_selection([])

    def refresh_details(self):
        box = self.det.inner
        for w in box.winfo_children():
            w.destroy()
        self.det.canvas.yview_moveto(0)
        F, p = self.F, self.p
        bg = T['panel']
        it = self.current
        pad = dict(padx=p(20))
        if isinstance(it, list):
            self._details_multi(box, it)
            return
        if it is None:
            self._details_empty(box)
            return
        st = self._state(it)
        # head: menu chip, origin, close
        head = tk.Frame(box, bg=bg)
        head.pack(fill='x', pady=(p(20), 0), **pad)
        col = menu_color(it.menu)
        Pill(head, self, C.MENU_SHORT.get(it.menu, '?'), col, font=F.mono_s, tint=.85, height=20, padx=7).pack(
            side='left')
        od = tk.Canvas(head, width=p(7), height=p(7), bg=bg, highlightthickness=0)
        aa.oval(od, p(3.5), p(3.5), p(3.5), fill=origin_color(it.origin))
        od.pack(side='left', padx=(p(10), p(6)))
        tk.Label(head, text=it.origin, font=F.small, bg=bg, fg=T['muted']).pack(side='left')
        Btn(head, self, '×', None, self.clear_selection, 'subtle', height=28, width=28, padx=0, font=F.h3,
            tip='Fechar (Esc)').pack(side='right')
        # name
        if it.kind == 'lib' and not it.problem:
            self._name_field(box, it)
        else:
            tk.Label(box, text=it.name, font=F.h_name, bg=bg, fg=T['bright'], anchor='w', wraplength=p(300),
                     justify='left').pack(anchor='w', fill='x', pady=(p(12), 0), **pad)
        why = self.project.can_toggle(it)
        ToggleRow(box, self, 'No firmware' if it.selected else 'Fora do firmware', it.selected,
                  lambda: self.toggle([it]), enabled=not why).pack(fill='x', pady=(p(12), 0), **pad)
        if why:
            self._note(box, why.capitalize() + '.', T['muted'])
        if it.kind == 'base' and it.presets and not it.selected:
            n = it.presets
            self._note(box, '%d preset%s este efeito. Sem ele, %s no 1º efeito do menu.' % (
                n, 's do Bank usam' if n > 1 else ' do Bank usa', 'esses blocos caem' if n > 1 else 'o bloco cai'),
                T['amber'], tint=True)
        if it.problem:
            self._note(box, it.problem, T['amber'], tint=True)
        # facts
        g = tk.Frame(box, bg=bg)
        g.pack(fill='x', pady=(p(22), 0), **pad)
        g.columnconfigure(1, weight=1)
        r = 0

        def kv(label, widget_or_text, mono=False, fg=None):
            nonlocal r
            tk.Label(g, text=label, font=F.body, bg=bg, fg=T['muted']).grid(row=r, column=0, sticky='w',
                                                                            pady=p(5), padx=(0, p(16)))
            if isinstance(widget_or_text, str):
                tk.Label(g, text=widget_or_text, font=F.mono if mono else F.body, bg=bg, fg=fg or T['text'],
                         anchor='w', wraplength=p(210), justify='left').grid(row=r, column=1, sticky='w', pady=p(5))
            else:
                widget_or_text.grid(row=r, column=1, sticky='we', pady=p(3))
            r += 1
        if it.kind == 'lib' and not it.problem:
            tabs = []
            for t in self.project.tabs(it.menu) + [it.orig_category]:
                if t not in tabs:
                    tabs.append(t)
            kv('Aba', self._tab_field(g, it, tabs))
        else:
            kv('Aba', it.category)
        kv('Menu', C.MENUS.get(it.menu, str(it.menu)))
        kv('Tamanho', C.fmt_size(it.size) if it.size else '—', mono=True)
        kv('ID', '%#010x' % it.id, mono=True, fg=T['soft'])
        if it.elf_size:
            kv('Módulo', C.fmt_size(it.elf_size) + ' (.axf)', mono=True, fg=T['soft'])
        if it.ram:
            kv('RAM', 'IRAM %s · SDRAM %s' % (C.fmt_size(it.ram.get('iram', 0)), C.fmt_size(it.ram.get('sram', 0))),
               mono=True, fg=T['soft'])
        # parameters
        self._section(box, 'Parâmetros · %d' % len(it.params))
        if it.params:
            n = lambda v: ('%d' % v) if float(v).is_integer() else ('%g' % v)
            tips = []
            for prm in it.params:
                f3 = prm.get('flags', [0, 0, 0x15])[2]
                rng = 'Off / On' if f3 == 0 and prm['max'] == 1 else '%s – %s' % (n(prm['min']), n(prm['max']))
                tips.append('%s: %s, padrão %s' % (prm['name'], rng, n(prm['default'])))
            ChipFlow(box, self, [prm['name'] for prm in it.params], 'outline', tips).pack(fill='x', **pad)
        else:
            tk.Label(box, text='Sem parâmetros.', font=F.small, bg=bg, fg=T['faint']).pack(anchor='w', **pad)
        # presets
        self._section(box, 'Usado em presets')
        if it.kind == 'base' and it.preset_ids:
            ChipFlow(box, self, [preset_label(i) for i in it.preset_ids], 'fill').pack(fill='x', **pad)
        else:
            tk.Label(box, text='Nenhum preset do Bank usa este efeito.' if it.kind == 'base' else
                     'Módulos da biblioteca não estão nos presets da base.', font=F.small, bg=bg,
                     fg=T['faint'], wraplength=p(300), justify='left').pack(anchor='w', **pad)
        # source
        if it.source or it.kind == 'lib':
            self._section(box, 'Origem')
            if it.source:
                tk.Label(box, text=it.source, font=F.small, bg=bg, fg=T['soft'], wraplength=p(300),
                         justify='left').pack(anchor='w', **pad)
            if it.kind == 'lib':
                tk.Label(box, text=C.rel(it.path), font=F.mono_xs, bg=bg, fg=T['faint'], wraplength=p(300),
                         justify='left').pack(anchor='w', pady=(p(4), 0), **pad)
                Btn(box, self, 'Mostrar na pasta', None, lambda: reveal(it.path), 'ghost', height=30, padx=12,
                    font=F.small, radius=7).pack(anchor='w', pady=(p(10), 0), **pad)
        tk.Frame(box, bg=bg, height=p(24)).pack()

    def _note(self, box, text, color, tint=False):
        p = self.p
        if tint:
            c = Card(box, self, T['amber_lo'], None, 8)
            c.pack(fill='x', padx=p(20), pady=(p(10), 0))
            tk.Label(c.body, text=text, font=self.F.small, bg=T['amber_lo'], fg=color, wraplength=p(272),
                     justify='left').pack(anchor='w', padx=p(9), pady=p(7))
        else:
            tk.Label(box, text=text, font=self.F.small, bg=T['panel'], fg=color, wraplength=p(300),
                     justify='left').pack(anchor='w', padx=p(20), pady=(p(8), 0))

    def _name_field(self, box, it):
        """the design's in-place name editor: no border until hovered, darker while editing"""
        F, p = self.F, self.p
        bg = T['panel']
        var = tk.StringVar(value=it.name)
        c = Card(box, self, bg, bg, 7)
        c.pack(fill='x', padx=(p(10), p(14)), pady=(p(8), 0))
        e = tk.Entry(c.body, textvariable=var, font=F.h_name, bg=bg, fg=T['bright'], bd=0, relief='flat',
                     insertbackground=T['text'], highlightthickness=0, selectbackground=T['seg_on'])
        e.pack(fill='x', padx=p(10) - c.i, pady=p(6) - c.i)
        state = {'focus': False}

        def paint(hover=False):
            if state['focus']:
                c.set_border(T['focus'], T['input'])
                e.configure(bg=T['input'])
            else:
                c.set_border(T['line2'] if hover else bg, bg)
                e.configure(bg=bg)
        e.bind('<Enter>', lambda ev: paint(True))
        e.bind('<Leave>', lambda ev: paint(False))
        e.bind('<FocusIn>', lambda ev: (state.update(focus=True), paint()))
        e.bind('<FocusOut>', lambda ev: (state.update(focus=False), paint(), self._apply_field(it, 'name', var)))
        e.bind('<Return>', lambda ev: self._apply_field(it, 'name', var))
        e.bind('<Escape>', lambda ev: (var.set(it.name), self.root.focus_set()))
        Tooltip(e, self, 'Nome no pedal: até %d caracteres' % fwtool.MAX_NAME)

    def _tab_field(self, parent, it, tabs):
        """tab name: free text, with the menu's existing tabs one click away"""
        p = self.p
        var = tk.StringVar(value=it.category)
        f = Field(parent, self, var, '', search=False, height=30, radius=6, font=self.F.body)
        f.entry.bind('<Return>', lambda e: self._apply_field(it, 'category', var))
        f.entry.bind('<FocusOut>', lambda e: self._apply_field(it, 'category', var), add='+')
        chev = tk.Label(f.body, text=ICO['chev'] if self.F.icons else '▾', font=self.F.icon_s, bg=T['input'],
                        fg=T['faint'], cursor='hand2')
        chev.pack(side='right', padx=(0, p(8)), before=f.entry)

        def pick(t):
            var.set(t)
            self._apply_field(it, 'category', var)
        chev.bind('<ButtonRelease-1>', lambda e: PopupMenu(
            self, [(t, lambda t=t: pick(t)) for t in tabs], f.winfo_rootx(), f.winfo_rooty() + f.winfo_height() + p(4),
            width=f.winfo_width(), current=var.get(), above=f.winfo_rooty()))
        Tooltip(f, self, 'Até 19 caracteres. Uma aba nova é criada no fim do menu.')
        return f

    def _details_multi(self, box, items):
        F, p = self.F, self.p
        bg = T['panel']
        pad = dict(padx=p(20))
        tk.Label(box, text='%d efeitos selecionados' % len(items), font=F.h2, bg=bg, fg=T['bright']).pack(
            anchor='w', pady=(p(22), p(10)), **pad)
        size_in = sum(i.size for i in items if i.selected)
        size_out = sum(i.size for i in items if not i.selected and not i.problem)
        g = tk.Frame(box, bg=bg)
        g.pack(fill='x', **pad)
        for r, (k, n, s) in enumerate((('No firmware', sum(1 for i in items if i.selected), size_in),
                                       ('Fora', sum(1 for i in items if not i.selected), size_out))):
            tk.Label(g, text=k, font=F.body, bg=bg, fg=T['muted']).grid(row=r, column=0, sticky='w', pady=p(4))
            tk.Label(g, text='%d  ·  %s' % (n, C.fmt_size(s)), font=F.mono, bg=bg, fg=T['text']).grid(
                row=r, column=1, sticky='w', padx=(p(16), 0), pady=p(4))
        row = tk.Frame(box, bg=bg)
        row.pack(fill='x', pady=p(18), **pad)
        Btn(row, self, 'Incluir todos', None, lambda: self.set_selected(True), 'teal', height=34).pack(side='left')
        Btn(row, self, 'Tirar todos', None, lambda: self.set_selected(False), 'danger', height=34).pack(
            side='left', padx=p(8))
        self._keys(box, [('Espaço', 'alterna'), ('Insert', 'inclui'), ('Delete', 'tira'), ('Esc', 'limpa a seleção')])

    def _keys(self, box, pairs):
        F, p = self.F, self.p
        bg = T['panel']
        g = tk.Frame(box, bg=bg)
        g.pack(fill='x', padx=p(20), pady=(p(6), 0))
        for r, (k, t) in enumerate(pairs):
            tk.Label(g, text=k, font=F.mono_s, bg=bg, fg=T['soft'], padx=p(6), highlightthickness=1,
                     highlightbackground=T['line2']).grid(row=r, column=0, sticky='w', pady=p(4))
            tk.Label(g, text=t, font=F.small, bg=bg, fg=T['muted']).grid(row=r, column=1, sticky='w',
                                                                         padx=(p(12), 0), pady=p(4))

    def _details_empty(self, box):
        F, p = self.F, self.p
        bg = T['panel']
        P = self.project
        tk.Label(box, text='Monte seu firmware', font=F.h2, bg=bg, fg=T['bright']).pack(anchor='w', padx=p(20),
                                                                                      pady=(p(22), p(14)))
        steps = ['Marque o que entra e desmarque o que sai. Efeitos da base desmarcados são removidos; da '
                 'biblioteca, marcados são adicionados.',
                 'Acompanhe o espaço na barra abaixo — a biblioteca DSP tem %s na flash.' % C.fmt_size(
                     fwtool.MAX_LIBRARY),
                 'Gere o firmware. Só a seção DSP muda; app%s, IRs e presets vêm da base.' % (
                     ' (com a GUI paralela)' if P and 'parallel' in P.patches else '')]
        g = tk.Frame(box, bg=bg)
        g.pack(fill='x', padx=p(20))
        for r, t in enumerate(steps):
            tk.Label(g, text='%02d' % (r + 1), font=F.mono_s, bg=bg, fg=T['accent']).grid(
                row=r, column=0, sticky='nw', pady=(p(2), p(10)))
            tk.Label(g, text=t, font=F.body, bg=bg, fg=T['soft'], wraplength=p(266), justify='left').grid(
                row=r, column=1, sticky='nw', padx=(p(10), 0), pady=(0, p(10)))
        if not P:
            return
        head = tk.Frame(box, bg=bg)
        head.pack(fill='x', padx=p(20), pady=(p(16), p(6)))
        tk.Label(head, text='ORIGENS', font=F.tiny, bg=bg, fg=T['muted']).pack(side='left')
        tk.Label(head, text='no firmware / total', font=F.xs, bg=bg, fg=T['faint']).pack(side='right')
        by = {}
        for it in P.items:
            if it.problem:
                continue
            n, s, k = by.get(it.origin, (0, 0, 0))
            by[it.origin] = (n + 1, s + it.size, k + it.selected)
        for o, (n, s, k) in sorted(by.items(), key=lambda kv: (kv[0] != C.FACTORY, kv[0])):
            RowButton(box, self, o, '%d/%d · %s' % (k, n, C.fmt_size(s)), origin_color(o), self.origin == o,
                      lambda o=o: self._jump_origin(o)).pack(fill='x', padx=p(10))
        if not P.sources:
            tk.Label(box, text='Nenhuma biblioteca cadastrada: só dá para tirar efeitos da base. Cadastre uma '
                     'pasta com manifestos (.json + .axf) para acrescentar módulos.', font=F.small, bg=bg,
                     fg=T['muted'], wraplength=p(300), justify='left').pack(anchor='w', padx=p(20), pady=(p(6), 0))
        Btn(box, self, 'Gerenciar bibliotecas', None, self.manage_libraries, 'ghost', height=30, padx=12,
            font=F.small, radius=7).pack(anchor='w', padx=p(20), pady=(p(12), p(10)))
        self._keys(box, [('↑ ↓', 'navegar na lista'), ('Espaço', 'marcar / desmarcar'), ('Esc', 'fechar detalhes')])
        tk.Frame(box, bg=bg, height=p(20)).pack()

    def _jump_origin(self, o):
        self.origin = 'Todas as origens' if self.origin == o else o
        self.origin_dd.set(self.origin)
        self.refresh_table()
        self.refresh_details()

    def _section(self, box, title, right=None):
        p = self.p
        row = tk.Frame(box, bg=T['panel'])
        row.pack(fill='x', padx=p(20), pady=(p(22), p(10)))
        tk.Label(row, text=title.upper(), font=self.F.tiny, bg=T['panel'], fg=T['muted']).pack(side='left')
        if right:
            tk.Label(row, text=right, font=self.F.xs, bg=T['panel'], fg=T['faint']).pack(side='right')
        return row

    def _kv(self, box, pairs):
        p = self.p
        g = tk.Frame(box, bg=T['panel'])
        g.pack(fill='x', padx=p(20))
        g.columnconfigure(1, weight=1)
        for r, (k, v) in enumerate(pairs):
            tk.Label(g, text=k, font=self.F.body, bg=T['panel'], fg=T['muted']).grid(row=r, column=0, sticky='w',
                                                                                     pady=p(4))
            tk.Label(g, text=v, font=self.F.body, bg=T['panel'], fg=T['text']).grid(row=r, column=1, sticky='w',
                                                                                    padx=(p(16), 0), pady=p(4))

    def _apply_field(self, it, attr, var):
        v = var.get().strip()
        if v == getattr(it, attr):
            return
        err = ''
        if not v:
            err = 'não pode ficar vazio'
        elif len(v) > fwtool.MAX_NAME:
            err = 'máximo de %d caracteres (tem %d)' % (fwtool.MAX_NAME, len(v))
        else:
            try:
                v.encode('latin1')
            except UnicodeEncodeError:
                err = 'use só caracteres latin-1'
        if err:
            self.toast_msg('%s: %s' % ('Nome' if attr == 'name' else 'Aba', err), 'error')
            var.set(getattr(it, attr))
            return
        setattr(it, attr, v)
        it.size = len(C.build_entry(it, 0).raw)
        self.update_rows([it])
        self.refresh_footer()
        self.toast_msg('%s alterado para "%s"' % ('Nome' if attr == 'name' else 'Aba', v), 'ok')
        self.root.after(10, self.refresh_details)

    # ------------------------------------------------------------------ actions
    def toggle_selected(self):
        items = [self.by_key[k] for k in self.table.selection() if k in self.by_key]
        if items:
            want = not all(it.selected for it in items)
            self.set_items(items, want)

    def set_selected(self, value):
        items = [self.by_key[k] for k in self.table.selection() if k in self.by_key]
        self.set_items(items, value)

    def toggle(self, items):
        for it in items:
            self.set_items([it], not it.selected)

    def set_items(self, items, value, quiet=False):
        P = self.project
        changed, blocked = [], []
        for it in items:
            if it.selected == value:
                continue
            why = P.can_toggle(it)
            if why:
                blocked.append((it, why))
                continue
            it.selected = value
            changed.append(it)
        if changed:
            self.refresh_table()
            self.refresh_footer()
            self.refresh_details()
        if blocked and not quiet:
            it, why = blocked[0]
            more = ' (+%d)' % (len(blocked) - 1) if len(blocked) > 1 else ''
            self.toast_msg('%s: %s%s' % (it.name, why, more), 'warn')
        return changed, blocked

    def bulk(self, value):
        vis = [it for it in self._visible() if it.selected != value]
        if not vis:
            return
        changed, blocked = self.set_items(vis, value, quiet=True)
        msg = '%d efeito%s %s' % (len(changed), 's' if len(changed) != 1 else '', 'incluídos' if value else 'retirados')
        if blocked:
            msg += ' · %d mantidos (fixos, duplicados ou último do menu)' % len(blocked)
        self.toast_msg(msg, 'ok' if changed else 'warn')

    def _set_filter(self, k):
        self.filter = k
        self.refresh_table()

    def _set_origin(self, v):
        self.origin = v
        self.refresh_table()
        if self.current is None:
            self.refresh_details()

    def _sort(self, col):
        key, rev = self.sort
        if key == col:
            self.sort = (None, False) if rev else (col, True)
        else:
            self.sort = (col, col in ('size', 'presets'))
        self.refresh_table()

    def reset(self):
        if not self.project:
            return
        if not ask(self, 'Restaurar seleção', 'Descartar as alterações e voltar ao conteúdo do firmware base?',
                   'Restaurar', 'danger'):
            return
        self.project.reset()
        self.refresh_all()
        self.toast_msg('Seleção restaurada', 'ok')

    def choose_base(self):
        BaseDialog(self)

    def set_base(self, f, keep=True):
        """load f as the base; keep=True carries the added modules over (and the removals too when f is the
        current base, i.e. a reload)"""
        same = self.project and os.path.normcase(os.path.abspath(f)) == os.path.normcase(
            os.path.abspath(self.project.base_path))
        prof = self.project.to_profile() if self.project and keep else None
        if not self.load_project(f):
            return False
        recent = [x for x in self.session.get('bases', []) if os.path.normcase(x) != os.path.normcase(f)]
        self.session['bases'] = [os.path.abspath(f)] + recent[:9]
        miss = []
        if prof:
            if not same:
                prof['remove'] = []
            miss = self.project.apply_profile(prof)
            self.refresh_all()
        self.toast_msg('Base: %s%s' % (os.path.basename(f), ' (%d módulos adicionados não entram nesta base)' %
                                       len(miss) if miss else ''), 'warn' if miss else 'ok')
        return True

    def save_profile(self):
        if not self.project:
            return
        f = filedialog.asksaveasfilename(parent=self.root, title='Salvar perfil', defaultextension='.json',
                                         initialdir=self.session.get('profile_dir', C.paths.BUILD_DIR),
                                         initialfile='meu_firmware.json', filetypes=[('Perfil', '*.json')])
        if not f:
            return
        json.dump(self.project.to_profile(), open(f, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
        self.session['profile_dir'] = os.path.dirname(f)
        self.toast_msg('Perfil salvo: %s' % os.path.basename(f), 'ok')

    def open_profile(self):
        f = filedialog.askopenfilename(parent=self.root, title='Abrir perfil',
                                       initialdir=self.session.get('profile_dir', C.paths.BUILD_DIR),
                                       filetypes=[('Perfil', '*.json')])
        if not f:
            return
        try:
            prof = json.load(open(f, encoding='utf-8'))
            base = C.absp(prof.get('base') or self.project.base_path)
            new_libs = self._merge_libraries(prof.get('sources', []))
            if new_libs or not self.project or os.path.abspath(base) != os.path.abspath(self.project.base_path):
                if not self.load_project(base):
                    return
            miss = self.project.apply_profile(prof)
        except Exception as ex:
            self.toast_msg('Perfil inválido: %s' % ex, 'error')
            return
        self.session['profile_dir'] = os.path.dirname(f)
        self.refresh_all()
        notes = (['%d itens não encontrados' % len(miss)] if miss else []) + \
                (['bibliotecas do perfil cadastradas: %s' % ', '.join(new_libs)] if new_libs else [])
        self.toast_msg('Perfil carregado%s' % (' (%s)' % '; '.join(notes) if notes else ''),
                       'warn' if miss else 'ok')

    def _merge_libraries(self, sources):
        """registers the folders of `sources` the user does not have yet (by folder; a name already taken gets
        a suffix); returns the names added"""
        libs = self.session.setdefault('libraries', [])
        have = {os.path.normcase(os.path.abspath(C.absp(d))) for d, _ in libs}
        added = []
        for d, name in sources:
            k = os.path.normcase(os.path.abspath(C.absp(d)))
            if k in have or not os.path.isdir(k):
                continue
            name = unique_name(name, [n for _, n in libs])
            libs.append([C.rel(C.absp(d)), name])
            have.add(k)
            added.append(name)
        return added

    def manage_libraries(self):
        LibraryDialog(self)

    def set_libraries(self, libs):
        """replace the user's library list and reload, keeping the selection"""
        self.session['libraries'] = [[C.rel(C.absp(d)), n] for d, n in libs]
        if not self.project:
            return
        prof = self.project.to_profile()
        if not self.load_project(self.project.base_path):
            return
        miss = self.project.apply_profile(prof)
        self.refresh_all()
        nlib = sum(1 for it in self.project.catalog if not it.problem)
        self.toast_msg('Bibliotecas atualizadas: %d módulos disponíveis%s' % (
            nlib, ' · %d adicionados saíram da seleção' % len(miss) if miss else ''), 'warn' if miss else 'ok')

    def show_issues(self):
        iss = self.project.issues() if self.project else []
        if not iss:
            self.toast_msg('Nenhum problema: a seleção cabe e segue as regras do app', 'ok')
            return
        dlg = Dialog(self, 'Verificação da seleção', width=620)
        for lvl, t in iss:
            r = tk.Frame(dlg.body, bg=T['panel'])
            r.pack(fill='x', pady=round(4 * self.S))
            Pill(r, self, 'ERRO' if lvl == 'erro' else 'AVISO', T['red'] if lvl == 'erro' else T['amber']).pack(
                side='left', anchor='n')
            tk.Label(r, text=t, font=self.F.body, bg=T['panel'], fg=T['soft'], wraplength=round(480 * self.S),
                     justify='left').pack(side='left', padx=round(10 * self.S))
        dlg.buttons([('Fechar', 'ghost', dlg.close)])
        dlg.show()

    def open_build(self):
        if not self.project:
            return
        self.build_dlg = BuildDialog(self)

    def open_update(self, path=None):
        """firmware upload (updateview.py); default file: the last one built, else the base"""
        from matribox.studio import updateview
        if path is None:
            last = self.session.get('out_path')
            path = last if last and os.path.exists(last) else (self.project.base_path if self.project else '')
        updateview.UpdateDialog(self, sys.modules[__name__], path)

    # ------------------------------------------------------------------ toast
    def toast_msg(self, text, kind='info'):
        if self.toast:
            self.toast.destroy()
        color = {'ok': T['green'], 'warn': T['amber'], 'error': T['red'], 'info': T['accent']}[kind]
        icon = {'ok': ICO['check'], 'warn': ICO['warn'], 'error': ICO['warn'], 'info': ICO['info']}[kind]
        p = self.p
        t = tk.Frame(self.root, bg=T['line2'])
        inner = tk.Frame(t, bg=T['raised'])
        inner.pack(padx=1, pady=1)
        tk.Frame(inner, bg=color, width=p(3)).pack(side='left', fill='y')
        if self.F.icons:
            tk.Label(inner, text=icon, font=self.F.icon, bg=T['raised'], fg=color).pack(side='left', padx=(p(12), 0))
        tk.Label(inner, text=text, font=self.F.body, bg=T['raised'], fg=T['text'], wraplength=p(520),
                 justify='left').pack(side='left', padx=p(12), pady=p(11))
        t.place(relx=.5, rely=1.0, y=-p(130), anchor='s')
        t.lift()
        self.toast = t
        t.after(4200 if kind in ('warn', 'error') else 2800, lambda: (t.destroy(), setattr(self, 'toast', None))
                if self.toast is t else None)

    def on_close(self):
        if self.nam_view is not None:
            self.nam_view.shutdown()
        if self.ir_view is not None:
            self.ir_view.shutdown()
        if self.global_view is not None:
            self.global_view.shutdown()
        if self.tools_view is not None:
            self.tools_view.shutdown()
        if self.pedal_view is not None:
            self.pedal_view.shutdown()
        self._save_session()
        self.root.destroy()


# ------------------------------------------------------------------------------------------------ dialogs
class Dialog:
    def __init__(self, app, title, width=560):
        self.app = app
        S = app.S
        self.top = tk.Toplevel(app.root)
        self.top.withdraw()
        self.top.title(title)
        self.top.configure(bg=T['line2'])
        self.top.transient(app.root)
        self.top.resizable(False, False)
        self.frame = tk.Frame(self.top, bg=T['panel'])
        self.frame.pack(fill='both', expand=True, padx=1, pady=1)
        self.w = round(width * S)
        tk.Label(self.frame, text=title, font=app.F.h2, bg=T['panel'], fg=T['bright']).pack(
            anchor='w', padx=round(26 * S), pady=(round(22 * S), round(10 * S)))
        self.body = tk.Frame(self.frame, bg=T['panel'], width=self.w)
        self.body.pack(fill='both', expand=True, padx=round(26 * S))
        self.bar = tk.Frame(self.frame, bg=T['panel'])
        self.bar.pack(fill='x', padx=round(26 * S), pady=(round(18 * S), round(22 * S)))
        self.top.bind('<Escape>', lambda e: self.close())
        self.result = None

    def buttons(self, specs):
        for w in self.bar.winfo_children():
            w.destroy()
        self.btns = []
        for text, kind, cmd in reversed(specs):
            b = Btn(self.bar, self.app, text, None, cmd, kind, height=36, padx=16)
            b.pack(side='right', padx=(round(8 * self.app.S), 0))
            self.btns.append(b)

    def _path_row(self, var, browse, parent=None):
        S, F = self.app.S, self.app.F
        p = lambda v: round(v * S)
        row = tk.Frame(parent or self.body, bg=T['panel'])
        row.pack(fill='x')
        f = Field(row, self.app, var, '', search=False, height=36, font=F.mono)
        f.pack(side='left', fill='x', expand=True)
        Btn(row, self.app, '', ICO['folder'], browse, 'ghost', height=36, padx=12).pack(side='left', padx=(p(8), 0))

    def show(self, wait=False):
        self.top.update_idletasks()
        self.top.update_idletasks()                 # Cards size themselves at idle time
        r = self.app.root
        w = max(self.w + round(54 * self.app.S), self.top.winfo_reqwidth())
        h = self.top.winfo_reqheight()
        x = r.winfo_rootx() + (r.winfo_width() - w) // 2
        y = r.winfo_rooty() + (r.winfo_height() - h) // 3
        self.top.minsize(w, 1)                      # the height keeps following the content
        self.top.geometry('+%d+%d' % (x, max(0, y)))
        self.top.deiconify()
        dark_titlebar(self.top)
        self.top.grab_set()
        self.top.focus_set()
        if wait:
            self.app.root.wait_window(self.top)
        return self.result

    def close(self, result=None):
        self.result = result
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()


def unique_name(name, taken):
    """name, or name (2), (3)... when another entry of `taken` already has it (case-insensitive)"""
    name = name.strip() or 'Biblioteca'
    low = {t.strip().lower() for t in taken} | {C.FACTORY.lower()}
    k, out = 2, name
    while out.lower() in low:
        out, k = '%s (%d)' % (name, k), k + 1
    return out


def ask(app, title, text, ok, kind='primary'):
    d = Dialog(app, title, 480)
    tk.Label(d.body, text=text, font=app.F.body, bg=T['panel'], fg=T['soft'], wraplength=round(430 * app.S),
             justify='left').pack(anchor='w')
    d.buttons([('Cancelar', 'ghost', d.close), (ok, kind, lambda: d.close(True))])
    return d.show(wait=True)


class BaseDialog(Dialog):
    """picks the base image among the QME-200 files of the project (C.BASE_DIRS) and the recently used ones"""
    APPS = {'patched': 'GUI paralela', 'original': 'original', 'own': 'próprio'}

    def __init__(self, app):
        super().__init__(app, 'Firmware base', 880)
        S, F = app.S, app.F
        p = lambda v: round(v * S)
        bg = T['panel']
        P = app.project
        self.cur = os.path.normcase(os.path.abspath(P.base_path)) if P else None
        self.rows, self.alive = {}, True
        if not hasattr(app, 'base_probe'):
            app.base_probe = {}                         # (path, mtime, size) -> C.probe_base result
        tk.Label(self.body, text='Só a seção DSP (efeitos) é refeita; app, IRs e presets vêm da base escolhida.',
                 font=F.small, bg=bg, fg=T['muted']).pack(anchor='w', pady=(0, p(12)))
        card = tk.Frame(self.body, bg=T['card'], highlightthickness=1, highlightbackground=T['cardline'])
        card.pack(fill='x')
        cols = ('file', 'folder', 'app', 'fx', 'lib', 'date')
        self.tree = ttk.Treeview(card, columns=cols, show='headings', style='Fx.Treeview', selectmode='browse',
                                 height=9)
        heads = dict(file='ARQUIVO', folder='PASTA', app='APP', fx='EFEITOS', lib='BIBLIOTECA', date='DATA')
        widths = dict(file=372, folder=92, app=112, fx=72, lib=92, date=122)
        for c in cols:
            anchor = 'e' if c in ('fx', 'lib') else 'w'
            self.tree.heading(c, text=heads[c], anchor=anchor)
            self.tree.column(c, width=p(widths[c]), stretch=c == 'file', minwidth=p(40), anchor=anchor)
        vs = ttk.Scrollbar(card, orient='vertical', command=self.tree.yview, style='Card.Vertical.TScrollbar')
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True, padx=(p(4), 0), pady=p(4))
        self.tree.tag_configure('cur', foreground=T['accent'])
        self.tree.tag_configure('bad', foreground=T['faint'])
        self.tree.bind('<<TreeviewSelect>>', lambda e: self._on_select())
        self.tree.bind('<Double-1>', lambda e: self.use() if self.tree.identify_row(e.y) else None)
        self.info = tk.Label(self.body, text='', font=F.small, bg=bg, fg=T['muted'], wraplength=p(820),
                             justify='left', anchor='nw', height=3)     # fixed: the dialog does not grow
        self.info.pack(fill='x', pady=(p(8), 0))
        self.keep = tk.BooleanVar(value=app.session.get('base_keep', True))
        if P:
            Switch(self.body, app, 'Manter os módulos adicionados na seleção atual', self.keep).pack(
                anchor='w', pady=(p(14), 0))
        self.buttons([('Cancelar', 'ghost', self.close), ('Usar como base', 'primary', self.use)])
        Btn(self.bar, app, 'Procurar…', ICO['folder'], self.browse, 'ghost', height=36, padx=14).pack(side='left')
        Btn(self.bar, app, 'Gerar do original…', ICO['build'], self.make, 'ghost', height=36, padx=14).pack(
            side='left', padx=(p(8), 0))
        self.top.bind('<Return>', lambda e: self.use())
        paths = C.find_bases(app.session.get('bases', []) + ([P.base_path] if P else []))
        for path in paths:
            self._add(path)
        if not paths:
            self.info.configure(text='Nenhum firmware QME-200 em %s. Use "Procurar…".' % ', '.join(
                C.rel(d) for d in C.BASE_DIRS), fg=T['amber'])
        sel = next((i for i, r in self.rows.items() if r['key'] == self.cur), next(iter(self.rows), None))
        self.show()
        if sel:
            self.tree.selection_set(sel)
            self.tree.see(sel)
            self.tree.focus(sel)
        self.tree.focus_set()
        threading.Thread(target=self._probe_all, args=(paths,), daemon=True).start()

    def _add(self, path):
        st = os.stat(path)
        key = os.path.normcase(os.path.abspath(path))
        folder = C.rel(os.path.dirname(path))
        iid = self.tree.insert('', 'end', values=(
            ('●  ' if key == self.cur else '') + os.path.basename(path), 'raiz' if folder == '.' else folder,
            '…', '…', C.fmt_size(st.st_size), time.strftime('%d/%m/%Y %H:%M', time.localtime(st.st_mtime))),
            tags=('cur',) if key == self.cur else ())
        self.rows[iid] = dict(path=path, key=key, stamp=(key, st.st_mtime, st.st_size), info=None)
        return iid

    def _probe_all(self, paths):
        for iid, r in list(self.rows.items()):
            if not self.alive:
                return
            info = self.app.base_probe.get(r['stamp'])
            if info is None:
                info = self.app.base_probe[r['stamp']] = C.probe_base(r['path'])
            self.app.root.after(0, lambda iid=iid, info=info: self._fill(iid, info))

    def _fill(self, iid, info):
        if not self.alive or iid not in self.rows:
            return
        self.rows[iid]['info'] = info
        if info['error']:
            self.tree.set(iid, 'app', 'inválido')
            self.tree.set(iid, 'fx', '—')
            self.tree.item(iid, tags=('bad',))
        else:
            self.tree.set(iid, 'app', '+'.join(info['patches']) or self.APPS.get(info['app'], '?'))
            self.tree.set(iid, 'fx', str(info['effects']))
            self.tree.set(iid, 'lib', C.fmt_size(info['library']))
        if iid in self.tree.selection():
            self._on_select()

    def _on_select(self):
        sel = self.tree.selection()
        if not sel:
            return
        r = self.rows[sel[0]]
        info = r['info']
        text, fg = r['path'], T['muted']
        if info is None:
            text += '\nlendo…'
        elif info['error']:
            text, fg = text + '\nNão dá para usar como base: %s' % info['error'], T['red']
        else:
            labels = {f: l for f, l, _ in C.APP_PATCHES}
            text += '\n%s · %d efeitos · biblioteca com %s de %s%s' % (
                'app com patches: ' + ', '.join(labels[f] for f in info['patches']) if info['patches'] else
                APP_KINDS.get(info['app'], 'app ?'), info['effects'], C.fmt_size(info['library']),
                C.fmt_size(fwtool.MAX_LIBRARY), ' · roteamento paralelo (Split / Path B / Merge)' if info['routing']
                else '')
        if r['key'] == self.cur:
            text += '\nÉ a base atual: escolher de novo relê o arquivo e mantém a seleção.'
        self.info.configure(text=text, fg=fg)

    def browse(self):
        P = self.app.project
        f = filedialog.askopenfilename(parent=self.top, title='Firmware base (QME-200)',
                                       initialdir=os.path.dirname(P.base_path) if P else C.ROOT,
                                       filetypes=[('Firmware QME-200', '*.bin'), ('Todos', '*.*')])
        if not f:
            return
        if not C.is_base(f):
            self.info.configure(text='%s não é um firmware do QME-200.' % os.path.basename(f), fg=T['red'])
            return
        key = os.path.normcase(os.path.abspath(f))
        iid = next((i for i, r in self.rows.items() if r['key'] == key), None) or self._add(f)
        self.tree.selection_set(iid)
        self.tree.see(iid)
        r = self.rows[iid]
        if r['info'] is None:
            def work():
                info = self.app.base_probe[r['stamp']] = C.probe_base(r['path'])
                self.app.root.after(0, lambda: self._fill(iid, info))
            threading.Thread(target=work, daemon=True).start()

    def use(self):
        sel = self.tree.selection()
        if not sel:
            return
        r = self.rows[sel[0]]
        if r['info'] and r['info']['error']:
            self._on_select()
            return
        self.app.session['base_keep'] = bool(self.keep.get())
        self.close()
        self.app.set_base(r['path'], bool(self.keep.get()))

    def make(self):
        self.close()
        MakeBaseDialog(self.app)

    def close(self, result=None):
        self.alive = False
        super().close(result)


class LibraryDialog(Dialog):
    """the user's library folders: any folder holding module manifests can be added, renamed or removed"""

    def __init__(self, app):
        super().__init__(app, 'Bibliotecas de efeitos', 820)
        S, F = app.S, app.F
        p = lambda v: round(v * S)
        bg = T['panel']
        self.rows = {}
        tk.Label(self.body, text='Cada biblioteca é uma pasta com manifestos .json e módulos .axf. Um library.json '
                 'opcional na pasta ({"name", "color", "description"}) dá o nome e a cor padrão.', font=F.small,
                 bg=bg, fg=T['muted'], wraplength=p(760), justify='left').pack(anchor='w', pady=(0, p(12)))
        card = tk.Frame(self.body, bg=T['card'], highlightthickness=1, highlightbackground=T['cardline'])
        card.pack(fill='x')
        cols = ('name', 'folder', 'mods')
        self.tree = ttk.Treeview(card, columns=cols, show='headings', style='Fx.Treeview', selectmode='browse',
                                 height=8)
        heads = dict(name='NOME', folder='PASTA', mods='MÓDULOS')
        widths = dict(name=220, folder=420, mods=110)
        for c in cols:
            anchor = 'e' if c == 'mods' else 'w'
            self.tree.heading(c, text=heads[c], anchor=anchor)
            self.tree.column(c, width=p(widths[c]), stretch=c == 'folder', minwidth=p(40), anchor=anchor)
        vs = ttk.Scrollbar(card, orient='vertical', command=self.tree.yview, style='Card.Vertical.TScrollbar')
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True, padx=(p(4), 0), pady=p(4))
        self.tree.tag_configure('bad', foreground=T['faint'])
        self.tree.bind('<<TreeviewSelect>>', lambda e: self._on_select())
        tk.Label(self.body, text='NOME (ORIGEM DOS EFEITOS)', font=F.tiny, bg=bg, fg=T['muted']).pack(
            anchor='w', pady=(p(14), p(6)))
        self.name = tk.StringVar()
        fr = Field(self.body, app, self.name, '', search=False, height=34)
        fr.pack(fill='x')
        self.name_entry = fr.entry
        self.name_entry.bind('<KeyRelease>', lambda e: self._rename())
        self.info = tk.Label(self.body, text='', font=F.small, bg=bg, fg=T['muted'], wraplength=p(760),
                             justify='left', anchor='nw', height=3)
        self.info.pack(fill='x', pady=(p(8), 0))
        self.buttons([('Cancelar', 'ghost', self.close), ('Aplicar', 'primary', self.apply)])
        Btn(self.bar, app, 'Adicionar pasta…', ICO['add'], self.add, 'ghost', height=36, padx=14).pack(side='left')
        Btn(self.bar, app, 'Remover', ICO['remove'], self.remove, 'subtle', height=36, padx=12).pack(
            side='left', padx=(p(8), 0))
        self.top.bind('<Return>', lambda e: self.apply())
        for d, n in app.libraries():
            self._insert(C.absp(d), n)
        self.show()
        first = next(iter(self.tree.get_children()), None)
        if first:
            self.tree.selection_set(first)
            self.tree.focus(first)
        else:
            self._on_select()

    def _insert(self, path, name):
        path = os.path.abspath(path)
        ok = os.path.isdir(path)
        mods = sum(1 for _ in C.manifests(path)) if ok else 0
        iid = self.tree.insert('', 'end', values=(name, C.rel(path), str(mods) if ok else 'pasta não existe'),
                               tags=() if mods else ('bad',))
        self.rows[iid] = dict(path=path, key=os.path.normcase(path), name=name, mods=mods, ok=ok)
        return iid

    def _selected(self):
        sel = self.tree.selection()
        return (sel[0], self.rows[sel[0]]) if sel else (None, None)

    def _on_select(self):
        iid, r = self._selected()
        if r is None:
            self.name.set('')
            self.name_entry.configure(state='disabled')
            self.info.configure(text='Nenhuma biblioteca cadastrada. Use "Adicionar pasta…".', fg=T['muted'])
            return
        self.name_entry.configure(state='normal')
        self.name.set(r['name'])
        self._describe(r)

    def _describe(self, r, extra='', fg=None):
        meta = C.library_meta(r['path'])
        if not r['ok']:
            text, fg = r['path'] + '\nA pasta não existe mais: a biblioteca não contribui com módulos.', fg or T['amber']
        else:
            text = '%s\n%d módulo%s' % (r['path'], r['mods'], 's' if r['mods'] != 1 else '')
            if meta['description']:
                text += '  ·  ' + meta['description']
            if not r['mods']:
                text += '  ·  nenhum manifesto por enquanto (a pasta é relida a cada carregamento)'
        self.info.configure(text=text + ('\n' + extra if extra else ''), fg=fg or T['muted'])

    def _rename(self):
        iid, r = self._selected()
        if r is not None:
            r['name'] = self.name.get()
            self.tree.set(iid, 'name', r['name'])

    def add(self):
        d = filedialog.askdirectory(parent=self.top, title='Pasta da biblioteca', mustexist=True,
                                    initialdir=self.app.session.get('lib_dir', C.ROOT))
        if not d:
            return
        d = os.path.abspath(d)
        self.app.session['lib_dir'] = os.path.dirname(d)
        iid = next((i for i, r in self.rows.items() if r['key'] == os.path.normcase(d)), None)
        if iid:
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self.info.configure(text='%s já está cadastrada como "%s".' % (d, self.rows[iid]['name']), fg=T['amber'])
            return
        name = unique_name(C.library_meta(d)['name'], [r['name'] for r in self.rows.values()])
        iid = self._insert(d, name)
        self.tree.selection_set(iid)
        self.tree.see(iid)
        self.name_entry.focus_set()
        self.name_entry.select_range(0, 'end')

    def remove(self):
        iid, r = self._selected()
        if r is None:
            return
        kids = list(self.tree.get_children())
        k = kids.index(iid)
        self.tree.delete(iid)
        del self.rows[iid]
        kids.remove(iid)
        if kids:
            nxt = kids[min(k, len(kids) - 1)]
            self.tree.selection_set(nxt)
            self.tree.focus(nxt)
        else:
            self._on_select()

    def apply(self):
        libs, seen = [], {}
        for iid in self.tree.get_children():
            r = self.rows[iid]
            name = r['name'].strip()
            if not name or name.lower() in seen or name == C.FACTORY:
                self.tree.selection_set(iid)
                why = 'O nome não pode ficar vazio.' if not name else \
                    'O nome "%s" é reservado aos efeitos da base.' % name if name == C.FACTORY else \
                    'Outra biblioteca já se chama "%s".' % seen[name.lower()]
                self._describe(r, why, T['red'])
                self.name_entry.focus_set()
                return
            seen[name.lower()] = name
            libs.append((r['path'], name))
        self.close()
        self.app.set_libraries(libs)


class MakeBaseDialog(Dialog):
    """a base made from the original firmware: app patches (apppatch.py) + the routing modules"""

    def __init__(self, app):
        super().__init__(app, 'Gerar base do firmware original', 640)
        S, F = app.S, app.F
        p = lambda v: round(v * S)
        bg = T['panel']
        ses = app.session
        tk.Label(self.body, text='Aplica os patches do app ao firmware original (compilados com Zig, como o '
                 'apppatch.py) e acrescenta Split / Path B / Merge à seção DSP. IRs e presets vêm do original.',
                 font=F.small, bg=bg, fg=T['muted'], wraplength=p(580), justify='left').pack(anchor='w')
        tk.Label(self.body, text='FIRMWARE ORIGINAL', font=F.tiny, bg=bg, fg=T['muted']).pack(
            anchor='w', pady=(p(18), p(6)))
        src = ses.get('mkbase_src') or C.ORIGINAL_FW
        self.src = tk.StringVar(value=src if os.path.exists(src) else C.ORIGINAL_FW)
        self._path_row(self.src, self._browse_src)
        tk.Label(self.body, text='PATCHES DO APP', font=F.tiny, bg=bg, fg=T['muted']).pack(
            anchor='w', pady=(p(18), p(6)))
        chosen = ses.get('mkbase_features', ['parallel'])
        self.feats, self.sw_feats = {}, {}
        for f, label, desc in C.APP_PATCHES:
            self.feats[f] = tk.BooleanVar(value=f in chosen)
            self.sw_feats[f] = Switch(self.body, app, label, self.feats[f], self._sync)
            self.sw_feats[f].pack(anchor='w', pady=(p(6), 0))
            tk.Label(self.body, text=desc, font=F.small, bg=bg, fg=T['faint'], wraplength=p(540),
                     justify='left').pack(anchor='w', padx=(p(44), 0))
        tk.Label(self.body, text='SEÇÃO DSP', font=F.tiny, bg=bg, fg=T['muted']).pack(
            anchor='w', pady=(p(18), p(6)))
        self.routing = tk.BooleanVar(value=ses.get('mkbase_routing', True))
        self.sw_routing = Switch(self.body, app, 'Adicionar Split / Path B / Merge', self.routing, self._sync)
        self.sw_routing.pack(anchor='w')
        self.routing_note = tk.Label(self.body, text='', font=F.small, bg=bg, fg=T['faint'], wraplength=p(540),
                                     justify='left')
        self.routing_note.pack(anchor='w', padx=(p(44), 0))
        tk.Label(self.body, text='ARQUIVO DE SAÍDA', font=F.tiny, bg=bg, fg=T['muted']).pack(
            anchor='w', pady=(p(18), p(6)))
        self.out = tk.StringVar()
        self.auto_out = True                    # the name follows the choices until the user picks one
        self._path_row(self.out, self._browse_out)
        self.out.trace_add('write', lambda *a: setattr(self, 'auto_out', self._setting))
        self._setting = False
        self.status = tk.Label(self.body, text='', font=F.semi, bg=bg, fg=T['accent'], wraplength=p(580),
                               justify='left')
        self.status.pack(anchor='w', pady=(p(14), 0))
        self.buttons([('Cancelar', 'ghost', self.close), ('Gerar base', 'primary', self.run)])
        self.src.trace_add('write', lambda *a: self._sync())
        self._sync()
        self.show()

    def features(self):
        return [f for f, _, _ in C.APP_PATCHES if self.feats[f].get()]

    def _sync(self):
        prev = getattr(self, '_prev_feats', set())
        for f, reqs in C.APP_PATCHES_REQUIRE.items():   # turning a dependency off turns the feature off too
            for r in reqs:
                if self.feats[f].get() and not self.feats[r].get():
                    v = self.feats[f] if r in prev and f in prev else self.feats[r]
                    v.set(v is self.feats[r])
                    self.sw_feats[f].draw()
                    self.sw_feats[r].draw()
        feats = self.features()
        self._prev_feats = set(feats)
        if 'parallel' in feats and not self.routing.get():
            self.routing.set(True)
        self.sw_routing.draw()
        self.routing_note.configure(text='Obrigatório com a GUI paralela.' if 'parallel' in feats else
                                    'Sem a GUI paralela, os três aparecem como efeitos comuns do menu EQ e a '
                                    'tela os desenha em série.' if self.routing.get() else
                                    'A seção DSP fica igual à do original.')
        if self.auto_out:
            self._setting = True
            self.out.set(os.path.normpath(C.base_name(self.src.get().strip() or C.ORIGINAL_FW, feats,
                                                      self.routing.get())))
            self._setting = False
        if hasattr(self, 'btns'):
            self.btns[0].config_btn(enabled=bool(feats or self.routing.get()))

    def _browse_src(self):
        f = filedialog.askopenfilename(parent=self.top, title='Firmware original (QME-200)',
                                       initialdir=os.path.dirname(self.src.get()) or C.ROOT,
                                       filetypes=[('Firmware QME-200', '*.bin'), ('Todos', '*.*')])
        if f:
            self.src.set(os.path.normpath(f))

    def _browse_out(self):
        f = filedialog.asksaveasfilename(parent=self.top, title='Salvar base', defaultextension='.bin',
                                         initialdir=os.path.dirname(self.out.get()),
                                         initialfile=os.path.basename(self.out.get()),
                                         filetypes=[('Firmware QME-200', '*.bin')])
        if f:
            self.out.set(os.path.normpath(f))

    def run(self):
        src, out, feats = self.src.get().strip(), self.out.get().strip(), self.features()
        if not C.is_base(src):
            self.status.configure(text='%s não é um firmware do QME-200.' % (os.path.basename(src) or 'Origem'),
                                  fg=T['red'])
            return
        if not out:
            return
        if os.path.normcase(os.path.abspath(out)) == os.path.normcase(os.path.abspath(src)):
            self.status.configure(text='Escolha um arquivo de saída diferente do original.', fg=T['red'])
            return
        for b in self.btns:
            b.config_btn(enabled=False)
        self.status.configure(text='Compilando os patches e montando a base…', fg=T['accent'])
        self.app.session.update(mkbase_src=src, mkbase_features=feats, mkbase_routing=bool(self.routing.get()))
        routing = bool(self.routing.get())

        def work():
            try:
                res = C.make_base(src, out, feats, routing)
                self.top.after(0, lambda: self.done(res))
            except Exception as ex:
                msg = str(ex) or type(ex).__name__
                self.top.after(0, lambda: self.fail(msg))
        threading.Thread(target=work, daemon=True).start()

    def fail(self, msg):
        self.status.configure(text='Falhou: ' + msg, fg=T['red'])
        for b in self.btns:
            b.config_btn(enabled=True)

    def done(self, res):
        S, F = self.app.S, self.app.F
        p = lambda v: round(v * S)
        for w in self.body.winfo_children():
            w.destroy()
        bg = T['panel']
        head = tk.Frame(self.body, bg=bg)
        head.pack(fill='x')
        badge = tk.Canvas(head, width=p(44), height=p(44), bg=bg, highlightthickness=0)
        aa.oval(badge, p(22), p(22), p(21), fill=mix(T['green'], bg, .8))
        if F.icons:
            badge.create_text(p(22), p(22), text=ICO['check'], font=F.icon_big, fill=T['green'])
        badge.pack(side='left')
        tb = tk.Frame(head, bg=bg)
        tb.pack(side='left', padx=p(14))
        tk.Label(tb, text='Base gerada', font=F.h3, bg=bg, fg=T['text']).pack(anchor='w')
        tk.Label(tb, text='Checksum e cabeçalho conferidos; IRs e presets idênticos aos do original.', font=F.small,
                 bg=bg, fg=T['muted']).pack(anchor='w')
        labels = {f: l for f, l, _ in C.APP_PATCHES}
        pt = res['patch']
        app_txt = ', '.join(labels[f] for f in res['features']) + (
            '  (%s de código em ITCM)' % C.fmt_size(pt['blob'])) if pt else 'original (sem patches)'
        if pt and pt['restarted']:
            app_txt += '; app de partida: %s' % os.path.basename(C.ORIGINAL_FW)
        rout = ', '.join(res['added']) if res['added'] else 'não adicionados'
        if res['present']:
            rout += ' (já presentes: %s)' % ', '.join(res['present'])
        rows = [('Arquivo', res['path']), ('Tamanho', '%s (%d bytes)' % (C.fmt_size(res['size']), res['size'])),
                ('App', app_txt), ('Roteamento', rout),
                ('Biblioteca', '%d efeitos, %s de %s' % (res['effects'], C.fmt_size(res['library']),
                                                          C.fmt_size(fwtool.MAX_LIBRARY)))]
        g = tk.Frame(self.body, bg=bg)
        g.pack(fill='x', pady=(p(18), 0))
        g.columnconfigure(1, weight=1)
        for r, (k, v) in enumerate(rows):
            tk.Label(g, text=k, font=F.body, bg=bg, fg=T['muted']).grid(row=r, column=0, sticky='nw', pady=p(3))
            tk.Label(g, text=v, font=F.semi, bg=bg, fg=T['text'], wraplength=round(470 * S), justify='left').grid(
                row=r, column=1, sticky='w', padx=(p(16), 0), pady=p(3))
        if 'clonedual' in res['features']:
            tk.Label(self.body, text='O motor NAM do Clone entra ao gerar o firmware: escolha "Fábrica + NAM A2" '
                     'em MOTOR DO CLONE.', font=F.small, bg=bg, fg=T['faint'], wraplength=round(580 * S),
                     justify='left').pack(anchor='w', pady=(p(14), 0))
        self.buttons([('Mostrar na pasta', 'ghost', lambda: reveal(res['path'])), ('Fechar', 'ghost', self.close),
                      ('Usar como base', 'primary', lambda: self.use(res['path']))])
        self.top.update_idletasks()
        self.top.geometry('')
        self.app.toast_msg('Base gerada: %s' % os.path.basename(res['path']), 'ok')

    def use(self, path):
        self.close()
        self.app.set_base(path, self.app.session.get('base_keep', True))


class BuildDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, 'Gerar firmware', 640)
        S, F = app.S, app.F
        p = lambda v: round(v * S)
        P = app.project
        bg = T['panel']
        total, idx, base, added = P.lib_size()
        add = [it for it in P.catalog if it.selected]
        rem = [it for it in P.base_items if not it.selected]
        # summary cards
        cards = tk.Frame(self.body, bg=bg)
        cards.pack(fill='x')
        for k, (lab, val, col) in enumerate((('Efeitos', str(len(P.selected())), T['text']),
                                             ('Adicionados', '+%d' % len(add), T['green']),
                                             ('Removidos', '−%d' % len(rem), T['red']),
                                             ('Biblioteca', '%.0f%%' % (100 * total / fwtool.MAX_LIBRARY), T['accent']))):
            c = Card(cards, app, T['card'], T['cardline'], 10)
            c.grid(row=0, column=k, sticky='nsew', padx=(0 if k == 0 else p(8), 0))
            cards.columnconfigure(k, weight=1, uniform='c')
            tk.Label(c.body, text=val, font=F.h2, bg=T['card'], fg=col).pack(anchor='w', padx=p(11), pady=(p(7), 0))
            tk.Label(c.body, text=lab, font=F.small, bg=T['card'], fg=T['muted']).pack(anchor='w', padx=p(11),
                                                                                       pady=(0, p(7)))
        # output path
        tk.Label(self.body, text='ARQUIVO DE SAÍDA', font=F.tiny, bg=bg, fg=T['muted']).pack(
            anchor='w', pady=(p(20), p(6)))
        last = app.session.get('out_path')
        default = last or C.paths.build_path('QME-200_FW_USB_V1.2.2_custom.bin')
        self.out = tk.StringVar(value=default)
        self._path_row(self.out, self._browse_out)
        # editor json
        self.ed = tk.BooleanVar(value=app.session.get('editor', False) and os.path.exists(C.DEFAULT_EDITOR_JSON))
        self.ed_src = tk.StringVar(value=app.session.get('editor_src', C.DEFAULT_EDITOR_JSON))
        Switch(self.body, app, 'Gerar também a lista do editor de PC (alg_data.json)', self.ed,
               self._sync).pack(anchor='w', pady=(p(18), p(8)))
        self.ed_box = tk.Frame(self.body, bg=bg)
        self.ed_box.pack(fill='x')
        tk.Label(self.ed_box, text='Lista de partida (do editor instalado ou da cópia em editor_patched):',
                 font=F.small, bg=bg, fg=T['muted']).pack(anchor='w', pady=(0, p(4)))
        self._path_row(self.ed_src, self._browse_ed, parent=self.ed_box)
        tk.Label(self.ed_box, text='Saída: ao lado do .bin, como <nome>_alg_data.json. Copie para '
                 'data\\flutter_assets\\assets\\alg\\ de uma cópia do editor.', font=F.small, bg=bg,
                 fg=T['faint'], wraplength=round(580 * S), justify='left').pack(anchor='w', pady=(p(4), 0))
        # Clone engines (menu 11)
        tk.Label(self.body, text='MOTOR DO CLONE', font=F.tiny, bg=bg, fg=T['muted']).pack(
            anchor='w', pady=(p(18), p(6)))
        self.clone_ok = [m for m, why in P.clone_modes() if not why]
        cur = P.clone_target()
        vals = [C.CLONE_LABELS[m] for m in self.clone_ok] + ([] if cur in self.clone_ok else [C.CLONE_LABELS[cur]])
        Dropdown(self.body, app, vals, C.CLONE_LABELS[cur], self._set_clone, width=380).pack(anchor='w')
        self.clone_note = tk.Label(self.body, text='', font=F.small, bg=bg, fg=T['faint'], wraplength=round(580 * S),
                                   justify='left')
        self.clone_note.pack(anchor='w', pady=(p(4), 0))
        self._clone_note()
        # warnings
        iss = P.issues()
        if iss:
            tk.Label(self.body, text='VERIFICAÇÃO', font=F.tiny, bg=bg, fg=T['muted']).pack(
                anchor='w', pady=(p(18), p(6)))
            for lvl, t in iss:
                tk.Label(self.body, text=('✖  ' if lvl == 'erro' else '!  ') + t, font=F.small, bg=bg,
                         fg=T['red'] if lvl == 'erro' else T['amber'], wraplength=round(580 * S),
                         justify='left').pack(anchor='w', pady=p(1))
        note = 'Só a seção DSP muda; app, IRs e presets vêm de %s. Nada é gravado no pedal.' % \
               os.path.basename(P.base_path)
        tk.Label(self.body, text=note, font=F.small, bg=bg, fg=T['faint'], wraplength=round(580 * S),
                 justify='left').pack(anchor='w', pady=(p(16), 0))
        self.status = tk.Label(self.body, text='', font=F.semi, bg=bg, fg=T['accent'])
        self.status.pack(anchor='w', pady=(p(8), 0))
        self.buttons([('Cancelar', 'ghost', self.close), ('Gerar firmware', 'primary', self.run)])
        if any(l == 'erro' for l, t in iss):
            self.btns[0].config_btn(enabled=False)
        self._sync()
        self.show()

    CLONE_NOTES = {
        'factory': 'Os 10 slots rodam o Clone de fábrica; um slot com captura NAM fica mudo.',
        'nam': 'Os 10 slots rodam o NAM A2; um slot com captura de fábrica passa o sinal seco.',
        'dual': 'Cada slot roda o motor da captura que guarda: NAM A2 para as gravadas pela aba Capturas NAM, '
                'fábrica para as outras.',
        '?': 'O menu Clone da base não é nenhum dos conhecidos; ele fica como está.'}

    def _set_clone(self, label):
        P = self.app.project
        mode = next((m for m, l in C.CLONE_LABELS.items() if l == label), None)
        P.clone_mode = None if mode in (None, P.clone_base) else mode
        self._clone_note()

    def _clone_note(self):
        P = self.app.project
        cur = P.clone_target()
        text = self.CLONE_NOTES.get(cur, '')
        if cur != P.clone_base:
            text += ' A base tem: %s.' % C.CLONE_LABELS[P.clone_base]
        for m, why in P.clone_modes():
            if why:
                text += '\n%s: indisponível (%s).' % (C.CLONE_LABELS[m], why)
        self.clone_note.configure(text=text)

    def _sync(self):
        st = 'normal' if self.ed.get() else 'disabled'
        stack = list(self.ed_box.winfo_children())
        while stack:
            w = stack.pop()
            stack += w.winfo_children()
            if isinstance(w, tk.Entry):
                w.configure(state=st, disabledforeground=T['faint'])

    def _browse_out(self):
        f = filedialog.asksaveasfilename(parent=self.top, title='Salvar firmware', defaultextension='.bin',
                                         initialdir=os.path.dirname(self.out.get()),
                                         initialfile=os.path.basename(self.out.get()),
                                         filetypes=[('Firmware QME-200', '*.bin')])
        if f:
            self.out.set(os.path.normpath(f))

    def _browse_ed(self):
        f = filedialog.askopenfilename(parent=self.top, title='alg_data.json de partida',
                                       initialdir=os.path.dirname(self.ed_src.get()),
                                       filetypes=[('alg_data.json', '*.json')])
        if f:
            self.ed_src.set(os.path.normpath(f))

    def run(self):
        out = self.out.get().strip()
        if not out:
            return
        if os.path.abspath(out) == os.path.abspath(self.app.project.base_path):
            self.status.configure(text='Escolha um arquivo diferente do firmware base.', fg=T['red'])
            return
        ed = self.ed.get() and self.ed_src.get().strip()
        if ed and not os.path.exists(ed):
            self.status.configure(text='alg_data.json de partida não encontrado.', fg=T['red'])
            return
        ed_out = os.path.splitext(out)[0] + '_alg_data.json' if ed else None
        for b in self.btns:
            b.config_btn(enabled=False)
        self.status.configure(text='Gerando…', fg=T['accent'])
        self.app.session.update(out_path=out, editor=bool(self.ed.get()), editor_src=self.ed_src.get())

        def work():
            try:
                res = self.app.project.build(out, ed, ed_out)
                self.top.after(0, lambda: self.done(res))
            except Exception as ex:
                msg = str(ex)
                self.top.after(0, lambda: self.fail(msg))
        threading.Thread(target=work, daemon=True).start()

    def fail(self, msg):
        self.status.configure(text='Falhou: ' + msg, fg=T['red'])
        for b in self.btns:
            b.config_btn(enabled=True)

    def done(self, res):
        S, F = self.app.S, self.app.F
        p = lambda v: round(v * S)
        for w in self.body.winfo_children():
            w.destroy()
        bg = T['panel']
        head = tk.Frame(self.body, bg=bg)
        head.pack(fill='x')
        badge = tk.Canvas(head, width=p(44), height=p(44), bg=bg, highlightthickness=0)
        aa.oval(badge, p(22), p(22), p(21), fill=mix(T['green'], bg, .8))
        if F.icons:
            badge.create_text(p(22), p(22), text=ICO['check'], font=F.icon_big, fill=T['green'])
        badge.pack(side='left')
        tb = tk.Frame(head, bg=bg)
        tb.pack(side='left', padx=p(14))
        tk.Label(tb, text='Firmware gerado', font=F.h3, bg=bg, fg=T['text']).pack(anchor='w')
        tk.Label(tb, text='Checksum e cabeçalho conferidos (o editor aceita o arquivo).', font=F.small, bg=bg,
                 fg=T['muted']).pack(anchor='w')
        rows = [('Arquivo', res['path']), ('Tamanho', '%s (%d bytes)' % (C.fmt_size(res['size']), res['size'])),
                ('Biblioteca', '%s de %s' % (C.fmt_size(res['library']), C.fmt_size(fwtool.MAX_LIBRARY))),
                ('Efeitos', '%d  (+%d / −%d)' % (res['effects'], res['added'], res['removed']))]
        if res.get('editor'):
            e = res['editor']
            rows.append(('Lista do editor', '%s  (+%d / −%d)' % (e['path'], e['added'], e['dropped'])))
        g = tk.Frame(self.body, bg=bg)
        g.pack(fill='x', pady=(p(18), 0))
        g.columnconfigure(1, weight=1)
        for r, (k, v) in enumerate(rows):
            tk.Label(g, text=k, font=F.body, bg=bg, fg=T['muted']).grid(row=r, column=0, sticky='nw', pady=p(3))
            tk.Label(g, text=v, font=F.semi, bg=bg, fg=T['text'], wraplength=round(470 * S), justify='left').grid(
                row=r, column=1, sticky='w', padx=(p(16), 0), pady=p(3))
        tk.Label(self.body, text='Toda atualização regrava o Bank: os presets do pedal são substituídos pelos '
                 'deste arquivo.', font=F.small, bg=bg, fg=T['amber'], wraplength=round(580 * S),
                 justify='left').pack(anchor='w', pady=(p(16), 0))
        self.buttons([('Mostrar na pasta', 'ghost', lambda: reveal(res['path'])),
                      ('Enviar ao pedal', 'ghost', lambda: (self.close(), self.app.open_update(res['path']))),
                      ('Concluir', 'primary', self.close)])
        self.top.update_idletasks()
        self.top.geometry('')
        self.app.toast_msg('Firmware gerado: %s' % os.path.basename(res['path']), 'ok')


# ------------------------------------------------------------------------------------------------ platform
def reveal(path):
    try:
        if sys.platform == 'win32':
            subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
        else:
            subprocess.Popen(['xdg-open', os.path.dirname(path)])
    except Exception:
        pass


def dark_titlebar(win):
    if sys.platform != 'win32':
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4) == 0:
                break
        bg = T['panel']
        cref = ctypes.c_int(int(bg[5:7], 16) << 16 | int(bg[3:5], 16) << 8 | int(bg[1:3], 16))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cref), 4)
    except Exception:
        pass


def main():
    if sys.platform == 'win32':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('rea.matribox.studio')
        except Exception:
            pass
    load_fonts()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
