"""Emulate a Matribox II Pro effect module the way the device firmware does.

Python port of the app's module loader (FUN_0004029c / FUN_000405c8 / FUN_00040b1c @ ITCM 0x40xxx):
sections are placed by name/address into four pools, SHT_REL relocations are applied with the
address high nibble selecting the pool, and the module header at code+0 provides Init/Process.

usage: effemu.py module.axf [knob values...]   (requires the 'unicorn' package)
"""
import sys, struct, math
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UcError
from unicorn.arm_const import *

POOLS = {0x0: 0x00058000, 0x1: 0x81301000, 0x2: 0x20204000, 0x3: 0x81321000}  # code, srom, iram, sram
POOL_SIZE = {0x0: 0x20000, 0x1: 0x20000, 0x2: 0x30000, 0x3: 0x6cf000}
MAGIC = 0xFFAA55AA
STACK = 0x20000000
SCRATCH = 0x20100000
STOP = 0x00001000
BLOCK = 64
# app V1.2.2 state that modules may read (see docs/firmware/audio-engine.md and examples/parallel/route.h)
HOST_RAM = 0x20234000                # mapped so the two addresses below exist in the emulator
HOST_MODULES = 0x20241f44            # u32[12]: header of each loaded module (FUN_0001b654)
HOST_TOGGLE = 0x20234025             # u8 flipped at the start of every audio block (FUN_00031e18)


def parse_elf(e):
    assert e[:4] == b'\x7fELF' and e[4] == 1, 'not ELF32'
    e_type = struct.unpack_from('<H', e, 16)[0]
    assert e_type in (2, 3), 'loader accepts ET_EXEC/ET_DYN only'
    shoff, = struct.unpack_from('<I', e, 32)
    shnum, shstrndx = struct.unpack_from('<HH', e, 48)
    secs = [struct.unpack_from('<10I', e, shoff + 40 * i) for i in range(shnum)]
    stro = secs[shstrndx][4]
    names = [e[stro + s[0]:e.index(b'\0', stro + s[0])].decode() for s in secs]
    return secs, names


class Module:
    pass


class Emu:
    def __init__(self, elf_bytes=None, fill=0x00):
        self.fill = fill
        self.uc = uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        uc.ctl_set_cpu_model(UC_CPU_ARM_MAX)   # ARMv8 FP ops (vminnm/vrint) used by the modules
        for k, base in POOLS.items():
            uc.mem_map(base, POOL_SIZE[k])
        uc.mem_map(HOST_RAM, 0x10000)
        uc.mem_map(STACK - 0x10000, 0x10000)
        uc.mem_map(SCRATCH, 0x100000)
        uc.mem_map(0, 0x2000)
        uc.mem_map(0xE000E000, 0x1000)       # SCS (SysTick), read by modules that time themselves
        uc.mem_write(STOP, b'\xfe\xe7')
        # FPU on (A-profile 'max' core executes the same Thumb-2/VFPv5 code as the M7)
        uc.reg_write(UC_ARM_REG_C1_C0_2, uc.reg_read(UC_ARM_REG_C1_C0_2) | (0xf << 20))
        uc.reg_write(UC_ARM_REG_FPEXC, 0x40000000)
        self.used = {k: 0 for k in POOLS}
        self.modules = []
        if elf_bytes is not None:
            m = self.load(elf_bytes)
            self.e, self.id, self.init, self.process, self.nrel = elf_bytes, m.id, m.init, m.process, m.nrel

    def load(self, e):
        """Load one more module after the ones already in the pools (each region 8-byte aligned, like the
        app's allocator) and register its header in the host module table. Returns a Module."""
        uc, used = self.uc, self.used
        secs, names = parse_elf(e)
        base = {k: POOLS[k] + ((used[k] + 7) & ~7) for k in POOLS}
        for k in used:
            used[k] = base[k] - POOLS[k]
        # pass 1: place sections (same rules as FUN_000405c8)
        for n, s in zip(names, secs):
            sh_name, sh_type, flags, addr, off, size = s[:6]
            region = addr >> 28
            if sh_type == 1 and (((flags & 6) == 6) or flags == 2) and addr in (0, 0x10000000):
                dst = POOLS[region] + used[region]; uc.mem_write(dst, e[off:off + size]); used[region] += size
            elif sh_type == 1 and (flags & 3) == 3 and n[:7] in ('ER_IRAM', 'ER_SRAM'):
                dst = POOLS[region] + used[region]; uc.mem_write(dst, e[off:off + size]); used[region] += size
            elif sh_type == 8 and (flags & 3) == 3 and n[:7] in ('ER_IRAM', 'ER_SRAM'):
                dst = POOLS[region] + used[region]; uc.mem_write(dst, bytes([self.fill]) * size); used[region] += size
        for k in used:
            assert used[k] <= POOL_SIZE[k], 'pool %d overflow' % k
        # pass 2: relocations
        nrel = 0
        for n, s in zip(names, secs):
            if s[1] != 9: continue
            symoff = secs[s[6]][4]
            for j in range(s[5] // 8):
                r_off, r_info = struct.unpack_from('<II', e, s[4] + 8 * j)
                st_name, st_value, st_size, st_info, st_other, st_shndx = struct.unpack_from('<IIIBBH', e, symoff + 16 * (r_info >> 8))
                if st_shndx == 0: continue
                if (st_info & 0xf) not in (1, 2, 3): continue
                S = base[st_value >> 28] + (st_value & 0x0fffffff)
                P = base[r_off >> 28] + (r_off & 0x0fffffff)
                self.reloc(r_info & 0xff, P, S)
                nrel += 1
        hdr = struct.unpack_from('<10I', uc.mem_read(base[0], 40))
        assert hdr[9] == MAGIC, 'bad module magic %#x' % hdr[9]
        m = Module()
        m.header, m.id, m.init, m.process, m.nrel = base[0], hdr[5], hdr[7], hdr[8], nrel
        uc.mem_write(HOST_MODULES + 4 * len(self.modules), struct.pack('<I', m.header))
        self.modules.append(m)
        return m

    def reloc(self, t, P, S):
        uc = self.uc
        w = struct.unpack('<I', uc.mem_read(P, 4))[0]
        if t in (2, 0x17):
            w = (w + S) & 0xffffffff
        elif t == 3:
            w = (w + S - P) & 0xffffffff
        elif t in (0x15, 0x16):
            w = S
        elif t in (0x2f, 0x30):
            v = (S & 0xffff) if t == 0x2f else (S >> 16)
            hw1, hw2 = w & 0xffff, w >> 16
            hw1 = (hw1 & 0xfbf0) | ((v >> 12) & 0xf) | (((v >> 11) & 1) << 10)
            hw2 = (hw2 & 0x8f00) | (((v >> 8) & 7) << 12) | (v & 0xff)
            w = hw1 | (hw2 << 16)
        elif t == 0:
            return
        else:
            raise NotImplementedError('reloc type %d (module not supported by this harness)' % t)
        uc.mem_write(P, struct.pack('<I', w))

    def call(self, fn, *args, budget=50_000_000):
        uc = self.uc
        regs = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3]
        sp = STACK - 0x100
        stack_args = args[4:]
        for i, a in enumerate(args[:4]): uc.reg_write(regs[i], a & 0xffffffff)
        uc.mem_write(sp, b''.join(struct.pack('<I', a & 0xffffffff) for a in stack_args) or b'\0')
        uc.reg_write(UC_ARM_REG_SP, sp)
        uc.reg_write(UC_ARM_REG_LR, STOP | 1)
        uc.emu_start(fn | 1, STOP, count=budget)

    def run(self, knobs, audio, beat_ms=500, enabled=1, ctx=b''):
        """knobs: list of floats; audio: list of (L,R) frames; ctx: bytes the app puts at the Init ctx
        (Cab/IR/Clone get a flash pointer in ctx[0]). Returns processed frames."""
        uc = self.uc
        K, BUF, AUX, EN, BEAT, EXTRA, CTX, X1, X2 = (SCRATCH + o for o in
                                                    (0x0, 0x1000, 0x2000, 0x3000, 0x3010, 0x3020, 0x3100, 0x4000, 0x5000))
        uc.mem_write(K, struct.pack('<%df' % len(knobs), *knobs))
        uc.mem_write(EN, bytes([enabled]))
        uc.mem_write(BEAT, struct.pack('<I', beat_ms))
        uc.mem_write(EXTRA, struct.pack('<II', X1, X2))
        uc.mem_write(CTX, bytes(ctx).ljust(64, b'\0'))
        self.call(self.init, K, CTX)
        out = []
        for b in range(0, len(audio), BLOCK):
            blk = audio[b:b + BLOCK]
            blk += [(0.0, 0.0)] * (BLOCK - len(blk))
            uc.mem_write(BUF, struct.pack('<%df' % (2 * BLOCK), *[x for fr in blk for x in fr]))
            uc.mem_write(AUX, b'\0' * (8 * BLOCK))
            # Process(io, (u16)nframes, knobs, aux, &enabled, &beat_ms, extra)
            self.call(self.process, BUF, BLOCK, K, AUX, EN, BEAT, EXTRA)
            v = struct.unpack('<%df' % (2 * BLOCK), uc.mem_read(BUF, 8 * BLOCK))
            out += list(zip(v[0::2], v[1::2]))
        return out[:len(audio)]


    def run_chain(self, slots, audio, beat_ms=500):
        """slots: list of (Module, knobs, enabled) processed in order on one shared io buffer, the way
        the audio loop FUN_00031e18 does (host block toggle flipped before each block)."""
        uc = self.uc
        BUF, AUX, BEAT, EXTRA, X1, X2 = (SCRATCH + o for o in (0x1000, 0x2000, 0x3010, 0x3020, 0x4000, 0x5000))
        per = []
        for i, (m, knobs, en) in enumerate(slots):
            K, EN, CTX = SCRATCH + 0x10000 + 0x100 * i, SCRATCH + 0x20000 + 0x10 * i, SCRATCH + 0x30000 + 0x100 * i
            uc.mem_write(K, struct.pack('<%df' % len(knobs), *knobs))
            uc.mem_write(EN, bytes([en]))
            uc.mem_write(CTX, b'\0' * 64)
            per.append((m, K, EN))
        uc.mem_write(BEAT, struct.pack('<I', beat_ms))
        uc.mem_write(EXTRA, struct.pack('<II', X1, X2))
        for (m, K, EN), i in zip(per, range(len(per))):
            self.call(m.init, K, SCRATCH + 0x30000 + 0x100 * i)
        out, toggle = [], 0
        for b in range(0, len(audio), BLOCK):
            toggle ^= 1
            uc.mem_write(HOST_TOGGLE, bytes([toggle]))
            blk = audio[b:b + BLOCK]
            blk += [(0.0, 0.0)] * (BLOCK - len(blk))
            uc.mem_write(BUF, struct.pack('<%df' % (2 * BLOCK), *[x for fr in blk for x in fr]))
            uc.mem_write(AUX, struct.pack('<%df' % (2 * BLOCK), *[x for fr in blk for x in fr]))
            for m, K, EN in per:
                self.call(m.process, BUF, BLOCK, K, AUX, EN, BEAT, EXTRA)
            v = struct.unpack('<%df' % (2 * BLOCK), uc.mem_read(BUF, 8 * BLOCK))
            out += list(zip(v[0::2], v[1::2]))
        return out[:len(audio)]


def test_signal(n, fs=44100):
    """impulse at 0, then a 220 Hz burst from 0.25 s for 50 ms, silence after"""
    sig = []
    for i in range(n):
        x = 1.0 if i == 0 else 0.0
        if fs // 4 <= i < fs // 4 + fs // 20:
            x = 0.5 * math.sin(2 * math.pi * 220 * i / fs)
        sig.append((x, x))
    return sig


if __name__ == '__main__':
    e = Emu(open(sys.argv[1], 'rb').read())
    knobs = [float(x) for x in sys.argv[2:]] or [50.0] * 12
    knobs += [0.0] * (16 - len(knobs))
    print('id=%#010x init=%#x process=%#x relocs=%d pools=%s' % (
        e.id, e.init, e.process, e.nrel, {k: hex(v) for k, v in e.used.items()}))
    fs = 44100
    sig = test_signal(fs)  # 1 s
    out = e.run(knobs, sig)
    def rms(a, b):
        seg = out[a:b]; return math.sqrt(sum(l * l + r * r for l, r in seg) / (2 * max(1, len(seg))))
    for t0 in (0, 0.05, 0.25, 0.3, 0.5, 0.75, 0.95):
        a = int(t0 * fs); print('  t=%.2fs rms=%.5f' % (t0, rms(a, a + fs // 20)))
    peak = max(range(len(out)), key=lambda i: abs(out[i][0]))
    print('  first-out', ['%.4f/%.4f' % fr for fr in out[:3]], 'peak@', peak)


