"""Build a Matribox II Pro effect module from C sources using Zig's bundled clang/lld.

  buildmod.py out.axf src1.c [src2.c ...]

Pipeline:
  1. zig cc  -> Thumb-2 / FPv5 objects (no movw/movt for addresses: literal pools only)
  2. zig ld.lld -T sdk/module.ld --emit-relocs  -> linked ELF that still carries relocations
  3. convert: keep the four ER_* sections; turn every absolute reference into an R_ARM_ABS32
     against a per-region STT_SECTION symbol with the in-region offset stored in place (the
     device loader computes *P += region_base); drop PC-relative relocations that stay inside
     one region; reject anything the device loader cannot handle.
"""
import os, sys, struct, subprocess
from matribox.paths import SDK_C as SDK
from matribox.toolchain import ZIG
CFLAGS = ['-target', 'thumb-freestanding-eabihf', '-mcpu=cortex_m7+no_movt+fp_armv8d16', '-mfloat-abi=hard',
          '-O2', '-ffreestanding', '-fno-pic', '-ffunction-sections', '-fdata-sections',
          '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-fno-exceptions',
          '-fno-stack-protector', '-fno-sanitize=all', '-Wall', '-I', SDK]
REGION_NAMES = ['ER_IROM', 'ER_SROM', 'ER_IRAM', 'ER_SRAM']
REGION_FLAGS = {'ER_IROM': 6, 'ER_SROM': 2, 'ER_IRAM': 3, 'ER_SRAM': 3}
MAX_ELF = 0x8000

R_ARM = {0: 'NONE', 2: 'ABS32', 3: 'REL32', 10: 'THM_CALL', 30: 'THM_JUMP24', 38: 'TARGET1',
         40: 'V4BX', 42: 'PREL31', 47: 'THM_MOVW_ABS_NC', 48: 'THM_MOVT_ABS', 51: 'THM_JUMP19',
         102: 'THM_JUMP11', 103: 'THM_JUMP8', 11: 'THM_PC8', 129: 'THM_ALU_ABS_G0_NC',
         21: 'GLOB_DAT', 22: 'JUMP_SLOT'}
PC_RELATIVE = {3, 10, 30, 42, 51, 102, 103, 11, 25, 26, 27, 28, 29, 41, 4, 5, 6, 7, 8, 9, 49, 50,
               52, 53, 54, 55, 56, 57, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92}
ABSOLUTE32 = {2, 38}


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit('command failed: %s\n%s%s' % (' '.join(cmd), r.stdout, r.stderr))
    if r.stderr.strip(): print(r.stderr.strip())


class Elf:
    def __init__(self, b):
        self.b = b
        self.shoff, = struct.unpack_from('<I', b, 32)
        shnum, shstrndx = struct.unpack_from('<HH', b, 48)
        self.secs = [list(struct.unpack_from('<10I', b, self.shoff + 40 * i)) for i in range(shnum)]
        so = self.secs[shstrndx][4]
        self.names = [b[so + s[0]:b.index(b'\0', so + s[0])].decode() for s in self.secs]
        self.byname = {n: i for i, n in enumerate(self.names)}
        st = self.byname['.symtab']
        sym, strs = self.secs[st], self.secs[self.secs[st][6]]
        self.syms = []
        for j in range(sym[5] // 16):
            nm, val, size, info, other, shndx = struct.unpack_from('<IIIBBH', b, sym[4] + 16 * j)
            self.syms.append((b[strs[4] + nm:b.index(b'\0', strs[4] + nm)].decode(), val, size, info, shndx))

    def data(self, i):
        s = self.secs[i]
        return bytearray(self.b[s[4]:s[4] + s[5]]) if s[1] != 8 else None


def convert(raw):
    e = Elf(raw)
    for n, val, size, info, shndx in e.syms[1:]:
        if shndx == 0 and n:
            sys.exit('undefined symbol %r (no libc/libm available on the device: implement it)' % n)
    # region index -> (name, section header, bytes)
    regions = {}
    for name in REGION_NAMES:
        if name in e.byname:
            i = e.byname[name]
            s = e.secs[i]
            if s[5] == 0: continue
            regions[name] = (i, s, e.data(i))
    allowed = {e.byname[n] for n in REGION_NAMES if n in e.byname}
    for i, (n, s) in enumerate(zip(e.names, e.secs)):
        if s[1] in (1, 8) and (s[2] & 2) and i not in allowed and s[5]:
            sys.exit('stray allocated section %s: map it in module.ld' % n)
    if 'ER_IROM' not in regions: sys.exit('no code')
    hdr = regions['ER_IROM'][2]
    if struct.unpack_from('<I', hdr, 0x24)[0] != 0xFFAA55AA:
        sys.exit('module header missing at ER_IROM+0 (use MB_MODULE)')

    def region_of(addr):
        r = addr >> 28
        name = REGION_NAMES[r] if r < 4 else None
        if name not in regions: return None
        s = regions[name][1]
        return name if s[3] <= addr <= s[3] + s[5] else None

    out_rel = []          # (offset, region_symbol_index)
    dropped = 0
    for i, (n, s) in enumerate(zip(e.names, e.secs)):
        if s[1] == 4: sys.exit('RELA relocations in %s not supported' % n)
        if s[1] != 9: continue
        target = e.names[s[7]]
        if target not in regions:
            continue                       # relocations for discarded/debug sections
        tsec = regions[target][1]
        tdata = regions[target][2]
        if tdata is None: sys.exit('relocation against NOBITS %s' % target)
        for j in range(s[5] // 8):
            P, info = struct.unpack_from('<II', e.b, s[4] + 8 * j)
            t, si = info & 0xff, info >> 8
            sym = e.syms[si]
            if t == 0 or t == 40: continue
            if t in ABSOLUTE32:
                o = P - tsec[3]
                T = struct.unpack_from('<I', tdata, o)[0]
                rname = region_of(T & ~1)
                if rname is None:
                    if sym[4] == 0xfff1: continue          # absolute symbol, nothing to relocate
                    sys.exit('ABS32 at %#x points outside the module (%#x, sym %s)' % (P, T, sym[0]))
                base = regions[rname][1][3] & 0xf0000000
                struct.pack_into('<I', tdata, o, T - base)
                out_rel.append((P, REGION_NAMES.index(rname)))
            elif t in PC_RELATIVE:
                src = region_of(P)
                S = sym[1]
                dst = region_of(S & ~1) if sym[4] not in (0, 0xfff1) else src
                if dst != src:
                    sys.exit('cross-region PC-relative %s at %#x -> %s (%s): not relocatable by the device'
                             % (R_ARM.get(t, t), P, sym[0], dst))
                dropped += 1
            else:
                sys.exit('unsupported relocation %s at %#x (sym %s) â€” compile with -mno-movt, no PIC'
                         % (R_ARM.get(t, t), P, sym[0]))

    # ---- write the device-format ELF ------------------------------------------------------
    order = [n for n in REGION_NAMES if n in regions]
    shstr = bytearray(b'\0')
    def sname(t):
        o = len(shstr); shstr.extend(t.encode() + b'\0'); return o
    body = bytearray()
    ehsize, phentsize = 52, 32
    loads = [n for n in order]
    data_start = ehsize + phentsize * len(loads)
    sh = [[0] * 10]
    ph = []
    sec_index = {}
    pos = data_start
    for n in order:
        i, s, d = regions[n]
        while (pos + len(body)) % 8: body.append(0)
        off = data_start + len(body)
        nobits = d is None
        if not nobits: body += d
        sec_index[n] = len(sh)
        sh.append([sname(n), 8 if nobits else 1, REGION_FLAGS[n], s[3], off, s[5], 0, 0, 8, 0])
        ph.append((1, off, s[3], s[3], 0 if nobits else s[5], s[5],
                   {6: 5, 2: 4, 3: 6}[REGION_FLAGS[n]], 8))
    # symtab: null + one STT_SECTION per region (value = region link base)
    strtab = b'\0'
    symtab = bytearray(16)
    region_sym = {}
    for n in order:
        region_sym[REGION_NAMES.index(n)] = len(symtab) // 16
        symtab += struct.pack('<IIIBBH', 0, regions[n][1][3], 0, 3, 0, sec_index[n])
    rel = b''.join(struct.pack('<II', P, (region_sym[r] << 8) | 2) for P, r in sorted(out_rel))
    def add(name, typ, data, link=0, info=0, align=4, entsize=0):
        while (data_start + len(body)) % 4: body.append(0)
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
    sh[shstri][5] = len(shstr)
    body[sh[shstri][4] - data_start:] = bytes(shstr)
    while (data_start + len(body)) % 4: body.append(0)
    shoff = data_start + len(body)
    entry = struct.unpack_from('<I', hdr, 0x20)[0]           # Process (keeps tools happy)
    ehdr = b'\x7fELF\x01\x01\x01' + b'\0' * 9 + struct.pack(
        '<HHIIIIIHHHHHH', 2, 40, 1, entry, ehsize, shoff, 0x05000400, ehsize, phentsize, len(ph),
        40, len(sh), shstri)
    out = bytearray(ehdr)
    for p in ph: out += struct.pack('<8I', *p)
    out += body
    for s in sh: out += struct.pack('<10I', *s)
    stats = {n: regions[n][1][5] for n in order}
    return bytes(out), stats, len(out_rel), dropped


def main():
    out, srcs = sys.argv[1], sys.argv[2:]
    bdir = os.path.join(os.path.dirname(os.path.abspath(out)), 'build')
    os.makedirs(bdir, exist_ok=True)
    objs = []
    for src in srcs + [os.path.join(SDK, 'mb_rt.c')]:
        o = os.path.join(bdir, os.path.splitext(os.path.basename(src))[0] + '.o')
        extra = ['-fno-builtin'] if src.endswith('mb_rt.c') else []
        # a source may ask for its own flags with a line "/* buildmod-cflags: -Os */" (they come after -O2)
        for line in open(src, encoding='latin-1'):
            if 'buildmod-cflags:' in line:
                extra += line.split('buildmod-cflags:')[1].replace('*/', '').split()
        run([ZIG, 'cc', '-c', src, '-o', o] + CFLAGS + extra + os.environ.get('MB_CFLAGS', '').split())
        objs.append(o)
    linked = os.path.join(bdir, 'linked.elf')
    run([ZIG, 'ld.lld', '-T', os.path.join(SDK, 'module.ld'), '--emit-relocs', '--gc-sections',
         '-static', '-o', linked] + objs)
    data, stats, nrel, dropped = convert(open(linked, 'rb').read())
    if len(data) > MAX_ELF:
        sys.exit('module is %d bytes; the device stages modules in 32 KB slots' % len(data))
    open(out, 'wb').write(data)
    print('%s: %d bytes, regions %s, %d ABS32 relocs (%d intra-region PC-rel dropped)' % (
        out, len(data), {k: hex(v) for k, v in stats.items()}, nrel, dropped))


if __name__ == '__main__':
    main()


