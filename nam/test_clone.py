"""Checks for loading NAM captures through the Clone slots.

    test_clone.py model.nam

1. protocol: the packets from matribox/usb/htmidi.py run through the app's own code (V1.2.2, matribox.firmware.stock.app_elf)
   in unicorn: FUN_00031a08 reassembles them, FUN_0001f1f4 (miniLZO) unpacks the message, and the
   result must be the command the editor would send. The Python LZO decoder must read what the app's
   compressor (FUN_0001f148, used for the pedal's replies) writes.
2. module: examples/nam_clone/gen/nam_clone.axf in matribox/module/effemu.py with the blob in a fake flash window
   (ctx[0], as FUN_000249ec gives it), against the host engine at neutral knobs; a damaged blob must
   leave the signal dry.
"""
import os, sys, struct, json, subprocess
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from matribox.usb import htmidi, nam_clone
from matribox.firmware import stock
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB
from unicorn.arm_const import *

AXF = os.path.join(os.path.dirname(HERE), 'examples', 'nam_clone', 'gen', 'nam_clone.axf')
STOP = 0x20010000
RX_STATE, RX_BUF = 0x202424c4, 0x202494c0       # DAT_00031bd4 / DAT_00031bd8
MSG_BUF = 0x2023ade0                            # DAT_0003963c (decompressed message)
SCR = 0x20270000


class App:
    def __init__(self):
        d = open(stock.app_elf(), 'rb').read()
        self.uc = uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        uc.ctl_set_cpu_model(UC_CPU_ARM_MAX)
        for base, size in ((0, 0x80000), (0x80000000, 0x1300000), (0x20000000, 0x20000), (0x20200000, 0x80000)):
            uc.mem_map(base, size)
        phoff, = struct.unpack_from('<I', d, 0x1c); n, = struct.unpack_from('<H', d, 0x2c)
        for i in range(n):
            _, off, va, _, fsz, _, _, _ = struct.unpack_from('<8I', d, phoff + 32 * i)
            if fsz:
                uc.mem_write(va, d[off:off + fsz])
        uc.mem_write(STOP, b'\xfe\xe7')

    def call(self, fn, *args):
        uc = self.uc
        for r, a in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
            uc.reg_write(r, a)
        uc.reg_write(UC_ARM_REG_SP, 0x2000f000)
        uc.reg_write(UC_ARM_REG_LR, STOP | 1)
        uc.emu_start(fn | 1, STOP, count=50_000_000)
        return uc.reg_read(UC_ARM_REG_R0)


def check_protocol(payload):
    app = App()
    uc = app.uc
    msg = htmidi.message(nam_clone.CMD_CLONE_DATA, payload)
    pk = htmidi.packets(msg)
    for p in pk:
        uc.mem_write(SCR, p)
        app.call(0x31a08, SCR, len(p))
    total = struct.unpack('<H', uc.mem_read(RX_STATE + 0x41c, 2))[0]
    flag = struct.unpack('<H', uc.mem_read(RX_STATE + 0x412, 2))[0] >> 8
    got = bytes(uc.mem_read(RX_BUF, total))
    assert total == len(msg) and got == msg and flag == htmidi.FLAG_DATA, 'reassembly failed'
    cmd, n = struct.unpack_from('<II', got)
    uc.mem_write(SCR, b'\0' * 4)
    app.call(0x1f1f4, RX_BUF + 8, MSG_BUF + 8, n, SCR)
    outlen = struct.unpack('<I', uc.mem_read(SCR, 4))[0]
    out = bytes(uc.mem_read(MSG_BUF + 8, outlen))
    assert out == payload, 'app LZO decode differs (%d vs %d bytes)' % (outlen, len(payload))
    end = MSG_BUF + 8 + outlen
    print('protocol: %d packets, message %d bytes -> FUN_00031a08 + FUN_0001f1f4 give the payload back '
          '(%d bytes, ends %#x, buffer limit 0x2023cde0)' % (len(pk), len(msg), outlen, end))
    assert end <= 0x2023cde0

    rnd = np.random.default_rng(1)
    cases = [bytes(300), bytes(range(256)) * 9, rnd.integers(0, 256, 5000, dtype=np.uint8).tobytes(),
             b'Clone 1\0' * 40 + rnd.integers(0, 4, 3000, dtype=np.uint8).tobytes(), payload]
    for c in cases:
        uc.mem_write(SCR, c)
        uc.mem_write(SCR + 0x8000, b'\0' * 4)
        app.call(0x1f148, SCR, SCR + 0x4000, len(c), SCR + 0x8000)
        zl = struct.unpack('<I', uc.mem_read(SCR + 0x8000, 4))[0]
        z = bytes(uc.mem_read(SCR + 0x4000, zl))
        assert htmidi.lzo1x_decompress(z) == c, 'python LZO decoder differs'
        assert htmidi.lzo1x_decompress(htmidi.lzo1x_store(c)) == c
    print('LZO: python decoder reads the app compressor output on %d inputs' % len(cases))


def check_module(model, blob):
    from matribox.module.effemu import Emu
    import test_a2
    gen = os.path.join(test_a2.BUILD, 'model_w')
    subprocess.run([sys.executable, '-I', '-m', 'matribox.nam.nam_a2', model, gen], check=True)
    exe = test_a2.build_host(gen + '.c')
    x = test_a2.test_signal()[:64 * 400]
    fin, fhost = os.path.join(test_a2.BUILD, 'in.f32'), os.path.join(test_a2.BUILD, 'host.f32')
    x.tofile(fin)
    subprocess.run([exe, fin, fhost], check=True)
    host = np.fromfile(fhost, np.float32)
    loud = json.load(open(gen + '.json'))['loudness']
    g = 10 ** ((-18.0 - (loud if loud is not None else -18.0)) / 20)
    sig = [(float(v) / 0.45659, float(v) / 0.45659) for v in x]
    knobs = [50.0] * 5 + [0.0] * 11
    flash = 0x60100000

    def run(b):
        e = Emu(open(AXF, 'rb').read())
        e.uc.mem_map(flash, 0x4000)
        e.uc.mem_write(flash, b.ljust(0x2000, b'\xff'))
        return e, np.array([l for l, r in e.run(knobs, sig, ctx=struct.pack('<I', flash))], np.float32)

    e, y = run(blob)
    d = np.abs(y.astype(np.float64) - host * g)
    print('module: emu vs host max |diff| %.3g (peak %.4f)' % (d.max(), np.abs(y).max()))
    assert d.max() < 1e-4 * max(1.0, np.abs(y).max())
    bad = bytearray(blob); bad[0x400] ^= 1
    _, y2 = run(bytes(bad))
    assert np.array_equal(y2, np.array([l for l, r in sig], np.float32)), 'damaged blob must pass dry'
    print('module: damaged blob (CRC) -> dry signal')
    e2 = Emu(open(AXF, 'rb').read())
    _, y3 = None, np.array([l for l, r in e2.run(knobs, sig[:640], ctx=struct.pack('<I', 0))], np.float32)
    assert np.array_equal(y3, np.array([l for l, r in sig[:640]], np.float32)), 'null slot must pass dry'
    print('module: empty slot (null) -> dry signal')

    fac = Emu(stock.factory_module(11, 0))       # the factory Clone module of the user's firmware
    fac.uc.mem_map(flash, 0x4000)
    fac.uc.mem_write(flash, blob.ljust(0x2000, b'\xff'))
    y4 = np.array(fac.run(knobs, sig[:6400], ctx=struct.pack('<I', flash)), np.float32)
    print('factory Clone on a NAM slot: runs, peak out %.3g (in %.3g)' % (np.abs(y4).max(), np.abs(x[:6400]).max() / 0.45659))


def main():
    model = sys.argv[1]
    blob, info = nam_clone.pack_blob(model, native_rate=True)   # 44.1 kHz path; nam/test_rs48.py covers 48k
    payload = nam_clone.clone_payload(2, 'Test capture', blob)
    print('blob %d bytes, payload %d bytes' % (len(blob), len(payload)))
    check_protocol(payload)
    check_module(model, blob)


if __name__ == '__main__':
    main()
