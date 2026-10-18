"""Matribox Studio, aba Global: o EQ global e os ajustes globais do pedal, como as duas páginas do menu de
configurações do editor original (global_eq_page.dart e global_settings_page.dart).

Tudo vem do pedal e vai para ele na hora (os comandos 0x11000000/1 e 0x16000000/1/2, ver qmepreset.py); o pedal
guarda os ajustes sozinho. Mudanças feitas nos botões do pedal chegam como avisos e atualizam a tela.

O EQ tem 31 parâmetros no firmware. A tela do editor mostra Low Cut, as bandas 1 a 4 e High Cut; os dois shelves
existem no firmware mas o editor os esconde. Aqui eles só aparecem se estiverem ligados no pedal, para a curva
não mostrar um efeito sem controle. A curva é a do editor: biquads do Audio EQ Cookbook a 44,1 kHz.

A porta MIDI é a da aba Pedal e presets (pedalview.Worker), com o mesmo indicador de conexão no topo.
"""
import cmath, math
import tkinter as tk

from matribox.studio import aa
from matribox.studio import pedalview as PV
from matribox.usb import qmepreset as Q

SEND_EVERY = 60                     # ms between messages while a control is dragged
FS = 44100.0
F_LO, F_HI = 20.0, 20000.0
DB_RANGE = 25.0                     # the editor's graph: -25..+25 dB

# bands of the EQ: parameter indexes (EQ_PARAMS) and the editor's colours
BANDS = [
    dict(key='locut', name='Low Cut', kind='hpf', sw=0, f=1, q=2, g=None, color='#d63f3f', tag='L'),
    dict(key='loshelf', name='Low Shelf', kind='lshelf', sw=3, f=4, q=5, g=6, color='#d6783e', tag='LS',
         hidden=True),
    dict(key='b1', name='Banda 1', kind='peak', sw=7, f=8, q=9, g=10, color='#a6d63e', tag='1'),
    dict(key='b2', name='Banda 2', kind='peak', sw=11, f=12, q=13, g=14, color='#3ed657', tag='2'),
    dict(key='b3', name='Banda 3', kind='peak', sw=15, f=16, q=17, g=18, color='#3ed6c9', tag='3'),
    dict(key='b4', name='Banda 4', kind='peak', sw=19, f=20, q=21, g=22, color='#3e57d6', tag='4'),
    dict(key='hishelf', name='High Shelf', kind='hshelf', sw=23, f=24, q=25, g=26, color='#943ed6', tag='HS',
         hidden=True),
    dict(key='hicut', name='High Cut', kind='lpf', sw=27, f=28, q=29, g=None, color='#d23dd2', tag='H'),
]
LEVEL = 30

FS_FUNCS = [(0, 'Nenhuma'), (1, 'CTRL 1'), (2, 'CTRL 2'), (3, 'CTRL 3'), (4, 'CTRL 4'), (8, 'Tap tempo'),
            (5, 'Looper'), (6, 'Bateria liga/desliga'), (19, 'Bateria: ritmo anterior'), (18, 'Bateria: próximo ritmo'),
            (7, 'Afinador'), (20, 'EXP 1 A/B'), (12, 'Preset anterior'), (11, 'Próximo preset')]
CH_IN = [(0, 'OMNI')] + [(k, str(k)) for k in range(1, 17)]
CH_OUT = [(-1, 'Desligado'), (0, 'OMNI')] + [(k, str(k)) for k in range(1, 17)]

# the settings page, section by section, in the editor's order
SECTIONS = [
    ('Entrada e saída', [
        ('dd', 'input_mode', 'Modo de entrada', [(0, 'Violão (A.GT)'), (1, 'Guitarra (E.GT)'), (2, 'Linha')]),
        ('slider', 'input_level', 'Ganho de entrada', -20, 20),
        ('toggle', 'nocab_l', 'Sem simulação de caixa na saída L'),
        ('toggle', 'nocab_r', 'Sem simulação de caixa na saída R')]),
    ('Áudio USB', [
        ('slider', 'rec_level', 'Volume de gravação', -20, 20),
        ('dd', 'rec_l', 'Gravação, canal L', [(0, 'Seco'), (1, 'Com efeitos')]),
        ('dd', 'rec_r', 'Gravação, canal R', [(0, 'Seco'), (1, 'Com efeitos')]),
        ('toggle', 'rec_aux', 'AUX IN também vai para o USB'),
        ('slider', 'monitor_level', 'Volume do monitor', -20, 20)]),
    ('Funções', [
        ('dd', 'work_mode', 'Modo de trabalho', [(0, 'Stomp'), (1, 'Preset')]),
        ('dd', 'bank_mode', 'Troca de banco', [(0, 'Pré-seleção (Wait)'), (1, 'Imediata (Initial)')]),
        ('toggle', 'auto_cab', 'CAB acompanha o AMP escolhido')]),
    ('EXP / footswitch externo', [
        ('dd', 'exp_mode', 'Modo', [(0, 'Pedal de expressão'), (1, '1 footswitch'), (2, '2 footswitches')]),
        ('dd', 'fs1', 'Footswitch 1', FS_FUNCS),
        ('dd', 'fs2', 'Footswitch 2', FS_FUNCS)]),
    ('MIDI', [
        ('dd', 'midi_source', 'Entrada MIDI', [(0, 'Só TRS'), (1, 'Só USB'), (2, 'TRS e USB')]),
        ('dd', 'midi_in_trs', 'Canal de entrada (TRS)', CH_IN),
        ('dd', 'midi_in_usb', 'Canal de entrada (USB)', CH_IN),
        ('dd', 'midi_out_trs', 'Canal de saída (TRS)', CH_OUT),
        ('dd', 'midi_out_usb', 'Canal de saída (USB)', CH_OUT),
        ('dd', 'clock_source', 'Fonte do clock', [(0, 'Interno'), (1, 'Só TRS'), (2, 'Só USB'), (3, 'Externo'),
                                                  (4, 'TRS e USB')]),
        ('bit', 'clock_out', 'Enviar clock pelo TRS', 1),
        ('bit', 'clock_out', 'Enviar clock pelo USB', 2)]),
]
COLUMNS = (('Entrada e saída', 'Áudio USB'), ('Funções', 'EXP / footswitch externo', 'MIDI'))
TYPE_KEY = {t: k for k, (t, _o, _f) in Q.GLOBAL_TYPES.items()}
TYPE_KEY[0x5100] = 'bank_mode'


# ------------------------------------------------------------------------------------------------ EQ maths
def biquad(kind, f0, q, gain):
    """Audio EQ Cookbook coefficients (b0, b1, b2, a0, a1, a2) at 44.1 kHz, as the editor's graph"""
    A = 10 ** (gain / 40)
    w0 = 2 * math.pi * f0 / FS
    cw, alpha = math.cos(w0), math.sin(w0) / (2 * q)
    if kind == 'hpf':
        return (1 + cw) / 2, -(1 + cw), (1 + cw) / 2, 1 + alpha, -2 * cw, 1 - alpha
    if kind == 'lpf':
        return (1 - cw) / 2, 1 - cw, (1 - cw) / 2, 1 + alpha, -2 * cw, 1 - alpha
    if kind == 'peak':
        return 1 + alpha * A, -2 * cw, 1 - alpha * A, 1 + alpha / A, -2 * cw, 1 - alpha / A
    sa = 2 * math.sqrt(A) * alpha
    if kind == 'lshelf':
        return (A * ((A + 1) - (A - 1) * cw + sa), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - sa),
                (A + 1) + (A - 1) * cw + sa, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - sa)
    return (A * ((A + 1) + (A - 1) * cw + sa), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - sa),
            (A + 1) - (A - 1) * cw + sa, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sa)


def band_active(b, eq):
    if eq[b['sw']] < .5:
        return False
    f = eq[b['f']]
    return not (b['kind'] == 'hpf' and f <= Q.EQ_LOCUT_OFF or b['kind'] == 'lpf' and f >= Q.EQ_HICUT_OFF)


def band_db(b, eq, freqs):
    c = biquad(b['kind'], eq[b['f']], max(.1, eq[b['q']]), eq[b['g']] if b['g'] is not None else 0)
    out = []
    for f in freqs:
        z = cmath.exp(-2j * math.pi * f / FS)
        h = (c[0] + c[1] * z + c[2] * z * z) / (c[3] + c[4] * z + c[5] * z * z)
        out.append(20 * math.log10(max(abs(h), 1e-9)))
    return out


def freq_txt(v, idx=None):
    if idx == 1 and v <= Q.EQ_LOCUT_OFF or idx == 28 and v >= Q.EQ_HICUT_OFF:
        return 'Desligado'
    if v < 1000:
        return '%d Hz' % round(v)
    return ('%.1f kHz' % (v / 1000)).replace('.0 kHz', ' kHz').replace('.', ',')


def db_txt(v):
    return '%+d dB' % round(v) if round(v) else '0 dB'


# ------------------------------------------------------------------------------------------------ widgets
class Slider(tk.Canvas):
    """the design's slider (4 px track, coloured fill, white thumb); command(value, final). log: logarithmic
    scale; center: the fill starts there (bipolar values); a double click sets default; the wheel steps"""
    def __init__(self, parent, app, W, lo, hi, value, command, color, log=False, center=None, default=None,
                 height=20, step=None):
        self.pbg = parent.cget('bg')
        super().__init__(parent, height=round(height * app.S), bg=self.pbg, highlightthickness=0, width=1,
                         cursor='sb_h_double_arrow')
        self.app, self.W = app, W
        self.lo, self.hi, self.value, self.command, self.color = lo, hi, value, command, color
        self.log, self.center, self.default, self.step = log, center, default, step
        self.enabled, self.wheel_job = True, None
        self.bind('<Configure>', lambda e: self.draw())
        self.bind('<Button-1>', lambda e: self._drag(e, False))
        self.bind('<B1-Motion>', lambda e: self._drag(e, False))
        self.bind('<ButtonRelease-1>', lambda e: self._drag(e, True))
        self.bind('<Double-Button-1>', self._reset)
        self.bind('<MouseWheel>', self._wheel)

    def p(self, v):
        return round(v * self.app.S)

    def frac(self, v):
        v = min(self.hi, max(self.lo, v))
        if self.log:
            return math.log(v / self.lo) / math.log(self.hi / self.lo)
        return (v - self.lo) / (self.hi - self.lo)

    def value_at(self, frac):
        frac = min(1.0, max(0.0, frac))
        return self.lo * (self.hi / self.lo) ** frac if self.log else self.lo + frac * (self.hi - self.lo)

    def set(self, value, enabled=None):
        self.value = value
        if enabled is not None:
            self.enabled = enabled
            self.configure(cursor='sb_h_double_arrow' if enabled else '')
        self.draw()

    def draw(self):
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        T, p = self.W.T, self.p
        r = p(7)
        y0, y1 = h / 2 - p(2), h / 2 + p(2)
        x = r + (w - 2 * r) * self.frac(self.value)
        col = self.color if self.enabled else T['dim']
        shapes = [('rrect', (0, y0, w, y1, p(2)), T['line2'])]
        if self.center is not None:
            xc = r + (w - 2 * r) * self.frac(self.center)
            if abs(x - xc) > .5:
                shapes.append(('rrect', (min(x, xc), y0, max(x, xc), y1, p(2)), col))
        elif x > r:
            shapes.append(('rrect', (0, y0, max(p(4), x), y1, p(2)), col))
        shapes += [('circle', (x, h / 2 + p(1), r), self.W.mix('#000000', self.pbg, .6)),
                   ('circle', (x, h / 2, r), T['bright'] if self.enabled else T['muted'])]
        aa.draw(self, 0, 0, w, h, shapes, self.pbg)

    def _drag(self, e, final):
        if not self.enabled:
            return
        r = self.p(7)
        v = self.value_at((e.x - r) / max(1, self.winfo_width() - 2 * r))
        self.set(v)
        self.command(v, final)

    def _reset(self, e):
        if self.enabled and self.default is not None:
            self.set(self.default)
            self.command(self.default, True)

    def _wheel(self, e):
        if not self.enabled:
            return 'break'
        d = 1 if e.delta > 0 else -1
        if self.step and not self.log:
            v = self.value + d * self.step
        else:
            v = self.value_at(self.frac(self.value) + d * .01)
        v = min(self.hi, max(self.lo, v))
        self.set(v)
        self.command(v, False)
        if self.wheel_job:
            self.after_cancel(self.wheel_job)
        self.wheel_job = self.after(350, lambda: (setattr(self, 'wheel_job', None), self.command(self.value, True)))
        return 'break'


class EqGraph(tk.Canvas):
    """the response of the global EQ: grid, the sum of the active bands, each band's own curve, and a draggable
    point per band (bands: frequency and gain; cuts: frequency). Wheel over a point: Q; double click: gain 0"""
    N = 220

    def __init__(self, parent, view):
        self.view, self.W, self.app = view, view.W, view.app
        super().__init__(parent, bg=view.W.T['card'], highlightthickness=0, height=round(300 * view.S))
        self.freqs = [F_LO * (F_HI / F_LO) ** (k / (self.N - 1)) for k in range(self.N)]
        self.nodes = []                 # (x, y, band)
        self.drag = self.hover = None
        self.cache = {}                 # band key -> (params, curve)
        self.bind('<Configure>', lambda e: self.redraw())
        self.bind('<Motion>', self._motion)
        self.bind('<Leave>', lambda e: self._set_hover(None))
        self.bind('<Button-1>', self._press)
        self.bind('<B1-Motion>', self._move)
        self.bind('<ButtonRelease-1>', self._release)
        self.bind('<Double-Button-1>', self._double)
        self.bind('<MouseWheel>', self._wheel)

    def p(self, v):
        return round(v * self.view.S)

    def box(self):
        w, h = self.winfo_width(), self.winfo_height()
        return self.p(38), self.p(14), w - self.p(16), h - self.p(26)

    def fx(self, f):
        x1, _y1, x2, _y2 = self.box()
        return x1 + (x2 - x1) * math.log(min(F_HI, max(F_LO, f)) / F_LO) / math.log(F_HI / F_LO)

    def fy(self, db):
        _x1, y1, _x2, y2 = self.box()
        return y1 + (y2 - y1) * (DB_RANGE - max(-DB_RANGE, min(DB_RANGE, db))) / (2 * DB_RANGE)

    def f_at(self, x):
        x1, _y1, x2, _y2 = self.box()
        return F_LO * (F_HI / F_LO) ** ((x - x1) / max(1, x2 - x1))

    def db_at(self, y):
        _x1, y1, _x2, y2 = self.box()
        return DB_RANGE - (y - y1) / max(1, y2 - y1) * 2 * DB_RANGE

    def curve(self, b, eq):
        key = (eq[b['f']], eq[b['q']], eq[b['g']] if b['g'] is not None else 0)
        c = self.cache.get(b['key'])
        if c is None or c[0] != key:
            c = self.cache[b['key']] = (key, band_db(b, eq, self.freqs))
        return c[1]

    def redraw(self):
        self.delete('all')
        v = self.view
        eq = v.eq
        w, h = self.winfo_width(), self.winfo_height()
        if w < 60 or h < 60 or eq is None:
            return
        T, F, p = self.W.T, self.app.F, self.p
        x1, y1, x2, y2 = self.box()
        for f, t in ((20, '20'), (50, '50'), (100, '100'), (200, '200'), (500, '500'), (1000, '1k'), (2000, '2k'),
                     (5000, '5k'), (10000, '10k'), (20000, '20k')):
            x = self.fx(f)
            self.create_line(x, y1, x, y2, fill=T['rowline'])
            self.create_text(x, y2 + p(6), text=t, font=F.mono_xs, fill=T['faint'], anchor='n')
        for db in (20, 10, 0, -10, -20):
            y = self.fy(db)
            self.create_line(x1, y, x2, y, fill=T['line2'] if db == 0 else T['rowline'])
            self.create_text(x1 - p(8), y, text='%+d' % db if db else '0', font=F.mono_xs, fill=T['faint'],
                             anchor='e')
        on = v.eq_on
        active = [b for b in v.bands() if band_active(b, eq)]
        total = [0.0] * self.N
        for b in active:
            c = self.curve(b, eq)
            total = [a + d for a, d in zip(total, c)]
            pts = []
            for f, d in zip(self.freqs, c):
                pts += [self.fx(f), self.fy(d)]
            self.create_line(*pts, fill=self.W.mix(b['color'], T['card'], .6), width=1)
        main = T['accent'] if on else T['muted']
        pts = []
        for f, d in zip(self.freqs, total):
            pts += [self.fx(f), self.fy(d)]
        y0 = self.fy(0)
        self.create_polygon(pts[0], y0, *pts, pts[-2], y0, fill=self.W.mix(main, T['card'], .88), outline='')
        self.create_line(*pts, fill=main, width=round(2 * self.view.S))
        if not on:
            self.create_text(x2 - p(8), y1 + p(6), text='EQ desligado no pedal', font=F.small, fill=T['amber'],
                             anchor='ne')
        # points
        self.nodes = []
        r = p(10)
        for b in v.bands():
            sw = eq[b['sw']] >= .5
            f = eq[b['f']]
            x = round(self.fx(f))
            y = round(self.fy(eq[b['g']] if b['g'] is not None else 0))
            self.nodes.append((x, y, b))
            fill = b['color'] if sw and band_active(b, eq) else T['card']
            ring = b['color'] if sw else T['dim']
            if self.hover is b or self.drag is b:
                aa.oval(self, x, y, r + p(4), fill=self.W.mix(b['color'], T['card'], .7), bg=T['card'])
            aa.oval(self, x, y, r, fill=fill, outline=ring, width=p(2), bg=T['card'])
            self.create_text(x, y, text=b['tag'], font=F.badge,
                             fill=T['accent_ink'] if fill != T['card'] else ring)
        if self.drag is not None or self.hover is not None:
            b = self.drag or self.hover
            t = '%s · %s' % (b['name'], freq_txt(eq[b['f']], b['f']))
            if b['g'] is not None:
                t += ' · %s · Q %s' % (db_txt(eq[b['g']]), ('%.1f' % eq[b['q']]).replace('.', ','))
            self.create_text(x1 + p(8), y1 + p(6), text=t, font=F.small, fill=T['text'], anchor='nw')

    def _node_at(self, x, y):
        best, bd = None, self.p(16)
        for nx, ny, b in self.nodes:
            d = math.hypot(nx - x, ny - y)
            if d < bd and self.view.eq[b['sw']] >= .5:
                best, bd = b, d
        return best

    def _set_hover(self, b):
        if b is not self.hover:
            self.hover = b
            self.configure(cursor='fleur' if b else '')
            self.redraw()

    def _motion(self, e):
        if self.drag is None:
            self._set_hover(self._node_at(e.x, e.y))

    def _press(self, e):
        self.drag = self._node_at(e.x, e.y)
        self.view.dragging = self.drag is not None
        self.redraw()

    def _move(self, e, final=False):
        b = self.drag
        if b is None:
            return
        x1, _y1, x2, _y2 = self.box()
        f = self.f_at(e.x)
        if b['kind'] == 'hpf' and e.x < x1:
            f = Q.EQ_LOCUT_OFF
        elif b['kind'] == 'lpf' and e.x > x2:
            f = Q.EQ_HICUT_OFF
        self.view.set_eq(b['f'], f, final)
        if b['g'] is not None:
            self.view.set_eq(b['g'], self.db_at(e.y), final)

    def _release(self, e):
        if self.drag is not None:
            self._move(e, True)
        self.drag = None
        self.view.dragging = False
        self.redraw()

    def _double(self, e):
        b = self._node_at(e.x, e.y)
        if b is None:
            return
        if b['g'] is not None:
            self.view.set_eq(b['g'], 0, True)
        else:
            self.view.set_eq(b['f'], Q.EQ_LOCUT_OFF if b['kind'] == 'hpf' else Q.EQ_HICUT_OFF, True)

    def _wheel(self, e):
        b = self._node_at(e.x, e.y)
        if b is not None:
            q = self.view.eq[b['q']] * (1.12 if e.delta > 0 else 1 / 1.12)
            self.view.set_eq(b['q'], q, False)
        return 'break'


# ------------------------------------------------------------------------------------------------ the view
class GlobalView(tk.Frame):
    def __init__(self, parent, app, W, pv):
        self.app, self.W, self.pv = app, W, pv
        T = W.T
        super().__init__(parent, bg=T['bg'])
        self.S, self.F = app.S, app.F
        self.page = app.session.get('global_page', 'eq')
        self.settings = self.eq = self.eq_on = None
        self.loading = False
        self.busy = None                # the header chip's connect button asks (nothing long runs here)
        self.dragging = False
        self.pending = {}               # EQ index -> value not sent yet
        self.flush_job = None
        self.set_pending = {}           # setting key -> value not sent yet
        self.set_job = None
        self.det = None                 # the settings page's ScrollFrame (the app routes the wheel to it)
        self.eq_ctl = {}                # EQ index -> (value label, slider)
        self.sw_ctl = {}                # band key -> switch canvas
        self.set_ctl = {}               # setting key -> update function
        self.graph = None
        pv.listeners.append(self._on_event)
        self._layout()

    def p(self, v):
        return round(v * self.S)

    def shutdown(self):
        self.app.session['global_page'] = self.page

    def bands(self):
        return [b for b in BANDS if not b.get('hidden') or (self.eq and self.eq[b['sw']] >= .5)]

    # ------------------------------------------------------------------ layout
    def _layout(self):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        top = tk.Frame(self, bg=T['bg'])
        top.pack(fill='x', padx=p(24), pady=(p(18), p(14)))
        self.seg = W.SegBar(top, self.app, [('eq', 'EQ global'), ('settings', 'Ajustes globais')], self.page,
                            self.set_page, height=36, radius=8, inner=5, pad=16)
        self.seg.pack(side='left')
        self.btn_reset = W.Btn(top, self.app, 'Restaurar o EQ de fábrica', None, self.reset_eq, 'ghost', height=34,
                               padx=14, tip='Volta os 31 parâmetros do EQ global aos valores de fábrica do pedal')
        self.btn_read = W.Btn(top, self.app, 'Ler do pedal', None, self.reload, 'subtle', height=34, padx=12,
                              tip='Relê o EQ e os ajustes do pedal')
        self.info = tk.Label(top, text='', font=F.small, bg=T['bg'], fg=T['faint'])
        self.body = tk.Frame(self, bg=T['bg'])
        self.body.pack(fill='both', expand=True)
        self.refresh()

    def set_page(self, page):
        self.page = page
        self.seg.set(active=page)
        self.show()

    def refresh(self):
        """the state of the connection decides what the body shows"""
        if self.pv.connected and self.settings is None and not self.loading:
            self.reload()
        self.show()

    def reload(self):
        if not self.pv.connected:
            return
        self.loading = True
        self.pv.worker.submit('glob_read', PV.job_glob_read)
        self.show()

    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.eq_ctl, self.sw_ctl, self.set_ctl = {}, {}, {}
        self.graph = self.det = None

    def show(self):
        self._clear()
        ready = self.pv.connected and self.settings is not None
        for w in (self.btn_reset, self.btn_read, self.info):
            w.pack_forget()
        if ready:
            self.btn_read.pack(side='right')
            if self.page == 'eq':
                self.btn_reset.pack(side='right', padx=(0, self.p(8)))
            self.info.pack(side='right', padx=(0, self.p(14)))
            self.info.configure(text='Cada mudança vai para o pedal na hora; o pedal guarda sozinho.')
            (self._eq_page if self.page == 'eq' else self._settings_page)()
        else:
            self._empty()

    def _empty(self):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        box = tk.Frame(self.body, bg=T['bg'])
        box.place(relx=.5, rely=.38, anchor='center')
        if self.pv.connected:
            tk.Label(box, text='Lendo o EQ e os ajustes do pedal…', font=F.h3, bg=T['bg'], fg=T['soft']).pack()
            return
        tk.Label(box, text='Pedal desconectado', font=F.h2, bg=T['bg'], fg=T['bright']).pack()
        tk.Label(box, text='O EQ global e os ajustes globais ficam no pedal: conecte para ler e mudar.\n'
                           'Feche o editor original antes (a porta MIDI só aceita um programa por vez).',
                 font=F.body, bg=T['bg'], fg=T['muted'], justify='center').pack(pady=(p(8), p(16)))
        W.Btn(box, self.app, 'Conectar', None, self.pv.connect_pedal, 'primary', height=36, padx=18).pack()

    # ------------------------------------------------------------------ EQ page
    def _eq_page(self):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        page = tk.Frame(self.body, bg=T['bg'])
        page.pack(fill='both', expand=True, padx=p(24), pady=(0, p(18)))
        # master switch + level
        row = tk.Frame(page, bg=T['bg'])
        row.pack(fill='x', pady=(0, p(12)))
        self.eq_sw = W.ToggleRow(row, self.app, 'EQ global', bool(self.eq_on), self.toggle_eq, T['accent'])
        self.eq_sw.configure(width=p(230))
        self.eq_sw.pack(side='left')
        lv = W.Card(row, self.app, T['tile'], T['line2'], 8, fit='x')
        lv.pack(side='left', fill='x', expand=True, padx=(p(12), 0))
        lb = lv.body
        lb.configure(height=p(40) - 2 * lv.i)
        lb.pack_propagate(False)
        tk.Label(lb, text='Nível', font=F.body, bg=T['tile'], fg=T['text']).pack(side='left', padx=(p(12) - lv.i, 0))
        val = tk.Label(lb, text='', font=F.mono, bg=T['tile'], fg=T['text'], width=4, anchor='e')
        val.pack(side='right', padx=(p(8), p(12) - lv.i))
        sl = Slider(lb, self.app, W, 0, 100, self.eq[LEVEL], lambda v, fin: self.set_eq(LEVEL, v, fin), T['accent'],
                    default=Q.EQ_PARAMS[LEVEL][4], step=1)
        sl.pack(side='left', fill='x', expand=True, padx=(p(14), 0))
        self.eq_ctl[LEVEL] = (val, sl)
        # graph
        gc = W.Card(page, self.app, T['card'], T['cardline'], 10, fit='both')
        gc.pack(fill='both', expand=True)
        self.graph = EqGraph(gc.body, self)
        self.graph.pack(fill='both', expand=True, padx=p(6), pady=p(6))
        tk.Label(page, text='Arraste os pontos: frequência e ganho (Low/High Cut: só frequência; para fora do gráfico '
                            'desliga). Roda do mouse sobre um ponto: Q. Duplo clique: ganho 0. Nos controles, duplo '
                            'clique volta ao valor de fábrica.', font=F.xs, bg=T['bg'], fg=T['faint'],
                 anchor='w', justify='left').pack(fill='x', pady=(p(6), p(10)))
        # one column per band
        cols = tk.Frame(page, bg=T['bg'])
        cols.pack(fill='x')
        bands = self.bands()
        for k, b in enumerate(bands):
            cols.columnconfigure(k, weight=1, uniform='band')
            self._band_card(cols, b).grid(row=0, column=k, sticky='nsew', padx=(0 if k == 0 else p(8), 0))
        self._update_eq()

    def _band_card(self, parent, b):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        bg = T['card']
        card = W.Card(parent, self.app, bg, T['cardline'], 10, fit='x')
        body = card.body
        head = tk.Frame(body, bg=bg)
        head.pack(fill='x', padx=p(10), pady=(p(8), p(4)))
        dot = tk.Canvas(head, width=p(10), height=p(10), bg=bg, highlightthickness=0)
        dot.pack(side='left')
        aa.oval(dot, p(5), p(5), p(5), fill=b['color'], bg=bg)
        tk.Label(head, text=b['name'], font=F.semi, bg=bg, fg=T['text']).pack(side='left', padx=(p(7), 0))
        sw = tk.Canvas(head, width=p(34), height=p(20), bg=bg, highlightthickness=0, cursor='hand2')
        sw.pack(side='right')
        sw.bind('<Button-1>', lambda e, b=b: self.set_eq(b['sw'], 0 if self.eq[b['sw']] >= .5 else 1, True))
        self.sw_ctl[b['key']] = sw
        rows = [('Frequência', b['f'])]
        if b['g'] is not None:
            rows += [('Q', b['q']), ('Ganho', b['g'])]
        for name, idx in rows:
            r = tk.Frame(body, bg=bg)
            r.pack(fill='x', padx=p(10), pady=(p(4), 0))
            tk.Label(r, text=name, font=F.small, bg=bg, fg=T['muted']).pack(side='left')
            val = tk.Label(r, text='', font=F.mono_s, bg=bg, fg=T['text'])
            val.pack(side='right')
            _n, lo, hi, step, dflt = Q.EQ_PARAMS[idx]
            if idx == b['f']:
                sl = Slider(body, self.app, W, lo, hi, self.eq[idx], lambda v, fin, i=idx: self.set_eq(i, v, fin),
                            b['color'], log=True, default=dflt)
            elif idx == b['q']:
                sl = Slider(body, self.app, W, lo, hi, self.eq[idx], lambda v, fin, i=idx: self.set_eq(i, v, fin),
                            b['color'], log=True, default=dflt)
            else:
                sl = Slider(body, self.app, W, lo, hi, self.eq[idx], lambda v, fin, i=idx: self.set_eq(i, v, fin),
                            b['color'], center=0, default=dflt, step=1)
            sl.pack(fill='x', padx=p(6), pady=(p(2), 0))
            self.eq_ctl[idx] = (val, sl)
        tk.Frame(body, bg=bg, height=p(10)).pack()
        return card

    def _update_eq(self, only=None):
        """labels, sliders, switches and the graph from self.eq"""
        if self.eq is None:
            return
        T = self.W.T
        try:
            for idx, (lbl, sl) in self.eq_ctl.items():
                if only is not None and idx not in only:
                    continue
                v = self.eq[idx]
                band = next((b for b in BANDS if idx in (b['f'], b['q'], b['g'])), None)
                on = band is None or self.eq[band['sw']] >= .5
                if band is None:
                    txt = '%d' % round(v)
                elif idx == band['f']:
                    txt = freq_txt(v, idx)
                elif idx == band['q']:
                    txt = ('%.1f' % v).replace('.', ',')
                else:
                    txt = db_txt(v)
                lbl.configure(text=txt, fg=T['text'] if on else T['faint'])
                sl.set(v, enabled=on)
            for b in BANDS:
                sw = self.sw_ctl.get(b['key'])
                if sw is not None:
                    sw.delete('all')
                    self.W.draw_switch(sw, self.S, self.eq[b['sw']] >= .5, b['color'], T['card'])
            if self.graph is not None:
                self.graph.redraw()
        except tk.TclError:
            pass

    def set_eq(self, idx, value, final=False):
        """optimistic local change; sent at most every SEND_EVERY ms (at once when final)"""
        if self.eq is None:
            return
        v = Q.eq_clamp(idx, value)
        changed = self.eq[idx] != v
        self.eq[idx] = v
        if changed or final:
            self.pending[idx] = v
        if any(b['sw'] == idx and b.get('hidden') for b in BANDS) and v < .5:
            self.show()                 # a shelf switched off disappears
        else:
            self._update_eq()
        if final:
            self.flush()
        elif self.pending and self.flush_job is None:
            self.flush_job = self.after(SEND_EVERY, self.flush)

    def flush(self):
        if self.flush_job is not None:
            self.after_cancel(self.flush_job)
            self.flush_job = None
        if not self.pv.connected:
            self.pending = {}
            return
        for idx, v in self.pending.items():
            self.pv.worker.submit('glob_eq', PV.job_glob_eq, idx, v)
        self.pending = {}

    def toggle_eq(self):
        if self.eq_on is None or not self.pv.connected:
            return
        self.eq_on = not self.eq_on
        self.eq_sw.set('EQ global', self.eq_on)
        self.pv.worker.submit('glob_eqon', PV.job_glob_eqon, self.eq_on)
        if self.graph is not None:
            self.graph.redraw()

    def reset_eq(self):
        if not self.pv.connected or self.eq is None:
            return
        if not self.W.ask(self.app, 'Restaurar o EQ global', 'Os 31 parâmetros do EQ global voltam aos valores de '
                          'fábrica do pedal: Low Cut e High Cut desligados, as quatro bandas em 100 Hz, 500 Hz, 1 kHz '
                          'e 5 kHz com ganho 0, Q 0,7 e nível 50. O liga/desliga do EQ não muda.', 'Restaurar'):
            return
        self.eq = [d for _n, _lo, _hi, _s, d in Q.EQ_PARAMS]
        self.pending = {}
        self.pv.worker.submit('glob_eq', PV.job_glob_eq_all, list(self.eq))
        self.show()

    # ------------------------------------------------------------------ settings page
    def _settings_page(self):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        sf = W.ScrollFrame(self.body, self.app, T['bg'])
        sf.pack(fill='both', expand=True)
        self.det = sf
        grid = tk.Frame(sf.inner, bg=T['bg'])
        grid.pack(fill='both', expand=True, padx=p(24), pady=(0, p(20)))
        secs = dict(SECTIONS)
        for c, names in enumerate(COLUMNS):
            grid.columnconfigure(c, weight=1, uniform='col')
            col = tk.Frame(grid, bg=T['bg'])
            col.grid(row=0, column=c, sticky='new', padx=(0 if c == 0 else p(14), 0))
            for n in names:
                self._section_card(col, n, secs[n]).pack(fill='x', pady=(0, p(14)))

    def _section_card(self, parent, title, rows):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        bg = T['card']
        card = W.Card(parent, self.app, bg, T['cardline'], 10, fit='x')
        body = card.body
        tk.Label(body, text=title.upper(), font=F.tiny, bg=bg, fg=T['muted']).pack(anchor='w', padx=p(14),
                                                                                   pady=(p(12), p(6)))
        s = self.settings
        for kind, key, label, *extra in rows:
            if key == 'fs1' and s['exp_mode'] == 0 or key == 'fs2' and s['exp_mode'] != 2:
                continue
            if kind == 'dd':
                self._dd_row(body, key, label, extra[0])
            elif kind == 'slider':
                self._slider_row(body, key, label, *extra)
            else:
                self._toggle_row(body, key, label, extra[0] if kind == 'bit' else None)
        tk.Frame(body, bg=bg, height=p(10)).pack()
        return card

    def _dd_row(self, body, key, label, options):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        bg = T['card']
        r = tk.Frame(body, bg=bg)
        r.pack(fill='x', padx=p(14), pady=p(4))
        tk.Label(r, text=label, font=F.body, bg=bg, fg=T['soft']).pack(side='left')
        texts = [t for _v, t in options]
        name = lambda v: next((t for o, t in options if o == v), 'valor %s' % v)
        dd = W.Dropdown(r, self.app, texts, name(self.settings[key]), None, width=None, height=32, font=F.small)
        dd.command = lambda t, key=key: self.set_setting(key, next(v for v, x in options if x == t))
        dd.configure(width=max(p(120), min(p(220), max(F.small.measure(t) for t in texts) + p(44))))
        dd.pack(side='right')
        self.set_ctl.setdefault(key, []).append(lambda v, dd=dd: dd.set(name(v)))

    def _slider_row(self, body, key, label, lo, hi):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        bg = T['card']
        r = tk.Frame(body, bg=bg)
        r.pack(fill='x', padx=p(14), pady=(p(6), 0))
        tk.Label(r, text=label, font=F.body, bg=bg, fg=T['soft']).pack(side='left')
        val = tk.Label(r, text=db_txt(self.settings[key]), font=F.mono_s, bg=bg, fg=T['text'])
        val.pack(side='right')
        sl = Slider(body, self.app, W, lo, hi, self.settings[key], None, T['accent'], center=0, default=0, step=1)
        sl.command = lambda v, fin, key=key, val=val: (val.configure(text=db_txt(round(v))),
                                                       self.set_setting(key, round(v), fin))
        sl.pack(fill='x', padx=p(10), pady=(p(2), p(4)))
        self.set_ctl.setdefault(key, []).append(lambda v, sl=sl, val=val: (sl.set(v), val.configure(text=db_txt(v))))

    def _toggle_row(self, body, key, label, bit):
        W, T, p = self.W, self.W.T, self.p
        on = lambda v: bool(v & bit) if bit else bool(v)
        tr = W.ToggleRow(body, self.app, label, on(self.settings[key]), None, T['accent'], height=38)

        def flip(key=key, bit=bit):
            v = self.settings[key]
            nv = (v ^ bit) if bit else (0 if v else 1)
            self.set_setting(key, nv)
        tr.command = flip
        tr.pack(fill='x', padx=p(10), pady=p(3))
        self.set_ctl.setdefault(key, []).append(lambda v, tr=tr: tr.set(label, on(v)))

    def set_setting(self, key, value, final=True):
        """optimistic local change, sent like the EQ (at most every SEND_EVERY ms while a slider moves)"""
        if self.settings is None or not self.pv.connected:
            return
        old = self.settings[key]
        if old == value and not final:
            return
        self.settings[key] = value
        for f in self.set_ctl.get(key, []):
            f(value)
        if key == 'exp_mode' and ((old == 0) != (value == 0) or (old == 2) != (value == 2)):
            self.after_idle(self.show)          # the footswitch rows follow the mode
        self.set_pending[key] = value
        if final:
            self.flush_settings()
        elif self.set_job is None:
            self.set_job = self.after(SEND_EVERY, self.flush_settings)

    def flush_settings(self):
        if self.set_job is not None:
            self.after_cancel(self.set_job)
            self.set_job = None
        if self.pv.connected:
            for key, v in self.set_pending.items():
                self.pv.worker.submit('glob_set', PV.job_glob_set, key, v)
        self.set_pending = {}

    # ------------------------------------------------------------------ pedal events
    def _on_event(self, kind, res, err):
        if kind == 'glob_read':
            self.loading = False
            if err:
                self.app.toast_msg('Não foi possível ler os ajustes globais: %s' % err, 'error')
                self.settings = None
            else:
                self.settings, self.eq, self.eq_on = res['settings'], res['eq'], res['eq_on']
            self.show()
        elif kind in ('glob_set', 'glob_eq', 'glob_eqon') and err:
            self.app.toast_msg('O pedal não recebeu a mudança: %s' % err, 'error')
        elif kind == 'glob_push':
            self._apply_push(res)
        elif kind in ('disconnect', 'lost'):
            self.settings = self.eq = self.eq_on = None
            self.loading = False
            self.pending, self.set_pending = {}, {}
            self.show()
        elif kind in ('connect', 'list') and not err:
            if self.app.mode == 'global' and self.settings is None and not self.loading:
                self.reload()
            elif self.app.mode == 'global':
                self.show()

    def _apply_push(self, items):
        """changes the pedal announces (its own knobs, or the echo of ours)"""
        if self.settings is None:
            return
        redraw_eq, rebuild = False, False
        for kind, a, v in items:
            if kind == 'eq' and 0 <= a < len(Q.EQ_PARAMS):
                if self.dragging or a in self.pending:
                    continue
                if abs(self.eq[a] - v) > 1e-4:
                    b = next((b for b in BANDS if b['sw'] == a and b.get('hidden')), None)
                    self.eq[a] = v
                    redraw_eq = True
                    rebuild = rebuild or b is not None
            elif kind == 'eqon':
                if bool(v) != bool(self.eq_on):
                    self.eq_on = bool(v)
                    if self.page == 'eq' and self.graph is not None:
                        self.eq_sw.set('EQ global', self.eq_on)
                        redraw_eq = True
            elif kind == 'set':
                key = TYPE_KEY.get(a)
                if key is None or key in self.set_pending or self.set_job is not None:
                    continue
                if self.settings.get(key) != v:
                    if key == 'exp_mode':
                        rebuild = True
                    self.settings[key] = v
                    for f in self.set_ctl.get(key, []):
                        f(v)
        if rebuild:
            self.show()
        elif redraw_eq:
            self._update_eq()
