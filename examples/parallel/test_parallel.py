"""Emulated checks of Split / Path B / Merge in a chain with factory effects (needs the unicorn package).

Path A = factory "Pure" delay, path B = factory "Guitar EQ 1". Every chain result is compared with
the effects run alone in separate emulators and mixed in Python.
"""
import os, sys, math
from matribox.firmware import stock
from matribox.module.effemu import Emu

HERE = os.path.dirname(os.path.abspath(__file__))
FS = 44100
N = FS // 2


def axf(name):
    return open(os.path.join(HERE, name + '.axf'), 'rb').read()


def factory(cat, slot):
    meta = stock.factory_entry(cat, slot)             # from the user's stock firmware
    knobs = [0.0] * 16
    for p in meta.params:
        knobs[p['idx']] = p['default']
    return meta.elf, knobs


PURE, PURE_K = factory(9, 1)
EQ, EQ_K = factory(7, 0)
EQ_K[0], EQ_K[2], EQ_K[4] = 40.0, -30.0, 25.0          # make path B clearly different


def k(*v):
    return list(v) + [0.0] * (16 - len(v))


SPLIT_K = k(2)                                         # Mode Stereo
PATHB_K = k(0)                                         # Input 0 dB
MERGE_K = k(0, 0, 0, 0, 0)                             # Level A/B 0 dB, centred, phase normal
MUTE = -61.0


def signal():
    out = []
    for i in range(N):
        x = 1.0 if i == 0 else 0.0
        if FS // 10 <= i < FS // 10 + FS // 20:
            x = 0.5 * math.sin(2 * math.pi * 220 * i / FS)
        out.append((x, 0.6 * x + 0.1 * math.sin(2 * math.pi * 3000 * i / FS)))
    return out


def alone(elf, knobs, sig):
    return Emu(elf).run(knobs, sig)


def chain(slots, sig):
    emu = Emu()
    loaded = [(emu.load(e), kn, en) for e, kn, en in slots]
    return emu.run_chain(loaded, sig)


def mix(a, ga, b, gb):
    return [(x[0] * ga[0] + y[0] * gb[0], x[1] * ga[1] + y[1] * gb[1]) for x, y in zip(a, b)]


def maxdiff(a, b):
    return max(max(abs(x[0] - y[0]), abs(x[1] - y[1])) for x, y in zip(a, b))


def main():
    sig = signal()
    A = alone(PURE, PURE_K, sig)
    B = alone(EQ, EQ_K, sig)
    AB = alone(EQ, EQ_K, A)
    zero = [(0.0, 0.0)] * N
    g6 = 10 ** (-6 / 20)
    split, pathb, merge = axf('split'), axf('pathb'), axf('merge')
    full = lambda m=MERGE_K, en=(1, 1, 1): [(split, SPLIT_K, en[0]), (PURE, PURE_K, 1), (pathb, PATHB_K, en[1]),
                                            (EQ, EQ_K, 1), (merge, m, en[2])]
    cases = [
        ('Split>Merge, so A (A = sinal seco)', chain([(split, SPLIT_K, 1), (merge, k(0, MUTE), 1)], sig), sig),
        ('Split>Merge, so B (B = seco)', chain([(split, SPLIT_K, 1), (merge, k(MUTE, 0), 1)], sig), sig),
        ('Split>Merge, A -6 dB', chain([(split, SPLIT_K, 1), (merge, k(-6, MUTE), 1)], sig), mix(sig, (g6, g6), zero, (0, 0))),
        ('Split>Merge, fase B invertida', chain([(split, SPLIT_K, 1), (merge, k(0, 0, 0, 0, 1), 1)], sig), zero),
        ('paralelo Pure | EQ', chain(full(), sig), mix(A, (1, 1), B, (1, 1))),
        ('paralelo, pan A=L50 B=R50', chain(full(k(0, 0, -50, 50)), sig), mix(A, (1, 0), B, (0, 1))),
        ('paralelo, Merge em bypass', chain(full(k(-20, -20), (1, 1, 0)), sig), mix(A, (1, 1), B, (1, 1))),
        ('paralelo, Path B em bypass', chain(full(en=(1, 0, 1)), sig), A),
        ('Split em bypass = serie', chain(full(en=(0, 1, 1)), sig), AB),
        ('Merge antes do Split (fora de ordem)', chain([(merge, MERGE_K, 1), (split, SPLIT_K, 1), (PURE, PURE_K, 1),
                                                     (pathb, PATHB_K, 1), (EQ, EQ_K, 1)], sig), B),
        ('Split Mono (L)', chain([(split, k(0), 1), (merge, k(0, MUTE), 1)], sig), [(l, l) for l, r in sig]),
    ]
    ok = True
    for name, got, want in cases:
        d = maxdiff(got, want)
        ok &= d < 1e-5
        print('%-40s max |dif| = %.2e  %s' % (name, d, 'OK' if d < 1e-5 else 'FALHOU'))
    print('todos OK' if ok else 'HA FALHAS')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
