"""Matribox Studio, aba IRs: lê os 20 slots de User IR do pedal, grava neles IRs de caixa (.wav) e renomeia os
slots, o que o matribox/usb/irwav.py faz pela linha de comando. Funciona com qualquer firmware: é o mesmo comando do
"Import IR" do editor original.

O pedal informa só o nome de cada slot, não devolve os dados. Por isso o Studio guarda um registro do que gravou
(ir_slots.json, ao lado do session.json, com as 1024 amostras de cada slot) e reconhece o slot pelo nome.

A porta MIDI é a da aba Pedal e presets (pedalview.Worker): a conexão é a mesma em todas as abas.
"""
import math, os, queue, threading
import tkinter as tk
from tkinter import ttk, filedialog

from matribox.studio import aa
from matribox.usb import irwav as I
from matribox.studio import pedalview as PV
from matribox.usb import qmepreset as Q
from matribox.studio.namview import same_path

SCAN_MAX = 3000                         # .wav files read from the library folder


def scan(folder):
    out = []
    for dp, dn, fn in os.walk(folder):
        dn.sort()
        for f in sorted(fn, key=str.lower):
            if not f.lower().endswith('.wav'):
                continue
            path = os.path.join(dp, f)
            try:
                out.append(I.describe(path))
            except Exception as ex:
                out.append(dict(path=path, file=f, name=os.path.splitext(f)[0], ok=False, rate=0, channels=0,
                                bits=0, fmt='?', frames=0, ms=0, peak_db=None, kept=0, native=False,
                                reason='não foi possível ler: %s' % ex))
            if len(out) >= SCAN_MAX:
                return out
    return out


def khz(rate):
    return ('%.1f kHz' % (rate / 1000)).replace('.0 kHz', ' kHz').replace('.', ',') if rate else '—'


def fmt_txt(d):
    return '%s %d-bit' % ('PCM' if d['fmt'] == 'PCM' else 'float', d['bits']) if d['bits'] else '—'


def ch_txt(n):
    return {0: '—', 1: 'mono', 2: 'estéreo'}.get(n, '%d canais' % n)


def dec(fmt, *v):
    return (fmt % v).replace('.', ',')


def tail_fit(font, text, width):
    """text cut at the start ('…' + the end) to fit width: for paths, whose end says the most"""
    if font.measure(text) <= width:
        return text
    while text and font.measure('…' + text) > width:
        text = text[1:]
    return '…' + text


class IrView(tk.Frame):
    def __init__(self, parent, app, W, pv):
        self.app, self.W, self.pv = app, W, pv
        T = W.T
        super().__init__(parent, bg=T['bg'])
        self.S, self.F = app.S, app.F
        s = app.session
        self.folder = s.get('ir_dir') or Q.paths.setting('ir_dir', Q.ROOT)
        self.slot = min(max(int(s.get('ir_slot', 0)), 0), I.SLOTS - 1)
        self.normalize = tk.BooleanVar(value=bool(s.get('ir_normalize', False)))
        self.name_var = tk.StringVar()
        self.files = []                 # describe() dicts of the library folder
        self.extra = []                 # files opened one by one, outside the folder
        self.sel = None                 # the .wav on the details panel
        self.busy = None                # (slot, file dict, name, info, data) while a slot is being written
        self.progress = (0, 0)
        self.prog_cv = self.prog_lbl = None
        self.curves = {}                # cache: path / 'slot:N:crc' -> response()
        self.log_path = os.path.join(os.path.dirname(W.SESSION), 'ir_slots.json')
        self.log = I.load_log(self.log_path)            # irwav.py send writes the same file
        self.q = queue.Queue()
        pv.listeners.append(self._on_event)
        self._layout()
        self.rescan()
        self.after(60, self._pump)

    def p(self, v):
        return round(v * self.S)

    # ------------------------------------------------------------------ upload log
    def names(self):
        return self.pv.names[1] if self.pv.connected else []

    def entry(self, slot):
        """the log entry of what the Studio wrote in this slot, if the slot still holds it (same name)"""
        e = self.log.get(str(slot))
        if not e:
            return None
        names = self.names()
        if names and (slot >= len(names) or names[slot] != e.get('name')):
            return None
        return e

    def shutdown(self):
        s = self.app.session
        s['ir_dir'], s['ir_slot'], s['ir_normalize'] = self.folder, self.slot, bool(self.normalize.get())

    # ------------------------------------------------------------------ layout
    def _layout(self):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        # left: connection + the 20 slots
        left = tk.Frame(self, bg=T['panel'], width=p(300))
        left.pack(side='left', fill='y')
        left.pack_propagate(False)
        tk.Frame(self, bg=T['line'], width=1).pack(side='left', fill='y')
        tk.Label(left, text='SLOTS DE USER IR NO PEDAL', font=F.tiny, bg=T['panel'], fg=T['muted']).pack(
            anchor='w', padx=p(16), pady=(p(16), p(10)))
        tk.Label(left, text='O pedal informa só o nome de cada slot: os dados gravados nele não podem ser lidos '
                            'de volta. Os IRs gravados por aqui ficam registrados e aparecem como WAV.',
                 font=F.small, bg=T['panel'], fg=T['faint'], wraplength=p(266), justify='left').pack(
            side='bottom', anchor='w', padx=p(16), pady=(p(4), p(14)))
        lcard = tk.Frame(left, bg=T['card'])
        lcard.pack(fill='both', expand=True, padx=p(8), pady=(0, p(8)))
        sty = ttk.Style(self)
        sty.configure('Pr.Treeview', background=T['card'], fieldbackground=T['card'], foreground=T['text'],
                      rowheight=p(28), borderwidth=0, relief='flat', font=F.body, indent=0)
        sty.map('Pr.Treeview', background=[('selected', T['sel'])], foreground=[('selected', T['text'])])
        sty.layout('Pr.Treeview', [('Pr.Treeview.treearea', {'sticky': 'nswe'})])
        self.slots = ttk.Treeview(lcard, columns=('kind',), style='Pr.Treeview', selectmode='browse', show='tree',
                                  height=I.SLOTS)
        self.slots.column('#0', width=p(200), stretch=True)
        self.slots.column('kind', width=p(60), stretch=False, anchor='e')
        svs = ttk.Scrollbar(lcard, orient='vertical', command=self.slots.yview, style='Card.Vertical.TScrollbar')

        def yscroll(a, b):                              # the bar only while the 20 rows do not fit
            svs.set(a, b)
            if float(a) > 0 or float(b) < 1:
                if not svs.winfo_ismapped():
                    svs.pack(side='right', fill='y', before=self.slots)
            else:
                svs.pack_forget()
        self.slots.configure(yscrollcommand=yscroll)
        self.slots.pack(side='left', fill='both', expand=True)
        self.slots.tag_configure('wav', foreground=T['accent'])
        self.slots.tag_configure('empty', foreground=T['faint'])
        self.slots.bind('<<TreeviewSelect>>', lambda e: self._on_slot())
        # right: the chosen file and the write
        self.det_outer = tk.Frame(self, bg=T['panel'], width=p(360))
        self.det_outer.pack(side='right', fill='y')
        self.det_outer.pack_propagate(False)
        tk.Frame(self, bg=T['line'], width=1).pack(side='right', fill='y')
        self.det = W.ScrollFrame(self.det_outer, self.app, T['panel'])
        self.det.pack(fill='both', expand=True)
        # center: slot header + .wav library
        center = tk.Frame(self, bg=T['bg'])
        center.pack(side='left', fill='both', expand=True, padx=p(24), pady=p(20))
        head = tk.Frame(center, bg=T['bg'])
        head.pack(fill='x')
        self.btn_rename = W.Btn(head, self.app, 'Renomear', None, self.rename, 'ghost', height=32, padx=12,
                                font=F.small, tip='Troca o nome do slot no pedal (os dados ficam)')
        self.btn_rename.pack(side='right', anchor='s')
        self.t_label = tk.Label(head, text='', font=F.h3, bg=T['bg'], fg=T['accent'])
        self.t_label.pack(side='left', anchor='s')
        self.t_name = tk.Label(head, text='', font=F.h1, bg=T['bg'], fg=T['text'])
        self.t_name.pack(side='left', padx=(p(10), 0))
        self.t_kind = tk.Frame(head, bg=T['bg'])
        self.t_kind.pack(side='left', padx=(p(12), 0), anchor='s', pady=(0, p(5)))
        self.t_sub = tk.Label(center, text='', font=F.small, bg=T['bg'], fg=T['muted'], anchor='w', justify='left')
        self.t_sub.pack(fill='x', pady=(p(2), p(12)))
        self.t_sub.bind('<Configure>', lambda e: self.t_sub.configure(wraplength=max(100, e.width - p(4))))
        tb = tk.Frame(center, bg=T['bg'])
        tb.pack(fill='x', pady=(0, p(8)))
        W.Btn(tb, self.app, 'Abrir .wav', W.ICO['open'], self.open_file, 'ghost', height=32, padx=12,
              tip='Escolhe um arquivo .wav fora da pasta').pack(side='right')
        W.Btn(tb, self.app, 'Pasta', W.ICO['folder'], self.choose_folder, 'ghost', height=32, padx=12,
              tip='Pasta com os IRs .wav (subpastas incluídas)').pack(side='right', padx=(0, p(8)))
        W.Btn(tb, self.app, '', W.ICO['reset'], self.rescan, 'subtle', height=32, padx=10,
              tip='Lê a pasta de novo').pack(side='right', padx=(0, p(8)))
        lb = tk.Frame(tb, bg=T['bg'])                   # packed after the buttons: it gets what is left
        lb.pack(side='left', fill='x', expand=True)
        tk.Label(lb, text='BIBLIOTECA DE IRs', font=F.tiny, bg=T['bg'], fg=T['muted']).pack(anchor='w')
        self.folder_lbl = tk.Label(lb, text='', font=F.small, bg=T['bg'], fg=T['soft'], anchor='w')
        self.folder_lbl.pack(anchor='w', fill='x')
        self.qv = tk.StringVar()
        W.Field(center, self.app, self.qv, 'Buscar IRs', height=34).pack(fill='x', pady=(0, p(10)))
        self.qv.trace_add('write', lambda *a: self.fill_lib())
        tcard = tk.Frame(center, bg=T['card'], highlightthickness=1, highlightbackground=T['cardline'])
        tcard.pack(fill='both', expand=True)
        cols = ('rate', 'fmt', 'ch', 'len', 'slot')
        self.lib = ttk.Treeview(tcard, columns=cols, style='Fx.Treeview', selectmode='browse')
        self.lib.heading('#0', text='IR', anchor='w')
        heads = dict(rate='TAXA', fmt='FORMATO', ch='CANAIS', len='DURAÇÃO', slot='NO PEDAL')
        widths = dict(rate=76, fmt=92, ch=70, len=80, slot=86)
        for c in cols:
            self.lib.heading(c, text=heads[c], anchor='w')
            self.lib.column(c, width=p(widths[c]), stretch=False, minwidth=p(40), anchor='w')
        self.lib.column('#0', width=p(220), minwidth=p(120), stretch=True)
        vs = ttk.Scrollbar(tcard, orient='vertical', command=self.lib.yview, style='Dark.Vertical.TScrollbar')
        self.lib.configure(yscrollcommand=vs.set)
        vs.pack(side='right', fill='y')
        self.lib.pack(fill='both', expand=True, padx=(p(4), 0), pady=p(4))
        self.lib.tag_configure('bad', foreground=T['faint'])
        self.lib.tag_configure('loaded', foreground=T['accent'])
        self.lib.bind('<<TreeviewSelect>>', lambda e: self._on_file())
        self.lib_empty = tk.Label(tcard, text='', font=F.body, bg=T['card'], fg=T['muted'], justify='center')
        leg = tk.Label(center, text='Escolha um slot à esquerda e um IR na lista; a gravação fica no painel da '
                                    'direita. O pedal guarda 1024 amostras a 44,1 kHz (23 ms), mono: arquivos em '
                                    'outra taxa são reamostrados e, nos estéreo, vale o canal esquerdo.',
                       font=F.small, bg=T['bg'], fg=T['faint'], anchor='w', justify='left')
        leg.pack(fill='x', pady=(p(8), 0))
        leg.bind('<Configure>', lambda e: leg.configure(wraplength=max(100, e.width - p(4))))
        self.refresh()

    # ------------------------------------------------------------------ connection / slots
    def reload(self):
        if self.pv.connected and not self.busy:
            self._status('Lendo os slots…', self.W.T['amber'])
            self.pv.worker.submit('ir_list', PV.job_ir_list)

    def _status(self, text=None, color=None):
        """the connection chip in the header (the pedal view's): a write in progress, else its own state"""
        if text is None and self.busy:
            text, color = 'Gravando o User IR %d…' % (self.busy[0] + 1), self.W.T['amber']
        if text is None and not self.pv.btn_conn.enabled:      # connecting: the chip says so already
            return
        self.pv._status(text, color)

    def _on_event(self, kind, res, err):
        """results of the pedal view's worker (connection) and of the User IR jobs"""
        if kind == 'ir_progress':
            self.progress = res[1:]
            self._draw_progress()
            return
        if kind.startswith(('nam', 'glob')):
            return
        if kind == 'ir_write':
            self._write_done(res, err)
        elif kind == 'ir_rename':
            self._rename_done(res, err)
        elif kind == 'ir_list' and err:
            self.app.toast_msg('Erro ao ler os slots de IR: %s' % err, 'error')
        elif kind in ('disconnect', 'lost') and self.busy:
            self.busy = None
            self.app.toast_msg('A conexão caiu durante a gravação: confira o slot depois de reconectar.', 'error')
        self.refresh()

    def refresh(self):
        self._status()
        self.fill_slots()
        self.fill_lib()
        self.show_slot()
        self.show_details()

    def fill_slots(self):
        tr = self.slots
        top = tr.yview()[0]
        tr.delete(*tr.get_children())
        names = self.names()
        for i in range(I.SLOTS):
            if names:
                n = names[i] if i < len(names) else ''
                e = self.entry(i)
                tags = ('wav',) if e else ('empty',) if not n else ()
                kind = 'WAV' if e else ''
                text = n or '(vazio)'
            else:
                tags, kind, text = ('empty',), '', '—'
            tr.insert('', 'end', iid=str(i), text='  IR %-3d %s' % (i + 1, text), values=(kind,), tags=tags)
        tr.selection_set(str(self.slot))
        tr.yview_moveto(top)                            # keep the scroll: only bring the slot in when hidden
        tr.update_idletasks()
        if tr.winfo_ismapped() and not tr.bbox(str(self.slot)):
            tr.see(str(self.slot))

    def _on_slot(self):
        sel = self.slots.selection()
        if sel and int(sel[0]) != self.slot:
            self.slot = int(sel[0])
            self.show_slot()
            self.show_details()

    def show_slot(self):
        T = self.W.T
        names = self.names()
        i = self.slot
        self.t_label.configure(text='USER IR %d' % (i + 1))
        for w in self.t_kind.winfo_children():
            w.destroy()
        e = self.entry(i)
        self.btn_rename.config_btn(enabled=bool(self.pv.connected and not self.busy and names))
        if not self.pv.connected:
            self.t_name.configure(text='Pedal desconectado')
            sub = 'Conecte o pedal para ler os nomes dos slots e gravar IRs.'
            if self.log.get(str(i)):
                le = self.log[str(i)]
                sub += ' Última gravação feita por aqui neste slot: "%s" (%s), em %s.' % (
                    le.get('name'), le.get('file'), le.get('date'))
        else:
            n = names[i] if i < len(names) else ''
            self.t_name.configure(text=n or '(vazio)')
            if e:
                self.W.Pill(self.t_kind, self.app, 'WAV', T['accent']).pack(side='left')
                sub = 'Gravado pelo Studio em %s, do arquivo %s (%s%s).' % (
                    e.get('date'), e.get('file'), 'de ' + khz(e.get('rate')) if e.get('rate') != I.RATE else
                    '44,1 kHz', ', normalizado' if e.get('normalized') else '')
            elif not n:
                sub = 'Slot vazio.'
            else:
                sub = ('Sem registro de gravação por aqui: IR do editor original ou gravado por outra ferramenta. '
                       'O pedal não devolve os dados de um slot, só o nome.')
        self.t_sub.configure(text=sub)

    # ------------------------------------------------------------------ rename
    def rename(self):
        names = self.names()
        slot = self.slot
        if not self.pv.connected or self.busy or slot >= len(names):
            return
        W, T, F, p = self.W, self.W.T, self.F, self.p
        old = names[slot]
        d = W.Dialog(self.app, 'Renomear o User IR %d' % (slot + 1), 420)
        var = tk.StringVar(value=old)
        tk.Label(d.body, text='Nome no pedal (até %d caracteres)' % I.NAME_MAX, font=F.small, bg=T['panel'],
                 fg=T['muted']).pack(anchor='w', pady=(0, p(6)))
        fld = W.Field(d.body, self.app, var, '', search=False, height=36)
        fld.pack(fill='x')
        var.trace_add('write', lambda *a: var.set(var.get()[:I.NAME_MAX]) if len(var.get()) > I.NAME_MAX else None)
        tk.Label(d.body, text='Só os slots que já têm um IR gravado podem ser renomeados.', font=F.small,
                 bg=T['panel'], fg=T['faint']).pack(anchor='w', pady=(p(8), 0))

        def ok():
            v = var.get().strip().encode('latin1', 'replace').decode('latin1')[:I.NAME_MAX]
            d.close(v or None)
        d.buttons([('Cancelar', 'ghost', d.close), ('Renomear', 'primary', ok)])
        fld.entry.bind('<Return>', lambda e: ok())
        d.top.after(50, lambda: (fld.entry.focus_set(), fld.entry.select_range(0, 'end')))
        new = d.show(wait=True)
        if not new or new == old or not self.pv.connected or self.busy:
            return
        self.pv.worker.submit('ir_rename', PV.job_ir_rename, slot, old, new)

    def _rename_done(self, res, err):
        if err:
            self.app.toast_msg('Não foi possível renomear: %s' % err, 'error')
            return
        slot, old, new, names = res
        got = names[slot] if slot < len(names) else ''
        if got == new:
            try:
                self.log = I.renamed(slot, old, new, self.log_path)
            except Exception as ex:
                self.app.toast_msg('Não foi possível salvar o registro de IRs: %s' % ex, 'warn')
                return
            self.app.toast_msg('User IR %d renomeado para "%s".' % (slot + 1, new), 'ok')
        else:
            self.app.toast_msg('O pedal não renomeou o User IR %d (só slots com um IR gravado aceitam).' % (
                slot + 1), 'warn')

    # ------------------------------------------------------------------ library
    def choose_folder(self):
        d = filedialog.askdirectory(parent=self.app.root, title='Pasta com IRs .wav',
                                    initialdir=self.folder if os.path.isdir(self.folder) else Q.ROOT)
        if d:
            self.folder = d
            self.rescan()

    def open_file(self):
        path = filedialog.askopenfilename(parent=self.app.root, title='IR .wav',
                                          initialdir=self.folder if os.path.isdir(self.folder) else Q.ROOT,
                                          filetypes=[('WAV', '*.wav'), ('Todos', '*.*')])
        if not path:
            return
        try:
            d = I.describe(path)
        except Exception as ex:
            self.app.toast_msg('Não foi possível ler %s: %s' % (os.path.basename(path), ex), 'error')
            return
        self.extra = [x for x in self.extra if not same_path(x['path'], path)] + [d]
        self.qv.set('')
        self.fill_lib()
        self.select_file(d)

    def rescan(self):
        if not os.path.isdir(self.folder):
            self.files = []
            self.folder_lbl.configure(text=self._short(self.folder) + '  (pasta não existe: escolha outra)')
            self.fill_lib()
            return
        self.folder_lbl.configure(text=self._short(self.folder) + '  (lendo…)')
        folder = self.folder
        threading.Thread(target=lambda: self.q.put(('scan', folder, scan(folder))), daemon=True).start()

    def _short(self, path, n=48):
        return path if len(path) <= n else '…' + path[-(n - 1):]

    def _pump(self):
        try:
            while True:
                kind, key, val = self.q.get_nowait()
                if kind == 'scan' and key == self.folder:
                    self.files = val
                    ok = sum(1 for d in val if d['ok'])
                    self.folder_lbl.configure(text='%s  ·  %d arquivos, %d IRs válidos' % (
                        self._short(self.folder), len(val), ok))
                    if self.sel is not None:
                        self.sel = next((d for d in self.all_files() if same_path(d['path'], self.sel['path'])),
                                        self.sel)
                    self.fill_lib()
                    if self.sel is None:
                        first = next((d for d in self.all_files() if d['ok']), None)
                        if first:
                            self.select_file(first)
                elif kind == 'curve':
                    self.curves[key] = val
                    self._draw_plot()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(60, self._pump)

    def all_files(self):
        paths = {os.path.normcase(os.path.abspath(d['path'])) for d in self.files}
        return [d for d in self.extra if os.path.normcase(os.path.abspath(d['path'])) not in paths] + self.files

    def slots_of(self, d):
        return [int(k) + 1 for k in sorted(self.log, key=int) if same_path(self.log[k].get('path'), d['path'])
                and self.entry(int(k))]

    def fill_lib(self):
        tr = self.lib
        tr.delete(*tr.get_children())
        q = self.qv.get().strip().lower()
        files = self.all_files()
        shown = 0
        for k, d in enumerate(files):
            if q and not any(q in str(x).lower() for x in (d['name'], d['file'], os.path.dirname(d['path']))):
                continue
            slots = self.slots_of(d)
            tags = ('loaded',) if slots else ('bad',) if not d['ok'] else ()
            tr.insert('', 'end', iid=str(k), text='  ' + d['name'],
                      values=(khz(d['rate']), fmt_txt(d), ch_txt(d['channels']),
                              '%.0f ms' % d['ms'] if d['frames'] else '—', ', '.join('IR %d' % s for s in slots)),
                      tags=tags)
            shown += 1
            if self.sel is not None and same_path(d['path'], self.sel['path']):
                tr.selection_set(str(k))
                tr.see(str(k))
        if shown:
            self.lib_empty.place_forget()
        else:
            self.lib_empty.configure(text='Nenhum arquivo .wav nesta pasta.' if not files else
                                     'Nada corresponde à busca.')
            self.lib_empty.place(relx=.5, rely=.4, anchor='center')

    def _on_file(self):
        sel = self.lib.selection()
        if not sel:
            return
        files = self.all_files()
        k = int(sel[0])
        if k < len(files) and (self.sel is None or not same_path(files[k]['path'], self.sel['path'])):
            self.select_file(files[k])

    def select_file(self, d):
        self.sel = d
        self.name_var.set(I.default_name(d['path']))
        self.fill_lib()
        self.show_details()

    # ------------------------------------------------------------------ response curves
    def _curve_key(self, d=None, slot=None):
        if d is not None:
            return ('file', os.path.normcase(os.path.abspath(d['path'])), bool(self.normalize.get()))
        e = self.entry(slot)
        return ('slot', slot, e.get('crc32')) if e else None

    def _curve(self, key, d=None, e=None):
        """the response for key, computed off the Tk thread the first time"""
        if key in self.curves:
            return self.curves[key]
        self.curves[key] = None
        norm = bool(self.normalize.get())

        def work():
            try:
                x = I.convert(d['path'], norm)[0] if d is not None else I.log_samples(e)
                val = I.response(x) if x else []
            except Exception:
                val = []
            self.q.put(('curve', key, val))
        threading.Thread(target=work, daemon=True).start()
        return None

    def _draw_plot(self):
        c = getattr(self, 'plot', None)
        try:
            if c is None or not c.winfo_exists():
                return
        except tk.TclError:
            return
        T, F, p = self.W.T, self.F, self.p
        c.delete('all')
        w, h = c.winfo_width(), c.winfo_height()
        if w < 20:
            return
        bg = T['card']
        cur = self.curves.get(self.plot_keys[0]) if self.plot_keys[0] else None
        old = self.curves.get(self.plot_keys[1]) if self.plot_keys[1] else None
        l, r, t, b = p(30), p(8), p(8), p(18)
        pw, ph = w - l - r, h - t - b
        ys = [v for curve in (cur, old) if curve for _f, v in curve]
        top = (max(ys) // 6 + 1) * 6 if ys else 0
        rng = 42
        fx = lambda f: l + pw * math.log(f / 20) / math.log(1000)
        fy = lambda v: t + ph * (top - max(top - rng, min(top, v))) / rng
        for f in (100, 1000, 10000):
            x = fx(f)
            c.create_line(x, t, x, t + ph, fill=T['rowline'])
            c.create_text(x, h - p(3), text={100: '100', 1000: '1k', 10000: '10k'}[f], font=F.mono_xs,
                          fill=T['faint'], anchor='s')
        for k in range(0, rng + 1, 12):
            y = t + ph * k / rng
            c.create_line(l, y, l + pw, y, fill=T['rowline'])
            c.create_text(l - p(5), y, text='%d' % (top - k), font=F.mono_xs, fill=T['faint'], anchor='e')
        for curve, col, width in ((old, T['muted'], 1), (cur, T['accent'], 2)):
            if curve:
                pts = []
                for f, v in curve:
                    pts += [fx(f), fy(v)]
                c.create_line(*pts, fill=col, width=round(width * self.S), smooth=False)
        if self.plot_keys[0] and cur is None:
            c.create_text(w / 2, h / 2, text='calculando…', font=F.small, fill=T['faint'])
        c.configure(bg=bg)

    # ------------------------------------------------------------------ details
    def _kv(self, box, pairs):
        T, F, p = self.W.T, self.F, self.p
        pairs = [(k, v) for k, v in pairs if v not in (None, '')]
        g = tk.Frame(box, bg=T['panel'])
        g.pack(fill='x', padx=p(20))
        g.columnconfigure(1, weight=1)
        for r, (k, v) in enumerate(pairs):
            tk.Label(g, text=k, font=F.body, bg=T['panel'], fg=T['muted']).grid(row=r, column=0, sticky='nw',
                                                                               pady=p(2))
            tk.Label(g, text=v, font=F.semi, bg=T['panel'], fg=T['text'], wraplength=p(200), justify='right').grid(
                row=r, column=1, sticky='ne', pady=p(2), padx=(p(12), 0))

    def show_details(self):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        box = self.det.inner
        for w in box.winfo_children():
            w.destroy()
        self.prog_cv = self.prog_lbl = self.plot = None
        bg = T['panel']
        pad = dict(padx=p(20))
        d = self.sel
        if d is None:
            tk.Label(box, text='IRs de caixa', font=F.h2, bg=bg, fg=T['text']).pack(anchor='w', pady=(p(24), p(8)),
                                                                                  **pad)
            for t in ('Escolha a pasta com os seus IRs .wav (o botão Pasta) ou abra um arquivo avulso.',
                      'O pedal guarda 1024 amostras a 44,1 kHz (23 ms) por slot, mono. Arquivos em outra taxa são '
                      'reamostrados; IRs mais longos são cortados com um fade curto; nos estéreo vale o canal '
                      'esquerdo.',
                      'Gravar substitui o IR do slot, sem volta: o pedal não deixa ler os dados de um slot.',
                      'Funciona com qualquer firmware: é o mesmo comando do "Import IR" do editor original.'):
                r = tk.Frame(box, bg=bg)
                r.pack(fill='x', padx=p(20), pady=p(4))
                tk.Label(r, text='•', font=F.body, bg=bg, fg=T['accent']).pack(side='left', anchor='n')
                tk.Label(r, text=t, font=F.body, bg=bg, fg=T['soft'], wraplength=p(290), justify='left').pack(
                    side='left', padx=(p(8), 0))
            return
        tk.Label(box, text=d['name'], font=F.h2, bg=bg, fg=T['text'], wraplength=p(320), justify='left').pack(
            anchor='w', pady=(p(22), p(2)), **pad)
        folder = tk.Label(box, text=tail_fit(F.small, os.path.dirname(d['path']), p(300)), font=F.small, bg=bg,
                          fg=T['muted'], anchor='w')
        folder.pack(anchor='w', **pad)
        W.Tooltip(folder, self.app, d['path'])
        chips = tk.Frame(box, bg=bg)
        chips.pack(anchor='w', fill='x', pady=(p(12), 0), **pad)
        W.Pill(chips, self.app, 'IR válido' if d['ok'] else 'Não serve', T['green'] if d['ok'] else T['red'],
               icon=W.ICO['check'] if d['ok'] else W.ICO['warn']).pack(side='left')
        if d['ok'] and d['rate'] != I.RATE:
            W.Pill(chips, self.app, 'Reamostrado', T['amber']).pack(side='left', padx=(p(6), 0))
        for s in self.slots_of(d):
            W.Pill(chips, self.app, 'IR %d' % s, T['accent']).pack(side='left', padx=(p(6), 0))
        if not d['ok']:
            tk.Label(box, text=d['reason'][:1].upper() + d['reason'][1:] + '.', font=F.small, bg=bg, fg=T['soft'],
                     wraplength=p(320), justify='left').pack(anchor='w', pady=(p(8), 0), **pad)
        if d['ok']:
            self._plot_box(box, d)
            self._send_box(box, d)
        self.app._section(box, 'ARQUIVO')
        self._kv(box, [('Formato', '%s, %s' % (fmt_txt(d), ch_txt(d['channels'])) if d['bits'] else None),
                       ('Taxa', khz(d['rate']) if d['rate'] else None),
                       ('Duração', dec('%.1f ms (%d amostras)', d['ms'], d['frames']) if d['frames'] else None),
                       ('Pico', dec('%.1f dBFS', d['peak_db']) if d.get('peak_db') is not None else None),
                       ('No pedal', '1024 amostras a 44,1 kHz' if d['ok'] else None),
                       ('Energia aproveitada', dec('%.1f%%', 100 * d['kept']) if d['ok'] else None)])
        tk.Frame(box, bg=bg, height=p(20)).pack()

    def _plot_box(self, box, d):
        T, F, p = self.W.T, self.F, self.p
        bg = T['panel']
        e = self.entry(self.slot) if self.pv.connected else None
        same = e is not None and same_path(e.get('path'), d['path'])
        self.app._section(box, 'RESPOSTA EM FREQUÊNCIA')
        card = tk.Frame(box, bg=T['card'], highlightthickness=1, highlightbackground=T['cardline'])
        card.pack(fill='x', padx=p(20))
        self.plot = tk.Canvas(card, height=p(150), bg=T['card'], highlightthickness=0)
        self.plot.pack(fill='x', padx=p(4), pady=p(4))
        k_file = self._curve_key(d)
        k_slot = self._curve_key(slot=self.slot) if e is not None and not same else None
        self.plot_keys = (k_file, k_slot)
        self._curve(k_file, d=d)
        if k_slot:
            self._curve(k_slot, e=e)
        self.plot.bind('<Configure>', lambda ev: self._draw_plot())
        for k, (col, txt) in enumerate(((T['accent'], 'este arquivo, como vai para o pedal'),) + (
                ((T['muted'], 'o que está no User IR %d hoje' % (self.slot + 1)),) if k_slot else ())):
            leg = tk.Frame(box, bg=bg)
            leg.pack(anchor='w', fill='x', padx=p(20), pady=(p(6) if k == 0 else p(2), 0))
            sw = tk.Canvas(leg, width=p(14), height=p(8), bg=bg, highlightthickness=0)
            sw.create_line(0, p(4), p(14), p(4), fill=col, width=round(2 * self.S))
            sw.pack(side='left')
            tk.Label(leg, text=txt, font=F.xs, bg=bg, fg=T['muted']).pack(side='left', padx=(p(6), 0))

    def _send_box(self, box, d):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        bg = T['panel']
        pad = dict(padx=p(20))
        self.app._section(box, 'GRAVAR NO PEDAL')
        tk.Label(box, text='Nome no pedal (até %d caracteres)' % I.NAME_MAX, font=F.small, bg=bg,
                 fg=T['muted']).pack(anchor='w', pady=(0, p(4)), **pad)
        fr = W.Field(box, self.app, self.name_var, '', search=False, height=34, bg=T['raised'])
        fr.pack(fill='x', **pad)
        cnt = tk.Label(fr.body, text='', font=F.tiny, bg=T['raised'], fg=T['faint'])
        cnt.pack(side='right', padx=(0, p(8)), before=fr.entry)

        def on_name(*a):
            v = self.name_var.get()
            if len(v) > I.NAME_MAX:
                self.name_var.set(v[:I.NAME_MAX])
                return
            try:
                cnt.configure(text='%d/%d' % (len(v), I.NAME_MAX))
            except tk.TclError:
                pass
        if getattr(self, '_name_trace', None):
            try:
                self.name_var.trace_remove('write', self._name_trace)
            except tk.TclError:
                pass
        self._name_trace = self.name_var.trace_add('write', on_name)
        on_name()
        W.Switch(box, self.app, 'Normalizar o pico (−0,1 dBFS)', self.normalize, command=self._on_normalize).pack(
            anchor='w', pady=(p(12), 0), **pad)
        tk.Label(box, text='Põe o IR no mesmo nível dos cabs de fábrica. Desligado, as amostras vão como estão no '
                           'arquivo, como faz o editor original.', font=F.small, bg=bg, fg=T['faint'],
                 wraplength=p(310), justify='left').pack(anchor='w', pady=(p(4), 0), **pad)
        can = self.pv.connected and not self.busy
        btn = W.Btn(box, self.app, 'Gravar no User IR %d' % (self.slot + 1), W.ICO['save'], self.send, 'danger',
                    height=38, padx=16, tip='Substitui o IR do slot escolhido à esquerda')
        btn.pack(anchor='w', pady=(p(16), 0), **pad)
        btn.config_btn(enabled=bool(can))
        if self.busy:
            self.prog_lbl = tk.Label(box, text='', font=F.small, bg=bg, fg=T['soft'])
            self.prog_lbl.pack(anchor='w', pady=(p(10), p(4)), **pad)
            self.prog_cv = tk.Canvas(box, height=p(6), bg=bg, highlightthickness=0)
            self.prog_cv.pack(fill='x', **pad)
            self.prog_cv.bind('<Configure>', lambda e: self._draw_progress())
        elif not self.pv.connected:
            tk.Label(box, text='Conecte o pedal para gravar.', font=F.small, bg=bg, fg=T['amber']).pack(
                anchor='w', pady=(p(8), 0), **pad)

    def _on_normalize(self):
        self.show_details()

    def _draw_progress(self):
        k, n = self.progress
        try:
            if self.prog_lbl is not None:
                self.prog_lbl.configure(text='Enviando… %d de %d pacotes' % (k, n) if n and k < n else
                                        'Gravando na flash…' if n else 'Preparando…')
            c = self.prog_cv
            if c is None:
                return
            c.delete('all')
            w, h = c.winfo_width(), c.winfo_height()
            if w < 4:
                return
            T = self.W.T
            shapes = [('rrect', (0, 0, w, h, h / 2), T['raised'])]
            if n and k:
                shapes.append(('rrect', (0, 0, max(h, w * k / n), h, h / 2), T['accent']))
            aa.draw(c, 0, 0, w, h, shapes, T['panel'])
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ write
    def send(self):
        d, slot = self.sel, self.slot
        if d is None or not d['ok'] or not self.pv.connected or self.busy:
            return
        name = self.name_var.get().strip() or I.default_name(d['path'])
        name = name.encode('latin1', 'replace')[:I.NAME_MAX].decode('latin1')
        try:
            data, info = I.pack_file(d['path'], bool(self.normalize.get()))
        except Exception as ex:
            self.app.toast_msg('Não foi possível preparar %s: %s' % (d['file'], ex), 'error')
            return
        names = self.names()
        old = names[slot] if slot < len(names) else ''
        e = self.entry(slot)
        if not old:
            what = 'O slot User IR %d está vazio.' % (slot + 1)
        elif e:
            what = 'O User IR %d tem o IR "%s", gravado por aqui em %s (%s).' % (slot + 1, old, e.get('date'),
                                                                               e.get('file'))
        else:
            what = ('O User IR %d tem o IR "%s", sem registro aqui: provavelmente gravado pelo editor original. O '
                    'pedal não deixa ler os dados de um slot, então ele não poderá ser recuperado depois (a não ser '
                    'que você tenha o arquivo original).' % (slot + 1, old))
        how = []
        if info['resampled']:
            how.append('reamostrado de %s' % khz(info['rate']))
        if info['cut']:
            how.append('cortado em 23 ms')
        if info['normalized']:
            how.append(dec('normalizado (%+.1f dB)', info['gain_db']))
        text = ('%s\n\nSerá gravado o IR "%s" (%s%s).\n\nOs presets que usam o User IR %d passam a tocar o IR novo. '
                'O pedal reinicia a cadeia ao gravar.' % (what, name, d['file'],
                                                           ', ' + ', '.join(how) if how else '', slot + 1))
        if not self.W.ask(self.app, 'Gravar no User IR %d' % (slot + 1), text, 'Substituir' if old else 'Gravar',
                          'danger'):
            return
        if not self.pv.connected or self.busy:
            return
        self.busy = (slot, d, name, info, data)
        self.progress = (0, 0)
        self.pv.worker.submit('ir_write', PV.job_ir_write, slot, name, data)
        self.refresh()
        self._draw_progress()

    def _write_done(self, res, err):
        busy, self.busy = self.busy, None
        if err:
            self.app.toast_msg('A gravação falhou: %s. Confira o nome do slot na lista.' % err, 'error')
            if self.pv.connected:
                self.pv.worker.submit('ir_list', PV.job_ir_list)
            return
        slot, name, names = res
        if busy is None or busy[0] != slot:
            return
        _s, d, _n, info, data = busy
        try:
            self.log = I.record(slot, name, d['path'], info, data, self.log_path)
        except Exception as ex:
            self.app.toast_msg('Não foi possível salvar o registro de IRs: %s' % ex, 'warn')
        got = names[slot] if slot < len(names) else ''
        if got == name:
            self.app.toast_msg('User IR %d gravado: "%s".' % (slot + 1, name), 'ok')
        else:
            self.app.toast_msg('O pedal confirmou a gravação, mas o slot agora se chama "%s".' % got, 'warn')
