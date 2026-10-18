"""Matribox Studio, aba Capturas NAM: lê os 10 slots do Clone do pedal e grava neles capturas .nam (rede A2-Lite),
o que o matribox/usb/nam_clone.py faz pela linha de comando. As capturas só tocam com um firmware com o motor NAM no
Clone (examples/nam_clone: namclone, ou clonedual com fábrica + NAM por slot); no de fábrica o slot fica mudo.

O pedal informa só o nome de cada slot: o firmware não tem comando que devolva os dados gravados. Por isso o
Studio guarda um registro do que gravou (nam_slots.json, ao lado do session.json) e reconhece o slot pelo nome.

A porta MIDI é a da aba Pedal e presets (pedalview.Worker): a conexão é a mesma nas duas abas.
"""
import os, queue, threading
import tkinter as tk
from tkinter import ttk, filedialog

from matribox.studio import aa
from matribox.usb import nam_clone as N
from matribox.studio import pedalview as PV
from matribox.usb import qmepreset as Q

SCAN_MAX = 2000                         # .nam files read from the library folder


def scan(folder):
    out = []
    for dp, dn, fn in os.walk(folder):
        dn.sort()
        for f in sorted(fn, key=str.lower):
            if not f.lower().endswith('.nam'):
                continue
            path = os.path.join(dp, f)
            try:
                out.append(N.describe(path))
            except Exception as ex:
                out.append(dict(path=path, file=f, name=os.path.splitext(f)[0], meta={}, lite=False, arch='?',
                                submodels=[], sample_rate=0, loudness=None, reason='não foi possível ler: %s' % ex))
            if len(out) >= SCAN_MAX:
                return out
    return out


def is48(d):
    return 47000 < (d.get('sample_rate') or 0) < 49000


def runs_txt(at48):
    return '48 kHz, com resampling' if at48 else '44,1 kHz'


def fmt_date(v):
    if isinstance(v, dict):
        try:
            return '%02d/%02d/%04d' % (v['day'], v['month'], v['year'])
        except Exception:
            return ''
    return str(v or '')


def same_path(a, b):
    return bool(a and b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


class NamView(tk.Frame):
    def __init__(self, parent, app, W, pv):
        self.app, self.W, self.pv = app, W, pv
        T = W.T
        super().__init__(parent, bg=T['bg'])
        self.S, self.F = app.S, app.F
        s = app.session
        self.folder = s.get('nam_dir') or Q.paths.setting('nam_dir', Q.ROOT)
        self.slot = min(max(int(s.get('nam_slot', 0)), 0), Q.CLONE_SLOTS - 1)
        self.native = tk.BooleanVar(value=bool(s.get('nam_native', False)))
        self.name_var = tk.StringVar()
        self.files = []                 # describe() dicts of the library folder
        self.extra = []                 # files opened one by one, outside the folder
        self.sel = None                 # the .nam file on the details panel
        self.busy = None                # (slot, file dict, name, info, crc) while a slot is being written
        self.progress = (0, 0)
        self.prog_cv = self.prog_lbl = None
        self.log_path = os.path.join(os.path.dirname(W.SESSION), 'nam_slots.json')
        self.log = self._load_log()
        self.q = queue.Queue()
        pv.listeners.append(self._on_event)
        self._layout()
        self.rescan()
        self.after(60, self._pump)

    def p(self, v):
        return round(v * self.S)

    # ------------------------------------------------------------------ upload log
    def _load_log(self):
        return N.load_log(self.log_path)            # nam_clone.py send writes the same file

    def names(self):
        return self.pv.names[0] if self.pv.connected else []

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
        s['nam_dir'], s['nam_slot'], s['nam_native'] = self.folder, self.slot, bool(self.native.get())

    # ------------------------------------------------------------------ layout
    def _layout(self):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        # left: connection + the 10 slots
        left = tk.Frame(self, bg=T['panel'], width=p(300))
        left.pack(side='left', fill='y')
        left.pack_propagate(False)
        tk.Frame(self, bg=T['line'], width=1).pack(side='left', fill='y')
        tk.Label(left, text='SLOTS DO CLONE NO PEDAL', font=F.tiny, bg=T['panel'], fg=T['muted']).pack(
            anchor='w', padx=p(16), pady=(p(16), p(10)))
        tk.Label(left, text='O pedal informa só o nome de cada slot: os dados gravados nele não podem ser lidos '
                            'de volta. As capturas gravadas por aqui ficam registradas e aparecem como NAM.',
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
                                  height=Q.CLONE_SLOTS)
        self.slots.column('#0', width=p(200), stretch=True)
        self.slots.column('kind', width=p(60), stretch=False, anchor='e')
        self.slots.pack(fill='both', expand=True)
        self.slots.tag_configure('nam', foreground=T['accent'])
        self.slots.tag_configure('empty', foreground=T['faint'])
        self.slots.bind('<<TreeviewSelect>>', lambda e: self._on_slot())
        # right: the chosen file and the write
        self.det_outer = tk.Frame(self, bg=T['panel'], width=p(360))
        self.det_outer.pack(side='right', fill='y')
        self.det_outer.pack_propagate(False)
        tk.Frame(self, bg=T['line'], width=1).pack(side='right', fill='y')
        self.det = W.ScrollFrame(self.det_outer, self.app, T['panel'])
        self.det.pack(fill='both', expand=True)
        # center: slot header + .nam library
        center = tk.Frame(self, bg=T['bg'])
        center.pack(side='left', fill='both', expand=True, padx=p(24), pady=p(20))
        head = tk.Frame(center, bg=T['bg'])
        head.pack(fill='x')
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
        W.Btn(tb, self.app, 'Abrir .nam', W.ICO['open'], self.open_file, 'ghost', height=32, padx=12,
              tip='Escolhe um arquivo .nam fora da pasta').pack(side='right')
        W.Btn(tb, self.app, 'Pasta', W.ICO['folder'], self.choose_folder, 'ghost', height=32, padx=12,
              tip='Pasta com as capturas .nam (subpastas incluídas)').pack(side='right', padx=(0, p(8)))
        W.Btn(tb, self.app, '', W.ICO['reset'], self.rescan, 'subtle', height=32, padx=10,
              tip='Lê a pasta de novo').pack(side='right', padx=(0, p(8)))
        lb = tk.Frame(tb, bg=T['bg'])                   # packed after the buttons: it gets what is left
        lb.pack(side='left', fill='x', expand=True)
        tk.Label(lb, text='BIBLIOTECA DE CAPTURAS', font=F.tiny, bg=T['bg'], fg=T['muted']).pack(anchor='w')
        self.folder_lbl = tk.Label(lb, text='', font=F.small, bg=T['bg'], fg=T['soft'], anchor='w')
        self.folder_lbl.pack(anchor='w', fill='x')
        self.qv = tk.StringVar()
        W.Field(center, self.app, self.qv, 'Buscar capturas', height=34).pack(fill='x', pady=(0, p(10)))
        self.qv.trace_add('write', lambda *a: self.fill_lib())
        tcard = tk.Frame(center, bg=T['card'], highlightthickness=1, highlightbackground=T['cardline'])
        tcard.pack(fill='both', expand=True)
        cols = ('gear', 'type', 'fit', 'slot')
        self.lib = ttk.Treeview(tcard, columns=cols, style='Fx.Treeview', selectmode='browse')
        self.lib.heading('#0', text='CAPTURA', anchor='w')
        heads = dict(gear='EQUIPAMENTO', type='TIPO', fit='REDE', slot='NO PEDAL')
        widths = dict(gear=130, type=56, fit=124, slot=76)
        for c in cols:
            self.lib.heading(c, text=heads[c], anchor='w')
            self.lib.column(c, width=p(widths[c]), stretch=c == 'gear', minwidth=p(40), anchor='w')
        self.lib.column('#0', width=p(170), minwidth=p(120), stretch=True)
        vs = ttk.Scrollbar(tcard, orient='vertical', command=self.lib.yview, style='Dark.Vertical.TScrollbar')
        self.lib.configure(yscrollcommand=vs.set)
        vs.pack(side='right', fill='y')
        self.lib.pack(fill='both', expand=True, padx=(p(4), 0), pady=p(4))
        self.lib.tag_configure('bad', foreground=T['faint'])
        self.lib.tag_configure('loaded', foreground=T['accent'])
        self.lib.bind('<<TreeviewSelect>>', lambda e: self._on_file())
        self.lib_empty = tk.Label(tcard, text='', font=F.body, bg=T['card'], fg=T['muted'], justify='center')
        leg = tk.Label(center, text='Escolha um slot à esquerda e uma captura na lista; a gravação fica no painel da '
                                    'direita. Só redes A2-Lite (WaveNet A2 de 3 canais, inclusive dentro de um '
                                    'container "slimmable") rodam no pedal; as outras aparecem apagadas.',
                       font=F.small, bg=T['bg'], fg=T['faint'], anchor='w', justify='left')
        leg.pack(fill='x', pady=(p(8), 0))
        leg.bind('<Configure>', lambda e: leg.configure(wraplength=max(100, e.width - p(4))))
        self.refresh()

    # ------------------------------------------------------------------ connection / slots
    def reload(self):
        if self.pv.connected and not self.busy:
            self._status('Lendo os slots…', self.W.T['amber'])
            self.pv.worker.submit('nam_list', PV.job_clone_list)

    def _status(self, text=None, color=None):
        """the connection chip in the header (the pedal view's): a write in progress, else its own state"""
        if text is None and self.busy:
            text, color = 'Gravando o Clone %d…' % (self.busy[0] + 1), self.W.T['amber']
        if text is None and not self.pv.btn_conn.enabled:      # connecting: the chip says so already
            return
        self.pv._status(text, color)

    def _on_event(self, kind, res, err):
        """results of the pedal view's worker (connection) and of the Clone jobs"""
        if kind == 'nam_progress':
            self.progress = res[1:]
            self._draw_progress()
            return
        if kind.startswith(('ir_', 'glob')):
            return
        if kind == 'nam_write':
            self._write_done(res, err)
        elif kind == 'nam_list' and err:
            self.app.toast_msg('Erro ao ler os slots do Clone: %s' % err, 'error')
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
        tr.delete(*tr.get_children())
        names = self.names()
        for i in range(Q.CLONE_SLOTS):
            if names:
                n = names[i] if i < len(names) else ''
                e = self.entry(i)
                tags = ('nam',) if e else ('empty',) if not n else ()
                kind = 'NAM' if e else ''
                text = n or '(vazio)'
            else:
                tags, kind, text = ('empty',), '', '—'
            tr.insert('', 'end', iid=str(i), text='  Clone %-3d %s' % (i + 1, text), values=(kind,), tags=tags)
        tr.selection_set(str(self.slot))

    def _on_slot(self):
        sel = self.slots.selection()
        if sel and int(sel[0]) != self.slot:
            self.slot = int(sel[0])
            self.show_slot()
            self.show_details()

    def show_slot(self):
        T, p = self.W.T, self.p
        names = self.names()
        i = self.slot
        self.t_label.configure(text='CLONE %d' % (i + 1))
        for w in self.t_kind.winfo_children():
            w.destroy()
        e = self.entry(i)
        if not self.pv.connected:
            self.t_name.configure(text='Pedal desconectado')
            sub = 'Conecte o pedal para ler os nomes dos slots e gravar capturas.'
            if self.log.get(str(i)):
                le = self.log[str(i)]
                sub += ' Última gravação feita por aqui neste slot: "%s" (%s), em %s.' % (
                    le.get('name'), le.get('file'), le.get('date'))
        else:
            n = names[i] if i < len(names) else ''
            self.t_name.configure(text=n or '(vazio)')
            if e:
                self.W.Pill(self.t_kind, self.app, 'NAM A2-Lite', T['accent']).pack(side='left')
                sub = 'Gravada pelo Studio em %s, do arquivo %s (roda a %s).' % (
                    e.get('date'), e.get('file'), e.get('runs_at'))
            elif not n:
                sub = 'Slot vazio.'
            else:
                sub = ('Sem registro de gravação por aqui: captura de fábrica, do editor original ou gravada por '
                       'outra ferramenta (nam_clone.py). O pedal não devolve os dados de um slot, só o nome; no '
                       'firmware namclone, um slot que não seja NAM toca o sinal seco (no clonedual, ele roda o '
                       'Clone de fábrica).')
        self.t_sub.configure(text=sub)

    # ------------------------------------------------------------------ library
    def choose_folder(self):
        d = filedialog.askdirectory(parent=self.app.root, title='Pasta com capturas .nam',
                                    initialdir=self.folder if os.path.isdir(self.folder) else Q.ROOT)
        if d:
            self.folder = d
            self.rescan()

    def open_file(self):
        path = filedialog.askopenfilename(parent=self.app.root, title='Captura .nam',
                                          initialdir=self.folder if os.path.isdir(self.folder) else Q.ROOT,
                                          filetypes=[('Neural Amp Modeler', '*.nam'), ('Todos', '*.*')])
        if not path:
            return
        try:
            d = N.describe(path)
        except Exception as ex:
            self.app.toast_msg('Não foi possível ler %s: %s' % (os.path.basename(path), ex), 'error')
            return
        self.extra = [x for x in self.extra if not same_path(x['path'], path)] + [d]
        self.qv.set('')
        self.fill_lib()
        self.select_file(d)

    def rescan(self):
        self.folder_lbl.configure(text=self._short(self.folder) + '  (lendo…)')
        folder = self.folder
        threading.Thread(target=lambda: self.q.put(('scan', folder, scan(folder) if os.path.isdir(folder) else [])),
                         daemon=True).start()

    def _short(self, path, n=48):
        return path if len(path) <= n else '…' + path[-(n - 1):]

    def _pump(self):
        try:
            while True:
                kind, folder, files = self.q.get_nowait()
                if kind == 'scan' and folder == self.folder:
                    self.files = files
                    ok = sum(1 for d in files if d['lite'])
                    self.folder_lbl.configure(text='%s  ·  %d arquivos, %d rodam no pedal' % (
                        self._short(self.folder), len(files), ok))
                    if self.sel is not None:
                        self.sel = next((d for d in self.all_files() if same_path(d['path'], self.sel['path'])),
                                        self.sel)
                    self.fill_lib()
                    if self.sel is None:
                        first = next((d for d in self.all_files() if d['lite']), None)
                        if first:
                            self.select_file(first)
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
            m = d['meta']
            gear = ' '.join(x for x in (m.get('gear_make'), m.get('gear_model')) if x)
            if m.get('gear_make') and m.get('gear_make') == m.get('gear_model'):
                gear = m['gear_make']
            if q and not any(q in str(x).lower() for x in (d['name'], d['file'], gear, m.get('modeled_by'))):
                continue
            slots = self.slots_of(d)
            fit = 'A2-Lite · %.0f kHz' % (d['sample_rate'] / 1000) if d['lite'] else d['arch']
            tags = ('loaded',) if slots else ('bad',) if not d['lite'] else ()
            tr.insert('', 'end', iid=str(k), text='  ' + d['name'],
                      values=(gear or '—', m.get('gear_type') or '—', fit,
                              ', '.join('Clone %d' % s for s in slots)), tags=tags)
            shown += 1
            if self.sel is not None and same_path(d['path'], self.sel['path']):
                tr.selection_set(str(k))
                tr.see(str(k))
        if shown:
            self.lib_empty.place_forget()
        else:
            self.lib_empty.configure(text='Nenhum arquivo .nam nesta pasta.' if not files else
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
        self.name_var.set(d['name'].encode('latin1', 'replace').decode('latin1')[:Q.CLONE_NAME_MAX])
        self.fill_lib()
        self.show_details()

    # ------------------------------------------------------------------ details
    def _kv(self, box, pairs):
        T, F, p = self.W.T, self.F, self.p
        pairs = [(k, v) for k, v in pairs if v not in (None, '')]
        if not pairs:
            tk.Label(box, text='O arquivo não traz essas informações.', font=F.small, bg=T['panel'],
                     fg=T['faint']).pack(anchor='w', padx=p(20))
            return
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
        self.prog_cv = self.prog_lbl = None
        bg = T['panel']
        pad = dict(padx=p(20))
        d = self.sel
        if d is None:
            tk.Label(box, text='Capturas NAM', font=F.h2, bg=bg, fg=T['text']).pack(anchor='w', pady=(p(24), p(8)),
                                                                                  **pad)
            for t in ('Escolha a pasta com os seus arquivos .nam (o botão Pasta) ou abra um arquivo avulso.',
                      'Só a rede A2-Lite roda no pedal: arquivos WaveNet A2 de 3 canais ou containers "slimmable" '
                      '(TONE3000 / NAM 0.7+) que a tenham. Capturas A1 "standard" e LSTM não servem.',
                      'Gravar substitui a captura do slot, sem volta: o pedal não deixa ler os dados de um slot.',
                      'As capturas NAM só tocam com o firmware namclone ou clonedual (fábrica + NAM por slot); '
                      'no de fábrica o slot fica mudo.'):
                r = tk.Frame(box, bg=bg)
                r.pack(fill='x', padx=p(20), pady=p(4))
                tk.Label(r, text='•', font=F.body, bg=bg, fg=T['accent']).pack(side='left', anchor='n')
                tk.Label(r, text=t, font=F.body, bg=bg, fg=T['soft'], wraplength=p(290), justify='left').pack(
                    side='left', padx=(p(8), 0))
            return
        m = d['meta']
        tk.Label(box, text=d['name'], font=F.h2, bg=bg, fg=T['text'], wraplength=p(320), justify='left').pack(
            anchor='w', pady=(p(22), p(2)), **pad)
        tk.Label(box, text=d['file'], font=F.small, bg=bg, fg=T['muted'], wraplength=p(320), justify='left').pack(
            anchor='w', **pad)
        chips = tk.Frame(box, bg=bg)
        chips.pack(anchor='w', fill='x', pady=(p(12), 0), **pad)
        W.Pill(chips, self.app, 'Roda no pedal' if d['lite'] else 'Não roda no pedal',
               T['green'] if d['lite'] else T['red'], icon=W.ICO['check'] if d['lite'] else W.ICO['warn']).pack(
            side='left')
        for s in self.slots_of(d):
            W.Pill(chips, self.app, 'Clone %d' % s, T['accent']).pack(side='left', padx=(p(6), 0))
        tk.Label(box, text=d['reason'][:1].upper() + d['reason'][1:] + '.', font=F.small, bg=bg, fg=T['soft'],
                 wraplength=p(320), justify='left').pack(anchor='w', pady=(p(8), 0), **pad)
        if d['lite']:
            self._send_box(box, d)
        self.app._section(box, 'CAPTURA')
        gear = ' '.join(x for x in (m.get('gear_make'), m.get('gear_model')) if x)
        if m.get('gear_make') and m.get('gear_make') == m.get('gear_model'):
            gear = m['gear_make']
        esr = (m.get('training') or {}).get('validation_esr') if isinstance(m.get('training'), dict) else None
        self._kv(box, [('Equipamento', gear), ('Tipo', m.get('gear_type')), ('Timbre', m.get('tone_type')),
                       ('Autor', m.get('modeled_by')), ('Treinada com', m.get('trainer')),
                       ('Data', fmt_date(m.get('date'))),
                       ('Nível de entrada', '%g dBu' % m['input_level_dbu'] if m.get('input_level_dbu') is not None
                        else None),
                       ('Nível de saída', '%g dBu' % m['output_level_dbu'] if m.get('output_level_dbu') is not None
                        else None),
                       ('Loudness', '%.1f dB' % d['loudness'] if d.get('loudness') is not None else None),
                       ('ESR de validação', '%.4f' % esr if isinstance(esr, (int, float)) else None)])
        self.app._section(box, 'REDE')
        rows = [('Arquitetura', d['arch'] + (' %s' % d['version'] if d.get('version') else '')),
                ('Taxa de treino', '%.1f kHz' % (d['sample_rate'] / 1000) if d.get('sample_rate') else None)]
        for k, s in enumerate(d['submodels']):
            rows.append(('Submodelo %d' % (k + 1), '%s, %d pesos%s' % (s['arch'], s['weights'],
                                                                   ' (a usada)' if s['lite'] else '')))
        if d['lite']:
            rows.append(('No pedal', runs_txt(is48(d) and not self.native.get())))
        self._kv(box, rows)
        tk.Frame(box, bg=bg, height=p(20)).pack()

    def _send_box(self, box, d):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        bg = T['panel']
        pad = dict(padx=p(20))
        self.app._section(box, 'GRAVAR NO PEDAL')
        tk.Label(box, text='Nome no pedal (até %d caracteres)' % Q.CLONE_NAME_MAX, font=F.small, bg=bg,
                 fg=T['muted']).pack(anchor='w', pady=(0, p(4)), **pad)
        eb = tk.Frame(box, bg=T['raised'], highlightthickness=1, highlightbackground=T['line'])
        eb.pack(fill='x', **pad)
        ent = tk.Entry(eb, textvariable=self.name_var, font=F.body, bg=T['raised'], fg=T['text'], bd=0,
                       insertbackground=T['text'], relief='flat', highlightthickness=0)
        ent.pack(side='left', fill='x', expand=True, padx=p(8), ipady=p(5))
        cnt = tk.Label(eb, text='', font=F.tiny, bg=T['raised'], fg=T['faint'])
        cnt.pack(side='right', padx=(0, p(8)))

        def on_name(*a):
            v = self.name_var.get()
            if len(v) > Q.CLONE_NAME_MAX:
                self.name_var.set(v[:Q.CLONE_NAME_MAX])
                return
            try:
                cnt.configure(text='%d/%d' % (len(v), Q.CLONE_NAME_MAX))
            except tk.TclError:
                pass
        for t in self.name_var.trace_info():
            self.name_var.trace_remove(*t)
        self.name_var.trace_add('write', on_name)
        on_name()
        if is48(d):
            W.Switch(box, self.app, 'Rodar a 44,1 kHz, sem resampling', self.native,
                     command=self.show_details).pack(anchor='w', pady=(p(12), 0), **pad)
            tk.Label(box, text='Uns 4% menos de CPU, mas a resposta fica cerca de 8% mais grave. Desligado, a rede '
                               'roda a 48 kHz como foi treinada.', font=F.small, bg=bg, fg=T['faint'],
                     wraplength=p(310), justify='left').pack(anchor='w', pady=(p(4), 0), **pad)
        can = self.pv.connected and not self.busy
        btn = W.Btn(box, self.app, 'Gravar no Clone %d' % (self.slot + 1), W.ICO['save'], self.send, 'danger',
                    height=38, padx=16, tip='Substitui a captura do slot escolhido à esquerda')
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
        tk.Label(box, text='Precisa do firmware namclone ou clonedual (motor do Clone em "Gerar firmware"); no de '
                           'fábrica o slot fica mudo. A captura atual do slot é substituída e não pode ser recuperada pelo '
                           'pedal.', font=F.small, bg=bg, fg=T['faint'], wraplength=p(310), justify='left').pack(
            anchor='w', pady=(p(12), 0), **pad)

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
        if d is None or not d['lite'] or not self.pv.connected or self.busy:
            return
        name = self.name_var.get().strip() or d['name']
        name = name.encode('latin1', 'replace')[:Q.CLONE_NAME_MAX].decode('latin1')
        native = bool(self.native.get()) and is48(d)
        try:
            blob, info = N.pack_blob(d['path'], native)
        except Exception as ex:
            self.app.toast_msg('Não foi possível preparar %s: %s' % (d['file'], ex), 'error')
            return
        names = self.names()
        old = names[slot] if slot < len(names) else ''
        e = self.entry(slot)
        if not old:
            what = 'O slot Clone %d está vazio.' % (slot + 1)
        elif e:
            what = 'O slot Clone %d tem a captura NAM "%s", gravada por aqui em %s (%s).' % (
                slot + 1, old, e.get('date'), e.get('file'))
        else:
            what = ('O slot Clone %d tem a captura "%s", sem registro aqui: provavelmente de fábrica ou do editor '
                    'original. O pedal não deixa ler os dados de um slot, então ela não poderá ser recuperada '
                    'depois (a não ser que você tenha o arquivo original).' % (slot + 1, old))
        text =('%s\n\nSerá gravada a captura "%s" (%s; roda a %s, %d bytes).\n\nA captura NAM só toca com o '
                'firmware namclone ou clonedual; com o firmware de fábrica o slot fica mudo. O pedal reinicia a cadeia ao '
                'gravar.' % (what, name, d['file'], runs_txt(is48(d) and not native), info['bytes']))
        if not self.W.ask(self.app, 'Gravar no Clone %d' % (slot + 1), text,
                          'Substituir' if old else 'Gravar', 'danger'):
            return
        if not self.pv.connected or self.busy:
            return
        self.busy = (slot, d, name, info, blob)
        self.progress = (0, 0)
        self.pv.worker.submit('nam_write', PV.job_clone_write, slot, name, blob)
        self.refresh()
        self._draw_progress()

    def _write_done(self, res, err):
        busy, self.busy = self.busy, None
        if err:
            self.app.toast_msg('A gravação falhou: %s. Confira o nome do slot na lista.' % err, 'error')
            if self.pv.connected:
                self.pv.worker.submit('nam_list', PV.job_clone_list)
            return
        slot, name, names = res
        if busy is None or busy[0] != slot:
            return
        _s, d, _n, info, blob = busy
        try:
            self.log = N.record(slot, name, d['path'], info, blob, self.log_path)
        except Exception as ex:
            self.app.toast_msg('Não foi possível salvar o registro de capturas: %s' % ex, 'warn')
        got = names[slot] if slot < len(names) else ''
        if got == name:
            self.app.toast_msg('Clone %d gravado: "%s". Escolha Clone %d no menu Clone do pedal para tocar.' % (
                slot + 1, name, slot + 1), 'ok')
        else:
            self.app.toast_msg('O pedal confirmou a gravação, mas o slot agora se chama "%s".' % got, 'warn')
