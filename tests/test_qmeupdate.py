"""Check matribox/usb/qmeupdate.py against the PC editor's own update code (HTUSBTools.dll 1.2.0, called through ctypes).

1. message builders: nibbles (FUN_180087cb0), data packet (FUN_1800873e0) and section header (FUN_180087690)
   give the same bytes as qmeupdate for many sizes / packet sizes / indexes;
2. state machine: the DLL's handleMessage (FUN_180085d40) and update timer (FUN_180086900) run on a fake
   HTFirmware object whose output port records every SysEx. The same fake bootloader answers both the DLL and
   qmeupdate.Updater; the two transcripts must be identical, and the bootloader must have received every
   section byte for byte. Variants: the bootloader also ACKs the version check, NAKs some packets, a section
   is empty, the packet size does not divide the sections.
3. qmeupdate.flash() over a fake MIDI port with the stock firmware (the whole 6 MB image).

  test_qmeupdate.py [firmware.bin]          (Windows: loads editor/HTUSBTools.dll)
"""
import ctypes, os, random, struct, sys, time
from matribox.usb import qmeupdate as U

from matribox import paths
DLL = os.environ.get('MATRIBOX_HTUSBTOOLS') or paths.setting('htusbtools', 'HTUSBTools.dll')   # the PC editor's DLL
RVA_ENCODE, RVA_DATA, RVA_HEADER = 0x87cb0, 0x873e0, 0x87690
RVA_HANDLE, RVA_TIMER, RVA_VERSION = 0x85d40, 0x86900, 0x853c0


class Arr(ctypes.Structure):                    # juce::Array<uint8>
    _fields_ = [('data', ctypes.c_void_p), ('alloc', ctypes.c_int), ('used', ctypes.c_int)]


class Midi(ctypes.Structure):                   # juce::MidiMessage: bytes inline up to 8, else a pointer
    _fields_ = [('packed', ctypes.c_uint64), ('ts', ctypes.c_double), ('size', ctypes.c_int), ('pad', ctypes.c_int)]


def midi_bytes(addr):
    m = Midi.from_address(addr)
    src = addr if m.size <= 8 else m.packed
    return ctypes.string_at(src, m.size)


def make_midi(data):
    m = Midi()
    m.size = len(data)
    keep = ctypes.create_string_buffer(bytes(data), max(len(data), 8))
    if len(data) <= 8:
        ctypes.memmove(ctypes.addressof(m), keep, len(data))
    else:
        m.packed = ctypes.addressof(keep)
    return m, keep


class Dll:
    def __init__(self):
        self.lib = ctypes.WinDLL(DLL)
        b = self.lib._handle
        P, I, V = ctypes.c_void_p, ctypes.c_int, None
        self.encode_f = ctypes.CFUNCTYPE(P, ctypes.POINTER(Arr), ctypes.c_char_p, I)(b + RVA_ENCODE)
        self.data_f = ctypes.CFUNCTYPE(P, P, ctypes.POINTER(Arr), I, ctypes.c_uint)(b + RVA_DATA)
        self.header_f = ctypes.CFUNCTYPE(P, P, ctypes.c_uint, ctypes.c_uint)(b + RVA_HEADER)
        self.handle_f = ctypes.CFUNCTYPE(V, P, P, P, P)(b + RVA_HANDLE)
        self.timer_f = ctypes.CFUNCTYPE(V, P, I)(b + RVA_TIMER)
        self.version_f = ctypes.CFUNCTYPE(V, P)(b + RVA_VERSION)

    def encode(self, data):
        a = Arr()
        self.encode_f(ctypes.byref(a), data, len(data))
        return a

    def data_msg(self, arr, pkt, idx):
        out = ctypes.create_string_buffer(0x20)
        self.data_f(ctypes.addressof(out), ctypes.byref(arr), pkt, idx)
        return midi_bytes(ctypes.addressof(out))

    def header_msg(self, size, count):
        out = ctypes.create_string_buffer(0x20)
        self.header_f(ctypes.addressof(out), size, count)
        return midi_bytes(ctypes.addressof(out))


# ------------------------------------------------------------------------------------------------ fake bootloader
class FakeBoot:
    """what the bootloader must do for the editor's sequence to make sense; collects the sections"""
    def __init__(self, pkt, ack_version=False, nak=()):
        self.pkt, self.ack_version, self.nak = pkt, ack_version, set(nak)
        self.sections, self.loc, self.buf, self.size = {}, None, None, 0
        self.expect, self.packets, self.jumped, self.nak_sent = 0, 0, False, 0
        self.mcuid = bytes(range(0x10, 0x20))
        self.armed = False

    def query_reply(self):
        m = bytearray(0x27)
        m[0:4] = b'\xf0\x7e\x48\x01'
        m[4:4 + 7] = b'QME-200'
        m[0x14:0x14 + 6] = b'V1.0.0'                  # what the user's pedal answers
        m[0x24], m[0x25] = self.pkt >> 4, self.pkt & 15
        m[0x26] = 0xF7
        return bytes(m)

    def handle(self, m):
        assert m[:3] == b'\xf0\x7e\x48' and m[-1] == 0xF7, m.hex(' ')
        cmd = m[3]
        if cmd == 0x01:
            return [self.query_reply()]
        if cmd == 0x0B:
            self.armed = True
            return [U.MSG_ACK] if self.ack_version else []
        if cmd == 0x7F:
            self.loc = m[4]
            return [U.MSG_ACK]
        if cmd == 0x02:
            if not self.armed:              # bootloader V1.0.0: no version check yet -> NAK (hardware)
                return [U.MSG_NAK]
            nib = m[4:-1]
            v = [int(''.join('%x' % x for x in nib[i:i + 8]), 16) for i in (0, 8)]
            self.size, count = v
            assert count == -(-self.size // self.pkt), (self.size, count)
            self.buf, self.expect = bytearray(), 0
            return [U.MSG_ACK]
        if cmd == 0x04:
            idx, body = m[4], m[5:-1]
            chunk, cs = body[:-2], body[-2] << 4 | body[-1]
            assert len(chunk) == 2 * self.pkt and sum(chunk) & 0xFF == cs
            key = (self.loc, self.expect)
            if key in self.nak:
                self.nak.discard(key)
                self.nak_sent += 1
                return [U.MSG_NAK]
            assert idx == self.expect & 0x7F, (idx, self.expect)
            self.buf += bytes(chunk[i] << 4 | chunk[i + 1] for i in range(0, len(chunk), 2))
            self.expect += 1
            self.packets += 1
            return [U.MSG_ACK]
        if cmd == 0x05:
            self.sections[self.loc] = bytes(self.buf[:self.size])
            return [U.MSG_ACK]
        if cmd == 0x07:
            return [bytes([0xF0, 0x7E, 0x48, 0x07]) + U.nibbles(self.mcuid) + b'\xf7']
        if cmd == 0x09:
            self.jumped = True
            return []
        raise AssertionError('unexpected %s' % m.hex(' '))


# ------------------------------------------------------------------------------------------------ the DLL's HTFirmware
class DllMachine:
    """a zeroed HTFirmware with the fields the update path reads, its MIDI output recorded"""
    def __init__(self, dll, img):
        self.dll, self.sent = dll, []
        self.obj = ctypes.create_string_buffer(0x400)
        self.dev = ctypes.create_string_buffer(0x220)
        self.out = ctypes.create_string_buffer(0x10)
        self.vtbl = (ctypes.c_void_p * 8)()
        self.cb = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(
            lambda this, msg: self.sent.append(midi_bytes(msg)))
        self.vtbl[3] = ctypes.cast(self.cb, ctypes.c_void_p).value            # MidiOutput vtable +0x18
        struct.pack_into('<Q', self.out, 0, ctypes.addressof(self.vtbl))
        struct.pack_into('<Q', self.dev, 0x210, ctypes.addressof(self.out))
        hdr = bytearray(img.raw[:0x30]) + bytes(0x30)                          # FIRMWARE_HEADER
        for k, loc in enumerate((U.LOC_MCU, U.LOC_IR, U.LOC_BANK, U.LOC_DSP)):
            struct.pack_into('<II', hdr, 0x30 + 8 * k, 0, len(img.sections[loc]))
        ctypes.memmove(ctypes.addressof(self.obj) + 0x130, bytes(hdr), 0x60)
        self.arrs = []
        for off, loc in zip((0xc8, 0xd8, 0xe8, 0xf8), (U.LOC_MCU, U.LOC_IR, U.LOC_BANK, U.LOC_DSP)):
            a = dll.encode(img.sections[loc])
            self.arrs.append(a)
            ctypes.memmove(ctypes.addressof(self.obj) + off, ctypes.addressof(a), ctypes.sizeof(a))

    def u32(self, off):
        return struct.unpack_from('<I', self.obj, off)[0]

    def start(self):
        """the tail of FUN_180086b80 with the output already set (setOutPut at connection): loc 0, version
        check, state 0"""
        struct.pack_into('<Q', self.obj, 0x190, ctypes.addressof(self.dev))
        struct.pack_into('<I', self.obj, 0x80, 0)
        self.dll.version_f(ctypes.addressof(self.obj))
        struct.pack_into('<I', self.obj, 0x84, 0)

    def tick(self):
        self.dll.timer_f(ctypes.addressof(self.obj), 2)

    def on_message(self, m):
        mm, keep = make_midi(m)
        self.dll.handle_f(ctypes.addressof(self.obj), ctypes.addressof(mm), None, None)

    @property
    def finished(self):
        return self.u32(0x84) == U.S_JUMPED

    @property
    def failed(self):
        return self.u32(0x84) == U.S_FAILED


class PyMachine:
    def __init__(self, img):
        self.sent = []
        self.up = U.Updater(img, self.sent.append, sleep=lambda s: None)
        self.start, self.tick, self.on_message = self.up.start, self.up.tick, self.up.on_message

    finished = property(lambda self: self.up.finished)
    failed = property(lambda self: self.up.failed)


def drive(machine, boot, ticks_before=1):
    """message loop: each reply is handled after the call that caused it returns, in order"""
    machine.start()
    pending, seen = [], 0
    for _ in range(ticks_before):
        machine.tick()
    for _ in range(10 ** 7):
        while seen < len(machine.sent):
            pending += boot.handle(machine.sent[seen])
            seen += 1
        if machine.finished or machine.failed:
            break
        if not pending:
            machine.tick()
            continue
        machine.on_message(pending.pop(0))
    return machine


# ------------------------------------------------------------------------------------------------ images
def synth(sizes, seed):
    """a valid FRMW with random sections (MCU, IR, Bank, DSP sizes) and the stock header fields"""
    rnd = random.Random(seed)
    sec = {loc: bytes(rnd.randrange(256) for _ in range(n)) for loc, n in zip(U.ORDER, sizes)}
    hdr = bytearray(b'FRMW' + bytes(4) + bytes([3, 0, 2, 1, 0, 0, 0, 0]) + b'QME-200'.ljust(16, b'\0')
                    + bytes([1, 2]).ljust(16, b'\0'))
    body, table = bytearray(), bytearray()
    for loc in U.FILE_ORDER:
        table += struct.pack('<II', 0x30 + len(body), len(sec[loc]))
        body += sec[loc]
    data = hdr + table + bytes(0x30 - len(table)) + body
    struct.pack_into('<I', data, 4, len(data) - 0x30)
    data[0x0e] = sum(data[0x30:]) & 0xFF
    return U.Image(data, 'synth%d.bin' % seed)


def check_builders(dll):
    rnd = random.Random(1)
    n = 0
    for _ in range(300):
        data = bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 900)))
        arr = dll.encode(data)
        enc = ctypes.string_at(arr.data, arr.used)
        assert enc == U.nibbles(data)
        pkt = rnd.choice((1, 3, 0x10, 0x40, 0x7f, 0x80, 0xb8, 0x100, 0x7ff))
        count = -(-len(data) // pkt)
        for idx in {0, count - 1, rnd.randrange(count), min(count - 1, 0x81)}:
            assert dll.data_msg(arr, pkt, idx) == U.msg_data(enc, pkt, idx), (len(data), pkt, idx)
            n += 1
    for size, count in ((0, 0), (0x37ff80, 0x3800), (0x12345678, 0x9abcdef0), (0xffffffff, 1)):
        assert dll.header_msg(size, count) == U.msg_header(size, count)
    print('builders: %d data packets, nibbles and headers identical to the DLL' % n)


def check_machine(dll, img, pkt, label, fails=False, **kw):
    runs = []
    for make in (lambda: DllMachine(dll, img), lambda: PyMachine(img)):
        boot = FakeBoot(pkt, **kw)
        m = drive(make(), boot)
        runs.append((m, boot))
    (dm, db), (pm, pb) = runs
    if fails:
        assert dm.failed and pm.failed and dm.sent == pm.sent, (dm.u32(0x84), pm.up.state)
        print('state machine (%s): both fail after the same %d messages (%s)' % (label, len(pm.sent), pm.up.error))
        return
    assert dm.finished and pm.finished, (dm.u32(0x84), pm.up.state, pm.up.error)
    if dm.sent != pm.sent:
        k = next(i for i, (a, b) in enumerate(zip(dm.sent, pm.sent)) if a != b) if len(dm.sent) == len(pm.sent) \
            else min(len(dm.sent), len(pm.sent))
        raise AssertionError('%s: transcripts differ at message %d (dll %d msgs, py %d)\n dll %s\n py  %s' % (
            label, k, len(dm.sent), len(pm.sent), dm.sent[k][:24].hex(' ') if k < len(dm.sent) else '-',
            pm.sent[k][:24].hex(' ') if k < len(pm.sent) else '-'))
    for loc in U.ORDER:                 # an empty section shifts the rest (the editor sends no location then)
        want = img.sections[loc]
        if all(img.sections.values()):
            assert pb.sections[loc] == want and db.sections[loc] == want, (label, loc)
    assert db.jumped and pb.jumped and pm.up.mcuid == pb.mcuid
    print('state machine (%s): %d messages, same as the DLL; %d packets, %d NAKs, sections intact' % (
        label, len(pm.sent), pb.packets, pb.nak_sent))


class FakePort:
    """htmidi.Port lookalike in front of a FakeBoot: replies come back on the next receive()"""
    def __init__(self, boot):
        self.boot, self.rx, self.sent = boot, [], 0

    def send(self, m):
        self.sent += 1
        self.rx += self.boot.handle(bytes(m))

    def receive(self):
        out, self.rx = self.rx, []
        return out


def check_flash(path):
    img = U.Image.load(path)
    boot = FakeBoot(0xB4)                       # the packet size of the user's pedal
    t0 = time.time()
    seen = []
    U.VERSION_SLEEP = 0.01
    up = U.flash(FakePort(boot), img, progress=lambda d, t: seen.append((d, t)))
    for loc in U.ORDER:
        assert boot.sections[loc] == img.sections[loc], loc
    assert boot.jumped and seen[-1][0] == seen[-1][1] == up.total
    print('flash(%s): %d packets of 0xB4 bytes over a fake port in %.1f s, all four sections intact' % (
        os.path.basename(path), up.total, time.time() - t0))


def main():
    fw = sys.argv[1] if len(sys.argv) > 1 else paths.original_firmware()
    dll = Dll()
    check_builders(dll)
    a = synth((5000, 777, 1024, 3001), 1)
    check_machine(dll, a, 0x40, 'packet 0x40')
    check_machine(dll, a, 0x7f, 'packet 0x7f')
    # the editor cannot work with a bootloader that answers the version check: the real one stays silent
    check_machine(dll, a, 0x7f, 'version check ACKed', fails=True, ack_version=True)
    check_machine(dll, a, 0x10, 'packet 0x10, NAKs', nak={(0, 0), (0, 130), (1, 5), (0x10, 187)})
    check_machine(dll, synth((4000, 0, 900, 2500), 2), 0x80, 'empty IR section')
    check_machine(dll, synth((300, 200, 100, 0), 3), 0x20, 'empty DSP section')
    check_flash(fw)
    print('OK')


if __name__ == '__main__':
    main()
