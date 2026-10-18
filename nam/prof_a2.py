"""Instruction profile of a module's Process in matribox/module/effemu.py: prof_a2.py module.axf linked.elf"""
import os, sys, struct, collections
HERE = os.path.dirname(os.path.abspath(__file__))
from matribox.module.effemu import Emu
from unicorn import UC_HOOK_CODE
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS, CS_MODE_V8

axf, linked = sys.argv[1], sys.argv[2]
e = Emu(open(axf, 'rb').read())
import math
sig = [(0.3 * math.sin(i * 0.05), 0.3 * math.sin(i * 0.05)) for i in range(64 * 30)]
hits = collections.Counter()
h = e.uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, u: hits.__setitem__(a, hits[a] + 1), begin=0x58000, end=0x78000)
e.run([0.0] * 16, sig)
nblk = len(sig) // 64 + 0
total = sum(hits.values())
# subtract Init's share: run Init alone
hits_init = collections.Counter()
e.uc.hook_del(h)
base = 0x58000
print('total %.0f instr/block (incl. Init amortised over %d blocks)' % (total / nblk, nblk))
# bucket by loop: group consecutive hot addresses
b = open(linked, 'rb').read()
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS | CS_MODE_V8); md.skipdata = True
ranges = []
cur = None
for a in sorted(hits):
    if cur and a - cur[1] <= 8 and abs(hits[a] - cur[2]) <= cur[2] * 0.6:
        cur[1] = a; cur[3] += hits[a]
    else:
        if cur: ranges.append(cur)
        cur = [a, a, hits[a], hits[a]]
ranges.append(cur)
ranges.sort(key=lambda r: -r[3])
for lo, hi, per, tot in ranges[:15]:
    print('%06x-%06x  %5.1f%%  %8.0f/block  (body runs %d times)' % (lo - base, hi - base, 100.0 * tot / total,
                                                                  tot / nblk, per))


