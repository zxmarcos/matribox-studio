"""Disassemble an extracted effect .axf (Thumb-2) with dynsym labels and reloc annotations."""
import sys, struct
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS

e = open(sys.argv[1], 'rb').read()
(e_type, e_machine, e_version, e_entry, e_phoff, e_shoff, e_flags, e_ehsize, e_phentsize, e_phnum,
 e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from('<HHIIIIIHHHHHH', e, 16)
secs = [struct.unpack_from('<10I', e, e_shoff + i * 40) for i in range(e_shnum)]
cstr = lambda o: e[o:e.index(b'\0', o)].decode('latin1')
names = [cstr(secs[e_shstrndx][4] + s[0]) for s in secs]
S = dict(zip(names, secs))
syms = {}
symlist = []
ds = S['.dynsym']; dstr = S['.dynstr']
for j in range(ds[5] // 16):
    nm, val, sz, inf, oth, shn = struct.unpack_from('<IIIBBH', e, ds[4] + j * 16)
    n = cstr(dstr[4] + nm)
    symlist.append((n, val))
    if n and (inf & 0xf) == 2: syms[val & ~1] = n
    elif n: syms.setdefault(val, n)
rels = {}
rd = S['.dyn']
for j in range(rd[5] // 8):
    off, info = struct.unpack_from('<II', e, rd[4] + j * 8)
    rels[off] = (info & 0xff, symlist[info >> 8][0] or hex(symlist[info >> 8][1]))
rom = S['ER_IROM']
code = e[rom[4]:rom[4] + rom[5]]
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
md.skipdata = True
start = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0
end = int(sys.argv[3], 16) if len(sys.argv) > 3 else len(code)
for ins in md.disasm(code[start:end], start):
    if ins.address in syms: print('\n%s:' % syms[ins.address])
    r = ''
    for k in range(ins.size):
        if ins.address + k in rels: r += '   ; R%d %s' % rels[ins.address + k]
    print('  %06x: %-24s %-8s %s%s' % (ins.address, ins.bytes.hex(), ins.mnemonic, ins.op_str, r))
