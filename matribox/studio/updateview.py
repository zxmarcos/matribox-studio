"""Matribox Studio: grava um firmware .bin no pedal pela USB, como o editor original (protocolo em qmeupdate.py).

O pedal precisa estar no modo de atualização (bootloader). Se estiver no modo normal, o Studio pede o reinício
nesse modo, como o editor faz na página de atualização. Também dá para ligá-lo segurando a combinação de
footswitches. O bootloader nunca é regravado, então uma atualização que falhe pode ser repetida.

A atualização roda num thread com a própria porta MIDI (a aba Pedal solta a dela antes). Cada mensagem vai
para um log em %APPDATA%\\MatriboxStudio\\update-*.log.
"""
import os, queue, threading, time
import tkinter as tk
from tkinter import filedialog

from matribox.studio import aa
from matribox.usb import htmidi
from matribox.usb import qmeupdate as U

LOG_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MatriboxStudio')


class UpdateDialog:
    """built on fwstudio.Dialog (W = the fwstudio module, as in pedalview / namview)"""

    def __init__(self, app, W, path=None):
        self.app, self.W, self.T = app, W, W.T
        T, S, F = W.T, app.S, app.F
        p = self.p = lambda v: round(v * S)
        self.d = d = W.Dialog(app, 'Atualizar o pedal', 640)
        d.close = self.close                            # Esc / buttons go through the running check
        self.img, self.running, self.cancelled = None, False, False
        self.q = queue.Queue()
        self.pedal_state = None                         # None unknown, 'busy', 'none', 'app', 'boot'
        bg = T['panel']
        body = d.body
        tk.Label(body, text='Grava o .bin no pedal pela USB, como o editor original. Feche o editor antes: a porta '
                 'MIDI é exclusiva.', font=F.small, bg=bg, fg=T['muted'], wraplength=p(580),
                 justify='left').pack(anchor='w', pady=(0, p(12)))
        # file --------------------------------------------------------------
        tk.Label(body, text='FIRMWARE', font=F.tiny, bg=bg, fg=T['muted']).pack(anchor='w', pady=(0, p(6)))
        row = tk.Frame(body, bg=bg)
        row.pack(fill='x')
        fr = tk.Frame(row, bg=T['raised'], highlightthickness=1, highlightbackground=T['line'])
        fr.pack(side='left', fill='x', expand=True)
        self.path = tk.StringVar(value=path or '')
        e = tk.Entry(fr, textvariable=self.path, font=F.mono, bg=T['raised'], fg=T['text'], bd=0, relief='flat',
                     insertbackground=T['text'], highlightthickness=0)
        e.pack(fill='x', padx=p(10), ipady=p(7))
        e.bind('<FocusOut>', lambda ev: self.load_file())
        e.bind('<Return>', lambda ev: self.load_file())
        self.btn_browse = W.Btn(row, app, '', W.ICO['folder'], self.browse, 'ghost', height=36, padx=12)
        self.btn_browse.pack(side='left', padx=(p(8), 0))
        self.file_info = tk.Label(body, text='', font=F.small, bg=bg, fg=T['muted'], justify='left', anchor='nw',
                                  wraplength=p(580))
        self.file_info.pack(fill='x', pady=(p(8), 0))
        # pedal -------------------------------------------------------------
        tk.Label(body, text='PEDAL', font=F.tiny, bg=bg, fg=T['muted']).pack(anchor='w', pady=(p(16), p(6)))
        prow = tk.Frame(body, bg=bg)
        prow.pack(fill='x')
        self.dot = tk.Canvas(prow, width=p(10), height=p(10), bg=bg, highlightthickness=0)
        self.dot.pack(side='left')
        self.pedal_lbl = tk.Label(prow, text='', font=F.body, bg=bg, fg=T['soft'], justify='left', anchor='w',
                                  wraplength=p(470))
        self.pedal_lbl.pack(side='left', padx=(p(8), 0), fill='x', expand=True)
        self.btn_probe = W.Btn(prow, app, 'Verificar', W.ICO['reset'], self.probe, 'ghost', height=32, padx=12,
                               tip='Procura o pedal e vê se está no modo normal ou no de atualização')
        self.btn_probe.pack(side='right')
        # confirmation --------------------------------------------------------
        self.agree = tk.BooleanVar(value=False)
        self.sw = W.Switch(body, app, 'Os presets do pedal (Bank) serão substituídos pelos do arquivo', self.agree,
                           self._sync)
        self.sw.pack(anchor='w', pady=(p(18), p(2)))
        tk.Label(body, text='Capturas NAM (Clone) e IRs de usuário ficam fora das seções gravadas e são mantidas.',
                 font=F.small, bg=bg, fg=T['faint'], wraplength=p(580), justify='left').pack(anchor='w')
        # progress (shown once an update starts) -----------------------------------
        self.prog = tk.Frame(body, bg=bg)
        self.phase = tk.Label(self.prog, text='', font=F.semi, bg=bg, fg=T['text'], anchor='w')
        self.phase.pack(fill='x')
        self.bar = tk.Canvas(self.prog, height=p(12), bg=bg, highlightthickness=0)
        self.bar.pack(fill='x', pady=(p(8), p(6)))
        self.bar.bind('<Configure>', lambda ev: self.draw_bar())
        self.stat = tk.Label(self.prog, text='', font=F.small, bg=bg, fg=T['muted'], anchor='w')
        self.stat.pack(fill='x')
        self.logbox = tk.Text(self.prog, height=5, font=F.mono, bg=T['card'], fg=T['soft'], bd=0, relief='flat',
                              highlightthickness=1, highlightbackground=T['line'], wrap='word', state='disabled',
                              padx=p(8), pady=p(6))
        self.logbox.pack(fill='x', pady=(p(8), 0))
        self.frac = 0.0
        self.status = tk.Label(body, text='', font=F.semi, bg=bg, fg=T['accent'], wraplength=p(580),
                               justify='left', anchor='w')
        self.status.pack(fill='x', pady=(p(12), 0))
        d.buttons([('Fechar', 'ghost', self.close), ('Atualizar pedal', 'primary', self.start)])
        self.btn_go = d.btns[0]
        self.load_file()
        pv = app.pedal_view
        if pv is not None and pv.connected:             # do not take the port away just to look
            self._pedal('app', 'Conectado na aba Pedal e presets (modo normal). Ao atualizar, o Studio solta a '
                               'porta e reinicia o pedal no modo de atualização.')
        else:
            self._pedal(None)
        d.show()
        self.d.top.after(80, self._pump)
        if self.pedal_state is None:
            self.d.top.after(200, self.probe)

    # ------------------------------------------------------------------ file / pedal checks
    def browse(self):
        cur = self.path.get().strip()
        f = filedialog.askopenfilename(parent=self.d.top, title='Firmware para gravar no pedal',
                                       initialdir=os.path.dirname(cur) if cur else self.W.C.ROOT,
                                       filetypes=[('Firmware QME-200', '*.bin'), ('Todos', '*.*')])
        if f:
            self.path.set(os.path.normpath(f))
            self.load_file()

    def load_file(self):
        if self.running:
            return
        T = self.T
        path = self.path.get().strip()
        self.img = None
        if not path:
            self.file_info.configure(text='Escolha o .bin (gerado aqui ou o original, para voltar).', fg=T['muted'])
        elif not os.path.exists(path):
            self.file_info.configure(text='Arquivo não encontrado.', fg=T['red'])
        else:
            try:
                self.img = U.Image.load(path)
                s = self.img.sections
                self.file_info.configure(fg=T['muted'], text='%s · %s  ·  app %s · efeitos %s · IRs %s · presets %s' % (
                    self.img.version, U.fmt_size(len(self.img.raw)), U.fmt_size(len(s[U.LOC_MCU])),
                    U.fmt_size(len(s[U.LOC_DSP])), U.fmt_size(len(s[U.LOC_IR])), U.fmt_size(len(s[U.LOC_BANK]))))
            except (OSError, U.FirmwareError) as ex:
                self.file_info.configure(text='Não dá para gravar: %s' % ex, fg=T['red'])
        self._sync()

    def _pedal(self, state, text=None):
        T = self.T
        self.pedal_state = state
        color, default = {
            None: (T['faint'], 'Ainda não verificado.'),
            'busy': (T['amber'], 'Procurando o pedal…'),
            'none': (T['red'], 'Pedal não encontrado. Ligue o cabo USB e feche o editor original.'),
            'app': (T['green'], 'Modo normal. Ao atualizar, o Studio reinicia o pedal no modo de atualização.'),
            'boot': (T['accent'], 'Modo de atualização (bootloader): pronto para gravar.'),
        }[state]
        self.pedal_lbl.configure(text=text or default)
        self.dot.delete('all')
        aa.oval(self.dot, self.p(10) / 2, self.p(10) / 2, self.p(10) / 2 - .5, fill=color)
        self._sync()

    def _sync(self):
        ok = bool(self.img) and self.agree.get() and not self.running and self.pedal_state in ('app', 'boot')
        if self.btn_go is not None:
            self.btn_go.config_btn(enabled=ok)
        self.btn_probe.config_btn(enabled=not self.running and self.pedal_state != 'busy')

    def _release_port(self):
        """the pedal view keeps the port open while connected: close it there first (worker thread)"""
        pv = self.app.pedal_view
        if pv is None or pv.worker.pedal is None:
            return
        from matribox.studio import pedalview
        pv.worker.submit('disconnect', pedalview.job_disconnect)
        t0 = time.time()
        while pv.worker.pedal is not None and time.time() - t0 < 5:
            time.sleep(0.05)

    def probe(self):
        if self.running or self.pedal_state == 'busy':
            return
        self._pedal('busy')
        want = self.app.session.get('pedal_port') or None

        def work():
            try:
                self._release_port()
                with htmidi.Port(want) as port:
                    boot = U.probe(port)
                    self.q.put(('pedal', ('boot' if boot else 'app', '%s: %s' % (
                        port.name, 'modo de atualização (bootloader), pronto para gravar.' if boot else
                        'modo normal. Ao atualizar, o Studio reinicia o pedal no modo de atualização.'))))
            except Exception as ex:
                self.q.put(('pedal', ('none', 'Pedal não encontrado: %s' % ex)))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ update
    def start(self):
        if not (self.img and self.agree.get()) or self.running:
            return
        W, p = self.W, self.p
        if self.pedal_state == 'app' and not W.ask(
                self.app, 'Reiniciar no modo de atualização',
                'O pedal está no modo normal. Ele vai reiniciar no modo de atualização e receber %s.\n\nNão desligue '
                'o pedal nem o cabo durante a gravação. Se algo falhar, o pedal continua no modo de atualização e '
                'a gravação pode ser repetida (ou ligue-o segurando a combinação de footswitches).'
                % os.path.basename(self.img.path), 'Atualizar', 'primary'):
            self.d.top.grab_set()
            return
        self.d.top.grab_set()
        self.running, self.cancelled, self.frac = True, False, 0.0
        self.t0 = time.time()
        self.prog.pack(fill='x', pady=(p(16), 0), before=self.status)
        self._log_clear()
        self.status.configure(text='')
        self.stat.configure(text='')
        self.btn_browse.config_btn(enabled=False)
        self.d.buttons([('Cancelar', 'danger', self.cancel)])
        self.btn_go = None
        self._sync()
        os.makedirs(LOG_DIR, exist_ok=True)
        self.trace_path = os.path.join(LOG_DIR, 'update-%s.log' % time.strftime('%Y%m%d-%H%M%S'))
        threading.Thread(target=self._work, args=(self.img,), daemon=True).start()
        self._refit()

    def _refit(self):
        """the dialog grew: natural size, moved up if its bottom would leave the screen's work area"""
        top = self.d.top
        top.update_idletasks()
        h = top.winfo_reqheight()
        x, y = top.winfo_rootx(), top.winfo_rooty()
        bottom = top.winfo_screenheight() - self.p(56)            # taskbar
        top.geometry('+%d+%d' % (x, max(0, min(y, bottom - h))))
        top.geometry('')

    def _work(self, img):
        want = self.app.session.get('pedal_port') or None
        trace = open(self.trace_path, 'w', encoding='utf-8')
        t0 = time.time()

        def log(text):
            trace.write('%9.3f %s\n' % (time.time() - t0, text))
            if not text.startswith('->'):
                self.q.put(('log', text))

        port = None
        try:
            log('firmware %s (%s)' % (img.path, img.version))
            self._release_port()
            port = htmidi.Port(want)
            if not U.probe(port):                       # the app: reboot it into the bootloader
                self.q.put(('phase', 'Reiniciando o pedal no modo de atualização…'))
                log('pedido de reinício no modo de atualização (0x0500000E)')
                U.request_jump(port)
                port.close()
                port = None
                port = U.wait_bootloader(want, log=log, cancel=lambda: self.cancelled)
                if port is None:
                    raise U.UpdateError('cancelado')
            log('pedal em modo de atualização: %s' % port.name)
            self.q.put(('phase', 'Consultando o pedal…'))
            last = [0.0]

            def progress(done, total):
                now = time.time()
                if now - last[0] > 0.1 or done == total:
                    last[0] = now
                    self.q.put(('progress', (done, total, U.LOC_NAMES.get(up.loc, ''))))
            up = U.Updater(img, port.send, progress, log)
            U.run(port, up, cancel=lambda: self.cancelled)
            log('concluído')
            self.q.put(('done', time.time() - t0))
        except Exception as ex:
            trace.write('%9.3f erro: %s\n' % (time.time() - t0, ex))       # the Updater already logged its own
            self.q.put(('fail', str(ex)))
        finally:
            if port:
                port.close()
            trace.close()

    def cancel(self):
        if self.running and self.W.ask(self.app, 'Cancelar a gravação',
                                       'O pedal fica no modo de atualização, sem firmware completo, até uma nova '
                                       'gravação terminar. Cancelar mesmo assim?', 'Cancelar gravação', 'danger'):
            self.cancelled = True
        if self.d.top.winfo_exists():
            self.d.top.grab_set()

    def _pump(self):
        try:
            while True:
                kind, v = self.q.get_nowait()
                self._on(kind, v)
        except queue.Empty:
            pass
        try:
            self.d.top.after(80, self._pump)
        except tk.TclError:
            pass

    def _on(self, kind, v):
        T = self.T
        if kind == 'pedal':
            self._pedal(*v)
        elif kind == 'log':
            self._log(v)
        elif kind == 'phase':
            self.phase.configure(text=v)
        elif kind == 'progress':
            done, total, sec = v
            self.frac = done / max(total, 1)
            el = time.time() - self.t0
            rate = done / max(el, 1e-3)
            self.phase.configure(text='Gravando: %s' % sec if done < total else 'Finalizando…')
            self.stat.configure(text='%.1f%%  ·  %d de %d pacotes  ·  %s restantes' % (
                100 * self.frac, done, total, fmt_time((total - done) / rate) if rate > 0 else '?'))
            self.draw_bar()
        elif kind in ('done', 'fail'):
            self.running = False
            self.btn_browse.config_btn(enabled=True)
            self.d.buttons([('Mostrar log', 'ghost', lambda: self.W.reveal(self.trace_path)),
                            ('Fechar', 'primary' if kind == 'done' else 'ghost', self.close)] +
                           ([] if kind == 'done' else [('Tentar de novo', 'primary', self.retry)]))
            self.btn_go = None
            if kind == 'done':
                self.frac = 1.0
                self.draw_bar()
                self.phase.configure(text='Firmware gravado em %s' % fmt_time(v))
                self.status.configure(text='O pedal reinicia com o firmware novo.', fg=T['green'])
                self._pedal(None, 'O pedal reinicia no modo normal com o firmware novo.')
                self.app.toast_msg('Pedal atualizado: %s' % os.path.basename(self.img.path), 'ok')
            else:
                self.status.configure(fg=T['red'], text='Falhou: %s. %s' % (v, 'O pedal pode estar no modo de '
                                      'atualização: dá para tentar de novo.' if v != 'cancelado' else ''))
                self._pedal(None, 'Estado incerto: provavelmente no modo de atualização (use Verificar).')
            self._sync()
            self._refit()

    def retry(self):
        """after a failure the pedal normally waits in the bootloader; _work probes again and reboots it if not"""
        self.d.buttons([('Fechar', 'ghost', self.close), ('Atualizar pedal', 'primary', self.start)])
        self.btn_go = self.d.btns[0]
        self.pedal_state = 'boot'
        self.start()

    def draw_bar(self):
        c, T = self.bar, self.T
        c.delete('all')
        w, h = c.winfo_width(), c.winfo_height()
        if w < 4:
            return
        self.W.rrect(c, 0, 0, w, h, h / 2, fill=T['raised'], outline=T['raised'])
        if self.frac > 0:
            x = max(h, w * self.frac)
            self.W.rrect(c, 0, 0, x, h, h / 2, fill=T['accent'], outline=T['accent'], bg=T['raised'])

    def _log(self, text):
        b = self.logbox
        b.configure(state='normal')
        b.insert('end', text + '\n')
        b.see('end')
        b.configure(state='disabled')

    def _log_clear(self):
        self.logbox.configure(state='normal')
        self.logbox.delete('1.0', 'end')
        self.logbox.configure(state='disabled')

    def close(self, result=None):
        if self.running:
            self.cancel()
            return
        self.W.Dialog.close(self.d, result)


def fmt_time(s):
    s = int(round(s))
    return '%d min %02d s' % divmod(s, 60) if s >= 60 else '%d s' % s
