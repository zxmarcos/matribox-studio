"""Run two builds of the NAM Clone module on the same input/blob in effemu: bit-exactness + instructions.

    cmp_axf.py ref.axf new.axf model.nam
"""
import os, sys, struct
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import numpy as np, test_a2
from matribox.usb import nam_clone
from matribox.module.effemu import Emu
from unicorn import UC_HOOK_CODE

FLASH = 0x60100000


def run(axf, blob, sig, knobs, count):
    e = Emu(open(axf, 'rb').read())
    e.uc.mem_map(FLASH, 0x4000)
    e.uc.mem_write(FLASH, blob)
    cnt = [0]
    if count:
        e.uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, u: cnt.__setitem__(0, cnt[0] + 1), begin=0x58000, end=0x78000)
        e.run(knobs, [], ctx=struct.pack('<I', FLASH))
        init = cnt[0]
    y = np.array(e.run(knobs, sig, ctx=struct.pack('<I', FLASH)), np.float32)
    return y, ((cnt[0] - 2 * init) / (len(sig) // 64) if count else None)


def main():
    ref, new, model = sys.argv[1:4]
    x = test_a2.test_signal()[:64 * 300]
    sig = [(float(v) / 0.45659, float(v) / 0.45659) for v in x]
    knobs = [62.0, 55.0, 60.0, 40.0, 58.0] + [0.0] * 11
    for native in (False, True):
        blob, info = nam_clone.pack_blob(model, native)
        ya, _ = run(ref, blob, sig, knobs, False)
        yb, _ = run(new, blob, sig, knobs, False)
        _, ia = run(ref, blob, sig[:64 * 30], knobs, True)
        _, ib = run(new, blob, sig[:64 * 30], knobs, True)
        same = np.array_equal(ya.view(np.uint32), yb.view(np.uint32))
        print('%-24s %s (max |diff| %.3g); instr/block %.0f -> %.0f (%.1f%%)' % (
            info['runs_at'], 'BIT-IDENTICAL' if same else 'DIFFERENT', np.abs(ya - yb).max(), ia, ib, 100 * (ib / ia - 1)))


if __name__ == '__main__':
    main()
