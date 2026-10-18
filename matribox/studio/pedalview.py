"""Matribox Studio, aba Pedal: lê os presets do Matribox II Pro pela USB (protocolo do editor, qmepreset.py) ou
do Bank de um firmware .bin e desenha a cadeia de sinal de cada um, sem o editor original.

No preset em edição do pedal dá para ligar/desligar blocos, mudar knobs e ativar outro preset. Tudo vai para
o buffer de edição do pedal, como pelos botões dele: nada é gravado na flash até o preset ser salvo no pedal.

A porta MIDI fica num thread próprio (Worker): pedidos em fila e, entre eles, a escuta do que o pedal envia
sozinho (troca de preset, bloco ligado/desligado, knob mexido, preset modificado).
"""
import glob, os, queue, struct, threading, time
import tkinter as tk
from tkinter import ttk, filedialog

from matribox.studio import aa
from matribox.usb import qmepreset as Q

REREAD_AFTER = 0.35                     # s of quiet after a change before the edit buffer is read again
KNOB_EVERY = 60                         # ms between knob messages while dragging


# ------------------------------------------------------------------------------------------------ MIDI thread
class Worker(threading.Thread):
    """owns the pedal's MIDI port: runs jobs in order; when idle, follows what the pedal pushes"""
    def __init__(self, post):
        super().__init__(daemon=True)
        self.post = post                # post(kind, result, error): called from this thread
        self.jobs = queue.Queue()
        self.pedal = None
        self.cur = -1
        self.dirty = None               # time of the last change that needs a re-read of the edit buffer
        self.quit = False

    def submit(self, kind, fn, *args):
        self.jobs.put((kind, fn, args))

    def run(self):
        while not self.quit:
            try:
                kind, fn, args = self.jobs.get(timeout=0.05)
            except queue.Empty:
                if self.pedal:
                    self._watch()
                continue
            try:
                self.post(kind, fn(self, *args), None)
            except Exception as ex:
                self.post(kind, args, ex)
        self.close()

    def close(self):
        if self.pedal:
            try:
                self.pedal.close()
            except Exception:
                pass
            self.pedal = None
        self.dirty = None

    def read_live(self):
        """(current index, edit buffer, modified)"""
        p = self.pedal.edit_buffer(self.cur)
        return self.cur, p, self.pedal.modified()

    def _watch(self):
        try:
            ev = self.pedal.poll()
            idx, patches, overload, modified, glob = None, [], 0, None, []
            for cmd, data in ev:
                sub = struct.unpack_from('<I', data)[0] if len(data) >= 4 else None
                if cmd == Q.CMD_SIGNAL and sub == Q.SUB_INDEX and len(data) >= 8:
                    idx = struct.unpack_from('<i', data, 4)[0]
                elif cmd == Q.CMD_SIGNAL and sub == Q.SUB_KNOB and len(data) >= 16:
                    patches.append(('knob',) + struct.unpack_from('<IIf', data, 4))
                elif cmd == Q.CMD_SIGNAL and sub == Q.SUB_SWITCH and len(data) >= 12:
                    patches.append(('on',) + struct.unpack_from('<II', data, 4))
                    self.dirty = time.time()
                elif cmd == Q.CMD_SIGNAL and sub in Q.LIVE_SUBS or cmd == Q.CMD_PRESET:
                    self.dirty = time.time()
                elif cmd == Q.CMD_GLOBAL_SINGLE and sub == Q.SUB_OVERLOAD and len(data) >= 8:
                    overload = struct.unpack_from('<I', data, 4)[0]
                elif cmd == Q.CMD_GLOBAL_SINGLE and sub == Q.SUB_MODIFIED and len(data) >= 8:
                    modified = bool(struct.unpack_from('<I', data, 4)[0])
                elif cmd == Q.CMD_GLOBAL_SINGLE and len(data) >= 8:            # a global setting changed
                    glob.append(('set',) + struct.unpack_from('<Ii', data))
                elif cmd == Q.CMD_EQ_PARAM and len(data) >= 8:
                    glob.append(('eq',) + struct.unpack_from('<If', data))
                elif cmd == Q.CMD_EQ_SWITCH and len(data) >= 8:
                    glob.append(('eqon', 0, struct.unpack_from('<I', data, 4)[0]))
            if glob:
                self.post('glob_push', glob, None)
            if overload:
                self.post('overload', overload, None)
            if modified is not None:
                self.post('modified', modified, None)
            if patches:
                self.post('patch', patches, None)
            if idx is not None and 0 <= idx < Q.NPRESETS:
                self.cur = idx
                self.dirty = None
                self.post('live', self.read_live(), None)
            elif self.dirty and time.time() - self.dirty > REREAD_AFTER:
                self.dirty = None
                self.post('live', self.read_live(), None)
        except Exception as ex:
            self.close()
            self.post('lost', None, ex)


def job_connect(w, port):
    w.close()
    w.pedal = Q.Pedal(port)
    p = w.pedal
    out = dict(name=p.name)
    out['clone'] = p.clone_names()
    out['ir'] = p.ir_names()
    out['list'] = p.preset_list()
    w.cur = out['cur'] = p.current_index()
    out['live'] = w.read_live()
    return out


def job_list(w):
    out = dict(list=w.pedal.preset_list(), clone=w.pedal.clone_names(), ir=w.pedal.ir_names())
    w.cur = out['cur'] = w.pedal.current_index()
    out['live'] = w.read_live()
    return out


def job_stored(w, i):
    return w.pedal.stored(i)


def job_modified(w, i):
    return i, w.pedal.modified()


def job_activate(w, i):
    w.pedal.activate(i)
    time.sleep(0.15)                    # FUN_00021660 reloads the chain before the next request is served
    w.cur = i
    w.dirty = None
    return w.read_live()


def job_on(w, slot, on):
    w.pedal.set_on(slot, on)
    w.dirty = time.time()


def job_knob(w, slot, order, value):
    w.pedal.set_knob(slot, order, value)
    w.dirty = time.time()


def job_chain(w, op, args):
    """a chain edit (Pedal.insert_block / remove_block / move_block / replace_block), then the edit buffer read
    back. An insert with an id the pedal's firmware lacks leaves an empty place in the order: removed here."""
    getattr(w.pedal, op + '_block')(*args)
    time.sleep(0.15)                    # the chain is re-initialised before the next request is served
    live = w.read_live()
    ph = live[1].phantoms()
    for k in reversed(ph):
        w.pedal.remove_block(k)
    if ph:
        time.sleep(0.15)
        live = w.read_live()
    w.dirty = None
    return live, op, args, len(ph)


def job_disconnect(w):
    w.close()


def job_clone_list(w):
    return w.pedal.clone_names()


def job_clone_write(w, slot, name, blob):
    """overwrites Clone slot `slot` (flash); progress goes out as 'nam_progress'"""
    w.pedal.write_clone(slot, name, blob, progress=lambda k, n: w.post('nam_progress', (slot, k, n), None))
    w.dirty = time.time()                   # the pedal re-inits the chain: re-read the edit buffer
    return slot, name, w.pedal.clone_names()


def job_ir_list(w):
    return w.pedal.ir_names()


def job_ir_write(w, slot, name, data):
    """overwrites User IR slot `slot` (flash); progress goes out as 'ir_progress'"""
    w.pedal.write_ir(slot, name, data, progress=lambda k, n: w.post('ir_progress', (slot, k, n), None))
    w.dirty = time.time()
    return slot, name, w.pedal.ir_names()


def job_glob_read(w):
    p = w.pedal
    return dict(settings=p.global_settings(), eq=p.eq(), eq_on=p.eq_switch())


def job_glob_set(w, key, value):
    w.pedal.set_global(key, value)


def job_glob_eq(w, idx, value):
    w.pedal.set_eq(idx, value)


def job_glob_eqon(w, on):
    w.pedal.set_eq_switch(on)


def job_glob_eq_all(w, values):
    for k, v in enumerate(values):
        w.pedal.set_eq(k, v)
        time.sleep(0.01)


def job_ir_rename(w, slot, old, name):
    w.pedal.rename_ir(slot, name)
    time.sleep(0.1)
    return slot, old, name, w.pedal.ir_names()


# ------------------------------------------------------------------------------------------------ preset list
class PresetList(tk.Frame):
    """the presets grouped by bank (left column): bank number, slot letter, name and style"""
    def __init__(self, parent, view):
        T = view.W.T
        super().__init__(parent, bg=T['panel'])
        self.v = view
        self.cv = tk.Canvas(self, bg=T['panel'], highlightthickness=0, takefocus=1)
        self.sb = ttk.Scrollbar(self, orient='vertical', command=self.cv.yview, style='Dark.Vertical.TScrollbar')
        self.cv.configure(yscrollcommand=self._sb_set)
        self.cv.pack(side='left', fill='both', expand=True)
        self.items, self.rows, self.message, self.hover = [], [], '', None
        cv = self.cv
        cv.bind('<Configure>', lambda e: self.draw())
        cv.bind('<Motion>', self._motion)
        cv.bind('<Leave>', lambda e: self._set_hover(None))
        cv.bind('<Button-1>', self._click)
        cv.bind('<Double-Button-1>', lambda e: self.v.activate_shown())
        cv.bind('<Return>', lambda e: self.v.activate_shown())
        cv.bind('<MouseWheel>', lambda e: cv.yview_scroll(-3 if e.delta > 0 else 3, 'units')
                if self.sb.winfo_manager() else None)
        cv.bind('<Up>', lambda e: self._step(-1))
        cv.bind('<Down>', lambda e: self._step(1))
        cv.configure(yscrollincrement=view.p(10))

    def _sb_set(self, a, b):
        self.sb.set(a, b)
        need = float(a) > 0 or float(b) < 1
        if need and not self.sb.winfo_manager():
            self.sb.pack(side='right', fill='y', before=self.cv)
        elif not need and self.sb.winfo_manager():
            self.sb.pack_forget()

    def fill(self, items, message=''):
        self.items, self.message = items, message
        self.draw()

    def draw(self):
        v, cv = self.v, self.cv
        W, T, F, p = v.W, v.W.T, v.F, v.p
        cv.delete('all')
        w = cv.winfo_width()
        if w < 40:
            return
        bg = T['panel']
        sel, cur = v.sel_index, v.cur if v.source == 'pedal' else -1
        rh = p(30)
        self.rows = []
        y = 0
        groups = []
        for it in self.items:
            if groups and groups[-1][0] == it[0] // 4:
                groups[-1][1].append(it)
            else:
                groups.append((it[0] // 4, [it]))
        x0 = p(30)
        for bank, rows in groups:
            cv.create_rectangle(0, y, w, y + v.W.lw_of(v.S), fill=T['rowline'], outline='')
            y += p(4)
            cv.create_text(p(8), y + rh / 2, text='%02d' % (bank + 1), font=F.mono_xs, fill=T['faint'], anchor='w')
            for i, name, style in rows:
                active, hov = i == sel, i == self.hover
                fill = W.mix(T['accent'], bg, .82 if hov else .87) if active else T['hover'] if hov else None
                if fill:
                    aa.rrect(cv, x0, y, w - p(2), y + rh, p(6), fill=fill, bg=bg)
                cy = y + rh / 2
                cv.create_text(x0 + p(10), cy, text='ABCD'[i % 4], font=F.mono_xs,
                               fill=T['accent'] if active or i == cur else T['faint'], anchor='w')
                gw = F.xs.measure(style) if style else 0
                nx = x0 + p(32)
                room = w - p(12) - gw - p(8) - nx - (p(14) if i == cur else 0)
                cv.create_text(nx, cy, text=W.fit_text(F.body, name or '(vazio)', room), font=F.body,
                               fill=T['bright'] if active else T['soft2'] if name else T['dim'], anchor='w')
                if i == cur:
                    aa.oval(cv, w - p(12) - gw - p(8) - p(4), cy, p(3), fill=T['accent'], bg=fill or bg)
                if style:
                    cv.create_text(w - p(12), cy, text=style, font=F.xs, fill=T['faint'], anchor='e')
                self.rows.append((y, y + rh, i))
                y += rh
            y += p(4)
        if not self.items:
            cv.create_text(w / 2, p(28), text=self.message, font=F.small, fill=T['faint'], width=w - p(32),
                           justify='center', anchor='n')
        cv.configure(scrollregion=(0, 0, w, max(y, 1)))

    def _row_at(self, e):
        y = self.cv.canvasy(e.y)
        for y1, y2, i in self.rows:
            if y1 <= y < y2 and e.x >= self.v.p(30):
                return i
        return None

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.draw()

    def _motion(self, e):
        self._set_hover(self._row_at(e))

    def _click(self, e):
        self.cv.focus_set()
        i = self._row_at(e)
        if i is not None:
            self.v.pick(i)

    def _step(self, d):
        order = [it[0] for it in self.items]
        if not order:
            return 'break'
        k = order.index(self.v.sel_index) + d if self.v.sel_index in order else 0
        k = max(0, min(len(order) - 1, k))
        self.v.pick(order[k])
        self.see(order[k])
        return 'break'

    def see(self, i):
        for y1, y2, k in self.rows:
            if k == i:
                sr = self.cv.cget('scrollregion').split()
                total = max(1.0, float(sr[3]) if len(sr) == 4 else 1.0)
                h = self.cv.winfo_height()
                top, bot = self.cv.canvasy(0), self.cv.canvasy(h)
                if y1 < top + self.v.p(8) or y2 > bot - self.v.p(8):
                    self.cv.yview_moveto(max(0.0, (y1 - h / 3) / total))
                return


# ------------------------------------------------------------------------------------------------ palette
class Palette(tk.Frame):
    """the effects that can go into the chain (EffectDB.palette()): category chips and effect buttons. A click
    swaps the selected block's effect (same category) or inserts after it; a drag puts it on the wire."""
    def __init__(self, parent, view):
        self.v = view
        W, T, F, p = view.W, view.W.T, view.F, view.p
        super().__init__(parent, bg=T['bg'])
        card = W.Card(self, view.app, T['card'], T['cardline'], 12, fit='x', under=T['bg'])
        card.pack(fill='x')
        b = card.body
        self.items = {}                 # menu -> [effect dicts]
        self.menu = 4
        self.tab_hits, self.chips = [], []
        self.press = None
        self.hover = None
        head = tk.Frame(b, bg=T['card'])
        head.pack(fill='x', padx=p(10), pady=(p(8), p(8)))
        tk.Label(head, text='EFEITOS', font=F.tiny, bg=T['card'], fg=T['muted']).pack(side='left')
        self.summary = tk.Label(head, text='', font=F.small, bg=T['card'], fg=T['faint'])
        self.summary.pack(side='left', padx=(p(12), 0))
        self.qv = tk.StringVar()
        W.Field(head, view.app, self.qv, 'Buscar em todos', height=30, radius=7, width=220, bg=T['bg'],
                font=F.small).pack(side='right')
        self.src = tk.Label(head, text='', font=F.xs, bg=T['card'], fg=T['dim'])
        self.src.pack(side='right', padx=(0, p(12)))
        self.qv.trace_add('write', lambda *a: self.layout())
        tk.Frame(b, bg=T['track'], height=1).pack(fill='x')
        self.tabs = tk.Canvas(b, height=p(28), bg=T['card'], highlightthickness=0, cursor='hand2')
        self.tabs.pack(fill='x', padx=p(10), pady=(p(12), 0))
        self.tabs.bind('<ButtonRelease-1>', self._on_tab)
        self.tabs.bind('<Configure>', lambda e: self._draw_tabs())
        body = tk.Frame(b, bg=T['card'])
        body.pack(fill='x', padx=(p(10), p(4)), pady=p(12))
        self.rows_shown = 2
        self.cv = tk.Canvas(body, height=p(self.rows_shown * 46 - 8), bg=T['card'], highlightthickness=0)
        self.sb = ttk.Scrollbar(body, orient='vertical', command=self.cv.yview, style='Card.Vertical.TScrollbar')
        self.cv.configure(yscrollcommand=self.sb.set)
        self.cv.pack(side='left', fill='x', expand=True)
        self.cv.bind('<Configure>', lambda e: self.layout())
        self.cv.bind('<ButtonPress-1>', self._press)
        self.cv.bind('<B1-Motion>', self._motion)
        self.cv.bind('<ButtonRelease-1>', self._release)
        self.cv.bind('<Motion>', self._hover)
        self.cv.bind('<Leave>', lambda e: self._set_hover(None))
        self.cv.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(-1 if e.delta > 0 else 1, 'units')
                     if self.sb.winfo_manager() else None)
        tk.Frame(b, bg=T['track'], height=1).pack(fill='x')
        self.hint = tk.Label(b, text='', font=F.xs, bg=T['card'], fg=T['faint'], anchor='w', justify='left')
        self.hint.pack(fill='x', padx=p(10), pady=(p(9), p(8)))
        self.hint.bind('<Configure>', lambda e: self.hint.configure(wraplength=max(100, e.width - p(4))))

    def refresh(self):
        db = self.v.db
        self.items = db.palette() if db else {}
        if self.items and self.menu not in self.items:
            self.menu = next(iter(self.items))
        self.src.configure(text='de %s' % db.menus_from if db and db.menus_from else '')
        self._draw_tabs()
        self.layout()

    def _draw_tabs(self):
        c, T, F, p, W = self.tabs, self.v.W.T, self.v.F, self.v.p, self.v.W
        c.delete('all')
        self.tab_hits = []
        w = max(c.winfo_width(), 100)
        th, gap = p(28), p(6)
        searching = bool(self.qv.get().strip())
        x = y = 0
        for m in self.items:
            code = Q.MENU_SHORT.get(m, str(m))
            tw = F.mono_s.measure(code) + p(20) + p(12)
            if x and x + tw > w:
                x, y = 0, y + th + gap
            col = W.menu_color(m)
            on = m == self.menu and not searching
            fill = W.mix(col, T['card'], .82) if on else None
            line = W.mix(col, T['card'], .4) if on else T['line2']
            aa.rrect(c, x, y, x + tw, y + th, p(7), fill=fill, outline=line, width=W.lw_of(self.v.S), bg=T['card'])
            aa.oval(c, x + p(10) + p(3), y + th / 2, p(3), fill=col, bg=fill or T['card'])
            c.create_text(x + p(22), y + th / 2, text=code, font=F.mono_s, anchor='w',
                          fill=T['bright'] if on else W.menu_color(m, .8, .1))
            self.tab_hits.append((x, y, x + tw, y + th, m))
            x += tw + gap
        c.configure(height=y + th)

    def _on_tab(self, e):
        for x1, y1, x2, y2, m in self.tab_hits:
            if x1 <= e.x <= x2 and y1 <= e.y <= y2:
                self.menu = m
                if self.qv.get():
                    self.qv.set('')                     # layout() through the trace
                else:
                    self.layout()
                return

    def shown_effects(self):
        q = self.qv.get().strip().lower()
        if not q:
            return self.items.get(self.menu, [])
        return [e for m, lst in self.items.items() for e in lst
                if q in e['name'].lower() or q in (e.get('category') or '').lower()
                or q == Q.MENU_SHORT.get(m, '').lower()]

    def layout(self):
        c, T, F, p, W = self.cv, self.v.W.T, self.v.F, self.v.p, self.v.W
        c.delete('all')
        self.chips = []
        self._draw_tabs()
        w = max(c.winfo_width(), 100)
        gap, ch = p(8), p(38)
        per = max(1, (w + gap) // (p(150) + gap))
        cw = int((w - (per - 1) * gap) / per)
        effs = self.shown_effects()
        searching = bool(self.qv.get().strip())
        live = self.v.live if self.v.editable() else None
        used = {(s.menu, s.id) for s in live.chain()} if live else set()
        lw = W.lw_of(self.v.S)
        for k, e in enumerate(effs):
            x, y = (k % per) * (cw + gap), (k // per) * (ch + gap)
            col = W.menu_color(e['menu'])
            u = (e['menu'], e['id']) in used
            hov = k == self.hover
            fill = T['hover'] if hov else W.mix(col, T['card'], .92) if u else T['tile']
            line = W.mix(col, T['card'], .3) if hov else W.mix(col, T['card'], .45) if u else T['line2']
            aa.rrect(c, x, y, x + cw, y + ch, p(8), fill=fill, outline=line, width=lw, bg=T['card'])
            aa.oval(c, x + p(12) + p(3), y + ch / 2, p(3), fill=col, bg=fill)
            tag = Q.MENU_SHORT.get(e['menu'], '') if searching else ('em uso' if u else '')
            tw = F.mono_s.measure(tag) + p(8) if tag else 0
            c.create_text(x + p(26), y + ch / 2, text=W.fit_text(F.body, e['name'], cw - p(26) - p(12) - tw),
                          font=F.body, fill=T['text'], anchor='w')
            if tag:
                c.create_text(x + cw - p(12), y + ch / 2, text=tag, font=F.mono_s, fill=T['muted'], anchor='e')
            self.chips.append((x, y, x + cw, y + ch, e))
        rows = -(-len(effs) // per) if effs else 0
        total = max(1, rows * (ch + gap) - gap)
        if not effs:
            c.create_text(w / 2, p(20), text='Nenhum efeito.' if searching else '', font=F.small, fill=T['faint'])
        c.configure(scrollregion=(0, 0, w, total))
        if total > int(c.cget('height')):
            self.sb.pack(side='right', fill='y')
        else:
            self.sb.pack_forget()
            c.yview_moveto(0)
        n = len(effs)
        self.summary.configure(text='%d resultado%s' % (n, 's' if n != 1 else '') if searching else
                               '%d em %s' % (n, self.v.W.C.MENUS.get(self.menu, '')))
        self._hint()

    def _hint(self):
        v = self.v
        s = v.live.slots[v.sel_slot] if v.editable() and v.sel_slot is not None else None
        if s is not None and not s.empty and not v._routing(s):
            name = v._effect(s)['name']
            t = ('Clique num efeito %s para trocar "%s"; de outra categoria, para inserir depois dele. Ou arraste-o '
                 'para o fio.' % (Q.MENU_SHORT.get(s.menu, ''), name))
        else:
            t = 'Clique num efeito para adicioná-lo ao fim da cadeia, ou arraste-o para o fio ou sobre um bloco.'
        self.hint.configure(text=t)

    def _chip(self, e):
        x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        for k, (x1, y1, x2, y2, eff) in enumerate(self.chips):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return k, eff
        return None, None

    def _set_hover(self, k):
        if k != self.hover:
            self.hover = k
            self.layout()

    def _hover(self, e):
        k, eff = self._chip(e)
        self.cv.configure(cursor='hand2' if eff else '')
        self._set_hover(k)

    def _press(self, e):
        k, eff = self._chip(e)
        self.press = (e.x_root, e.y_root, eff) if eff else None

    def _motion(self, e):
        v = self.v
        if v.dnd:
            v.drag_motion(e.x_root, e.y_root)
        elif self.press and abs(e.x_root - self.press[0]) + abs(e.y_root - self.press[1]) >= v.p(6):
            v.drag_start('new', e.x_root, e.y_root, effect=self.press[2])

    def _release(self, e):
        press, self.press = self.press, None
        if self.v.dnd:
            self.v.drag_end(e.x_root, e.y_root)
        elif press:
            self.v.click_effect(press[2])


# ------------------------------------------------------------------------------------------------ view
class PedalView(tk.Frame):
    def __init__(self, parent, app, W):
        self.app, self.W = app, W
        T = W.T
        super().__init__(parent, bg=T['bg'])
        self.S, self.F = app.S, app.F
        self.db = None
        self.source = app.session.get('pedal_source', 'pedal')
        self.bank_path = app.session.get('pedal_bank', '')
        self.port_name = app.session.get('pedal_port') or None
        self.connected = False
        self.presets = []               # [(i, name, style)]
        self.cache = {}                 # i -> Preset (stored, read from the pedal or the Bank)
        self.live = None                # edit buffer of the pedal
        self.cur = -1                   # preset active on the pedal
        self.shown = None               # Preset on screen
        self.sel_index = None
        self.sel_slot = None
        self.waiting = None             # preset index being read
        self.hits = []                  # (x1, y1, x2, y2, slot id) of the drawn blocks
        self.names = ([], [])           # Clone / User IR names read from the pedal
        self.worker_name = ''
        self.modified = False           # the pedal's edit buffer has unsaved changes
        self.pending = {}               # (slot, order) -> value not sent yet (knob drag)
        self.flush_job = None
        self.dragging = False
        self.ctl = {}                   # order -> (value label, bar canvas, colour) on the details panel
        self.listeners = []             # f(kind, result, error): the NAM captures view (namview.py) shares the port
        self.gaps = []                  # (x, y, position in the order array) where a block can be dropped
        self.blocks = []                # (x1, y1, x2, y2, slot id) of the effect blocks (not Split / Merge)
        self.block_h = 0
        self.gap_w = 0
        self.dnd = None                 # drag in progress: dict(kind 'new' | 'block', effect, sid, target)
        self.press = None               # (x_root, y_root, slot id) of a button press on the chain
        self.ghost = None
        self.chain_jobs = 0             # chain edits sent and not read back yet
        self.status_msg = None          # (text, colour) shown in the header chip instead of the connection
        self.title_key = None
        self.grid_img = None
        self.q = queue.Queue()
        self.worker = Worker(lambda k, r, e: self.q.put((k, r, e)))
        self.worker.start()
        self._layout()
        self.after(40, self._pump)
        threading.Thread(target=self._build_db, daemon=True).start()

    # ------------------------------------------------------------------ helpers
    def p(self, v):
        return round(v * self.S)

    def _build_db(self):
        paths = []
        P = self.app.project
        if P:
            paths.append(P.base_path)
        outs = sorted(glob.glob(Q.paths.build_path('QME-200_FW_USB_*.bin')), key=os.path.getmtime,
                      reverse=True)
        paths += [x for x in outs if os.path.abspath(x) not in map(os.path.abspath, paths)]
        db = Q.default_db(paths, P.items if P else ())
        self.q.put(('db', db, None))

    def _pump(self):
        try:
            while True:
                kind, res, err = self.q.get_nowait()
                self._on_result(kind, res, err)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(40, self._pump)

    def shutdown(self):
        self.worker.quit = True
        self.app.session['pedal_source'] = self.source
        self.app.session['pedal_bank'] = self.bank_path
        if self.port_name:
            self.app.session['pedal_port'] = self.port_name

    # ------------------------------------------------------------------ layout
    def _layout(self):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        # header actions: connection chip, reload, connect -----------------
        ha = self.app.actions_frame('pedal')
        self.btn_conn = W.Btn(ha, self.app, 'Conectar', None, self.toggle_connect, 'ghost', height=32, padx=12,
                              font=F.body, tip='Abre a porta MIDI do pedal (feche o editor original antes)')
        self.btn_conn.pack(side='right')
        self.btn_reload = W.Btn(ha, self.app, '' if F.icons else '↻', W.ICO['refresh'], self.reload, 'ghost',
                                height=32, width=32, padx=0, tip='Recarregar do pedal')
        self.btn_reload.pack(side='right', padx=(0, p(8)))
        chip = W.Card(ha, self.app, T['panel'], T['line2'], 8, fit='none')
        chip.pack(side='right', padx=(0, p(8)))
        cb = chip.body
        inner = tk.Frame(cb, bg=T['panel'], height=p(32) - 2 * chip.i)
        inner.pack()
        self.dot = tk.Canvas(cb, width=p(7), height=p(7), bg=T['panel'], highlightthickness=0)
        self.dot.place(in_=inner, x=p(12) - chip.i, rely=.5, anchor='w')
        self.status = tk.Label(cb, text='', font=F.body, bg=T['panel'], fg=T['soft'])
        tk.Label(cb, text='USB', font=F.mono_s, bg=T['panel'], fg=T['faint'])
        self.chip, self.chip_inner = chip, inner
        self.chip_usb = cb.winfo_children()[-1]
        # left: presets -----------------------------------------------------
        left = tk.Frame(self, bg=T['panel'], width=p(300))
        left.pack(side='left', fill='y')
        left.pack_propagate(False)
        tk.Frame(self, bg=T['line'], width=1).pack(side='left', fill='y')
        top = tk.Frame(left, bg=T['panel'])
        top.pack(fill='x', padx=p(16), pady=(p(16), p(12)))
        row = tk.Frame(top, bg=T['panel'])
        row.pack(fill='x')
        tk.Label(row, text='Presets', font=F.semi, bg=T['panel'], fg=T['text']).pack(side='left')
        self.count_lbl = tk.Label(row, text='', font=F.mono_xs, bg=T['panel'], fg=T['faint'])
        self.count_lbl.pack(side='right')
        self.src_seg = W.SegBar(top, self.app, [('pedal', 'Pedal (USB)'), ('bank', 'Bank de um .bin')], self.source,
                                self.set_source, height=34, radius=8, inner=5, font=F.small_m, fill=True)
        self.src_seg.pack(fill='x', pady=(p(12), 0))
        self.bank_row = tk.Frame(top, bg=T['panel'])
        self.bank_lbl = tk.Label(self.bank_row, text='', font=F.mono_xs, bg=T['panel'], fg=T['muted'], anchor='w')
        self.btn_bin = W.Btn(self.bank_row, self.app, 'Escolher…', None, self.choose_bank, 'ghost', height=28,
                             padx=10, font=F.small, radius=7, tip='Firmware cujo Bank (presets de fábrica) é mostrado')
        self.btn_bin.pack(side='right')
        self.bank_lbl.pack(side='left', fill='x', expand=True)
        self.qv = tk.StringVar()
        self.search_f = W.Field(top, self.app, self.qv, 'Buscar por nome ou slot (ex.: 34-D)', height=34,
                                bg=T['bg'], font=F.body)
        self.search_f.pack(fill='x', pady=(p(12), 0))
        self.qv.trace_add('write', lambda *a: self.fill_list())
        self.plist = PresetList(left, self)
        self.plist.pack(fill='both', expand=True, padx=(p(8), p(4)), pady=(0, p(12)))
        # right: details ------------------------------------------------------
        self.det_outer = tk.Frame(self, bg=T['panel'], width=p(340))
        self.det_outer.pack(side='right', fill='y')
        self.det_outer.pack_propagate(False)
        tk.Frame(self, bg=T['line'], width=1).pack(side='right', fill='y')
        self.det_foot = tk.Frame(self.det_outer, bg=T['panel'])
        self.det = W.ScrollFrame(self.det_outer, self.app, T['panel'])
        self.det.pack(fill='both', expand=True)
        self.empty_box = tk.Frame(self.det_outer, bg=T['panel'])
        tk.Label(self.empty_box, text='Nenhum bloco selecionado', font=F.medium, bg=T['panel'],
                 fg=T['text']).pack()
        self.empty_sub = tk.Label(self.empty_box, text='', font=F.small, bg=T['panel'], fg=T['faint'],
                                  wraplength=p(260), justify='center')
        self.empty_sub.pack(pady=(p(6), 0))
        # center: title, chain, palette -----------------------------------------
        center = tk.Frame(self, bg=T['bg'])
        center.pack(side='left', fill='both', expand=True)
        head = tk.Frame(center, bg=T['bg'])
        head.pack(fill='x', padx=p(24), pady=(p(20), p(16)))
        acts = tk.Frame(head, bg=T['bg'])
        acts.pack(side='right', anchor='n')
        self.btn_act = W.Btn(acts, self.app, 'Ativar no pedal', None, self.activate_shown, 'primary', height=36,
                             padx=16, tip='Carrega este preset no pedal (duplo clique na lista faz o mesmo)')
        self.btn_discard = W.Btn(acts, self.app, 'Descartar', None, self.discard, 'ghost', height=36, padx=14,
                                 tip='Recarrega o preset salvo no pedal, perdendo as mudanças não salvas')
        hl = tk.Frame(head, bg=T['bg'])
        hl.pack(side='left', fill='x', expand=True)
        r1 = tk.Frame(hl, bg=T['bg'])
        r1.pack(fill='x')
        self.t_label_card = W.Card(r1, self.app, T['bg'], W.mix(T['accent'], T['bg'], .6), 6, fit='none')
        self.t_label = tk.Label(self.t_label_card.body, text='', font=F.mono_m, bg=T['bg'], fg=T['accent'],
                                padx=p(8) - self.t_label_card.i, pady=p(3) - self.t_label_card.i)
        self.t_label.pack()
        self.t_name = tk.Label(r1, text='', font=F.h1, bg=T['bg'], fg=T['text'])
        self.t_name.pack(side='left')
        self.t_mod = tk.Frame(r1, bg=T['bg'])
        md = tk.Canvas(self.t_mod, width=p(6), height=p(6), bg=T['bg'], highlightthickness=0)
        aa.oval(md, p(3), p(3), p(3), fill=T['accent'])
        md.pack(side='left')
        tk.Label(self.t_mod, text='Não salvo', font=F.small, bg=T['bg'], fg='#f2b36b').pack(side='left',
                                                                                          padx=(p(6), 0))
        self.t_chips = tk.Frame(hl, bg=T['bg'])
        self.t_chips.pack(fill='x', pady=(p(12), 0))
        # chain card
        self.chain_card = W.Card(center, self.app, T['chain'], T['cardline'], 12, fit='both', under=T['bg'])
        self.chain_card.pack(fill='both', expand=True, padx=p(24), pady=(0, p(24)))
        cb = self.chain_card.body
        ch = tk.Frame(cb, bg=T['chain'])
        ch.pack(fill='x', padx=p(12), pady=(p(8), 0))
        self.chain_lbl = tk.Label(ch, text='CADEIA DE SINAL', font=F.tiny, bg=T['chain'], fg=T['muted'])
        self.chain_lbl.pack(side='left')
        self.legend = tk.Label(ch, text='', font=F.xs, bg=T['chain'], fg=T['faint'], anchor='e')
        self.legend.pack(side='right', fill='x', expand=True, padx=(p(16), 0))
        self.cv = tk.Canvas(cb, bg=T['chain'], highlightthickness=0)
        hs = ttk.Scrollbar(cb, orient='horizontal', command=self.cv.xview, style='Chain.Horizontal.TScrollbar')
        self.vs = ttk.Scrollbar(cb, orient='vertical', command=self.cv.yview, style='Chain.Vertical.TScrollbar')
        self.cv.configure(xscrollcommand=hs.set, yscrollcommand=self.vs.set)
        self.cv.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(-1 if e.delta > 0 else 1, 'units')
                     if self.vs.winfo_manager() else None)
        self.hs = hs
        self.cv.pack(fill='both', expand=True)
        self.cv.bind('<Configure>', lambda e: self.draw())
        self.cv.bind('<Button-1>', self._on_canvas_click)
        self.cv.bind('<B1-Motion>', self._on_canvas_drag)
        self.cv.bind('<ButtonRelease-1>', self._on_canvas_release)
        self.cv.bind('<Double-Button-1>', self._on_canvas_double)
        self.cv.bind('<Motion>', self._on_canvas_motion)
        self.cv.bind('<Delete>', lambda e: self.remove_selected())
        self.cv.bind('<Escape>', lambda e: self.drag_cancel())
        self.palette = Palette(center, self)            # packed by _title() while the preset is editable
        self.set_source(self.source, initial=True)

    # ------------------------------------------------------------------ source / connection
    def set_source(self, key, initial=False):
        self.source = key
        self.src_seg.set(active=key)
        if key == 'pedal':
            self.bank_row.pack_forget()
            self.presets, self.cache = [], {}
            self.shown = None
            self.sel_slot = None
            self._status()
            if self.connected:
                self.reload()
            elif not initial:
                self.toggle_connect()
        else:
            self.bank_row.pack(fill='x', pady=(self.p(10), 0), after=self.src_seg)
            if not self.bank_path or not os.path.exists(self.bank_path):
                P = self.app.project
                self.bank_path = P.base_path if P else ''
            self.load_bank(self.bank_path)
        self.fill_list()
        self.show(self.shown)

    def _status(self, text=None, color=None):
        """the header chip: connection state, or a passing message (connecting, reading, errors)"""
        T = self.W.T
        if text is None:
            if self.connected:
                text, color = self.worker_name or 'Conectado', T['green']
            else:
                text, color = 'Desconectado', T['faint']
        self.status_msg = text
        self.status.configure(text=self.W.fit_text(self.F.body, text, self.p(260)))
        self.dot.delete('all')
        aa.oval(self.dot, self.p(7) / 2, self.p(7) / 2, self.p(7) / 2, fill=color)
        i = self.chip.i
        x = self.p(12) - i + self.p(7) + self.p(8)
        self.status.place(in_=self.chip_inner, x=x, rely=.5, anchor='w')
        self.status.update_idletasks()
        x2 = x + self.status.winfo_reqwidth() + self.p(8)
        self.chip_usb.place(in_=self.chip_inner, x=x2, rely=.5, anchor='w')
        self.chip_inner.configure(width=x2 + self.chip_usb.winfo_reqwidth() + self.p(12) - i)
        self.app.set_device(('Porta: %s' % self.worker_name) if self.connected else '')
        if self.connected:
            self.btn_conn.config_btn(text='Desconectar')
        else:
            self.btn_conn.config_btn(text='Conectar')
        self._list_message()

    def _list_message(self):
        if self.presets:
            return
        if self.source == 'bank':
            msg = 'Nenhum preset no Bank deste arquivo.' if self.bank_path else 'Escolha um firmware .bin.'
        elif self.connected:
            msg = 'Lendo a lista de presets…'
        else:
            msg = 'Desconectado. Feche o editor original e clique em Conectar.' if self.btn_conn.enabled else 'Conectando…'
        self.plist.fill([], msg)

    def _slot_view(self):
        """the Capturas NAM / IRs view while it is the one on screen (it shares this header chip)"""
        return {'nam': getattr(self.app, 'nam_view', None), 'ir': getattr(self.app, 'ir_view', None),
                'global': getattr(self.app, 'global_view', None)}.get(self.app.mode)

    def toggle_connect(self):
        sv = self._slot_view()
        if sv is not None and sv.busy:      # a slot is being written: keep the port
            return
        if self.connected and (sv is not None or self.source == 'pedal'):
            self.worker.submit('disconnect', job_disconnect)
        else:
            self.connect_pedal()

    def connect_pedal(self):
        """also for the other views sharing the port: switch to the pedal source (the connection's preset list
        lands in this view) and connect"""
        if not self.btn_conn.enabled:       # connecting
            return
        if self.source != 'pedal':
            self.set_source('pedal')        # reloads when connected, else comes back here with the pedal source
            return
        if self.connected:
            return
        self._status('Conectando…', self.W.T['amber'])
        self.btn_conn.config_btn(enabled=False)
        self._list_message()
        self.worker.submit('connect', job_connect, self.port_name)

    def reload(self):
        sv = self._slot_view()
        if sv is not None:
            if self.connected:
                sv.reload()
            else:
                self.toggle_connect()
            return
        if self.source == 'pedal' and self.connected:
            self._status('Lendo a lista de presets…', self.W.T['amber'])
            self.cache = {}
            self.worker.submit('list', job_list)
        elif self.source == 'bank':
            self.load_bank(self.bank_path)
            self.fill_list()
        elif self.source == 'pedal':
            self.toggle_connect()

    def choose_bank(self):
        path = filedialog.askopenfilename(parent=self.app.root, title='Firmware com o Bank a mostrar',
                                          initialdir=os.path.dirname(self.bank_path or Q.ROOT),
                                          filetypes=[('Firmware QME-200', '*.bin'), ('Todos', '*.*')])
        if path:
            self.load_bank(path)
            self.fill_list()

    def load_bank(self, path):
        from matribox.firmware import fwtool
        self.presets, self.cache = [], {}
        self.shown = None
        if path:
            try:
                fw = fwtool.Firmware(open(path, 'rb').read())
                for pr in Q.bank_presets(fw.sections[3]):
                    self.cache[pr.index] = pr
                    self.presets.append((pr.index, pr.name, pr.style))
                self.bank_path = path
            except Exception as ex:
                self.app.toast_msg('Não foi possível ler o Bank de %s: %s' % (os.path.basename(path), ex), 'error')
        self.bank_lbl.configure(text=self.W.fit_text(self.F.mono_xs, os.path.basename(self.bank_path) or
                                                     'nenhum .bin', self.p(170)))
        self.cur = -1
        if self.presets and self.source == 'bank':
            self.after(10, lambda: self.select(self.presets[0][0]))

    # ------------------------------------------------------------------ results from the worker
    def _on_result(self, kind, res, err):
        if kind.startswith('nam'):
            if kind in ('nam_list', 'nam_write') and not err:
                self._set_clone_names(res if kind == 'nam_list' else res[2])
        elif kind.startswith('ir_'):
            if kind in ('ir_list', 'ir_write', 'ir_rename') and not err:
                self._set_ir_names(res if kind == 'ir_list' else res[-1])
        elif not kind.startswith('glob'):
            self._handle(kind, res, err)
        if kind.startswith(('nam', 'ir_', 'glob')) or kind in ('connect', 'list', 'disconnect', 'lost'):
            for f in self.listeners:
                f(kind, res, err)

    def _set_clone_names(self, names):
        self.names = (names, self.names[1])
        if self.db:
            self.db.clone_names = names
        self.draw()
        self.show_details()
        self.palette.refresh()

    def _set_ir_names(self, names):
        self.names = (self.names[0], names)
        if self.db:
            self.db.ir_names = names
        self.draw()
        self.show_details()
        self.palette.refresh()

    def _handle(self, kind, res, err):
        T = self.W.T
        if kind == 'db':
            self.db = res
            self.db.clone_names, self.db.ir_names = self.names
            self.draw()
            self.show_details()
            self.palette.refresh()
            return
        if kind == 'connect':
            self.btn_conn.config_btn(enabled=True)
            if err:
                self.connected = False
                self._status('Falhou', T['red'])
                self.plist.fill([], 'Não foi possível conectar: %s' % err)
                self.app.toast_msg('Não foi possível conectar ao pedal: %s' % err, 'error')
                return
            self.connected = True
            self.worker_name = res['name']
            kind = 'list'
        if kind == 'list':
            if err:
                self._status('Erro ao ler', T['red'])
                self.app.toast_msg('Erro ao ler o pedal: %s' % err, 'error')
                return
            self.names = (res['clone'], res['ir'])
            if self.db:
                self.db.clone_names, self.db.ir_names = self.names
            self.palette.refresh()
            self.presets = [(i, n, s) for i, _pos, n, s in res['list']]
            self.cur, self.live, self.modified = res['live']
            self.live.index = self.cur
            self._status()
            self.fill_list()
            self.select(self.cur)
            return
        if kind == 'disconnect' or kind == 'lost':
            self.connected = False
            self._status()
            if self.source == 'pedal':
                self.presets, self.cache, self.shown, self.live = [], {}, None, None
                self.fill_list()
                self.show(None)
            if kind == 'lost':
                self.app.toast_msg('Conexão com o pedal perdida: %s' % err, 'warn')
            return
        if kind == 'stored':
            if err:
                i = res[0] if res else -1
                if self.waiting == i:
                    self.waiting = None
                self.app.toast_msg('Erro ao ler o preset %s: %s' % (Q.preset_label(i), err), 'error')
                return
            self.cache[res.index] = res
            if self.waiting == res.index:
                self.waiting = None
                self.show(res)
            return
        if kind in ('live', 'activate'):
            if err:
                self.app.toast_msg('Erro ao %s: %s' % ('ativar o preset' if kind == 'activate' else 'ler o pedal',
                                                       err), 'error')
                return
            cur, pr, self.modified = res
            if kind == 'live' and cur == self.cur and (self.dragging or self.pending or self.chain_jobs or self.dnd):
                self._title()                   # mid-drag: the write that ends it triggers another re-read
                return
            changed = cur != self.cur
            self.cur, self.live = cur, pr
            pr.index = cur
            if changed or kind == 'activate':
                self.sel_index = None
                self.fill_list()
                self.select(cur)
            elif self.sel_index == cur and not self.dragging and not self.pending:
                self.show(pr, keep_slot=True)
            else:
                self._title()
            return
        if kind == 'check_mod':
            if err:
                self.app.toast_msg('Erro ao consultar o pedal: %s' % err, 'error')
                return
            i, self.modified = res
            self._title()
            if self.modified and not self.W.ask(
                    self.app, 'Trocar de preset',
                    'O preset %s "%s" tem mudanças que ainda não foram salvas no pedal. Ao ativar %s "%s" elas se '
                    'perdem (como ao trocar de preset no próprio pedal).' % (
                        Q.preset_label(self.cur), self.live.name if self.live else '', Q.preset_label(i),
                        self._name_of(i)), 'Descartar e ativar', 'danger'):
                return
            self.worker.submit('activate', job_activate, i)
            return
        if kind == 'modified':
            self.modified = res
            self._title()
            return
        if kind == 'patch':
            self._apply_patches(res)
            return
        if kind == 'write':
            if err:
                self.app.toast_msg('O pedal não aceitou a mudança: %s' % err, 'error')
            return
        if kind == 'chain':
            self._chain_done(res, err)
            return
        if kind == 'overload':
            self.app.toast_msg('O pedal avisou "System overload" (%s).' % (
                'CPU' if res == 1 else 'memória' if res == 2 else res), 'warn')

    # ------------------------------------------------------------------ list
    def fill_list(self):
        q = self.qv.get().strip().lower()
        items = [(i, name, style) for i, name, style in self.presets
                 if not q or q in name.lower() or q in style.lower() or q in Q.preset_label(i).lower()]
        self.count_lbl.configure(text='%d de %d' % (len(items), len(self.presets)) if self.presets else '')
        if self.presets:
            self.plist.fill(items, 'Nenhum preset encontrado.')
        else:
            self._list_message()

    def select(self, i):
        if i is None or i < 0:
            return
        self.pick(i)
        self.plist.see(i)

    def pick(self, i):
        if i == self.sel_index and self.shown is not None and self.shown.index == i:
            return
        self.sel_index = i
        self.sel_slot = None
        self.plist.draw()
        self._open(i)

    def _open(self, i):
        if self.source == 'pedal' and i == self.cur and self.live is not None:
            self.show(self.live)
        elif i in self.cache:
            self.show(self.cache[i])
        elif self.source == 'pedal' and self.connected:
            self.waiting = i
            self.show(None, loading=i)
            self.worker.submit('stored', job_stored, i)

    # ------------------------------------------------------------------ preset on screen
    def show(self, pr, keep_slot=False, loading=None):
        self.shown = pr
        if not keep_slot or pr is None or self.sel_slot is not None and pr.slots[self.sel_slot].empty:
            self.sel_slot = None
        self._title(loading)
        self.draw()
        self.show_details()

    def _title(self, loading=None):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        pr = self.shown
        edit = self.editable()
        if pr is None:
            label = Q.preset_label(loading) if loading is not None else ''
            name = 'Lendo do pedal…' if loading is not None else ('Escolha um preset' if self.presets else
                                                                  'Nenhum preset')
            chips = ()
            state = None
        else:
            label = Q.preset_label(pr.index) if pr.index >= 0 else ''
            name = pr.name or '(sem nome)'
            pan = 'centro' if pr.pan == 0 else ('R%d' % pr.pan if pr.pan > 0 else 'L%d' % -pr.pan)
            chips = (('Volume', str(pr.volume)), ('BPM', str(pr.bpm))) + ((('Pan', pan),) if pr.pan else ()) + \
                    ((('Estilo', pr.style),) if pr.style else ())
            state = ('live', 'Ao vivo — edições tocam no pedal na hora') if edit else \
                ('info', 'Ativo no pedal') if pr.live else \
                ('info', 'Salvo no pedal') if pr.source == 'pedal' else ('info', 'Bank de fábrica do firmware')
        mod = pr is not None and pr.live and self.modified
        can = (self.source == 'pedal' and self.connected and pr is not None and not pr.live and
               0 <= pr.index < Q.NPRESETS)
        key = (label, name, chips, state, mod, can, edit)
        if key != self.title_key:
            self.title_key = key
            if label:
                self.t_label.configure(text=label)
                self.t_label_card.pack(side='left', padx=(0, p(12)), before=self.t_name)
            else:
                self.t_label_card.pack_forget()
            self.t_name.configure(text=name)
            if mod:
                self.t_mod.pack(side='left', padx=(p(12), 0))
            else:
                self.t_mod.pack_forget()
            for w in self.t_chips.winfo_children():
                w.destroy()
            for k, v in chips:
                c = W.Card(self.t_chips, self.app, T['bg'], T['line2'], 7, fit='none')
                c.pack(side='left', padx=(0, p(8)))
                fr = tk.Frame(c.body, bg=T['bg'])
                fr.pack(padx=p(10) - c.i, pady=p(6) - c.i)
                tk.Label(fr, text=k, font=F.small, bg=T['bg'], fg=T['muted']).pack(side='left')
                tk.Label(fr, text=v, font=F.mono, bg=T['bg'], fg=T['text']).pack(side='left', padx=(p(8), 0))
            if state:
                live = state[0] == 'live'
                col = T['green'] if live else T['faint']
                tint = W.mix(T['green'], T['bg'], .9) if live else T['bg']
                c = W.Card(self.t_chips, self.app, tint, None if live else T['line'], 7, fit='none')
                c.pack(side='left')
                fr = tk.Frame(c.body, bg=tint)
                fr.pack(padx=p(10) - c.i, pady=p(6) - c.i)
                d = tk.Canvas(fr, width=p(6), height=p(6), bg=tint, highlightthickness=0)
                aa.oval(d, p(3), p(3), p(3), fill=col)
                d.pack(side='left')
                tk.Label(fr, text=state[1], font=F.small, bg=tint, fg=T['green_soft'] if live else T['muted']).pack(
                    side='left', padx=(p(7), 0))
            self.btn_act.pack_forget()
            self.btn_discard.pack_forget()
            if can:
                self.btn_act.pack(side='right')
            if edit and mod:
                self.btn_discard.pack(side='right')
        if edit and not self.palette.winfo_manager():
            self.chain_card.pack_configure(pady=(0, p(16)))
            self.palette.pack(side='bottom', fill='x', padx=p(24), pady=(0, p(24)), before=self.chain_card)
            self.palette.refresh()
        elif not edit and self.palette.winfo_manager():
            self.palette.pack_forget()
            self.chain_card.pack_configure(pady=(0, p(24)))
        n = len(pr.chain()) if pr is not None else 0
        self.chain_lbl.configure(text='CADEIA DE SINAL · %d bloco%s' % (n, 's' if n != 1 else '') if pr else
                                 'CADEIA DE SINAL')
        self.legend.configure(text='Clique para editar · duplo clique liga/desliga · arraste para mover · Delete '
                                   'remove' if edit else 'Clique num bloco para ver os parâmetros · apagados estão em '
                                                         'bypass')

    def editable(self):
        """the preset on screen is the pedal's edit buffer and the pedal is connected"""
        return (self.source == 'pedal' and self.connected and self.shown is not None and self.shown.live
                and self.shown is self.live)

    def _name_of(self, i):
        for k, n, _s in self.presets:
            if k == i:
                return n
        return ''

    # ------------------------------------------------------------------ writes
    def activate_shown(self):
        i = self.sel_index if self.sel_index is not None else (self.shown.index if self.shown else -1)
        if self.source != 'pedal' or not self.connected or not 0 <= i < Q.NPRESETS or i == self.cur:
            return
        self.worker.submit('check_mod', job_modified, i)

    def discard(self):
        """reload the active preset from the pedal's flash: its unsaved changes are lost"""
        if not self.editable() or not self.modified:
            return
        if not self.W.ask(self.app, 'Descartar as mudanças',
                          'As mudanças do preset %s "%s" que ainda não foram salvas no pedal serão perdidas: o pedal '
                          'recarrega o preset salvo.' % (Q.preset_label(self.cur), self.live.name if self.live else ''),
                          'Descartar', 'danger'):
            return
        self.worker.submit('activate', job_activate, self.cur)

    def toggle_slot(self, sid):
        if not self.editable():
            return
        s = self.live.slots[sid]
        s.on = not s.on
        self.worker.submit('write', job_on, sid, s.on)
        self.draw()
        self.show_details()

    # chain edits: applied here at once (Preset.edit does what the firmware does), then read back from the pedal
    def chain_edit(self, op, *args):
        if not self.editable():
            return
        pr = self.live
        if op == 'insert' and len(pr.order) >= Q.NSLOTS:
            self.app.toast_msg('A cadeia já tem %d blocos, o máximo do pedal.' % Q.NSLOTS, 'warn')
            return
        sid = pr.edit(op, *args, knobs=self.db.default_knobs if self.db else None)
        if sid is None:
            return
        self.chain_jobs += 1
        self.worker.submit('chain', job_chain, op, args)
        if op == 'remove':
            if self.sel_slot == sid:
                self.sel_slot = None
        else:
            self.sel_slot = sid
        self.modified = True
        self.show(pr, keep_slot=True)
        self.palette.layout()

    def _chain_done(self, res, err):
        self.chain_jobs = max(0, self.chain_jobs - 1)
        if err:
            self.app.toast_msg('Falha ao editar a cadeia: %s' % err, 'error')
            if not self.chain_jobs:
                self.worker.dirty = time.time()         # re-read what the pedal has
            return
        (cur, pr, mod), op, args, phantoms = res
        if phantoms:
            self.app.toast_msg('O firmware do pedal não tem esse efeito (é de outro firmware): o pedal deixou um '
                               'lugar vazio na cadeia, que já foi removido.', 'warn')
        if self.chain_jobs or cur != self.cur:
            return                                      # a later edit is on its way: its read-back wins
        mine = self.live
        same = mine is not None and [(s.sid, s.id) for s in mine.chain()] == [(s.sid, s.id) for s in pr.chain()]
        if not same and not phantoms:
            self.app.toast_msg('O pedal não aplicou a mudança na cadeia como esperado (memória insuficiente para '
                               'o bloco?). A cadeia mostrada agora é a do pedal.', 'warn')
        self.live, self.modified = pr, mod
        pr.index = cur
        if self.sel_slot is not None and pr.slots[self.sel_slot].empty:
            self.sel_slot = None
        if self.shown is mine or self.sel_index == cur:
            self.show(pr, keep_slot=True)

    def remove_selected(self):
        if self.editable() and self.sel_slot is not None and not self.live.slots[self.sel_slot].empty \
                and self.sel_slot in self.live.order and not self._routing(self.live.slots[self.sel_slot]):
            self.chain_edit('remove', self.live.pos(self.sel_slot))

    def move_selected(self, d):
        pr = self.live
        if not self.editable() or self.sel_slot is None or self.sel_slot not in pr.order:
            return
        old = pr.pos(self.sel_slot)
        if 0 <= old + d < len(pr.order):
            self.chain_edit('move', old, old + d)

    @staticmethod
    def _routing(s):
        return s.id in (Q.SPLIT, Q.PATH_B, Q.MERGE)

    def insert_effect(self, e, pos=None):
        """after the selected block, else at the end"""
        pr = self.live
        if not self.editable():
            return
        if pos is None:
            pos = len(pr.order)
            if self.sel_slot is not None and self.sel_slot in pr.order:
                pos = pr.pos(self.sel_slot) + 1
        self.chain_edit('insert', pos, e['menu'], e['id'])

    def click_effect(self, e):
        """palette click: same category as the selected block -> swap its effect; else insert after it"""
        if not self.editable():
            return
        pr = self.live
        s = pr.slots[self.sel_slot] if self.sel_slot is not None else None
        if s is not None and not s.empty and s.sid in pr.order and not self._routing(s) and s.menu == e['menu']:
            if s.id != e['id']:
                self.chain_edit('replace', pr.pos(s.sid), e['menu'], e['id'])
            return
        self.insert_effect(e)

    def set_knob(self, c, value, final=False):
        """optimistic local update; the message is sent at most every KNOB_EVERY ms"""
        if not self.editable() or self.sel_slot is None:
            return
        s = self.live.slots[self.sel_slot]
        v = Q.quantise(c, value)
        if 0 <= c['pos'] < len(s.knobs) and s.knobs[c['pos']] == v and not final:
            return
        if 0 <= c['pos'] < len(s.knobs):
            s.knobs[c['pos']] = v
        self.pending[(s.sid, c['order'])] = v
        self._refresh_control(c['order'])
        if final:
            self._flush()
        elif self.flush_job is None:
            self.flush_job = self.after(KNOB_EVERY, self._flush)

    def _flush(self):
        if self.flush_job is not None:
            self.after_cancel(self.flush_job)
            self.flush_job = None
        for (sid, order), v in self.pending.items():
            self.worker.submit('write', job_knob, sid, order, v)
        self.pending = {}

    def _apply_patches(self, patches):
        """changes pushed by the pedal (or echoes of ours): update the edit buffer in place"""
        pr = self.live
        if pr is None:
            return
        redraw = False
        for kind, sid, a, *b in patches:
            if not 0 <= sid < Q.NSLOTS:
                continue
            s = pr.slots[sid]
            if kind == 'on':
                if s.on != bool(a):
                    s.on, redraw = bool(a), True
            elif kind == 'knob' and 0 <= a < len(s.knobs):
                if self.dragging or any(k[0] == sid for k in self.pending):
                    continue                    # our own drag is ahead of this echo
                s.knobs[a] = b[0]
                if self.shown is pr and self.sel_slot == sid and self.db:
                    for c in Q.controls(self.db, s):
                        if c['pos'] == a:
                            self._refresh_control(c['order'])
        if redraw and self.shown is pr:
            self.draw()
            if not self.dragging:
                self.show_details()

    # ------------------------------------------------------------------ chain drawing
    def _effect(self, s):
        if self.db:
            return self.db.lookup(s)
        return dict(menu=s.menu, id=s.id, name='…', category='', params=[], origin='')

    def _grid(self, width, height):
        """the design's dot grid behind the chain: one tile image repeated"""
        T, p = self.W.T, self.p
        step = p(18)
        n = 8
        if self.grid_img is None:
            size = step * n
            img = tk.PhotoImage(master=self.cv, width=size, height=size)
            bg, dot = T['chain'], T['hover']
            half = self.W.mix(dot, bg, .55)
            rows = []
            for y in range(size):
                row = []
                for x in range(size):
                    dx, dy = (x - step // 2) % step, (y - step // 2) % step
                    row.append(dot if dx == 0 and dy == 0 else half if (dx, dy) in ((1, 0), (0, 1), (step - 1, 0),
                                                                                   (0, step - 1)) else bg)
                rows.append('{' + ' '.join(row) + '}')
            img.put(' '.join(rows))
            self.grid_img = img
        size = step * n
        for gx in range(0, int(width) + 1, size):
            for gy in range(0, int(height) + 1, size):
                self.cv.create_image(gx, gy, image=self.grid_img, anchor='nw', tags=('grid',))
        self.cv.tag_lower('grid')

    def draw(self):
        cv, T, F, p = self.cv, self.W.T, self.F, self.p
        cv.delete('all')
        self.hits, self.gaps, self.blocks = [], [], []
        pr = self.shown
        w, h = max(cv.winfo_width(), 50), max(cv.winfo_height(), 50)
        if pr is None:
            self._grid(w, h)
            cv.configure(scrollregion=(0, 0, w, h))
            self.hs.pack_forget()
            return
        ch = pr.chain()
        bw, bh, gap = p(156), p(104), p(28)
        self.block_h, self.gap_w = bh, gap
        side = p(56)                                    # room for IN / OUT
        if not ch:
            self._grid(w, h)
            xin, xout = w / 2 - p(140), w / 2 + p(140)
            self._io(xin, h / 2, 'IN')
            self._wire((xin + p(7), h / 2), (xout - p(7), h / 2))
            self._io(xout, h / 2, 'OUT')
            self.gap_w = p(60)
            self.gaps.append((w / 2, h / 2, 0))
            cv.create_text(w / 2, h / 2 + p(52), font=F.body, fill=T['muted'],
                           text='Preset sem blocos' + ('. Arraste um efeito da paleta para o fio.'
                                                       if self.editable() else ''))
            self._draw_drop()
            cv.configure(scrollregion=(0, 0, w, h))
            self.hs.pack_forget()
            return
        end = len(pr.order)                             # position after the last block
        rt = pr.routed()
        if rt:
            pre, pa, pb, post, n_split, n_pathb, n_merge = rt
            node = p(34)
            ncol = len(pre) + max(len(pa), len(pb), 1) + len(post)
            fixed = 2 * side + 2 * (node + gap)
            if fixed + ncol * (bw + gap) > w:           # shrink the blocks (and the gaps) before scrolling
                gap = p(20)
                fixed = 2 * side + 2 * (node + gap)
                bw = max(p(84), int((w - fixed) / ncol - gap))
            bh = min(bh, max(p(84), int((h - p(64)) / 2 - p(22))))
            self.block_h = bh
            self.gap_w = gap
            cols_w = ncol * (bw + gap) + 2 * (node + gap)
            total = side * 2 + cols_w
            width = max(w, total)
            height = max(h, 2 * bh + p(44) + p(48))
            self._grid(width, height)
            x0 = (width - total) / 2 + side
            ym = height / 2
            dy = bh / 2 + p(22)
            ya, yb = ym - dy, ym + dy
            xin = x0 - side + p(18)
            self._io(xin, ym, 'IN')
            x = x0
            prev = (xin + p(7), ym)
            gp = self._gap_at                           # drop places: (x, y, position in the order array)
            for s in pre:
                gp((prev[0] + x) / 2, ym, pr.pos(s.sid))
                prev = self._block(s, x, ym, bw, bh, prev)
                x += bw + gap
            # split node
            sx = x + node / 2
            gp((prev[0] + sx - node / 2) / 2, ym, pr.pos(n_split.sid))
            self._wire(prev, (sx - node / 2, ym))
            self._node(n_split, sx, ym, node, 'SPLIT')
            x += node + gap
            xa = xb = x
            x = x0 + (len(pre)) * (bw + gap) + node + gap + max(len(pa), len(pb), 1) * (bw + gap)
            mx = x + node / 2
            a_end = pr.pos(n_pathb.sid) if n_pathb is not None else pr.pos(n_merge.sid)
            pa_prev = self._elbow((sx, ym - node / 2), (xa, ya))
            self._tag(None, xa - gap / 2, ya, 'A')
            for s in pa:
                gp((pa_prev[0] + xa) / 2, ya, pr.pos(s.sid))
                pa_prev = self._block(s, xa, ya, bw, bh, pa_prev)
                xa += bw + gap
            gp((pa_prev[0] + mx) / 2 if not pa else pa_prev[0] + gap / 2, ya, a_end)
            pb_prev = self._elbow((sx, ym + node / 2), (xb, yb))
            if n_pathb is not None:
                self._tag(n_pathb, xb - gap / 2, yb)
                for s in pb:
                    gp((pb_prev[0] + xb) / 2, yb, pr.pos(s.sid))
                    pb_prev = self._block(s, xb, yb, bw, bh, pb_prev)
                    xb += bw + gap
                gp((pb_prev[0] + mx) / 2 if not pb else pb_prev[0] + gap / 2, yb, pr.pos(n_merge.sid))
            self._wire(pa_prev, (mx, ya)); self._wire((mx, ya), (mx, ym - node / 2))
            self._wire(pb_prev, (mx, yb)); self._wire((mx, yb), (mx, ym + node / 2))
            self._node(n_merge, mx, ym, node, 'MERGE')
            prev = (mx + node / 2, ym)
            x += node + gap
            for s in post:
                gp((prev[0] + x) / 2, ym, pr.pos(s.sid))
                prev = self._block(s, x, ym, bw, bh, prev)
                x += bw + gap
            xout = x - gap + side - p(18)
            gp((prev[0] + xout - p(7)) / 2, ym, end)
            self._wire(prev, (xout - p(7), ym))
            self._io(xout, ym, 'OUT')
        else:
            avail = w - 2 * side
            n = len(ch)
            fit = (avail + gap) / n - gap
            max_rows = max(1, int((h - p(20)) // (p(56) + p(24))))     # rows that fit the card's height
            if fit >= p(116) or max_rows == 1:          # one row: blocks shrunk to 116 px (84 when low)
                per = n
                bw = max(p(84), min(bw, int(fit)))
                bh = max(p(64), min(bh, h - p(64)))
                self.block_h = bh
            else:                                       # balanced rows that always fit
                gap = p(24)
                per = max(2, int((avail + gap) // (p(116) + gap)))
                rows_n = min(-(-n // per), max_rows)
                per = -(-n // rows_n)
                bw = max(p(84), min(bw, int((avail + gap) / per - gap)))
            self.gap_w = gap
            rows = [ch[k:k + per] for k in range(0, len(ch), per)]
            rh = bh + p(46)
            if len(rows) * rh + p(40) > h:              # lower blocks (no subtitle) before scrolling
                bh = max(p(56), int((h - p(20)) / len(rows) - p(24)))
                rh = bh + p(24)
                self.block_h = bh
            height = max(h, len(rows) * rh + p(20))
            width = max(w, side * 2 + per * (bw + gap) - gap)
            self._grid(width, height)
            row_w = per * (bw + gap) - gap
            x0 = (width - row_w) / 2
            y0 = (height - len(rows) * rh) / 2 + rh / 2
            xin = x0 - side + p(18)
            self._io(xin, y0, 'IN')
            prev = (xin + p(7), y0)
            for r, row in enumerate(rows):
                y = y0 + r * rh
                if r:                                   # carriage return from the end of the previous row
                    ye = y - rh / 2
                    self._gap_at(prev[0] + gap / 2, prev[1], pr.pos(row[0].sid))   # end of the row above
                    self._wire(prev, (prev[0] + gap / 2, prev[1]))
                    self._wire((prev[0] + gap / 2, prev[1]), (prev[0] + gap / 2, ye))
                    self._wire((prev[0] + gap / 2, ye), (x0 - gap / 2, ye))
                    self._wire((x0 - gap / 2, ye), (x0 - gap / 2, y))
                    prev = (x0 - gap / 2, y)
                x = x0
                for s in row:
                    self._gap_at((prev[0] + x) / 2, y, pr.pos(s.sid))
                    prev = self._block(s, x, y, bw, bh, prev)
                    x += bw + gap
            xout = prev[0] + side - p(18)
            self._gap_at((prev[0] + xout - p(7)) / 2, prev[1], end)
            self._wire(prev, (xout - p(7), prev[1]))
            self._io(xout, prev[1], 'OUT')
        self._draw_drop()
        cv.tag_raise('lblbg')                           # node labels above the wires drawn after them
        cv.tag_raise('lbl')
        cv.tag_raise('dnd')                             # the drop marker above everything
        cv.configure(scrollregion=(0, 0, width, height))
        if width > w + 1:
            self.hs.pack(side='bottom', fill='x', before=self.cv)
        else:
            self.hs.pack_forget()
            cv.xview_moveto(0)
        if height > h + 1:
            self.vs.pack(side='right', fill='y', before=self.cv)
        else:
            self.vs.pack_forget()
            cv.yview_moveto(0)

    # Shapes go through aa.py (anti-aliased images): Tk draws canvas polygons/ovals with GDI, without AA.
    # Wires stay canvas lines: they are horizontal/vertical, so they are already crisp.
    def _wire(self, a, b):
        T, p = self.W.T, self.p
        self.cv.create_line(a[0], a[1], b[0], b[1], fill=T['wire'], width=max(2, p(2)), capstyle='round')

    def _elbow(self, a, b):
        """vertical out of a node, then horizontal: returns where the next block's wire starts"""
        self._wire(a, (a[0], b[1]))
        return (a[0], b[1])

    def _io(self, x, y, text):
        cv, T, p = self.cv, self.W.T, self.p
        r = p(7)
        aa.oval(cv, x, y, r, fill=T['chain'], outline=T['dim'], width=max(2, p(2)), bg=T['chain'])
        cv.create_text(x, y + r + p(6), text=text, font=self.F.mono_s, fill=T['faint'], anchor='n')
        return (x, y)

    def _block(self, s, x, y, bw, bh, prev):
        """draw slot s with its left edge at x, centred on y; wire from prev; returns the output point"""
        cv, T, F, p, W = self.cv, self.W.T, self.F, self.p, self.W
        x, bw, bh = round(x), round(bw), round(bh)
        self._wire(prev, (x, y))
        e = self._effect(s)
        menu = e['menu'] if e['menu'] is not None and e['menu'] >= 0 else s.menu
        on = s.on
        sel = self.sel_slot == s.sid
        d = self.dnd
        t = d and d.get('target')
        swap = bool(t) and t[0] == 'replace' and t[1] == s.sid     # preview of the effect dropped on it
        faded = bool(d) and d['kind'] == 'block' and d['sid'] == s.sid
        if swap:
            e = dict(d['effect'], category='trocar por')
            menu, on = e['menu'], True
        col = W.menu_color(menu)
        under = T['chain']
        x1, y1 = x, round(y - bh / 2)
        x2, y2 = x1 + bw, y1 + bh
        fill = W.mix(col, T['tile'], .84 if sel else .91)
        line = W.mix(col, under, .1 if sel else .65)
        ink, sub_ink, code_ink, dot = T['text'], T['muted'], col, T['green']
        if not on:                                      # bypass: the whole block at 45 % opacity
            fade = lambda c: W.mix(c, under, .55)
            fill, line, ink, sub_ink, code_ink, dot = map(fade, (fill, line, ink, sub_ink, code_ink, T['dim']))
        if faded:                                       # the block being dragged: a ghost of itself
            fill, line, ink, sub_ink, code_ink, dot = under, W.mix(T['line2'], under, .3), T['dim'], T['dim'], \
                T['dim'], T['dim']
        lw, r = W.lw_of(self.S), p(12)
        shapes = []
        bx1, by1, bx2, by2 = x1, y1, x2, y2
        if sel or swap:                                 # the design's glow ring
            g = p(4)
            bx1, by1, bx2, by2 = x1 - g, y1 - g, x2 + g, y2 + g
            shapes.append(('rrect', (bx1, by1, bx2, by2, r + g),
                           W.mix(T['green'], under, .7) if swap else W.mix(col, under, .8)))
        shapes += [('rrect', (x1, y1, x2, y2, r), T['green'] if swap else line),
                   ('rrect', (x1 + lw, y1 + lw, x2 - lw, y2 - lw, r - lw), fill)]
        aa.draw(cv, bx1, by1, bx2, by2, shapes, under)
        low = bh < p(84)                                # low blocks: no subtitle, the header row closer to the top
        ty = y1 + (p(14) if low else p(18))
        cv.create_text(x1 + p(12), ty, text=Q.MENU_SHORT.get(menu, '?'), font=F.mono_s, fill=code_ink, anchor='w')
        aa.oval(cv, x2 - p(12) - p(3.5), ty, p(3.5), fill=dot, bg=fill)
        sub = e.get('category') or ''
        if not any(c.isalnum() for c in sub):
            sub = ''
        if menu in (5, 6, 11) and not swap:
            sub = {5: 'Cab', 6: 'User IR', 11: 'Clone'}[menu]
        name = e['name']
        if sub == name:
            sub = ''
        room = bw - p(24)
        yb = y2 - (p(8) if low else p(10))
        if sub and not low:
            cv.create_text(x1 + p(12), yb, text=W.fit_text(F.xs, sub, room), font=F.xs,
                           fill=T['green'] if swap else sub_ink, anchor='sw')
            yb -= F.xs.metrics('linespace') + p(1)
        nf = F.semi if bw >= p(120) else F.medium
        cv.create_text(x1 + p(12), yb, text=W.fit_text(nf, name, room), font=nf, fill=ink, anchor='sw')
        self.hits.append((x1, y1, x2, y2, s.sid))
        self.blocks.append((x1, y1, x2, y2, s.sid))
        return (x2, y)

    def _gap_at(self, x, y, pos):
        self.gaps.append((x, y, pos))

    def _draw_drop(self):
        """the marker on the wire where the dragged effect will go: a bar across the wire with a round head,
        and a label with what will happen and the resulting position"""
        d = self.dnd
        t = d and d.get('target')
        if not t or t[0] not in ('gap', 'full'):
            return
        cv, T, F, p = self.cv, self.W.T, self.F, self.p
        x, y, pos = t[1]
        col = T['red'] if t[0] == 'full' else T['green'] if d['kind'] == 'new' else T['accent']
        hb = self.block_h / 2 + p(6)
        r = min(p(12), max(p(7), self.gap_w / 2 - p(1)))
        bar = max(2.0, p(2))
        shapes = [('rrect', (x - bar, y - hb, x + bar, y + hb, bar), col), ('circle', (x, y, r), col)]
        ink = T['chain']
        a, sw = r * .45, max(1.1, p(1.3))
        if d['kind'] == 'new':                          # plus: something is added here
            shapes += [('seg', (x - a, y, x + a, y, sw), ink), ('seg', (x, y - a, x, y + a, sw), ink)]
        else:                                           # arrow down into the wire: the block lands here
            shapes += [('seg', (x, y - a, x, y + a * .7, sw), ink),
                       ('poly', (x - a * .75, y + a * .05, x + a * .75, y + a * .05, x, y + a * 1.05), ink)]
        aa.draw(cv, x - r, y - hb, x + r, y + hb, shapes, T['chain'], tags=('dnd',))
        txt = t[2]
        tw = F.badge.measure(txt) + p(16)
        ty = y - hb - p(13)
        aa.rrect(cv, x - tw / 2, ty - p(10), x + tw / 2, ty + p(10), p(10), fill=col, bg=T['chain'], tags=('dnd',))
        cv.create_text(x, ty, text=txt, font=F.badge, fill=T['chain'], tags=('dnd',))

    def _node(self, s, x, y, d, label):
        cv, T, F, p = self.cv, self.W.T, self.F, self.p
        col = T['soft'] if s.on else T['dim']
        sel = self.sel_slot == s.sid
        r = d / 2
        lw = max(1.5, p(2))
        R = r + (p(4) if sel else 0)
        under = T['chain']
        shapes = []
        if sel:
            shapes.append(('circle', (x, y, R), self.W.mix(T['accent'], under, .6)))
        shapes += [('circle', (x, y, r), T['accent'] if sel else col),
                   ('circle', (x, y, r - lw), self.W.mix(col, T['tile'], .85))]
        a, sw = r * .5, max(1.0, p(1))                  # fork glyph: one line in, two out (mirrored for Merge)
        sgn = 1 if label == 'SPLIT' else -1
        shapes.append(('seg', (x - sgn * a, y, x, y, sw), col))
        for dy in (-a * .8, a * .8):
            shapes.append(('seg', (x, y, x + sgn * a, y + dy, sw), col))
        aa.draw(cv, x - R, y - R, x + R, y + R, shapes, under, tags=('lbl',))   # above the branch wires
        # label over the branch wire that leaves the node's bottom, on a patch of background
        t = cv.create_text(x, y + r + p(12), text=label, font=F.mono_s, fill=T['faint'], tags=('lbl',))
        bx = cv.bbox(t)
        cv.create_rectangle(bx[0] - p(3), bx[1] - p(1), bx[2] + p(3), bx[3] + p(1), fill=under, outline='',
                            tags=('lblbg',))
        self.hits.append((x - r, y - r, x + r, y + r, s.sid))

    def _tag(self, s, x, y, text='B'):
        """branch marker: B is the Path B slot (clickable), A only a label"""
        cv, T, F, p = self.cv, self.W.T, self.F, self.p
        col = T['soft'] if s is None or s.on else T['dim']
        if s is not None and self.sel_slot == s.sid:
            col = T['accent']
        r = p(10)
        # tagged 'lbl': raised above the wire of the branch's first block, drawn after it
        aa.oval(cv, x, y, r, fill=T['chain'], outline=col, width=self.W.lw_of(self.S) + .5, bg=T['chain'],
                tags=('lbl',))
        cv.create_text(x, y, text=text, font=F.mono_s, fill=col, tags=('lbl',))
        if s is not None:
            self.hits.append((x - r, y - r, x + r, y + r, s.sid))

    # ------------------------------------------------------------------ drag and drop
    def drag_start(self, kind, xr, yr, effect=None, sid=None):
        """kind 'new': an effect from the palette; 'block': a block of the chain (slot sid)"""
        if not self.editable() or self.dnd:
            return
        if kind == 'block':
            effect = self._effect(self.live.slots[sid])
        self.dnd = dict(kind=kind, effect=effect, sid=sid, target=None)
        self.cv.focus_set()
        p = self.p
        g = tk.Toplevel(self)
        g.overrideredirect(True)
        key = '#010203'                                 # transparent colour: rounded corners
        try:
            g.attributes('-topmost', True)
            g.attributes('-alpha', .92)
            g.attributes('-transparentcolor', key)
        except tk.TclError:
            pass
        g.configure(bg=key)
        self.ghost = (g, tk.Canvas(g, width=p(156), height=p(48), bg=key, highlightthickness=0), key)
        self.ghost[1].pack()
        self.ghost_state = None
        self.drag_motion(xr, yr)
        if kind == 'block':
            self.draw()

    def drag_motion(self, xr, yr):
        d = self.dnd
        if not d:
            return
        t = self._target(xr, yr)
        state = 'remove' if t and t[0] == 'remove' else 'none' if not t or t[0] == 'full' else 'ok'
        if self.ghost:
            g, c, key = self.ghost
            if state != self.ghost_state:
                self.ghost_state = state
                self._ghost_draw(c, key, d['effect'], state)
            g.geometry('+%d+%d' % (xr + self.p(14), yr + self.p(12)))
        if t != d['target']:
            d['target'] = t
            self.draw()

    def drag_end(self, xr, yr):
        d = self.dnd
        if not d:
            return
        t = self._target(xr, yr)
        self.drag_cancel(redraw=False)
        pr = self.live
        if not t or pr is None:
            self.draw()
            return
        e = d['effect']
        if d['kind'] == 'new' and t[0] == 'gap':
            self.chain_edit('insert', t[1][2], e['menu'], e['id'])
        elif d['kind'] == 'new' and t[0] == 'replace':
            self.chain_edit('replace', pr.pos(t[1]), e['menu'], e['id'])
        elif d['kind'] == 'block' and t[0] == 'gap':
            old, g = pr.pos(d['sid']), t[1][2]
            self.chain_edit('move', old, g if g <= old else g - 1)     # move takes the final position
        elif d['kind'] == 'block' and t[0] == 'remove':
            self.chain_edit('remove', pr.pos(d['sid']))
        else:
            self.draw()

    def drag_cancel(self, redraw=True):
        if self.ghost:
            self.ghost[0].destroy()
            self.ghost = None
        was, self.dnd = self.dnd, None
        if was and redraw:
            self.draw()

    def _target(self, xr, yr):
        """what dropping at the root point would do: ('gap', (x, y, position), label) insert / move there,
        ('full', gap, label), ('replace', slot id), ('remove',), or None (nothing)"""
        d, cv, pr = self.dnd, self.cv, self.live
        if not d or pr is None or self.shown is not pr:
            return None
        lx, ly = xr - cv.winfo_rootx(), yr - cv.winfo_rooty()
        if not (0 <= lx < cv.winfo_width() and 0 <= ly < cv.winfo_height()):
            return ('remove',) if d['kind'] == 'block' else None
        x, y = cv.canvasx(lx), cv.canvasy(ly)
        want = None
        for x1, y1, x2, y2, sid in self.blocks:
            if x1 <= x <= x2 and y1 <= y <= y2:
                if d['kind'] == 'new':
                    q = (x2 - x1) * .22                 # middle of a block: replace; its sides: insert beside
                    if x1 + q <= x <= x2 - q:
                        return None if pr.slots[sid].id == d['effect']['id'] else ('replace', sid)
                elif sid == d['sid']:
                    return None
                want = pr.pos(sid) + (0 if x < (x1 + x2) / 2 else 1)
                break
        cands = [g for g in self.gaps if want is None or g[2] == want]
        if not cands:
            return None
        g = min(cands, key=lambda g: abs(g[0] - x) + 1.6 * abs(g[1] - y))
        if want is None and abs(g[1] - y) > self.block_h:
            return None
        before = sum(1 for s in pr.chain() if not self._routing(s) and pr.pos(s.sid) < g[2])
        if d['kind'] == 'new':
            if len(pr.order) >= Q.NSLOTS:
                return ('full', g, 'cadeia cheia (%d blocos)' % Q.NSLOTS)
            return ('gap', g, 'inserir aqui  ·  %dº bloco' % (before + 1))
        old = pr.pos(d['sid'])
        if g[2] in (old, old + 1):
            return None
        return ('gap', g, 'mover para cá  ·  %dº bloco' % (before + (0 if old < g[2] else 1)))

    def _ghost_draw(self, c, key, e, state):
        T, F, p, W = self.W.T, self.F, self.p, self.W
        c.delete('all')
        w, h = int(c.cget('width')), int(c.cget('height'))
        col = W.menu_color(e['menu'])
        if state == 'remove':
            col = T['red']
        fill = W.mix(col, T['tile'], .84)
        if state == 'none':
            fill, col = T['raised'], T['line2']
        aa.rrect(c, 1, 1, w - 1, h - 1, p(12), fill=fill, outline=col, width=max(1.5, p(2)), bg=key)
        name = e['name']
        if state == 'remove':
            c.create_text(w / 2, h / 2 - p(8), text=W.fit_text(F.small, name, w - p(20)), font=F.small, fill=T['text'])
            c.create_text(w / 2, h / 2 + p(9), text='soltar para remover', font=F.badge, fill=T['red'])
        else:
            c.create_text(p(12), p(13), text=Q.MENU_SHORT.get(e['menu'], ''), font=F.mono_s, fill=col, anchor='w')
            c.create_text(p(12), h - p(9), text=W.fit_text(F.semi, name, w - p(24)), font=F.semi, anchor='sw',
                          fill=T['text'] if state == 'ok' else T['muted'])

    def _hit(self, e):
        x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        for x1, y1, x2, y2, sid in self.hits:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return sid
        return None

    def _on_canvas_drag(self, e):
        if self.dnd:
            self.drag_motion(e.x_root, e.y_root)
            return
        pr = self.press
        if not pr or pr[2] is None or not self.editable():
            return
        if abs(e.x_root - pr[0]) + abs(e.y_root - pr[1]) < self.p(6):
            return
        if pr[2] in [b[4] for b in self.blocks]:        # effect blocks only: Split / Path B / Merge stay put
            self.drag_start('block', e.x_root, e.y_root, sid=pr[2])

    def _on_canvas_release(self, e):
        self.press = None
        if self.dnd:
            self.drag_end(e.x_root, e.y_root)

    def _on_canvas_click(self, e):
        sid = self._hit(e)
        self.press = (e.x_root, e.y_root, sid)
        self.cv.focus_set()
        if sid != self.sel_slot:
            self.sel_slot = sid
            self.draw()
            self.show_details()
            if self.palette.winfo_manager():
                self.palette._hint()

    def _on_canvas_double(self, e):
        sid = self._hit(e)
        if sid is not None:
            self.toggle_slot(sid)

    def _on_canvas_motion(self, e):
        self.cv.configure(cursor='hand2' if self._hit(e) is not None else '')

    # ------------------------------------------------------------------ details panel
    def show_details(self):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        box = self.det.inner
        for w in box.winfo_children():
            w.destroy()
        for w in self.det_foot.winfo_children():
            w.destroy()
        self.det_foot.pack_forget()
        self.det.canvas.yview_moveto(0)
        bg = T['panel']
        pad = dict(padx=p(20))
        pr = self.shown
        if pr is not None and (self.sel_slot is None or pr.slots[self.sel_slot].empty):
            self.det.pack_forget()
            self.empty_sub.configure(text='Clique em um bloco da cadeia para %s seus parâmetros.' % (
                'editar' if self.editable() else 'ver'))
            self.empty_box.place(relx=.5, rely=.45, anchor='center')
            return
        self.empty_box.place_forget()
        if not self.det.winfo_manager():
            self.det.pack(fill='both', expand=True)
        if pr is None:
            self._tips(box)
            return
        s = pr.slots[self.sel_slot]
        e = self._effect(s)
        menu = e['menu'] if e['menu'] >= 0 else s.menu
        edit = self.editable()
        col = W.menu_color(menu)
        head = tk.Frame(box, bg=bg)
        head.pack(fill='x', pady=(p(20), 0), **pad)
        W.Pill(head, self.app, Q.MENU_SHORT.get(menu, '?'), col, font=F.mono_s, height=20, padx=7).pack(side='left')
        tk.Label(head, text=W.C.MENUS.get(menu, str(menu)), font=F.small, bg=bg, fg=T['muted']).pack(
            side='left', padx=(p(8), 0))
        tk.Label(head, text='%#010x' % s.id, font=F.mono_s, bg=bg, fg=T['faint']).pack(side='right')
        tk.Label(box, text=e['name'], font=F.h_block, bg=bg, fg=T['bright'], wraplength=p(300), justify='left',
                 anchor='w').pack(anchor='w', fill='x', pady=(p(14), 0), **pad)
        if e.get('origin'):
            tk.Label(box, text='de %s' % e['origin'], font=F.xs, bg=bg, fg=T['faint']).pack(anchor='w', **pad)
        if edit:
            W.ToggleRow(box, self.app, 'Ligado' if s.on else 'Desligado (bypass)', s.on,
                        lambda: self.toggle_slot(s.sid), color=col).pack(fill='x', pady=(p(14), 0), **pad)
        else:
            W.Pill(box, self.app, 'Ligado' if s.on else 'Desligado (bypass)', T['green'] if s.on else T['muted'],
                   font=F.small, height=26, padx=10).pack(anchor='w', pady=(p(12), 0), **pad)
        ctls = Q.controls(self.db, s) if self.db else []
        self.ctl = {}
        self.app._section(box, 'Parâmetros', 'duplo clique = padrão' if edit and ctls else None)
        on_col = col if s.on else T['dim']
        for c in ctls:
            r = tk.Frame(box, bg=bg)
            r.pack(fill='x', padx=p(20), pady=(p(4), 0))
            tk.Label(r, text=c['param']['name'], font=F.body, bg=bg, fg=T['soft']).pack(side='left')
            bar = lbl = None
            if c['options'] and edit:
                choices = c['options'][int(c['lo']):int(c['hi']) + 1]
                k = int(round(c['value'] - c['lo']))
                cur = choices[k] if 0 <= k < len(choices) else ''
                if len(choices) <= 3 and sum(F.small.measure(x) for x in choices) < p(150):
                    sb = W.SegBar(r, self.app, [(x, x) for x in choices], cur, None, height=28, radius=7, inner=5,
                                  pad=10, font=F.small, margin=2)
                    sb.command = lambda v, c=c, ch=choices, sb=sb: (sb.set(active=v),
                                                                     self.set_knob(c, c['lo'] + ch.index(v), True))
                    sb.pack(side='right')
                else:
                    dd = W.Dropdown(r, self.app, choices, cur, None, width=None, height=30, font=F.small)
                    dd.command = lambda v, c=c, ch=choices: self.set_knob(c, c['lo'] + ch.index(v), final=True)
                    dd.configure(width=max(p(90), min(p(170), max(F.small.measure(x) for x in choices) + p(40))))
                    dd.pack(side='right')
            else:
                dflt = c['param'].get('default')
                changed = dflt is not None and abs(c['value'] - dflt) > 1e-6
                lbl = tk.Label(r, text=c['text'], font=F.mono, bg=bg,
                               fg=W.menu_color(menu, .82, .12) if changed and s.on else T['text'])
                lbl.pack(side='right')
            if not c['options'] and c['hi'] > c['lo']:
                bar = tk.Canvas(box, height=p(18) if edit else p(10), bg=bg, highlightthickness=0,
                                cursor='sb_h_double_arrow' if edit else '')
                bar.pack(fill='x', padx=p(20), pady=(p(4), p(8)))
                bar.bind('<Configure>', lambda ev, o=c['order']: self._refresh_control(o, text=False))
                if edit:
                    bar.bind('<Button-1>', lambda ev, c=c, b=bar: self._drag(c, b, ev, 'start'))
                    bar.bind('<B1-Motion>', lambda ev, c=c, b=bar: self._drag(c, b, ev, 'move'))
                    bar.bind('<ButtonRelease-1>', lambda ev, c=c, b=bar: self._drag(c, b, ev, 'end'))
                    bar.bind('<Double-Button-1>',
                             lambda ev, c=c: self.set_knob(c, c['param'].get('default', c['lo']), final=True))
            self.ctl[c['order']] = (lbl, bar, on_col, menu)
        if not ctls:
            raw = ', '.join('%g' % v for v in s.knobs)
            tk.Label(box, text='Efeito sem descrição conhecida. Valores brutos:\n' + raw, font=F.small, bg=bg,
                     fg=T['soft'], wraplength=p(300), justify='left').pack(anchor='w', **pad)
        elif edit:
            tk.Label(box, text='As mudanças vão para o preset em edição do pedal: salve no pedal para guardá-las.',
                     font=F.xs, bg=bg, fg=T['faint'], wraplength=p(300), justify='left').pack(
                anchor='w', pady=(p(6), 0), **pad)
        self.app._section(box, 'Posição')
        chain = pr.chain()
        pos = [x.sid for x in chain].index(s.sid) + 1 if s in chain else '?'
        row = tk.Frame(box, bg=bg)
        row.pack(fill='x', **pad)
        for t, f, fg in (('Bloco ', F.body, T['soft']), (str(pos), F.mono, T['text']),
                         (' de %d · slot interno ' % len(chain), F.body, T['soft']), (str(s.sid), F.mono, T['text'])):
            tk.Label(row, text=t, font=f, bg=bg, fg=fg, padx=0).pack(side='left')
        if edit and s.sid in pr.order:
            o = pr.pos(s.sid)
            bb = W.Btn(row, self.app, '→', None, lambda: self.move_selected(1), 'ghost', height=28, width=30, padx=0,
                       radius=6, tip='Mover para a direita')
            bb.pack(side='right')
            bb.config_btn(enabled=o + 1 < len(pr.order))
            ba = W.Btn(row, self.app, '←', None, lambda: self.move_selected(-1), 'ghost', height=28, width=30, padx=0,
                       radius=6, tip='Mover para a esquerda')
            ba.pack(side='right', padx=(0, p(4)))
            ba.config_btn(enabled=o > 0)
        tk.Frame(box, bg=bg, height=p(24)).pack()
        if edit and not self._routing(s):
            self.det_foot.pack(side='bottom', fill='x', before=self.det)
            tk.Frame(self.det_foot, bg=T['line'], height=1).pack(fill='x')
            W.Btn(self.det_foot, self.app, 'Remover da cadeia', None, self.remove_selected, 'danger', height=36,
                  width=300, padx=0, font=F.body, tip='Tecla Delete; ou arraste o bloco para fora da cadeia').pack(
                padx=p(20), pady=p(14))

    def _refresh_control(self, order, text=True):
        """redraw one parameter of the details panel from the slot's current knobs"""
        if order not in self.ctl or self.shown is None or self.sel_slot is None or not self.db:
            return
        lbl, bar, col, menu = self.ctl[order]
        cs = Q.controls(self.db, self.shown.slots[self.sel_slot])
        if order >= len(cs):
            return
        c = cs[order]
        try:
            if text and lbl is not None:
                dflt = c['param'].get('default')
                changed = dflt is not None and abs(c['value'] - dflt) > 1e-6
                lbl.configure(text=c['text'], fg=self.W.menu_color(menu, .82, .12) if changed and
                              self.shown.slots[self.sel_slot].on else self.W.T['text'])
            if bar is not None:
                frac = min(1.0, max(0.0, (c['value'] - c['lo']) / (c['hi'] - c['lo'])))
                self._bar(bar, frac, col, handle=self.editable())
        except tk.TclError:
            pass

    def _drag(self, c, bar, ev, phase):
        w = max(1, bar.winfo_width())
        r = self.p(7)
        frac = min(1.0, max(0.0, (ev.x - r) / max(1, w - 2 * r)))
        self.dragging = phase != 'end'
        self.set_knob(c, c['lo'] + frac * (c['hi'] - c['lo']), final=phase == 'end')

    def _bar(self, c, frac, col, handle=False):
        """the design's slider: 4 px track, coloured fill, white 14 px thumb"""
        T, p = self.W.T, self.p
        c.delete('all')
        w, h = c.winfo_width(), c.winfo_height()
        if w < 4 or h < 2:
            return
        bg = T['panel']
        r = p(7)
        y0, y1 = h / 2 - p(2), h / 2 + p(2)
        x = r + (w - 2 * r) * frac
        shapes = [('rrect', (0, y0, w, y1, p(2)), T['line2'])]
        if frac > 0:
            shapes.append(('rrect', (0, y0, max(p(4), x), y1, p(2)), col))
        if handle:
            shapes += [('circle', (x, h / 2 + p(1), r), self.W.mix('#000000', bg, .6)),
                       ('circle', (x, h / 2, r), T['bright'])]
        aa.draw(c, 0, 0, w, h, shapes, bg)

    def _pick_slot(self, sid):
        self.sel_slot = sid
        self.draw()
        self.show_details()

    def _tips(self, box):
        T, F, p = self.W.T, self.F, self.p
        bg = T['panel']
        tk.Label(box, text='Presets e cadeias', font=F.h2, bg=bg, fg=T['bright']).pack(anchor='w', padx=p(20),
                                                                                     pady=(p(22), p(14)))
        tips = ['Pedal (USB): lê a lista, o preset em edição (com mudanças ainda não salvas) e qualquer preset '
                'salvo, sem trocar o preset ativo. Feche o editor original antes: a porta MIDI só aceita um '
                'programa por vez.',
                'O preset ativo no pedal tem a letra em laranja; mudanças feitas no pedal (trocar de preset, ligar '
                'um bloco, mexer num knob) aparecem aqui na hora.',
                'No preset ativo: duplo clique num bloco liga/desliga, os parâmetros ficam no painel e a paleta '
                'monta a cadeia. Duplo clique num preset da lista (ou Ativar no pedal) troca o preset.',
                'Tudo vai para o preset em edição, como pelos botões do pedal: para guardar, salve no próprio pedal.',
                'Bank de um .bin: mostra os presets de fábrica de um firmware, sem o pedal.']
        g = tk.Frame(box, bg=bg)
        g.pack(fill='x', padx=p(20))
        for r, t in enumerate(tips):
            tk.Label(g, text='%02d' % (r + 1), font=F.mono_s, bg=bg, fg=T['accent']).grid(
                row=r, column=0, sticky='nw', pady=(p(2), p(10)))
            tk.Label(g, text=t, font=F.body, bg=bg, fg=T['soft'], wraplength=p(266), justify='left').grid(
                row=r, column=1, sticky='nw', padx=(p(10), 0), pady=(0, p(10)))
