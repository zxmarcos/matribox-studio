"""Matribox II Pro (QME-200) editor protocol over USB-MIDI SysEx, as used by the PC editor (HTUSBTools)
and parsed by the app (V1.2.2: FUN_00031a08 packets, FUN_0003a084 messages).

Message:  u32 cmd | u32 n | n bytes of LZO1X data (the payload, compressed)
          a request (flag 0x11) has n = 0 and no data
Packet:   F0 21 25 4D 50 00 00 cs flag len_lo len_hi off_lo off_hi <2 nibbles per byte> F7
          len = message length, off = byte offset of this chunk (7-bit halves), at most 0xB8 bytes per
          packet, cs = (sum of the nibbles) & 0x7F, flag 0x11 request / 0x12 data / 0x13 NAK.
The pedal answers with the same framing; its payloads are LZO1X compressed too (FUN_00030b38).
"""
import ctypes, struct, time

HDR = bytes([0x21, 0x25, 0x4D, 0x50, 0x00, 0x00])
CHUNK = 0xB8
FLAG_REQ, FLAG_DATA, FLAG_NAK = 0x11, 0x12, 0x13


# ---------------------------------------------------------------- LZO1X

def lzo1x_store(data):
    """valid LZO1X stream holding data as literal runs only (what lzo1x_decompress expects)"""
    data = bytes(data)
    n = len(data)
    if n == 0:
        return bytes([0x11, 0, 0])
    if n <= 238:                                    # first byte > 17: run of (byte - 17) literals
        return bytes([n + 17]) + data + bytes([0x11, 0, 0])
    k, r = divmod(n - 18, 255)                      # 0, k zero bytes, r: run of 18 + 255k + r literals
    if r == 0:
        k, r = k - 1, 255
    return bytes([0]) + bytes(k) + bytes([r]) + data + bytes([0x11, 0, 0])


def lzo1x_decompress(src):
    src = bytes(src)
    out = bytearray()
    ip = 0

    def ext(t, base):
        nonlocal ip
        while src[ip] == 0:
            t += 255
            ip += 1
        t += base + src[ip]
        ip += 1
        return t

    def copy_match(pos, n):
        for i in range(n):
            out.append(out[pos + i])

    state = 'start'
    t = 0
    while True:
        if state == 'start':
            if src[0] > 17:
                t = src[0] - 17
                ip = 1
                out += src[ip:ip + t]
                ip += t
                state = 'first_literal_run' if t >= 4 else 'match_next_copied'
            else:
                state = 'loop'
        elif state == 'loop':
            t = src[ip]; ip += 1
            if t >= 16:
                state = 'match'
                continue
            if t == 0:
                t = ext(0, 15)
            out += src[ip:ip + t + 3]
            ip += t + 3
            state = 'first_literal_run'
        elif state == 'first_literal_run':
            t = src[ip]; ip += 1
            if t >= 16:
                state = 'match'
                continue
            pos = len(out) - (1 + 0x0800) - (t >> 2) - (src[ip] << 2); ip += 1
            copy_match(pos, 3)
            state = 'match_done'
        elif state == 'match':
            if t >= 64:
                pos = len(out) - 1 - ((t >> 2) & 7) - (src[ip] << 3); ip += 1
                copy_match(pos, (t >> 5) - 1 + 2)
            elif t >= 32:
                t &= 31
                if t == 0:
                    t = ext(0, 31)
                pos = len(out) - 1 - ((src[ip] | src[ip + 1] << 8) >> 2); ip += 2
                copy_match(pos, t + 2)
            elif t >= 16:
                pos = len(out) - ((t & 8) << 11)
                t &= 7
                if t == 0:
                    t = ext(0, 7)
                pos -= (src[ip] | src[ip + 1] << 8) >> 2; ip += 2
                if pos == len(out):
                    return bytes(out)
                pos -= 0x4000
                copy_match(pos, t + 2)
            else:
                pos = len(out) - 1 - (t >> 2) - (src[ip] << 2); ip += 1
                copy_match(pos, 2)
            state = 'match_done'
        elif state == 'match_done':
            t = src[ip - 2] & 3
            if t == 0:
                state = 'loop'
                continue
            out += src[ip:ip + t]
            ip += t
            state = 'match_next_copied'
        elif state == 'match_next_copied':
            t = src[ip]; ip += 1
            state = 'match'


# ---------------------------------------------------------------- messages / packets

def message(cmd, payload=b''):
    if not payload:
        return struct.pack('<II', cmd, 0)
    z = lzo1x_store(payload)
    return struct.pack('<II', cmd, len(z)) + z


def packets(msg, flag=FLAG_DATA):
    total = len(msg)
    if total >= 1 << 14:
        raise ValueError('message of %d bytes does not fit the 14-bit length field' % total)
    out = []
    for off in range(0, total, CHUNK):
        part = msg[off:off + CHUNK]
        nib = bytes(x for b in part for x in (b >> 4, b & 15))
        cs = sum(nib) & 0x7F
        out.append(bytes([0xF0]) + HDR + bytes([cs, flag, total & 0x7F, total >> 7, off & 0x7F, off >> 7])
                   + nib + bytes([0xF7]))
    return out


class Reassembler:
    """feed raw SysEx packets from the pedal; yields (flag, cmd, payload) per complete message"""
    def __init__(self):
        self.buf, self.total, self.got = bytearray(), 0, 0

    def feed(self, p):
        if len(p) < 14 or p[0] != 0xF0 or p[-1] != 0xF7 or p[3:5] != b'MP':
            return None
        flag = p[8]
        total = p[9] | p[10] << 7
        off = p[11] | p[12] << 7
        nib = p[13:-1]
        data = bytes(nib[i] << 4 | nib[i + 1] for i in range(0, len(nib) - 1, 2))
        if flag == FLAG_NAK:
            return (flag, None, off)
        if off == 0:
            self.buf, self.total, self.got = bytearray(total), total, 0
        if off != self.got or off + len(data) > self.total:
            self.got = -1
            return None
        self.buf[off:off + len(data)] = data
        self.got += len(data)
        if self.got != self.total or self.total < 8:
            return None
        cmd, n = struct.unpack_from('<II', self.buf, 0)
        body = bytes(self.buf[8:8 + n])
        if not n:
            return (flag, cmd, b'')
        try:
            return (flag, cmd, lzo1x_decompress(body))
        except IndexError:                          # not every reply path compresses
            return (flag, cmd, body)


# ---------------------------------------------------------------- Windows MIDI (winmm)

class _MIDIHDR(ctypes.Structure):
    _fields_ = [('lpData', ctypes.c_void_p), ('dwBufferLength', ctypes.c_uint32),
                ('dwBytesRecorded', ctypes.c_uint32), ('dwUser', ctypes.c_size_t),
                ('dwFlags', ctypes.c_uint32), ('lpNext', ctypes.c_void_p), ('reserved', ctypes.c_size_t),
                ('dwOffset', ctypes.c_uint32), ('dwReserved', ctypes.c_size_t * 8)]


class _INCAPS(ctypes.Structure):
    _fields_ = [('wMid', ctypes.c_uint16), ('wPid', ctypes.c_uint16), ('vDriverVersion', ctypes.c_uint32),
                ('szPname', ctypes.c_wchar * 32), ('dwSupport', ctypes.c_uint32)]


class _OUTCAPS(ctypes.Structure):
    _fields_ = [('wMid', ctypes.c_uint16), ('wPid', ctypes.c_uint16), ('vDriverVersion', ctypes.c_uint32),
                ('szPname', ctypes.c_wchar * 32), ('wTechnology', ctypes.c_uint16), ('wVoices', ctypes.c_uint16),
                ('wNotes', ctypes.c_uint16), ('wChannelMask', ctypes.c_uint16), ('dwSupport', ctypes.c_uint32)]


MHDR_DONE = 1
PORT_HINTS = ('matribox', 'qme', 'sonicake')


def _winmm():
    return ctypes.WinDLL('winmm')


def list_ports():
    mm = _winmm()
    ins, outs = [], []
    for i in range(mm.midiInGetNumDevs()):
        c = _INCAPS()
        if mm.midiInGetDevCapsW(i, ctypes.byref(c), ctypes.sizeof(c)) == 0:
            ins.append(c.szPname)
    for i in range(mm.midiOutGetNumDevs()):
        c = _OUTCAPS()
        if mm.midiOutGetDevCapsW(i, ctypes.byref(c), ctypes.sizeof(c)) == 0:
            outs.append(c.szPname)
    return ins, outs


def _pick(names, want):
    if want is not None:
        if str(want).isdigit():
            return int(want)
        hits = [i for i, n in enumerate(names) if str(want).lower() in n.lower()]
    else:
        hits = [i for i, n in enumerate(names) if any(h in n.lower() for h in PORT_HINTS)]
    if len(hits) != 1:
        raise RuntimeError('MIDI port not found (%s); available: %s' % (want or '/'.join(PORT_HINTS), names))
    return hits[0]


class Port:
    """SysEx in/out on the pedal's USB-MIDI ports (no callbacks: input buffers are polled)"""
    NBUF, BUFSZ = 32, 1024

    def __init__(self, want=None):
        self.mm = mm = _winmm()
        ins, outs = list_ports()
        self.in_id, self.out_id = _pick(ins, want), _pick(outs, want)
        self.name = outs[self.out_id]
        self.hin, self.hout = ctypes.c_void_p(), ctypes.c_void_p()
        r = mm.midiOutOpen(ctypes.byref(self.hout), self.out_id, None, None, 0)
        if r:
            raise RuntimeError('midiOutOpen failed (%d): is the PC editor still open?' % r)
        r = mm.midiInOpen(ctypes.byref(self.hin), self.in_id, None, None, 0)
        if r:
            mm.midiOutClose(self.hout)
            raise RuntimeError('midiInOpen failed (%d): is the PC editor still open?' % r)
        self.bufs, self.hdrs = [], []
        for _ in range(self.NBUF):
            b = ctypes.create_string_buffer(self.BUFSZ)
            h = _MIDIHDR(ctypes.cast(b, ctypes.c_void_p), self.BUFSZ, 0, 0, 0)
            mm.midiInPrepareHeader(self.hin, ctypes.byref(h), ctypes.sizeof(h))
            mm.midiInAddBuffer(self.hin, ctypes.byref(h), ctypes.sizeof(h))
            self.bufs.append(b); self.hdrs.append(h)
        self.next = 0
        self.partial = bytearray()
        mm.midiInStart(self.hin)

    def send(self, sysex):
        b = ctypes.create_string_buffer(bytes(sysex), len(sysex))
        h = _MIDIHDR(ctypes.cast(b, ctypes.c_void_p), len(sysex), len(sysex), 0, 0)
        mm = self.mm
        mm.midiOutPrepareHeader(self.hout, ctypes.byref(h), ctypes.sizeof(h))
        r = mm.midiOutLongMsg(self.hout, ctypes.byref(h), ctypes.sizeof(h))
        if r:
            raise RuntimeError('midiOutLongMsg failed (%d)' % r)
        t0 = time.time()
        while not h.dwFlags & MHDR_DONE and time.time() - t0 < 2:
            time.sleep(0.001)
        mm.midiOutUnprepareHeader(self.hout, ctypes.byref(h), ctypes.sizeof(h))

    def receive(self):
        """complete SysEx messages received so far (buffers are recycled in order)"""
        out = []
        while True:
            h = self.hdrs[self.next]
            if not h.dwFlags & MHDR_DONE:
                break
            self.partial += ctypes.string_at(h.lpData, h.dwBytesRecorded)
            mm = self.mm
            mm.midiInUnprepareHeader(self.hin, ctypes.byref(h), ctypes.sizeof(h))
            h.dwFlags, h.dwBytesRecorded = 0, 0
            mm.midiInPrepareHeader(self.hin, ctypes.byref(h), ctypes.sizeof(h))
            mm.midiInAddBuffer(self.hin, ctypes.byref(h), ctypes.sizeof(h))
            self.next = (self.next + 1) % self.NBUF
            while 0xF7 in self.partial:
                e = self.partial.index(0xF7)
                s = self.partial.rfind(0xF0, 0, e)
                if s >= 0:
                    out.append(bytes(self.partial[s:e + 1]))
                del self.partial[:e + 1]
        return out

    def close(self):
        mm = self.mm
        mm.midiInStop(self.hin)
        mm.midiInReset(self.hin)
        for h in self.hdrs:
            mm.midiInUnprepareHeader(self.hin, ctypes.byref(h), ctypes.sizeof(h))
        mm.midiInClose(self.hin)
        mm.midiOutClose(self.hout)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def transact(port, cmd, payload=b'', flag=FLAG_DATA, want=None, timeout=8.0, gap=0.04, log=print):
    """send one message; return the payload of the first reply whose cmd == want (None if want is None)"""
    pk = packets(message(cmd, payload), flag)
    rx = Reassembler()
    port.receive()
    for p in pk:
        port.send(p)
        time.sleep(gap)
    if want is None:
        return None
    t0 = time.time()
    while time.time() - t0 < timeout:
        for p in port.receive():
            r = rx.feed(p)
            if not r:
                continue
            if r[0] == FLAG_NAK:
                raise RuntimeError('pedal rejected the packet at offset %d (NAK)' % r[2])
            if r[1] == want:
                return r[2]
        time.sleep(0.01)
    raise TimeoutError('no reply %#010x from the pedal' % want)
