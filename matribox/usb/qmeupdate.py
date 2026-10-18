"""Firmware update of the Matribox II Pro (QME-200) over USB-MIDI, the way the PC editor 1.2.0 does it.

Ported from HTUSBTools.dll (the Windows build that updates the pedal; the macOS dylib has the same protocol with
symbols): handleMessage FUN_180085d40, sendData FUN_180084850, sendHeader FUN_180085a10, sendLocation
FUN_180085670, sendVersionCheck FUN_1800853c0, sendJump FUN_180085740, timer FUN_180086900, start
FUN_180086b80, data packet FUN_1800873e0, header FUN_180087690, nibbles FUN_180087cb0.

Getting into update mode (the bootloader; it is never rewritten by an update):
  - from the app: editor message 0x0500000E (flag 0x12, no payload). The app's handler (FUN_00030020) writes
    0x5A5A5A5A to 0x400A4000 and resets through AIRCR; the pedal comes back as the bootloader;
  - or hold the footswitch combination while powering on.
  The editor tells the two apart with request 0x12000000 (flag 0x11) every 200 ms: only the bootloader answers
  (the app drops group 2). The USB-MIDI port keeps its name ("Matribox II PRO") in both modes.

Update protocol, SysEx F0 7E 48 ... F7 (the pedal acknowledges with 03 7F, 03 7E is a NAK):
  01                          device query; reply >= 39 bytes: name @4 (16), version @0x14/0x15, packet
                              size P = msg[0x24] << 4 | msg[0x25] (bytes of the image per data packet)
  0B 01 'V'|'B' d d d 00      version check ('V' when nReleaseType == 3; digits hdr[0x0b], hdr[0x0a],
                              hdr[0x21]: "V122"); sent at the start (required: without it the header gets
                              a NAK), after each section header ACK and after each section end, then the
                              editor sleeps 200 ms; the bootloader never answers it
  7F loc                      location: MCU 0, IR 1, Bank 2, DSP 0x10 (in that order)
  02 <8 nibbles size> <8 nibbles packets>      section header (size in bytes, packets = ceil(size / P))
  04 idx&7F <2P nibbles, padded with 0F> <sum of those nibbles & FF as 2 nibbles>        data packet
  05 7B                       end of section
  07                          MCU id request; reply 07 <nibbles>
  09                          jump: the bootloader starts the new app

The editor reacts to each reply (state machine below, kept as in the DLL so the pedal sees the same sequence)
and has a 500 ms timer: the query goes out every 10 ticks, 100 ticks without a reply abort.
Every update rewrites all four sections, the Bank (presets) included. Clone/NAM slots and user IRs live
elsewhere in the flash (0x40A000-0x440000) and survive.

  qmeupdate.py check FW.bin                  validate a firmware file as the editor does
  qmeupdate.py ports                         list MIDI ports
  qmeupdate.py probe [--port P]              is the pedal in update mode?
  qmeupdate.py jump  [--port P]              reboot the pedal (app) into update mode
  qmeupdate.py flash FW.bin [--port P] [--jump] [--trace FILE] [--yes]
"""
import math, os, struct, sys, time

from matribox.usb import htmidi

ADDR_SOFTWARE_STATE = 0x12000000            # request; only the bootloader answers
CMD_SYS_JUMP = 0x0500000E                   # app: reboot into the bootloader

LOC_MCU, LOC_IR, LOC_BANK, LOC_DSP, LOC_DONE = 0, 1, 2, 0x10, 0x83
ORDER = (LOC_MCU, LOC_IR, LOC_BANK, LOC_DSP)
NEXT = {LOC_MCU: LOC_IR, LOC_IR: LOC_BANK, LOC_BANK: LOC_DSP, LOC_DSP: LOC_DONE}
LOC_NAMES = {LOC_MCU: 'MCU (app)', LOC_IR: 'IR (cabinets)', LOC_BANK: 'Bank (presets)', LOC_DSP: 'DSP (efeitos)'}
FILE_ORDER = (LOC_MCU, LOC_DSP, LOC_IR, LOC_BANK)  # (offset, size) pairs at 0x30 of the .bin
DEVICE = b'QME-200'

TICK = 0.5                                  # the editor drives the update timer every 500 ms (Dart side)
QUERY_EVERY, TICKS_MAX = 10, 100
VERSION_SLEEP = 0.2
MAX_NAKS = 20                               # the editor resends a NAKed packet forever; we give up instead

# states (HTFirmware +0x84)
S_QUERY, S_LOCATION, S_HEADER, S_DATA, S_SECTION_END, S_MCUID, S_JUMPED, S_FAILED = 0, 1, 2, 3, 4, 5, 7, 9


def sysex(*body):
    return bytes([0xF0, 0x7E, 0x48, *body, 0xF7])


MSG_QUERY = sysex(0x01)
MSG_EOF = sysex(0x05, 0x7B)
MSG_MCUID = sysex(0x07)
MSG_JUMP = sysex(0x09)
MSG_ACK = sysex(0x03, 0x7F)
MSG_NAK = sysex(0x03, 0x7E)


# ------------------------------------------------------------------------------------------------ image
class FirmwareError(ValueError):
    pass


class Image:
    """a QME-200 .bin as the editor reads it (isDeviceFirmware + isValidFirmwareFile + the section table)"""

    def __init__(self, data, path=''):
        data = bytes(data)
        self.path = path
        if len(data) < 0x60 or data[:4] != b'FRMW':
            raise FirmwareError('não é um firmware (falta o cabeçalho FRMW)')
        if data[16:23] != DEVICE:
            raise FirmwareError('o firmware é de outro aparelho (%r, esperado QME-200)'
                                % data[16:32].split(b'\0')[0].decode('latin-1'))
        n = struct.unpack_from('<I', data, 4)[0]
        if n > len(data) - 0x30:
            raise FirmwareError('cabeçalho diz %d bytes de dados, o arquivo tem %d' % (n, len(data) - 0x30))
        if sum(data[0x30:0x30 + n]) & 0xFF != data[0x0E]:
            raise FirmwareError('checksum não confere (o editor recusaria o arquivo)')
        self.release = struct.unpack_from('<H', data, 8)[0]
        self.sections = {}
        for k, loc in enumerate(FILE_ORDER):
            off, size = struct.unpack_from('<II', data, 0x30 + 8 * k)
            if off + 0x30 + size > len(data):
                raise FirmwareError('seção %s fora do arquivo' % LOC_NAMES[loc])
            self.sections[loc] = data[0x30 + off:0x30 + off + size]
        self.version = '%s%d%d%d' % ('V' if self.release == 3 else 'B', data[0x0B], data[0x0A], data[0x21])
        self.raw = data

    @classmethod
    def load(cls, path):
        with open(path, 'rb') as f:
            return cls(f.read(), path)

    def describe(self):
        out = ['%s  %s, %s' % (os.path.basename(self.path) or 'firmware', self.version, fmt_size(len(self.raw)))]
        for loc in ORDER:
            out.append('  %-15s %s' % (LOC_NAMES[loc], fmt_size(len(self.sections[loc]))))
        return '\n'.join(out)


def fmt_size(n):
    return '%.1f MB' % (n / 1048576) if n >= 1048576 else '%.0f KB' % (n / 1024) if n >= 1024 else '%d B' % n


# ------------------------------------------------------------------------------------------------ messages
_HEX_NIBBLE = bytes.maketrans(b'0123456789abcdef', bytes(range(16)))


def nibbles(data):
    """each byte as two bytes: high nibble, low nibble (encodeToSysEx)"""
    return bytes(data).hex().encode().translate(_HEX_NIBBLE)


def nib32(v):
    return bytes((v >> s) & 15 for s in range(28, -4, -4))


def msg_version(img):
    return sysex(0x0B, 0x01, *img.version.encode(), 0x00)


def msg_location(loc):
    return sysex(0x7F, loc)


def msg_header(size, count):
    return sysex(0x02, *nib32(size), *nib32(count))


def msg_data(enc, pkt, idx):
    """packet idx of a section: 2*pkt nibbles of the image (0x0F past the end) and their 8-bit sum"""
    n = 2 * pkt
    chunk = enc[idx * n:(idx + 1) * n]
    chunk += bytes([0x0F]) * (n - len(chunk))
    cs = sum(chunk) & 0xFF
    return sysex(0x04, idx & 0x7F, *chunk, cs >> 4, cs & 15)


def is_ack(m):
    return len(m) > 4 and m[0] == 0xF0 and m[3] == 0x03 and m[4] == 0x7F


# ------------------------------------------------------------------------------------------------ state machine
class UpdateError(RuntimeError):
    pass


class Updater:
    """HTFirmware of the editor. send(bytes) writes one SysEx; feed every SysEx the pedal sends to on_message()
    and call tick() every TICK seconds. progress(done, total) counts data packets; log(text) gets the events."""

    def __init__(self, img, send, progress=None, log=None, sleep=time.sleep):
        self.img = img
        self._send = send
        self.progress = progress or (lambda done, total: None)
        self.log = log or (lambda text: None)
        self.sleep = sleep
        self.enc = {loc: nibbles(img.sections[loc]) for loc in ORDER}
        self.count = dict.fromkeys(ORDER, 0)
        self.state, self.loc, self.idx, self.pkt = S_QUERY, LOC_MCU, 0, 0
        self.idle = self.qtick = self.done = self.naks = 0
        self.error = None
        self.device = self.mcuid = None

    # -------------------------------------------------------------- driven from outside
    @property
    def finished(self):
        return self.state == S_JUMPED

    @property
    def failed(self):
        return self.state == S_FAILED

    @property
    def total(self):
        return sum(self.count.values())

    def start(self):
        """FUN_180086b80: location MCU, version check, then the timer sends the query. The output is already set
        (setOutPut at connection), so the version check goes out: bootloader V1.0.0 NAKs the section header
        without it (hardware, 2026-10-17)."""
        self.loc = LOC_MCU
        self._version()
        self.state = S_QUERY

    def tick(self):
        """FUN_180086900"""
        if self.state in (S_FAILED, S_JUMPED):
            return
        if self.state == S_QUERY:
            if self.qtick > TICKS_MAX - 1:
                return self._fail('o pedal não respondeu à consulta (está no modo de atualização?)')
            if self.qtick % QUERY_EVERY == 0:
                self.send(MSG_QUERY, 'query')
            self.qtick += 1
        elif self.idle < TICKS_MAX:
            self.idle += 1
        else:
            self._fail('o pedal parou de responder (%s, pacote %d)' % (LOC_NAMES.get(self.loc, self.loc), self.idx))

    def on_message(self, m):
        """FUN_180085d40"""
        if len(m) < 3 or m[0] != 0xF0 or m[2] != 0x48:
            return
        self.idle = 0
        st = self.state
        if st != S_DATA or not is_ack(m):
            self.log('<- %s' % m[:40].hex(' '))
        if st == S_QUERY:
            accepted = len(m) - 2 >= 0x25 or len(m) > 4 and m[4] == 1      # the editor's test
            if not accepted or len(m) < 0x26:                                 # shorter: no packet size
                self.log('resposta curta à consulta ignorada: %s' % m.hex(' '))
                return
            text = lambda b: b.split(b'\0')[0].decode('latin-1', 'replace')
            self.device = dict(name=text(m[4:0x14]), version=text(m[0x14:0x24]), raw=m)    # "QME-200", "V1.0.0"
            self.pkt = m[0x24] << 4 | m[0x25]
            if not self.pkt:
                return self._fail('o pedal informou pacote de tamanho 0')
            for loc in ORDER:
                self.count[loc] = math.ceil(len(self.enc[loc]) / (2 * self.pkt))
            self.log('pedal: %r, bootloader %r, %d bytes por pacote, %d pacotes'
                     % (self.device['name'], self.device['version'], self.pkt, self.total))
            self.loc = LOC_MCU
            self._location()
        elif st == S_LOCATION:
            if len(m) > 5 and m[5] == 0x7E:
                return self._fail('o pedal recusou a seção %s' % LOC_NAMES.get(self.loc, self.loc))
            self._header()
        elif st == S_HEADER:
            if not is_ack(m):
                return self._fail('o pedal recusou o cabeçalho da seção %s' % LOC_NAMES.get(self.loc, self.loc))
            self._version()
            self._data()
        elif st == S_DATA:
            if is_ack(m):
                self.idx += 1
                self.naks = 0
                self.done = self.idx + sum(self.count[l] for l in ORDER[:ORDER.index(self.loc)])
                self.progress(self.done, self.total)
            else:
                self.idle = 1
                self.naks += 1
                self.log('NAK no pacote %d de %s: reenviando' % (self.idx, LOC_NAMES[self.loc]))
                if self.naks > MAX_NAKS:
                    return self._fail('%d NAKs seguidos no pacote %d de %s'
                                      % (self.naks, self.idx, LOC_NAMES[self.loc]))
            self._data()
        elif st == S_SECTION_END:
            if not is_ack(m):
                return self._fail('o pedal recusou o fim da seção')
            if self.loc != LOC_DONE:
                self._location()
            else:
                self.state = S_MCUID
                self.send(MSG_MCUID, 'mcuid')
        elif st == S_MCUID:
            if len(m) > 3 and m[3] == 0x07:
                body = m[4:-1]
                self.mcuid = bytes(body[i] << 4 | body[i + 1] for i in range(0, len(body) - 1, 2))
                self.log('MCU id %s' % self.mcuid.hex())
                self._jump()
            else:
                self._fail('resposta inesperada ao pedido de MCU id: %s' % m.hex(' '))

    # -------------------------------------------------------------- the editor's send* functions
    def send(self, msg, what):
        self.log('-> %s %s' % (what, msg.hex(' ') if len(msg) <= 24 else '(%d bytes)' % len(msg)))
        self._send(msg)

    def _version(self):
        self.send(msg_version(self.img), 'version')
        self.sleep(VERSION_SLEEP)

    def _location(self):
        self.state = S_LOCATION
        self.send(msg_location(self.loc), 'location %s' % LOC_NAMES.get(self.loc, self.loc))

    def _header(self):
        """FUN_180085a10: header of the current section; an empty one is skipped (no location for the next)"""
        self.state, self.idx = S_HEADER, 0
        for loc in ORDER:
            if self.loc != loc:
                continue
            if self.count[loc]:
                return self.send(msg_header(len(self.img.sections[loc]), self.count[loc]), 'header')
            self.loc = NEXT[loc]
        self.loc, self.state = LOC_DONE, S_SECTION_END
        self.send(MSG_EOF, 'end')

    def _data(self):
        """FUN_180084850: next packet, or end of section + version check"""
        self.state = S_DATA
        loc = self.loc
        if self.idx == self.count[loc]:
            self.idx, self.state, self.loc = 0, S_SECTION_END, NEXT[loc]
            self.send(MSG_EOF, 'end %s' % LOC_NAMES[loc])
            self._version()
            return
        m = msg_data(self.enc[loc], self.pkt, self.idx)
        self._send(m)

    def _jump(self):
        self.state = S_JUMPED
        self.send(MSG_JUMP, 'jump')
        self.progress(self.total, self.total)

    def _fail(self, why):
        self.state, self.error = S_FAILED, why
        self.log('falhou: ' + why)


# ------------------------------------------------------------------------------------------------ pedal side
def probe(port, tries=3, gap=0.25, log=None):
    """True when the pedal is in update mode. The bootloader (V1.0.0 on the user's pedal) does not answer the
    editor's request 0x12000000 in our packet format, but it answers the device query at once; the app drops
    the query (FUN_00031a08 returns unless bytes 3-4 are "MP"). Both are sent, any answer counts."""
    rx = htmidi.Reassembler()
    port.receive()
    for _ in range(tries):
        for p in htmidi.packets(htmidi.message(ADDR_SOFTWARE_STATE), htmidi.FLAG_REQ):
            port.send(p)
        port.send(MSG_QUERY)
        t0 = time.time()
        while time.time() - t0 < gap:
            for m in port.receive():
                if m[:3] == b'\xf0\x7e\x48':
                    if log:
                        log('<- %s' % m.hex(' '))
                    return True
                r = rx.feed(m)
                if r and r[1] == ADDR_SOFTWARE_STATE:
                    return True
                if log and not r:
                    log('<- %s' % m[:24].hex(' '))
            time.sleep(0.005)
    return False


def request_jump(port):
    """app -> bootloader (the editor's "update firmware" from the settings page); the pedal reboots"""
    for p in htmidi.packets(htmidi.message(CMD_SYS_JUMP), htmidi.FLAG_DATA):
        port.send(p)


def wait_bootloader(want=None, timeout=25.0, log=None, cancel=None):
    """after a jump: reopen the port when the pedal re-enumerates and probe until the bootloader answers"""
    t0 = time.time()
    time.sleep(1.0)
    while time.time() - t0 < timeout:
        if cancel and cancel():
            return None
        try:
            port = htmidi.Port(want)
        except RuntimeError as ex:
            if log:
                log('porta ainda indisponível: %s' % ex)
            time.sleep(0.5)
            continue
        try:
            if probe(port, log=log):
                return port
            if log:
                log('%s abriu, mas sem resposta de bootloader' % port.name)
        except Exception as ex:
            if log:
                log('erro na sondagem: %s' % ex)
        port.close()
        time.sleep(0.5)
    raise UpdateError('o pedal não voltou em modo de atualização em %.0f s' % timeout)


def flash(port, img, progress=None, log=None, cancel=None):
    """runs a whole update on an open port (pedal already in update mode); returns the Updater"""
    return run(port, Updater(img, port.send, progress, log), cancel)


def run(port, up, cancel=None, clock=time.time):
    """drives an Updater: the pedal's messages as they come, the timer every TICK s"""
    up.start()
    next_tick = clock() + TICK
    while not (up.finished or up.failed):
        if cancel and cancel():
            up._fail('cancelado')
            break
        msgs = port.receive()
        for m in msgs:
            up.on_message(m)
            if up.finished or up.failed:
                break
        now = clock()
        if now >= next_tick:
            up.tick()
            next_tick = max(next_tick + TICK, now)
        if not msgs:
            time.sleep(0.0005)
    if up.failed:
        raise UpdateError(up.error)
    return up


# ------------------------------------------------------------------------------------------------ CLI
def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('check'); p.add_argument('fw')
    sub.add_parser('ports')
    p = sub.add_parser('probe'); p.add_argument('--port')
    p = sub.add_parser('jump'); p.add_argument('--port')
    p = sub.add_parser('flash'); p.add_argument('fw'); p.add_argument('--port')
    p.add_argument('--jump', action='store_true', help='reboot the pedal into update mode first')
    p.add_argument('--trace', help='write every message (data packets summarised) to this file')
    p.add_argument('--yes', action='store_true', help='do not ask for confirmation')
    a = ap.parse_args()

    if a.cmd == 'check':
        try:
            print(Image.load(a.fw).describe())
        except FirmwareError as ex:
            raise SystemExit('%s: %s' % (a.fw, ex))
        return
    if a.cmd == 'ports':
        ins, outs = htmidi.list_ports()
        print('in: ', ins)
        print('out:', outs)
        return
    if a.cmd in ('probe', 'jump'):
        with htmidi.Port(a.port) as port:
            boot = probe(port, log=print)
            print('%s: %s' % (port.name, 'modo de atualização (bootloader)' if boot else 'app (modo normal)'))
            if a.cmd == 'jump' and not boot:
                request_jump(port)
                print('pedido de reinício no modo de atualização enviado')
        return

    img = Image.load(a.fw)
    print(img.describe())
    print('A atualização regrava as quatro seções, inclusive o Bank: os presets do pedal serão substituídos.')
    if not a.yes and input('Continuar? [s/N] ').strip().lower() not in ('s', 'sim', 'y', 'yes'):
        return
    trace = open(a.trace, 'w', encoding='utf-8') if a.trace else None
    t0 = time.time()

    def log(text):
        if trace:
            trace.write('%9.3f %s\n' % (time.time() - t0, text))
        if not text.startswith('->'):
            print('\n' + text)

    port = htmidi.Port(a.port)
    try:
        if not probe(port):
            if not a.jump:
                raise SystemExit('o pedal está no modo normal: use --jump ou ligue-o segurando a combinação de '
                                 'footswitches')
            request_jump(port)
            port.close()
            port = None
            print('reiniciando o pedal no modo de atualização…')
            port = wait_bootloader(a.port, log=log)
        last = [0]

        def progress(done, total):
            now = time.time()
            if now - last[0] > 0.25 or done == total:
                last[0] = now
                rate = done / max(now - t0, 1e-3)
                print('\r%5.1f%%  %d/%d pacotes  %4.0f s restantes' % (
                    100 * done / max(total, 1), done, total, (total - done) / max(rate, 1e-3)), end='', flush=True)
        flash(port, img, progress, log)
        print('\nconcluído em %.0f s: o pedal reinicia no firmware novo' % (time.time() - t0))
    finally:
        if port:
            port.close()
        if trace:
            trace.close()


if __name__ == '__main__':
    main()
