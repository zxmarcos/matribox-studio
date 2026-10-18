"""Matribox Studio, aba Ferramentas: o que antes pedia scripts na linha de comando.

Por enquanto, os ports de firmware: extrair do firmware oficial do Valeton GP-200 e do Matribox II (QME-100) os
efeitos que o QME-200 não tem, como `matribox port export` (matribox/ports/gpport.py). A conversão não compila
nada: ela desliga os módulos pré-linkados do outro pedal e mede a RAM de cada um no emulador (pacote unicorn).
A pasta de saída ganha um library.json e vira uma biblioteca do Studio, pronta para a aba Firmware.
"""
import os, queue, threading, time
import tkinter as tk
from tkinter import ttk, filedialog

from matribox import paths
from matribox.ports import gpport
from matribox.studio import aa

STATUS = {'ok': 'convertido', 'failed': 'falhou', 'skipped': 'sem menu', 'folder': 'na pasta'}
DESCRIPTIONS = {
    'gp200': 'O GP-200 usa a mesma plataforma do QME-200 e tem efeitos que ele não tem: drives, amps, delays e '
             'reverbs como Bad-KT, Silver Twin, Ice Delay, Broken Delay e Tube Spring. A receita foi feita para o '
             'firmware V1.8.2 e extrai %d efeitos.',
    'qme100': 'O Matribox II é o irmão menor do QME-200, com um container próprio (HTFW). Quase tudo o que ele tem '
              'a mais também está no GP-200; a receita foi feita para o firmware V1.3.0 e, depois do GP-200, '
              'acrescenta %d efeitos (Digi Octa e Leslee).',
}


def same_path(a, b):
    return bool(a and b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


class ToolsView(tk.Frame):
    def __init__(self, parent, app, W):
        self.app, self.W = app, W
        T = W.T
        super().__init__(parent, bg=T['bg'])
        self.S, self.F = app.S, app.F
        s = app.session.setdefault('tools', {})
        self.cur = s.get('port') if s.get('port') in gpport.PORT else gpport.PORTS[0]['key']
        self.add_lib = tk.BooleanVar(value=s.get('add_lib', True))
        self.state = {}
        for r in gpport.PORTS:
            k = r['key']
            mine = s.get(k, {})
            src = mine.get('src') or os.path.join(paths.FIRMWARE_DIR, r['file'])
            self.state[k] = dict(src=tk.StringVar(value=src if os.path.exists(src) else ''),
                                 out=tk.StringVar(value=mine.get('out') or paths.build_path('ports', k)),
                                 records=None, progress=(0, 0), last='', ident=None, ident_key=None)
        self.job = None                 # (port key, cancel flag list) while an export runs
        self.q = queue.Queue()
        self._layout()
        for k, st in self.state.items():
            st['src'].trace_add('write', lambda *a, k=k: self._src_changed(k))
            st['out'].trace_add('write', lambda *a, k=k: self._out_changed(k))
        self.select(self.cur)

    def p(self, v):
        return round(v * self.S)

    def shutdown(self):
        s = self.app.session.setdefault('tools', {})
        s['port'], s['add_lib'] = self.cur, bool(self.add_lib.get())
        for k, st in self.state.items():
            s[k] = dict(src=st['src'].get().strip(), out=st['out'].get().strip())
        if self.job:
            self.job[1].append(True)

    def refresh(self):
        self.fill_side()
        self.show()

    # ------------------------------------------------------------------ layout
    def _layout(self):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        left = tk.Frame(self, bg=T['panel'], width=p(260))
        left.pack(side='left', fill='y')
        left.pack_propagate(False)
        tk.Frame(self, bg=T['line'], width=1).pack(side='left', fill='y')
        tk.Label(left, text='PORTS DE FIRMWARE', font=F.tiny, bg=T['panel'], fg=T['muted']).pack(
            anchor='w', padx=p(16), pady=(p(16), p(8)))
        self.side = tk.Frame(left, bg=T['panel'])
        self.side.pack(fill='x', padx=p(8))
        tk.Label(left, text='Os ports convertem efeitos de pedais da mesma plataforma em módulos do QME-200. Nada '
                            'vem com o Studio: cada port lê o firmware oficial do outro pedal, que você baixa do '
                            'fabricante, e gera os módulos no seu computador.',
                 font=F.small, bg=T['panel'], fg=T['faint'], wraplength=p(226), justify='left').pack(
            side='bottom', anchor='w', padx=p(16), pady=(p(4), p(14)))
        center = tk.Frame(self, bg=T['bg'])
        center.pack(side='left', fill='both', expand=True, padx=p(24), pady=p(20))
        head = tk.Frame(center, bg=T['bg'])
        head.pack(fill='x')
        self.t_name = tk.Label(head, text='', font=F.h1, bg=T['bg'], fg=T['text'])
        self.t_name.pack(side='left')
        self.t_pill = tk.Frame(head, bg=T['bg'])
        self.t_pill.pack(side='left', padx=(p(12), 0), anchor='s', pady=(0, p(5)))
        self.t_sub = tk.Label(center, text='', font=F.small, bg=T['bg'], fg=T['muted'], anchor='w', justify='left')
        self.t_sub.pack(fill='x', pady=(p(4), p(14)))
        self.t_sub.bind('<Configure>', lambda e: self.t_sub.configure(wraplength=max(100, e.width - p(4))))
        form = tk.Frame(center, bg=T['bg'])
        form.pack(fill='x')
        form.columnconfigure(0, weight=1, uniform='f')
        form.columnconfigure(1, weight=1, uniform='f')
        self.src_box = self._path_box(form, 0, 'FIRMWARE ORIGINAL', self.browse_src)
        self.out_box = self._path_box(form, 1, 'PASTA DE SAÍDA', self.browse_out)
        self.src_info = self._info(self.src_box)
        self.out_info = self._info(self.out_box)
        opts = tk.Frame(center, bg=T['bg'])
        opts.pack(fill='x', pady=(p(12), 0))
        W.Switch(opts, self.app, 'Adicionar a pasta às bibliotecas do Studio', self.add_lib).pack(anchor='w')
        self.cmp_info = tk.Label(opts, text='', font=F.small, bg=T['bg'], fg=T['faint'], anchor='w', justify='left')
        self.cmp_info.pack(fill='x', pady=(p(8), 0))
        self.cmp_info.bind('<Configure>', lambda e: self.cmp_info.configure(wraplength=max(100, e.width - p(4))))
        acts = tk.Frame(center, bg=T['bg'])
        acts.pack(fill='x', pady=(p(14), p(12)))
        self.btn_run = W.Btn(acts, self.app, 'Extrair efeitos', W.ICO['build'], self.run, 'primary', height=36,
                             padx=16, tip='Converte os efeitos e grava os módulos na pasta de saída')
        self.btn_run.pack(side='left')
        self.btn_cancel = W.Btn(acts, self.app, 'Cancelar', None, self.cancel, 'ghost', height=36, padx=14)
        self.btn_reveal = W.Btn(acts, self.app, 'Abrir a pasta', W.ICO['folder'], self.reveal, 'ghost', height=36,
                                padx=14)
        self.btn_reveal.pack(side='right')
        self.prog = tk.Frame(acts, bg=T['bg'])
        self.prog.pack(side='left', fill='x', expand=True, padx=(p(18), p(18)))
        self.prog_lbl = tk.Label(self.prog, text='', font=F.small, bg=T['bg'], fg=T['soft'], anchor='w')
        self.prog_lbl.pack(fill='x')
        self.prog_cv = tk.Canvas(self.prog, height=p(6), bg=T['bg'], highlightthickness=0)
        self.prog_cv.bind('<Configure>', lambda e: self._draw_progress())
        tcard = tk.Frame(center, bg=T['card'], highlightthickness=1, highlightbackground=T['cardline'])
        tcard.pack(fill='both', expand=True)
        cols = ('menu', 'cat', 'res', 'size', 'iram', 'sram')
        self.tree = ttk.Treeview(tcard, columns=cols, style='Fx.Treeview', selectmode='browse')
        self.tree.heading('#0', text='EFEITO', anchor='w')
        heads = dict(menu='MENU', cat='CATEGORIA', res='RESULTADO', size='MÓDULO', iram='IRAM', sram='SDRAM')
        widths = dict(menu=150, cat=130, res=110, size=90, iram=90, sram=90)
        for c in cols:
            self.tree.heading(c, text=heads[c], anchor='w')
            self.tree.column(c, width=p(widths[c]), stretch=False, minwidth=p(40), anchor='w')
        self.tree.column('#0', width=p(200), minwidth=p(120), stretch=True)
        vs = ttk.Scrollbar(tcard, orient='vertical', command=self.tree.yview, style='Dark.Vertical.TScrollbar')
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True, padx=(p(4), 0), pady=p(4))
        self.tree.tag_configure('failed', foreground=T['red'])
        self.tree.tag_configure('skipped', foreground=T['faint'])
        self.tree.tag_configure('folder', foreground=T['soft'])
        self.tree.bind('<<TreeviewSelect>>', lambda e: self._on_row())
        self.empty = tk.Label(tcard, text='', font=F.body, bg=T['card'], fg=T['muted'], justify='center')
        self.row_info = tk.Label(center, text='', font=F.small, bg=T['bg'], fg=T['faint'], anchor='w',
                                 justify='left')
        self.row_info.pack(fill='x', pady=(p(8), 0))
        self.row_info.bind('<Configure>', lambda e: self.row_info.configure(wraplength=max(100, e.width - p(4))))

    def _path_box(self, form, col, title, browse):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        box = tk.Frame(form, bg=T['bg'])
        box.grid(row=0, column=col, sticky='new', padx=(0, p(10)) if col == 0 else (p(10), 0))
        box.title = tk.Label(box, text=title, font=F.tiny, bg=T['bg'], fg=T['muted'])
        box.title.pack(anchor='w', pady=(0, p(6)))
        row = tk.Frame(box, bg=T['bg'])
        row.pack(fill='x')
        box.field = W.Field(row, self.app, tk.StringVar(), '', search=False, height=36, font=F.mono)
        box.field.pack(side='left', fill='x', expand=True)
        W.Btn(row, self.app, '', W.ICO['folder'], browse, 'ghost', height=36, padx=12).pack(side='left', padx=(p(8), 0))
        return box

    def _info(self, box):
        lb = tk.Label(box, text='', font=self.F.small, bg=self.W.T['bg'], fg=self.W.T['muted'], anchor='nw',
                      justify='left', height=2)
        lb.pack(fill='x', pady=(self.p(6), 0))
        lb.bind('<Configure>', lambda e: lb.configure(wraplength=max(100, e.width - self.p(4))))
        return lb

    # ------------------------------------------------------------------ state
    def recipe(self, k=None):
        return gpport.PORT[k or self.cur]

    def identify(self, k):
        """(recipe, version, verified) or the error text, cached per path + mtime"""
        st = self.state[k]
        src = st['src'].get().strip()
        if not src:
            return 'Escolha o arquivo .bin do firmware oficial.'
        if not os.path.isfile(src):
            return 'Arquivo não encontrado.'
        key = (os.path.normcase(os.path.abspath(src)), os.path.getmtime(src))
        if st['ident_key'] != key:
            try:
                with open(src, 'rb') as f:
                    st['ident'] = gpport.identify(f.read())
            except Exception as ex:
                st['ident'] = 'Não serve: %s.' % (str(ex) or type(ex).__name__)
            st['ident_key'] = key
        return st['ident']

    def skip_dirs(self, k):
        """folders whose ids this export leaves out: the GP-200 export, for the QME-100 (as the recipe says)"""
        after = self.recipe(k).get('after')
        if not after:
            return []
        d = self.state[after]['out'].get().strip()
        return [d] if d and gpport.folder_port(d) == after and gpport.manifest_ids(d) else []

    def reference(self):
        """the QME-200 image whose effects are left out: the original firmware, else the Studio's base"""
        C = self.W.C
        if os.path.exists(C.ORIGINAL_FW):
            return C.ORIGINAL_FW
        P = self.app.project
        return P.base_path if P else None

    def _src_changed(self, k):
        if k == self.cur:
            self.show()

    def _out_changed(self, k):
        if k == self.cur and not (self.job and self.job[0] == k):
            self.state[k]['records'] = None
            self.fill_side()
            self.show()

    # ------------------------------------------------------------------ views
    def select(self, k):
        self.cur = k
        st = self.state[k]
        for box, var in ((self.src_box, st['src']), (self.out_box, st['out'])):
            box.field.entry.configure(textvariable=var)
        self.refresh()

    def fill_side(self):
        T = self.W.T
        for w in self.side.winfo_children():
            w.destroy()
        for r in gpport.PORTS:
            k = r['key']
            n = len(gpport.manifest_ids(self.state[k]['out'].get().strip()))
            right = '…' if self.job and self.job[0] == k else str(n) if n else ''
            self.W.RowButton(self.side, self.app, r['name'], right, r['color'], k == self.cur,
                             lambda k=k: self.select(k), height=36).pack(fill='x', pady=(0, self.p(2)))

    def show(self):
        W, T, F, p = self.W, self.W.T, self.F, self.p
        k, r, st = self.cur, self.recipe(), self.state[self.cur]
        self.t_name.configure(text=r['name'])
        for w in self.t_pill.winfo_children():
            w.destroy()
        W.Pill(self.t_pill, self.app, 'firmware %s' % r['version'], r['color']).pack(side='left')
        self.t_sub.configure(text=DESCRIPTIONS[k] % r['expect'])
        self.src_box.title.configure(text='FIRMWARE ORIGINAL DO %s' % r['model'])
        ident = self.identify(k)
        ok = not isinstance(ident, str)
        if not ok:
            self.src_info.configure(text=ident + '  Nome do arquivo oficial: %s' % r['file'], fg=T['amber']
                                    if not st['src'].get().strip() else T['red'])
        elif ident[0]['key'] != k:
            ok = False
            self.src_info.configure(text='Este é o firmware do %s: escolha-o no port dele, à esquerda.' %
                                    ident[0]['name'], fg=T['red'])
        elif ident[2]:
            self.src_info.configure(text='%s %s · arquivo conferido (é o firmware para o qual a receita foi feita).'
                                    % (r['model'], ident[1]), fg=T['green'])
        else:
            self.src_info.configure(text='%s %s, mas não o arquivo da receita (%s). Dá para tentar: os efeitos que '
                                    'não passarem na conversão ficam de fora.' % (r['model'], ident[1], r['version']),
                                    fg=T['amber'])
        out = st['out'].get().strip()
        n = len(gpport.manifest_ids(out)) if out else 0
        if not out:
            self.out_info.configure(text='Escolha a pasta onde os módulos serão gravados.', fg=T['amber'])
        elif n and gpport.folder_port(out) != k:
            self.out_info.configure(text='A pasta já tem módulos que não vieram deste port: escolha outra.',
                                    fg=T['red'])
        elif n:
            self.out_info.configure(text='%d efeitos de uma extração anterior: extrair de novo os substitui.' % n,
                                    fg=T['muted'])
        else:
            self.out_info.configure(text='Os módulos (.axf + manifesto .json) e um library.json vão para esta pasta.',
                                    fg=T['muted'])
        ref = self.reference()
        cmp = ('Ficam de fora os efeitos que o QME-200 já tem (lidos de %s).' % os.path.basename(ref) if ref else
               'Falta o firmware do QME-200 (%s em %s): ele diz quais efeitos já existem.' % (
                   paths.ORIGINAL_NAME, paths.FIRMWARE_DIR))
        if r.get('after'):
            skip = self.skip_dirs(k)
            a = self.recipe(r['after'])
            cmp += (' Também ficam de fora os %d que o port do %s já extraiu.' % (
                len(gpport.manifest_ids(skip[0])), a['name']) if skip else
                ' O port do %s ainda não foi extraído: extraia-o antes, senão saem daqui também os efeitos que os '
                'dois pedais têm em comum.' % a['name'])
        cmp += ' Leva cerca de um minuto: cada efeito roda no emulador para medir a RAM e conferir a saída.'
        self.cmp_info.configure(text=cmp, fg=T['faint'] if ref else T['red'])
        running = self.job is not None
        mine = running and self.job[0] == k
        can = ok and bool(ref) and bool(out) and not (n and gpport.folder_port(out) != k)
        self.btn_run.config_btn(enabled=can and not running)
        if mine:
            if not self.btn_cancel.winfo_ismapped():
                self.btn_cancel.pack(side='left', padx=(p(8), 0), before=self.prog)
        else:
            self.btn_cancel.pack_forget()
        self.btn_reveal.config_btn(enabled=bool(out) and os.path.isdir(out))
        self._show_progress()
        self.fill_rows()

    def _show_progress(self):
        k, st = self.cur, self.state[self.cur]
        mine = self.job is not None and self.job[0] == k
        if mine:
            done, total = st['progress']
            txt = 'Lendo os firmwares…' if not total else '%d de %d efeitos%s' % (
                done, total, ' · último: ' + st['last'] if st['last'] else '')
            if self.job[1]:
                txt = 'Cancelando depois do efeito atual…'
            self.prog_lbl.configure(text=txt, fg=self.W.T['soft'])
            if not self.prog_cv.winfo_ismapped():
                self.prog_cv.pack(fill='x', pady=(self.p(4), 0))
        else:
            self.prog_cv.pack_forget()
            self.prog_lbl.configure(text=st.get('summary', ''), fg=st.get('summary_fg', self.W.T['muted']))
        self._draw_progress()

    def _draw_progress(self):
        c = self.prog_cv
        try:
            c.delete('all')
            w, h = c.winfo_width(), c.winfo_height()
            if w < 4:
                return
            T = self.W.T
            done, total = self.state[self.cur]['progress']
            shapes = [('rrect', (0, 0, w, h, h / 2), T['raised'])]
            if total and done:
                shapes.append(('rrect', (0, 0, max(h, w * done / total), h, h / 2), self.recipe()['color']))
            aa.draw(c, 0, 0, w, h, shapes, T['bg'])
        except tk.TclError:
            pass

    def rows(self):
        """the records of this session's export, else what the output folder holds"""
        st = self.state[self.cur]
        if st['records'] is not None:
            return st['records']
        out = []
        for path, m in self.W.C.manifests(st['out'].get().strip()):
            ram = m.get('ram', {})
            elf = os.path.join(os.path.dirname(path), m['elf'])
            out.append(dict(id=int(str(m['id']), 0), name=m['name'], category=m.get('category', ''),
                            menu=m['menu'], status='folder', reason='', checks=[],
                            elf=os.path.getsize(elf) if os.path.exists(elf) else 0,
                            iram=int(str(ram.get('iram', 0)), 0), sram=int(str(ram.get('sram', 0)), 0),
                            source=m.get('source', '')))
        return out

    def fill_rows(self):
        C, T = self.W.C, self.W.T
        sel = self.tree.selection()
        keep = self.tree.item(sel[0], 'text') if sel else None
        self.tree.delete(*self.tree.get_children())
        rows = self.rows()
        self.row_data = {}
        for r in rows:
            menu = C.MENUS.get(r['menu'], str(r['menu'])) if r['menu'] is not None else '—'
            ok = r['status'] in ('ok', 'folder')
            iid = self.tree.insert('', 'end', text=r['name'], values=(
                menu, r['category'] or '—', STATUS[r['status']], C.fmt_size(r['elf']) if ok else '',
                C.fmt_size(r['iram']) if ok and r['iram'] else '—' if ok else '',
                C.fmt_size(r['sram']) if ok and r['sram'] else '—' if ok else ''), tags=(r['status'],))
            self.row_data[iid] = r
            if r['name'] == keep:
                self.tree.selection_set(iid)
        if self.job and self.job[0] == self.cur and rows:
            self.tree.see(self.tree.get_children()[-1])
        if rows:
            self.empty.place_forget()
        else:
            self.empty.configure(text='Nenhum efeito extraído ainda.\nEscolha o firmware e clique em "Extrair efeitos".'
                                 if not (self.job and self.job[0] == self.cur) else 'Convertendo…')
            self.empty.place(relx=.5, rely=.45, anchor='center')
        self._on_row()

    def _on_row(self):
        sel = self.tree.selection()
        r = self.row_data.get(sel[0]) if sel else None
        T = self.W.T
        if r is None:
            fails = sum(1 for x in self.row_data.values() if x['status'] == 'failed')
            self.row_info.configure(text='Selecione um efeito para ver os detalhes.' + (
                ' %d não puderam ser convertidos.' % fails if fails else ''), fg=T['faint'])
        elif r['status'] == 'failed':
            self.row_info.configure(text='%s: %s' % (r['name'], r['reason']), fg=T['red'])
        elif r['status'] == 'skipped':
            self.row_info.configure(text='%s: %s' % (r['name'], r['reason']), fg=T['muted'])
        elif r['status'] == 'ok':
            self.row_info.configure(text='%s · id %#010x · %d relocações · saída no emulador: %s' % (
                r['name'], r['id'], r['relocs'], ', '.join(r['checks'])), fg=T['muted'])
        else:
            self.row_info.configure(text='%s · id %#010x · %s' % (r['name'], r['id'], r.get('source', '')),
                                    fg=T['muted'])

    # ------------------------------------------------------------------ actions
    def browse_src(self):
        st, r = self.state[self.cur], self.recipe()
        cur = st['src'].get().strip()
        f = filedialog.askopenfilename(parent=self, title='Firmware original do %s' % r['model'],
                                       initialdir=os.path.dirname(cur) if cur else paths.FIRMWARE_DIR,
                                       filetypes=[('Firmware %s' % r['model'], '*.bin'), ('Todos', '*.*')])
        if f:
            st['src'].set(os.path.normpath(f))

    def browse_out(self):
        st = self.state[self.cur]
        cur = st['out'].get().strip()
        d = filedialog.askdirectory(parent=self, title='Pasta de saída dos módulos',
                                    initialdir=cur if os.path.isdir(cur) else paths.BUILD_DIR)
        if d:
            st['out'].set(os.path.normpath(d))

    def reveal(self):
        out = self.state[self.cur]['out'].get().strip()
        if os.path.isdir(out):
            self.W.reveal(os.path.join(out, 'library.json') if os.path.exists(os.path.join(out, 'library.json'))
                          else out)

    def cancel(self):
        if self.job:
            self.job[1].append(True)
            self._show_progress()

    def run(self):
        W, T = self.W, self.W.T
        k, r, st = self.cur, self.recipe(), self.state[self.cur]
        if self.job:
            return
        ident = self.identify(k)
        ref = self.reference()
        if isinstance(ident, str) or ident[0]['key'] != k or not ref:
            self.show()
            return
        try:
            import unicorn  # noqa: F401  (gp2qme.measure and the checks run the modules in matribox.module.effemu)
        except ImportError:
            self.app.toast_msg('Falta o emulador: instale o pacote unicorn (pip install unicorn)', 'error')
            return
        src, out = st['src'].get().strip(), st['out'].get().strip()
        if not ident[2] and not W.ask(self.app, 'Firmware diferente do da receita', (
                'Este arquivo não é o firmware %s %s para o qual a receita foi feita. As janelas de memória de cada '
                'menu vêm daquela versão: se mudaram, os efeitos que não passarem na conversão ou na conferência no '
                'emulador ficam de fora, mas um que passe ainda pode se comportar mal no pedal.' % (
                    r['model'], r['version'])), 'Extrair mesmo assim', 'danger'):
            return
        old = len(gpport.manifest_ids(out))
        if old and not W.ask(self.app, 'Substituir a extração anterior', (
                'A pasta %s já tem %d efeitos deste port. Eles serão apagados e extraídos de novo.' % (out, old)),
                'Substituir'):
            return
        skip = self.skip_dirs(k)
        flag = []
        self.job = (k, flag)
        st.update(records=[], progress=(0, 0), last='', summary='')
        self.refresh()

        def work():
            try:
                with open(src, 'rb') as f:
                    data = f.read()
                with open(ref, 'rb') as f:
                    qm = f.read()
                gpport.clear_export(out, k)
                t0 = time.time()
                recs = gpport.export(data, qm, out, skip, log=lambda s: None,
                                     on_item=lambda rec, done, total: self.q.put(('item', dict(rec), done, total)),
                                     cancel=lambda: bool(flag))
                self.q.put(('done', recs, time.time() - t0, None))
            except Exception as ex:
                self.q.put(('error', str(ex) or type(ex).__name__, None, None))
        threading.Thread(target=work, daemon=True).start()
        self.after(80, self._pump)

    def _pump(self):
        if self.job is None:
            return
        k, flag = self.job
        st = self.state[k]
        finished = None
        try:
            while True:
                kind, a, b, c = self.q.get_nowait()
                if kind == 'item':
                    recs = [x for x in st['records'] if x['id'] != a['id']] + [a]
                    st.update(records=recs, progress=(b, c), last=a['name'])
                else:
                    finished = (kind, a, b)
        except queue.Empty:
            pass
        if finished:
            self._finish(k, flag, *finished)
            return
        if k == self.cur:
            self._show_progress()
            if len(st['records']) != len(self.tree.get_children()):
                self.fill_rows()
        self.after(80, self._pump)

    def _finish(self, k, flag, kind, a, secs):
        T = self.W.T
        st, r = self.state[k], self.recipe(k)
        self.job = None
        if kind == 'error':
            st.update(summary='Falhou: %s' % a, summary_fg=T['red'])
            self.app.toast_msg('O port do %s falhou: %s' % (r['name'], a), 'error')
            self.refresh()
            return
        st['records'] = a
        ok = sum(1 for x in a if x['status'] == 'ok')
        bad = sum(1 for x in a if x['status'] == 'failed')
        st.update(summary='%s%d efeitos convertidos%s em %d s' % (
            'Cancelado: ' if flag else '', ok, ', %d falharam' % bad if bad else '', round(secs)),
            summary_fg=T['amber'] if flag or bad else T['green'])
        out = st['out'].get().strip()
        added = ''
        if ok and self.add_lib.get():
            libs = self.app.libraries()
            if not any(same_path(self.W.C.absp(d), out) for d, n in libs):
                name = self.W.unique_name(r['name'], [n for d, n in libs])
                libs.append((out, name))
                added = ' e adicionados às bibliotecas como "%s"' % name
            self.app.set_libraries(libs)        # rereads the folders: the new modules show up in Firmware
        self.refresh()
        self.app.toast_msg('%s: %d efeitos extraídos%s' % (r['name'], ok, added), 'warn' if bad or flag else 'ok')
