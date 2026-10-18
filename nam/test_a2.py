"""Checks for the NAM A2-Lite engine (nam/a2lite.c).

    test_a2.py model.nam [--emu axf]

1. host: a2lite.c compiled for the PC (no FMA contraction, same op order as the core) against
   the official NeuralAmpModelerCore (ref_render.exe built from nam/ref_render.cpp, slim 0 = A2-Lite), sample by sample;
2. --emu: the built module runs in matribox/module/effemu.py (device loader + Cortex-M7 code) on the same
   signal; reports the difference to the host engine and the instruction count per 64-frame block.
"""
import os, sys, subprocess, json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
from matribox import paths
from matribox.toolchain import ZIG
BUILD = paths.build_path('nam')
os.makedirs(BUILD, exist_ok=True)
# NeuralAmpModelerCore's reference renderer, built from ref_render.cpp (see the NAM docs)
REF = paths.setting('nam_ref_render', os.path.join(BUILD, 'ref_render.exe'))

DRIVER = r'''
#include <stdio.h>
#include "a2lite.h"
extern const float a2_model_weights[A2_NUM_WEIGHTS];
int main(int argc, char **argv)
{
    float buf[A2_BLOCK];
    size_t n;
    FILE *fi = fopen(argv[1], "rb"), *fo = fopen(argv[2], "wb");
    if (!fi || !fo) return 2;
    a2_init(a2_model_weights);
    while ((n = fread(buf, sizeof(float), A2_BLOCK, fi)) > 0) {
        a2_process(buf, buf, (int)n);
        fwrite(buf, sizeof(float), n, fo);
    }
    fclose(fo);
    return 0;
}
'''


def test_signal(fs=48000, seed=3):
    """plucks at three levels, a noise burst, a sine sweep and silence"""
    rnd = np.random.default_rng(seed)
    parts = []
    for level in (0.05, 0.3, 1.0):
        y = np.zeros(int(0.6 * fs))
        for t0, f in ((0.0, 82.41), (0.15, 196.0), (0.3, 329.63)):
            n = int(fs / f)
            buf = rnd.uniform(-1, 1, n); buf -= buf.mean()
            s = int(t0 * fs)
            for i in range(len(y) - s):
                y[s + i] += level * buf[i % n]
                buf[i % n] = 0.995 * 0.5 * (buf[i % n] + buf[(i + 1) % n])
        parts.append(y)
    parts.append(rnd.uniform(-0.5, 0.5, int(0.2 * fs)))
    t = np.arange(int(0.5 * fs)) / fs
    parts.append(0.4 * np.sin(2 * np.pi * (40 * t + 0.5 * (4000 - 40) / 0.5 * t * t)))
    parts.append(np.zeros(int(0.3 * fs)))
    x = np.concatenate(parts)
    return x[:len(x) // 64 * 64].astype(np.float32)


def build_host(wsrc):
    os.makedirs(BUILD, exist_ok=True)
    drv = os.path.join(BUILD, 'drv.c')
    open(drv, 'w').write(DRIVER)
    exe = os.path.join(BUILD, 'a2_host.exe')
    r = subprocess.run([ZIG, 'cc', '-O2', '-ffp-contract=off', '-DA2_HOST', '-I', HERE, '-o', exe, drv,
                        os.path.join(HERE, 'a2lite.c'), wsrc], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr)
    return exe


def main():
    model = sys.argv[1]
    gen = os.path.join(BUILD, 'model_w')
    subprocess.run([sys.executable, '-I', '-m', 'matribox.nam.nam_a2', model, gen], check=True)
    exe = build_host(gen + '.c')
    x = test_signal()
    fin, fref, fhost = (os.path.join(BUILD, n) for n in ('in.f32', 'ref.f32', 'host.f32'))
    x.tofile(fin)
    subprocess.run([REF, model, '0.0', fin, fref], check=True)
    subprocess.run([exe, fin, fhost], check=True)
    ref, host = np.fromfile(fref, np.float32), np.fromfile(fhost, np.float32)
    d = np.abs(ref.astype(np.float64) - host)
    bad = np.nonzero(ref != host)[0]
    print('host vs core: %d samples, peak out %.4f, max |diff| %.3g, %s' % (
        len(ref), np.abs(ref).max(), d.max(),
        'bit-exact' if len(bad) == 0 else '%d differ, first at %d' % (len(bad), bad[0])))
    if '--emu' in sys.argv:
        emu(sys.argv[sys.argv.index('--emu') + 1], x, host)


def emu(axf, x, host):
    from matribox.module.effemu import Emu
    from unicorn import UC_HOOK_CODE
    e = Emu(open(axf, 'rb').read())
    man = json.load(open(axf[:-4] + '.json'))
    knobs = [p['default'] for p in man['params']] + [0.0] * 16
    knobs = knobs[:16]
    adc_to_nam = 0.45659                                       # nam_mod.c ADC_TO_NAM
    sig = [(float(v) / adc_to_nam, float(v) / adc_to_nam) for v in x]
    count = [0]
    h = e.uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, u: count.__setitem__(0, count[0] + 1), begin=0x58000, end=0x78000)
    e.run(knobs, sig[:64 * 20])
    per_block = count[0] / 20
    e.uc.hook_del(h)
    out = e.run(knobs, sig)
    y = np.array([l for l, r in out], np.float32)
    info = json.load(open(os.path.join(BUILD, 'model_w.json')))
    g = 10 ** ((-18.0 - info['loudness']) / 20)                 # module normalises like the NAM plugin
    d = np.abs(y.astype(np.float64) - host * g)
    print('emu vs host: max |diff| %.3g (peak %.4f); %.0f instr/block = %.1f%% of 600 MHz at 1 instr/cycle' % (
        d.max(), np.abs(y).max(), per_block, per_block * (44100 / 64) / 6e6))


if __name__ == '__main__':
    main()
