"""Checks for the 44.1 <-> 48 kHz path (nam/rs48.c) and the Clone module running captures at 48 kHz.

    test_rs48.py model.nam [--emu]

1. resampler (host build) against a float64 reference written from the formulas in rs48.c, plus its
   frequency response, aliasing and latency measured on sines;
2. a2lite.c with A2_BLOCK=72 fed the 69/70-sample blocks of the up path (and odd sizes) against the
   official core: still bit-exact;
3. up -> NAM -> down on the host against float64 resampling around the core;
4. --emu: examples/nam_clone/gen/nam_clone.axf with a 48 kHz blob in effemu against the host pipeline,
   and its instruction count per block.
"""
import os, sys, subprocess, struct, json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_a2

BUILD = test_a2.BUILD

DRIVER = r'''
#include <stdio.h>
#include <stdlib.h>
#include "a2lite.h"
#include "rs48.h"
extern const float a2_model_weights[A2_NUM_WEIGHTS];
static float buf[1 << 22];
int main(int argc, char **argv)
{
    char mode = argv[1][0];
    FILE *fi = fopen(argv[2], "rb"), *fo = fopen(argv[3], "wb");
    if (!fi || !fo) return 2;
    size_t n = fread(buf, sizeof(float), sizeof buf / sizeof buf[0], fi);
    float b48[RS_MAX48], b44[64];
    rs48_design(); rs48_reset(); a2_init(a2_model_weights);
    if (mode == 'v') {                     /* a2 alone, odd block sizes */
        static const int sz[] = {69, 70, 70, 1, 72, 33, 64, 2, 71, 70};
        size_t i = 0; int k = 0;
        while (i < n) {
            int m = sz[k++ % 10]; if (i + m > n) m = (int)(n - i);
            a2_process(buf + i, buf + i, m); i += m;
        }
        fwrite(buf, sizeof(float), n, fo);
        return 0;
    }
    for (size_t i = 0; i + 64 <= n; i += 64) {
        int c = rs48_up(buf + i, b48);
        if (mode == 'u') { fwrite(b48, sizeof(float), c, fo); continue; }
        if (mode == 'n') a2_process(b48, b48, c);
        rs48_down(b48, c, b44);
        fwrite(b44, sizeof(float), 64, fo);
    }
    fclose(fo);
    return 0;
}
'''


def kaiser_sinc(N, fs, fc, atten=70.0):
    t = np.arange(N) - (N - 1) / 2
    h = np.sinc(2 * fc / fs * t) * np.kaiser(N, 0.1102 * (atten - 8.7))
    return h / h.sum()


P1U = kaiser_sinc(120, 220500.0, 24000.0)
P1D = kaiser_sinc(240, 220500.0, 22000.0)
P2 = kaiser_sinc(1280, 2352000.0, 24000.0)


def stage(x, h, Pi, Po):
    """y[n] = Pi sum_j x[b-j] h[p+Pi j], b = floor(n Po / Pi), every n whose b < len(x)"""
    nout = (len(x) * Pi - 1) // Po + 1
    y = np.zeros(nout)
    j = np.arange((len(h) + Pi - 1) // Pi)
    for n in range(nout):
        b, p = divmod(Po * n, Pi)
        t = p + Pi * j
        k = b - j
        ok = (t < len(h)) & (k >= 0)
        y[n] = Pi * np.dot(x[k[ok]], h[t[ok]])
    return y


def ref_up(x):
    return stage(stage(x, P1U, 5, 3), P2, 32, 49)


def ref_down(w, nout):
    return stage(stage(w, P2, 49, 32), P1D, 3, 5)[:nout]


def build(wsrc):
    exe = os.path.join(BUILD, 'rs_host.exe')
    drv = os.path.join(BUILD, 'rs_drv.c')
    open(drv, 'w').write(DRIVER)
    r = subprocess.run([test_a2.ZIG, 'cc', '-O2', '-ffp-contract=off', '-DA2_HOST', '-DA2_BLOCK=72', '-I', HERE,
                        '-o', exe, drv, os.path.join(HERE, 'a2lite.c'), os.path.join(HERE, 'rs48.c'), wsrc],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr)
    return exe


def run(exe, mode, x):
    fi, fo = os.path.join(BUILD, 'rs_in.f32'), os.path.join(BUILD, 'rs_out.f32')
    np.asarray(x, np.float32).tofile(fi)
    subprocess.run([exe, mode, fi, fo], check=True)
    return np.fromfile(fo, np.float32)


def main():
    model = sys.argv[1]
    gen = os.path.join(BUILD, 'model_w')
    subprocess.run([sys.executable, '-I', '-m', 'matribox.nam.nam_a2', model, gen], check=True)
    exe = build(gen + '.c')

    # 1. resampler vs float64 reference
    rnd = np.random.default_rng(5)
    x = (rnd.uniform(-1, 1, 64 * 40)).astype(np.float32)
    u = run(exe, 'u', x)
    ur = ref_up(x.astype(np.float64))[:len(u)]
    d = run(exe, 'r', x)
    dr = ref_down(ref_up(x.astype(np.float64)), len(d))
    print('rs48: up %d -> %d samples, max |err| vs float64 %.2g; up+down max |err| %.2g' % (
        len(x), len(u), np.abs(u - ur).max(), np.abs(d - dr).max()))
    assert np.abs(u - ur).max() < 2e-6 and np.abs(d - dr).max() < 2e-6

    fs = 44100.0
    n = 64 * 400
    tt = np.arange(n) / fs
    lat = None
    print('   f (Hz)   gain (dB)   other (dB rel.)')
    for f in (100.0, 1000.0, 5000.0, 10000.0, 15000.0, 19000.0, 20000.0, 21000.0):
        s = np.sin(2 * np.pi * f * tt).astype(np.float32)
        y = run(exe, 'r', s)[n // 2:]
        t2 = tt[n // 2:]
        A = np.stack([np.sin(2 * np.pi * f * t2), np.cos(2 * np.pi * f * t2)], 1)
        co, *_ = np.linalg.lstsq(A, y, rcond=None)
        resid = y - A @ co
        g = np.hypot(*co)
        if f == 100.0:                     # period 441 samples: the phase gives the delay unambiguously
            lat = -np.arctan2(co[1], co[0]) / (2 * np.pi * f) * fs
        print('  %7.0f   %+8.3f   %8.1f' % (f, 20 * np.log10(g), 20 * np.log10(np.sqrt(np.mean(resid ** 2)) * np.sqrt(2) / g + 1e-12)))
    print('rs48: latency %.2f samples at 44.1 kHz (%.3f ms)' % (lat, lat / 44.1))

    # 2. a2 with A2_BLOCK=72 and odd block sizes vs the core (48 kHz stream)
    x48 = test_a2.test_signal()
    fin, fref = os.path.join(BUILD, 'v_in.f32'), os.path.join(BUILD, 'v_ref.f32')
    x48.tofile(fin)
    subprocess.run([test_a2.REF, model, '0.0', fin, fref], check=True)
    ref = np.fromfile(fref, np.float32)
    v = run(exe, 'v', x48)
    print('a2 (A2_BLOCK=72, blocks 1..72) vs core: %s' % ('bit-exact' if np.array_equal(v, ref) else
                                                        'max |diff| %.3g' % np.abs(v - ref).max()))
    assert np.array_equal(v, ref)

    # 3. pipeline vs float64 resampling around the core
    x44 = x48[:64 * 700]
    y = run(exe, 'n', x44)
    up64 = ref_up(x44.astype(np.float64))
    up64 = up64[:len(up64) // 64 * 64].astype(np.float32)
    up64.tofile(fin)
    subprocess.run([test_a2.REF, model, '0.0', fin, fref], check=True)
    core48 = np.fromfile(fref, np.float32).astype(np.float64)
    yr = ref_down(core48, len(y))
    n = min(len(y), len(yr)) - 64                  # the float64 reference stops short of the stream's tail
    err = np.abs(y[:n] - yr[:n])
    print('pipeline up->NAM->down vs float64 resampling + core: max |diff| %.2g (peak %.3f)' % (err.max(), np.abs(yr).max()))
    np.save(os.path.join(BUILD, 'rs_pipe.npy'), np.stack([x44[:len(y)], y]))

    if '--emu' in sys.argv:
        emu(model, x44[:64 * 300], y[:64 * 300], gen)


def emu(model, x, host, gen):
    from matribox.module.effemu import Emu
    from unicorn import UC_HOOK_CODE
    from matribox.usb import nam_clone
    blob, info = nam_clone.pack_blob(model)
    loud = json.load(open(gen + '.json'))['loudness']
    g = 10 ** ((-18.0 - (loud if loud is not None else -18.0)) / 20)
    axf = os.path.join(os.path.dirname(HERE), 'examples', 'nam_clone', 'gen', 'nam_clone.axf')
    e = Emu(open(axf, 'rb').read())
    e.uc.mem_map(0x60100000, 0x4000)
    e.uc.mem_write(0x60100000, blob)
    sig = [(float(v) / 0.45659, float(v) / 0.45659) for v in x]
    knobs = [50.0] * 5 + [0.0] * 11
    out = np.array([l for l, r in e.run(knobs, sig, ctx=struct.pack('<I', 0x60100000))], np.float32)
    d = np.abs(out - host * g)
    print('emu clone module (48 kHz blob) vs host pipeline: max |diff| %.2g (peak %.3f)' % (d.max(), np.abs(out).max()))
    # unicorn only applies a code hook to blocks translated after it: count on a fresh instance
    for label, b in (('48 kHz', blob), ('44.1 kHz (--native-rate)', nam_clone.pack_blob(model, True)[0])):
        e = Emu(open(axf, 'rb').read())
        e.uc.mem_map(0x60100000, 0x4000)
        e.uc.mem_write(0x60100000, b)
        cnt = [0]
        e.uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, u: cnt.__setitem__(0, cnt[0] + 1), begin=0x58000, end=0x78000)
        e.run(knobs, [], ctx=struct.pack('<I', 0x60100000))
        init = cnt[0]
        e.run(knobs, sig[:64 * 40], ctx=struct.pack('<I', 0x60100000))
        blk = (cnt[0] - 2 * init) / 40
        print('emu %s: Init %d instr, %.0f instr per block = %.1f%% of 600 MHz at 1 instr/cycle' % (
            label, init, blk, blk * (44100 / 64) / 6e6))


if __name__ == '__main__':
    main()
