"""Convert a pre-linked Valeton GP-200 effect module into a relocatable Matribox (QME-200) module ELF.

GP-200 modules are the same Keil-built algorithms as the QME-200 ones, but linked to fixed per-menu
addresses (matribox/ports/gp200lib.py MENU_MEM). Measured on modules whose code is byte-identical in both
firmwares, the mapping is one constant per region:

  ER_IROM  VA v              <-> GP code_addr - 0x28 + v   (the 0x28-byte header stays in the entry)
  ER_SROM  VA 0x10000000 + v <-> GP sram_addr + v          (the entry's const tail is copied there)
  ER_IRAM  VA 0x20000000 + v <-> GP iram_addr + v
Menus without IRAM (3 = amps, 10) keep const data and state in ITCM: SROM lives inside the code blob
(header +18 points into it) and the state follows the code. Big buffers (ER_SRAM) follow the const
data in the menu's SDRAM window.

Delinking = finding every absolute reference: literal-pool words (ABS32, or REL32 when P + word lands
in another region), Thumb-2 MOVW/MOVT pairs, words in the const data, and the header's SROM/Init/Process.
Output relocations follow the device loader (matribox/module/effemu.py): ABS32/REL32 against a section symbol
with the target's in-section offset in place; MOVW/MOVT against one OBJECT symbol per target (the
loader uses the symbol value and ignores the addend, like the factory modules).

RAM sizes are not recorded by the GP-200 (each menu has a fixed window); they are estimated as
(highest referenced offset + slack) and capped by the menu window. Check them with --iram/--sram.

usage:
  gp2qme.py list  <GP-200 fw>                                  modules with their estimated sizes
  gp2qme.py scan  <GP-200 fw> <name|menu.index>                list references found
  gp2qme.py check <GP-200 fw> <QME-200 fw>                     compare with QME relocations
  gp2qme.py conv  <GP-200 fw> <name|menu.index> <out.axf> [--iram HEX] [--sram HEX]
"""
import os, re, struct, sys
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS

from matribox.ports import gp200lib, qme100lib

HDR = 0x28
IROM, SROM, IRAM, SRAM = 0, 1, 2, 3
REGION_NAMES = ['ER_IROM', 'ER_SROM', 'ER_IRAM', 'ER_SRAM']
REGION_FLAGS = {IROM: 6, SROM: 2, IRAM: 3, SRAM: 3}
LIT_RE = re.compile(r'\[pc(?:, #(-?(?:0x[0-9a-f]+|\d+)))?\]$')
REGS = ['r0', 'r1', 'r2', 'r3', 'r4', 'r5', 'r6', 'r7', 'r8', 'sb', 'sl', 'fp', 'ip', 'sp', 'lr', 'pc']
SLACK = {IRAM: 0x100, SRAM: 0x1000}      # beyond the highest referenced offset (see 'list')
MAX_ELF = 0x8000


def is_host_addr(v):
    """GP-200 app RAM (OCRAM data/BSS, SDRAM) outside any module window."""
    return 0x20200000 <= v < 0x20280000 or 0x80000000 <= v < 0x81c00000


def thm_imm16(hw1, hw2):
    return ((hw1 & 0xf) << 12) | (((hw1 >> 10) & 1) << 11) | (((hw2 >> 12) & 7) << 8) | (hw2 & 0xff)


def align(x, a=8):
    return (x + a - 1) & ~(a - 1)


class GpModule:
    def __init__(self, entry, raw, mem=gp200lib.MENU_MEM):
        self.e, self.raw, self.mem = entry, raw, mem
        self.menu = entry['menu']
        code_addr, code_cap, iram, iram_cap, sram, sram_cap = mem[self.menu]
        self.code_addr = code_addr
        self.iram_in_itcm = False
        self.code = raw[entry['code_off']:entry['code_off'] + entry['code_size']]
        self.const = raw[entry['code_off'] + entry['code_size']:]
        code_end = code_addr + len(self.code)
        # (region, gp_lo, gp_hi_inclusive, gp address of VA offset 0, menu limit for the size)
        self.regions = [(IROM, code_addr, code_end, code_addr - HDR, None)]
        if self.const:
            self.regions.append((SROM, sram, sram + len(self.const), sram, None))
        if iram_cap:
            self.regions.append((IRAM, iram, iram + iram_cap, iram, iram_cap))
        else:
            lo = align(code_end)
            self.iram_in_itcm = True
            self.regions.append((IRAM, lo, code_addr + code_cap, lo, code_addr + code_cap - lo))
        lo = sram + align(len(self.const))
        if sram_cap > lo - sram:
            self.regions.append((SRAM, lo, sram + sram_cap, lo, sram + sram_cap - lo))

    def classify(self, v):
        """GP absolute address -> (region, offset from the region's VA base) or None."""
        for r, lo, hi, base, _ in self.regions:
            if lo <= v < hi:
                return r, v - base
        for r, lo, hi, base, _ in self.regions:
            if v == hi and r in (IROM, SROM):      # one-past-the-end, when no region starts there
                return r, v - base
        return None

    def gp_addr(self, sec, off):
        """GP address of a fixup site (sec IROM: offset from VA 0; SROM: offset in const data)."""
        return self.code_addr - HDR + off if sec == IROM else self.mem[self.menu][4] + off

    def scan(self):
        """-> list of (type, sec, off, target_region, target_offset, gp_value). type: 2 ABS32, 3 REL32,
        47 MOVW, 48 MOVT; sec/off: where the fixup lives (IROM offset from VA 0, or SROM offset)."""
        code, base = self.code, self.code_addr
        md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
        md.detail = True
        self.external = set()          # references to GP-200 app memory/code: no QME equivalent
        self.warnings = set()          # look like app addresses but are probably constants
        # pass 1: linear sweep, collect PC-relative literal loads -> literal pool words
        lits = set()
        pcrel = {}                     # literal offset -> address of the 'add rX, pc' that consumes it
        loaded = {}                    # register -> (literal offset, position) of the last 'ldr rX, [pc]'
        p = 0
        while p < len(code):
            ins = next(md.disasm(code[p:p + 4], base + p, count=1), None)
            if ins is None:
                p += 2
                continue
            lm = LIT_RE.search(ins.op_str)
            if lm and ins.mnemonic.startswith(('ldr', 'vldr')):
                t = ((ins.address + 4) & ~3) + (int(lm.group(1), 0) if lm.group(1) else 0) - base
                wide = ins.mnemonic.startswith('ldrd') or (ins.mnemonic.startswith('vldr') and ins.op_str.startswith('d'))
                lits.update(range(t, t + (8 if wide else 4), 4))
                if ins.mnemonic in ('ldr', 'ldr.w'):
                    loaded[ins.op_str.split(',')[0]] = (t, p)
            elif ins.mnemonic in ('add', 'adds') and ins.op_str.endswith(', pc'):
                r = ins.op_str.split(',')[0]
                if r in loaded and p - loaded[r][1] < 0x40:
                    pcrel[loaded.pop(r)[0]] = ins.address
            p += ins.size
        # pass 2: pair MOVW/MOVT per destination register. Data decoded as 'ldr [pc]' can plant false
        # literal words that desync a sweep that skips them, so sweep both ways and merge.
        pairs, unresolved = {}, {}
        for skip in (True, False):
            pp, un = self._movw_pairs(md, lits if skip else ())
            for k, v in pp.items():
                pairs.setdefault(k, v)
            unresolved.update(un)
        refs = []
        for pt, (pw, v) in sorted(pairs.items()):
            c = self.classify(v)
            refs.append((47, IROM, pw + HDR) + c + (v,))
            refs.append((48, IROM, pt + HDR) + c + (v,))
        for pt, (rd, imm) in sorted(unresolved.items()):
            if pt not in pairs:
                print('  warning: %s: unresolved movt %s at %#x (imm %#x)' % (self.e['name'], REGS[rd], base + pt, imm))
        for o in sorted(lits):
            if o % 4 or o + 4 > len(code):
                continue
            v = struct.unpack_from('<I', code, o)[0]
            c = self.classify(v)
            if c:
                refs.append((2, IROM, o + HDR) + c + (v,))
                continue
            if 0x20200000 <= v < 0x20280000:
                self.external.add(('literal', base + o, v))
            elif is_host_addr(v):
                # mostly tiny negative floats (-0.0, anti-denormal constants); real pointers would
                # fault in the emulator, where the GP-200 SDRAM is not mapped
                self.warnings.add(('literal', base + o, v))
            if o in pcrel:
                # 'ldr rX, =v; add rX, pc': effective address is add+4+v; REL32 keeps S+A = P+v
                eff = (pcrel[o] + 4 + v) & 0xffffffff
                c = self.classify(eff)
                if c and c[0] != IROM:
                    refs.append((3, IROM, o + HDR, c[0], c[1] - (pcrel[o] + 4 - (base + o)), v))
            else:
                c = self.classify((base + o + v) & 0xffffffff)
                if c and c[0] in (SROM, SRAM) or (c and c[0] == IRAM and not self.iram_in_itcm):
                    refs.append((3, IROM, o + HDR) + c + (v,))
        for o in range(0, len(self.const) - 3, 4):
            v = struct.unpack_from('<I', self.const, o)[0]
            c = self.classify(v)
            if c and c[0] != IROM or (c and v & 1):
                refs.append((2, SROM, o) + c + (v,))
        for o in (0x18, 0x1c, 0x20):
            v = struct.unpack_from('<I', self.raw, o)[0]
            c = self.classify(v)
            if not c or v == self.code_addr + len(self.code):
                c = (IROM, len(self.code) + HDR)       # no SROM at all: point past the code, as Keil does
            refs.append((2, IROM, o) + c + (v,))
        return refs

    def _movw_pairs(self, md, lits):
        """Linear sweep -> ({movt_pos: (movw_pos, value)} for values inside the module,
        {movt_pos: (rd, imm)} for MOVTs that look like module addresses but have no MOVW)."""
        code, base = self.code, self.code_addr
        pairs, unresolved, pending = {}, {}, {}
        p = 0
        while p < len(code):
            if p in lits:
                p += 4
                continue
            ins = next(md.disasm(code[p:p + 4], base + p, count=1), None)
            if ins is None:
                p += 2
                continue
            m = ins.mnemonic
            if m in ('movw', 'movt') and ins.size == 4:
                hw1, hw2 = struct.unpack_from('<HH', code, p)
                rd, imm = (hw2 >> 8) & 0xf, thm_imm16(hw1, hw2)
                if m == 'movw':
                    pending[rd] = (p, imm)
                else:
                    lw = pending.pop(rd, None)
                    if lw and self.classify((imm << 16) | lw[1]):
                        pairs[p] = (lw[0], (imm << 16) | lw[1])
                    elif not lw and (self.classify(imm << 16) or self.classify((imm << 16) | 0xffff)):
                        unresolved[p] = (rd, imm)
                    elif lw and is_host_addr((imm << 16) | lw[1]) and lits:
                        self.external.add(('movw/movt', base + lw[0], (imm << 16) | lw[1]))
            elif m in ('bl', 'b.w', 'blx') and ins.op_str.startswith('#') and lits:
                t = int(ins.op_str[1:], 16)
                if not base <= t < base + len(code) and (t < 0x80000 or 0x80000000 <= t < 0x80400000):
                    self.warnings.add(('call', base + p, t))      # usually data decoded as code
            else:
                try:
                    written = ins.regs_access()[1]
                except Exception:
                    written = []
                for r in written:
                    n = ins.reg_name(r)
                    if n in REGS:
                        pending.pop(REGS.index(n), None)
            p += ins.size
        return pairs, unresolved

    def sizes(self, refs, iram=None, sram=None):
        top = {}
        for t, sec, off, r, toff, v in refs:
            top[r] = max(top.get(r, 0), toff)
        out = {}
        for r, lo, hi, base, limit in self.regions:
            if r in (IRAM, SRAM):
                want = {IRAM: iram, SRAM: sram}[r]
                if want is None:
                    if r not in top:
                        continue
                    want = align(top[r] + SLACK[r])
                    if limit is not None:
                        want = min(want, limit)
                out[r] = want
        return out, top

    # --- ELF writer -------------------------------------------------------------------------
    def to_elf(self, iram=None, sram=None):
        refs = self.scan()
        sizes, top = self.sizes(refs, iram, sram)
        irom = bytearray(self.raw[:HDR]) + bytearray(self.code)
        srom = bytearray(self.const)
        struct.pack_into('<I', irom, 0, 0)
        sec_data = {IROM: irom, SROM: srom}
        rels = []                    # (r_offset VA, type, symbol key)
        syms = {}                    # key -> (value VA, region)
        for t, sec, off, r, toff, v in refs:
            data = sec_data[sec]
            va_p = off + (0x10000000 if sec == SROM else 0)
            if t in (2, 3):
                struct.pack_into('<I', data, off, toff & 0xffffffff)
                key = ('sec', r)
                syms[key] = (r << 28, r)
            else:
                hw1, hw2 = struct.unpack_from('<HH', data, off)
                struct.pack_into('<HH', data, off, hw1 & 0xfbf0, hw2 & 0x8f00)   # imm16 = 0, like Keil
                key = ('obj', r, toff)
                syms[key] = ((r << 28) | toff, r)
            rels.append((va_p, t, key))
        for r in set(k[1] for k in syms):
            if r in (SROM,) and not srom:
                raise SystemExit('reference into SROM but the module has no const data')
            if r in (IRAM, SRAM) and r not in sizes:
                raise SystemExit('reference into %s without a size' % REGION_NAMES[r])
        if not srom and any(k[1] == SROM for k in syms):
            raise SystemExit('SROM reference without const data')
        regions = [(IROM, bytes(irom), len(irom))]
        if srom:
            regions.append((SROM, bytes(srom), len(srom)))
        for r in (IRAM, SRAM):
            if r in sizes:
                regions.append((r, None, sizes[r]))
        return write_elf(regions, rels, syms), sizes, top, len(rels)


def write_elf(regions, rels, syms):
    shstr = bytearray(b'\0')

    def sname(t):
        o = len(shstr)
        shstr.extend(t.encode() + b'\0')
        return o
    ehsize, phentsize = 52, 32
    data_start = ehsize + phentsize * len(regions)
    body = bytearray()
    sh = [[0] * 10]
    ph = []
    sec_index = {}
    for r, data, size in regions:
        while (data_start + len(body)) % 8:
            body.append(0)
        off = data_start + len(body)
        if data is not None:
            body += data
        sec_index[r] = len(sh)
        va = r << 28
        sh.append([sname(REGION_NAMES[r]), 1 if data is not None else 8, REGION_FLAGS[r], va, off, size, 0, 0, 8, 0])
        ph.append((1, off, va, va, len(data) if data is not None else 0, size, {6: 5, 2: 4, 3: 6}[REGION_FLAGS[r]], 8))
    strtab = bytearray(b'\0')
    symtab = bytearray(16)
    symidx = {}
    for key in sorted(syms, key=lambda k: (k[0] != 'sec', k[1:])):
        value, r = syms[key]
        if key[0] == 'sec':
            name, info = 0, 3                               # STT_SECTION, local
        else:
            name = len(strtab)
            strtab += ('%s_%x' % (REGION_NAMES[r][3:].lower(), key[2])).encode() + b'\0'
            info = 1                                        # STT_OBJECT, local
        symidx[key] = len(symtab) // 16
        symtab += struct.pack('<IIIBBH', name, value, 0, info, 0, sec_index[r])
    rel = b''.join(struct.pack('<II', P, (symidx[k] << 8) | t) for P, t, k in sorted(rels))

    def add(name, typ, data, link=0, info=0, align=4, entsize=0):
        while (data_start + len(body)) % 4:
            body.append(0)
        off = data_start + len(body)
        body.extend(data)
        sh.append([sname(name), typ, 0, 0, off, len(data), link, info, align, entsize])
        return len(sh) - 1
    symi = add('.symtab', 2, symtab, 0, len(symtab) // 16, 4, 16)
    stri = add('.strtab', 3, strtab, align=1)
    sh[symi][6] = stri
    add('.rel.dyn', 9, rel, symi, 0, 4, 8)
    shstr_name = sname('.shstrtab')
    shstri = add('.shstrtab', 3, bytes(shstr), align=1)
    sh[shstri][0] = shstr_name
    body[sh[shstri][4] - data_start:] = bytes(shstr)
    sh[shstri][5] = len(shstr)
    while (data_start + len(body)) % 4:
        body.append(0)
    shoff = data_start + len(body)
    entry = 0
    ehdr = b'\x7fELF\x01\x01\x01' + b'\0' * 9 + struct.pack(
        '<HHIIIIIHHHHHH', 2, 40, 1, entry, ehsize, shoff, 0x05000400, ehsize, phentsize, len(ph),
        40, len(sh), shstri)
    out = bytearray(ehdr)
    for p in ph:
        out += struct.pack('<8I', *p)
    out += body
    for s in sh:
        out += struct.pack('<10I', *s)
    return bytes(out)


def strip_names(elf):
    """Drop the OBJECT symbol names (.strtab -> one NUL): the device loader only reads st_value/st_info/
    st_shndx, like the unnamed symbols of matribox/module/buildmod.py (Mod Echo, confirmed on hardware). Saves
    ~10 bytes per MOVW/MOVT target in the library."""
    e = bytearray(elf)
    shoff, = struct.unpack_from('<I', e, 0x20)
    n, si = struct.unpack_from('<HH', e, 0x30)
    S = [list(struct.unpack_from('<10I', e, shoff + i * 40)) for i in range(n)]
    so = S[si][4]
    names = [bytes(e[so + s[0]:e.index(b'\0', so + s[0])]) for s in S]
    st = names.index(b'.strtab')
    sym = S[names.index(b'.symtab')]
    for o in range(sym[4], sym[4] + sym[5], 16):
        struct.pack_into('<I', e, o, 0)
    cut_lo, cut_hi = S[st][4] + 1, S[st][4] + S[st][5]
    cut = (cut_hi - cut_lo) & ~3                    # keep the 4-byte alignment of what follows
    if cut <= 0:
        return bytes(e)
    S[st][5] = 1
    for s in S:
        if s[4] >= cut_hi:
            s[4] -= cut
    shoff -= cut
    out = e[:cut_lo] + e[cut_lo + cut:]
    out = out[:shoff]
    struct.pack_into('<I', out, 0x20, shoff)
    for s in S:
        out += struct.pack('<10I', *s)
    return bytes(out)


def measure(m, blocks=None, verbose=False):
    """Run the module (converted with RAM sized to the menu window) in matribox/module/effemu.py with the knobs at
    default / max / min and return the highest IRAM/SRAM offsets it modified (needs unicorn).
    RAM is pre-filled with a sentinel byte and compared afterwards (two sentinels, max taken):
    unicorn memory hooks are not usable here, they break IT blocks around STM in the Keil memset."""
    import random
    from matribox.module import effemu
    lim = {r: l for r, lo, hi, base, l in m.regions if r in (IRAM, SRAM)}
    lim = {r: min(l, effemu.POOL_SIZE[r]) for r, l in lim.items()}
    elf, sizes, top, nrel = m.to_elf(lim.get(IRAM), lim.get(SRAM))
    if blocks is None:
        blocks = 400 if m.menu in (8, 9) else 60
    rnd = random.Random(1)
    audio = [(rnd.uniform(-.5, .5),) * 2 for _ in range(64 * blocks)]
    hit = {}
    for fill in (0xa5, 0x5a):
        emu = effemu.Emu(elf, fill=fill)
        for pick in ('default', 'max', 'min'):
            knobs = [0.0] * 16
            for p in m.e['params']:
                knobs[p['idx']] = p[pick]
            out = emu.run(knobs, audio)
            if verbose and fill == 0xa5:
                bad = sum(1 for l, r in out if l != l or r != r or abs(l) > 100 or abs(r) > 100)
                print('  %-7s knobs=%s  out rms=%.4f  bad=%d' % (
                    pick, ['%g' % k for k in knobs[:len(m.e['params'])]],
                    (sum(l * l for l, r in out) / len(out)) ** .5, bad))
        for r, l in lim.items():
            mem = bytes(emu.uc.mem_read(effemu.POOLS[r], l))
            stripped = mem.rstrip(bytes([fill]))
            hit[r] = max(hit.get(r, 0), len(stripped))
    return {r: v for r, v in hit.items() if v}


def all_modules(fw):
    if fw[:4] == b'HTFW':                         # Matribox II (QME-100): same entry layout, own windows
        return [GpModule(e, raw, qme100lib.MENU_MEM) for e, raw in gp200lib.entries(qme100lib.library(fw))]
    return [GpModule(e, raw) for e, raw in gp200lib.entries(gp200lib.library(fw))]


def find(fw, sel):
    for m in all_modules(fw):
        if sel in ('%d.%d' % (m.menu, m.e['index']), m.e['name']):
            return m
    raise SystemExit('no module %r' % sel)


def cmd_scan(fw, sel):
    m = find(fw, sel)
    refs = m.scan()
    print('%s menu %d code %#x+%#x const %#x' % (m.e['name'], m.menu, m.code_addr, len(m.code), len(m.const)))
    for t, sec, off, r, toff, v in refs:
        print('  t=%-2d %s+%#06x -> %s+%#x (%#010x)' % (t, REGION_NAMES[sec], off, REGION_NAMES[r], toff, v))
    sizes, top = m.sizes(refs)
    print('top offsets:', {REGION_NAMES[k]: hex(v) for k, v in top.items()},
          'sizes:', {REGION_NAMES[k]: hex(v) for k, v in sizes.items()})


def qme_info(elf):
    shoff, = struct.unpack_from('<I', elf, 0x20)
    n, si = struct.unpack_from('<HH', elf, 0x30)
    S = [struct.unpack_from('<10I', elf, shoff + i * 40) for i in range(n)]
    so = S[si][4]
    rels, sizes = set(), {}
    for s in S:
        nm = elf[so + s[0]:elf.index(b'\0', so + s[0])].decode()
        if nm in REGION_NAMES:
            sizes[REGION_NAMES.index(nm)] = s[5]
        if s[1] == 9:
            for j in range(s[5] // 8):
                off, info = struct.unpack_from('<II', elf, s[4] + 8 * j)
                rels.add((off, info & 0xff))
    return rels, sizes


def cmd_check(gpfw, qmfw):
    from matribox.firmware import fwtool
    lib = fwtool.Firmware(qmfw).library
    nok = 0
    for m in all_modules(gpfw):
        q = [x for x in lib.entries if x.id == m.e['id']]
        if not q:
            continue
        refs = m.scan()
        mine = {(off + (0x10000000 if sec == SROM else 0), t) for t, sec, off, r, toff, v in refs}
        theirs, qs = qme_info(q[0].elf)
        same_code = len(m.code) + HDR == qs.get(IROM)
        miss, extra = theirs - mine, mine - theirs
        ok = not miss and not extra
        nok += ok
        sizes, top = m.sizes(refs)
        print('%s %-15s/%-15s rel qme=%3d gp=%3d miss=%3d extra=%3d | IRAM qme=%6s top=%6s | SRAM qme=%8s top=%8s' % (
            'OK' if ok else ('  ' if not same_code else '!!'), m.e['name'], q[0].name, len(theirs), len(mine),
            len(miss), len(extra), hex(qs.get(IRAM, 0)), hex(top.get(IRAM, 0)), hex(qs.get(SRAM, 0)),
            hex(top.get(SRAM, 0))))
    print('exact matches:', nok)


def cmd_list(fw):
    for m in all_modules(fw):
        refs = m.scan()
        sizes, top = m.sizes(refs)
        print('%2d.%-2d %-18s code=%#06x const=%#06x IRAM top=%#7x -> %#7x  SRAM top=%#8x -> %#8x relocs=%d' % (
            m.menu, m.e['index'], m.e['name'], len(m.code), len(m.const), top.get(IRAM, 0), sizes.get(IRAM, 0),
            top.get(SRAM, 0), sizes.get(SRAM, 0), len(refs)))


def main():
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    fw = open(a[1], 'rb').read()
    if a[0] == 'scan':
        cmd_scan(fw, a[2])
    elif a[0] == 'check':
        cmd_check(fw, open(a[2], 'rb').read())
    elif a[0] == 'list':
        cmd_list(fw)
    elif a[0] == 'measure':
        m = find(fw, a[2])
        m.scan()
        print('external refs:', sorted(m.external) or 'none')
        hit = measure(m, int(a[3]) if len(a) > 3 else None, verbose=True)
        print('touched:', {REGION_NAMES[k]: hex(v) for k, v in hit.items()})
    elif a[0] == 'conv':
        opts = dict(zip(a[4::2], a[5::2]))
        m = find(fw, a[2])
        m.scan()
        if m.external:
            raise SystemExit('%s references GP-200 app memory/code: %s' % (
                m.e['name'], ', '.join('%s@%#x->%#x' % x for x in sorted(m.external))))
        iram = int(opts['--iram'], 16) if '--iram' in opts else None
        sram = int(opts['--sram'], 16) if '--sram' in opts else None
        if (iram is None or sram is None) and '--static' not in a:
            hit = measure(m)                       # Init clears its buffers: touched == section size
            refs = m.scan()
            _, top0 = m.sizes(refs)
            if iram is None and IRAM in hit:
                iram = align(max(hit[IRAM], top0.get(IRAM, 0) + 4), 0x10)
            if sram is None and SRAM in hit:
                sram = align(max(hit[SRAM], top0.get(SRAM, 0) + 4), 0x10)
        elf, sizes, top, nrel = m.to_elf(iram, sram)
        if len(elf) > MAX_ELF:
            raise SystemExit('module is %#x bytes; the device stages modules in 32 KB slots' % len(elf))
        open(a[3], 'wb').write(elf)
        print('%s: %d bytes, %d relocs, IRAM %#x SRAM %#x (top %s)' % (
            a[3], len(elf), nrel, sizes.get(IRAM, 0), sizes.get(SRAM, 0),
            {REGION_NAMES[k]: hex(v) for k, v in top.items()}))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()

