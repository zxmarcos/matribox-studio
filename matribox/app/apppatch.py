"""Patch the original app (MCU section of V1.2.2) with C code placed in its free ITCM.

  apppatch.py [--features=parallel,textlist] [base.bin] [out.bin]

Features (each one = C files in patches/ + its hooks; any combination can be built):
  parallel  Split / Path B / Merge drawn as two branches (parallel_ui.c)
  textlist  parameters shown as a list of texts from the effect library (text_list.c)
  clonedual Clone menu with the factory engine and NAM A2, chosen per slot by its data (clone_dual.c;
            the NAM entry 11.1 is added by examples/nam_clone/build_clone.py --dual)
  clonetabs Clone menu tabs "Native" / "NAM A2" next to ALL (clone_tabs.c; needs clonedual)
Default: parallel (base = firmware with the routing modules, out = ..._parallel_ui.bin).

The selected sources -> <build>/apppatch/patch.elf, linked at 0x53cc0 (the ITCM after the original image, below
the effect module pool at 0x58000). The ITCM image of the MCU section grows to hold it, and the hooks are
written into the original code. The other sections (DSP, IR, Bank) come from the base firmware.
"""
import os, sys, struct, glob, subprocess
from matribox import paths
from matribox.firmware.fwtool import Firmware
from matribox.toolchain import ZIG, APP_CFLAGS as CFLAGS, FLASH_BASE

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'patches')
OBJ = paths.build_path('apppatch')
ORIGINAL = paths.original_firmware()                                         # source of the unpatched app
BASE = paths.build_path('QME-200_FW_USB_V1.2.2_parallel.bin')
OUT = paths.build_path('QME-200_FW_USB_V1.2.2_parallel_ui.bin')
PATCH_AT = 0x53cc0
ITCM_END = 0x58000

# hooks: (address, original bytes, symbol to branch to): b.w written over the original instructions
# pointers: (address, original word, symbol): code pointers in the SDRAM image (literal pools) replaced by ours
FEATURES = {
    'parallel': dict(
        sources=['parallel_ui.c'],
        hooks=[(0xfec6, bytes.fromhex('70b51446'), 'setpos_hook')],     # WM_SetWindowPos: push {r4-r6,lr}; mov r4, r2
        pointers=[(0x8000af98, 0x80018361, 'main_cb'),                  # GUI_CreateDialogBox callback, main screen
                  (0x800037c4, 0x800159b5, 'edit_cb')]),                # same, chain edit screen
    'textlist': dict(
        sources=['text_list.c'],
        hooks=[(0x21f94, bytes.fromhex('2de9f043'), 'fmt_hook')],       # FUN_00021f94: push.w {r4-r9, lr}
        pointers=[]),
    'clonedual': dict(
        sources=['clone_dual.c'],
        hooks=[(0x1b538, bytes.fromhex('144b10b5'), 'clone_stage_hook'),     # ldr r3, [pc, #0x50]; push {r4, lr}
               (0x1b4f4, bytes.fromhex('84460f48'), 'clone_insert_hook'),    # mov ip, r0; ldr r0, [pc, #0x3c]
               (0x1b598, bytes.fromhex('84460f48'), 'clone_replace_hook')],
        pointers=[],
        # after a Clone slot is written (0x3960c) or cleared (0x2d35a), reload the chain (mode 3: copies the
        # modules again) instead of re-running Init only (mode 2), so the slot's engine follows its new data
        pokes=[(0x3960c, bytes.fromhex('0220'), bytes.fromhex('0320')),     # movs r0, #2 -> #3; bl FUN_00020424
               (0x2d35a, bytes.fromhex('0220'), bytes.fromhex('0320'))]),
    'clonetabs': dict(
        sources=['clone_tabs.c'],
        requires=['clonedual'],
        hooks=[(0x1ebd2, bytes.fromhex('052819bf'), 'clone_subgroup'),      # cmp r0, #5; ittee ne
               (0x270c8, bytes.fromhex('024901eb'), 'clone_tab_count'),     # ldr r1, [pc, #8]; add.w ...
               (0x1eb96, bytes.fromhex('30b40528'), 'clone_tabname_hook')], # push {r4, r5}; cmp r0, #5
        pointers=[],
        # the effect-select screen leaves menus 11-15 with the ALL tab only (sub #0xb; cmp #5; blo):
        # sub #0xc; cmp #4 leaves out 12-15 only
        pokes=[(0x8000157a, bytes.fromhex('a0f10b000528'), bytes.fromhex('a0f10c000428')),  # opening the screen
               (0x800019b4, bytes.fromhex('a0f10b010529'), bytes.fromhex('a0f10c010429')),  # tab +1 (encoder)
               (0x80001a92, bytes.fromhex('a0f10b010529'), bytes.fromhex('a0f10c010429')),  # tab -1 (encoder)
               (0x80001d5a, bytes.fromhex('a5f10b000528'), bytes.fromhex('a5f10c000428')),  # sync with the block
               (0x80000fb0, bytes.fromhex('a6f10b08'), bytes.fromhex('a6f10c08')),          # tab names: subne.w
               (0x80000fd4, bytes.fromhex('b8f1050f'), bytes.fromhex('b8f1040f'))]),        #   cmp.w r8, #5
}
DEFAULT_FEATURES = ['parallel']


def run(cmd):
    """like buildpoc.run, but a failure raises (the Studio calls this from a thread)"""
    if not os.path.exists(ZIG):
        raise RuntimeError('Zig não encontrado em %s' % ZIG)
    r = subprocess.run(cmd, capture_output=True, text=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if r.returncode or r.stderr.strip():
        print(r.stdout + r.stderr)
    if r.returncode:
        raise RuntimeError('falhou: %s\n%s' % (os.path.basename(cmd[-1]), (r.stdout + r.stderr).strip()))


def default_name(features):
    """file name suffix used for a build with these features"""
    return '_parallel_ui' if features == ['parallel'] else '_' + '_'.join(features)


def build(features):
    objs = []
    os.makedirs(OBJ, exist_ok=True)
    for src in sorted({s for f in features for s in FEATURES[f]['sources']}):
        obj = os.path.join(OBJ, src[:-2] + '.o')
        run([ZIG, 'cc', *CFLAGS, '-c', os.path.join(SRC, src), '-o', obj])
        objs.append(obj)
    elf = os.path.join(OBJ, 'patch.elf')
    run([ZIG, 'ld.lld', '-T', os.path.join(SRC, 'link.ld'), '--gc-sections', '-o', elf, *objs])
    e = open(elf, 'rb').read()
    phoff, = struct.unpack_from('<I', e, 28)
    n, = struct.unpack_from('<H', e, 44)
    blob = bytearray()
    for i in range(n):
        typ, off, va, pa, fs, ms, fl, al = struct.unpack_from('<8I', e, phoff + 32 * i)
        if typ == 1 and fs:
            assert PATCH_AT <= pa and pa + fs <= ITCM_END, hex(pa)
            end = pa - PATCH_AT + fs
            blob.extend(b'\0' * max(0, end - len(blob)))
            blob[pa - PATCH_AT:end] = e[off:off + fs]
    return bytes(blob), symbols(e)


def symbols(e):
    shoff, = struct.unpack_from('<I', e, 32)
    shentsize, shnum = struct.unpack_from('<HH', e, 46)
    secs = [struct.unpack_from('<10I', e, shoff + shentsize * i) for i in range(shnum)]
    syms = {}
    for s in secs:
        if s[1] != 2:                               # SHT_SYMTAB
            continue
        strtab = secs[s[6]]
        for k in range(s[5] // 16):
            name, value, size, info, other, shndx = struct.unpack_from('<IIIBBH', e, s[4] + 16 * k)
            if name:
                nm = e[strtab[4] + name:e.index(b'\0', strtab[4] + name)].decode()
                syms[nm] = value
    return syms


def b_w(src, dst):
    """Thumb-2 b.w (T4) at src to dst"""
    off = dst - (src + 4)
    assert -(1 << 24) <= off < (1 << 24) and not off & 1
    s = (off >> 24) & 1
    i1, i2 = (off >> 23) & 1, (off >> 22) & 1
    j1, j2 = (~(i1 ^ s)) & 1, (~(i2 ^ s)) & 1
    hw1 = 0xf000 | (s << 10) | ((off >> 12) & 0x3ff)
    hw2 = 0x9000 | (j1 << 13) | (j2 << 11) | ((off >> 1) & 0x7ff)
    return struct.pack('<HH', hw1, hw2)


def split_mcu(sec):
    if sec[:8] != b'\xff' * 8 or sec[8:12] != b'V122':
        raise ValueError('o app não é o V1.2.2 (versão %r)' % bytes(sec[8:12]))
    f0, n0, d0, f1, n1, d1 = struct.unpack_from('<6I', sec, 0x10)
    if (f0, d0, d1) != (FLASH_BASE + 0x28, 0, 0x80000000) or f1 != f0 + n0:
        raise ValueError('layout da seção MCU desconhecido')
    return sec[:0x10], bytearray(sec[0x28:0x28 + n0]), sec[0x28 + n0:0x28 + n0 + n1]


def patch(data, features, original=None):
    """firmware image `data` with its app patched by `features` -> (image, info). When the app of `data` is
    already patched, the unpatched app comes from `original` (an image, default the V1.2.2 file); the DSP, IR
    and Bank sections always come from `data`."""
    bad = [f for f in features if f not in FEATURES]
    if not features or bad:
        raise ValueError('features: %s' % ', '.join(FEATURES))
    for f in features:
        missing = [r for r in FEATURES[f].get('requires', []) if r not in features]
        if missing:
            raise ValueError('a feature %s precisa de %s' % (f, ', '.join(missing)))
    HOOKS = [h for f in features for h in FEATURES[f]['hooks']]
    POINTERS = [p for f in features for p in FEATURES[f]['pointers']]
    POKES = [p for f in features for p in FEATURES[f].get('pokes', [])]
    fw = Firmware(data)
    head, itcm, sdram = split_mcu(fw.sections[0])
    restarted = len(itcm) > PATCH_AT        # app already patched (e.g. a ..._parallel_ui base): start over from
    if restarted:                           # the original app
        if original is None:
            original = open(ORIGINAL, 'rb').read()
        head, itcm, sdram = split_mcu(Firmware(original).sections[0])
        if len(itcm) > PATCH_AT:
            raise ValueError('o firmware de onde viria o app original também está patcheado')
    blob, syms = build(features)
    for addr, orig, sym in HOOKS:
        if itcm[addr:addr + len(orig)] != orig:
            raise ValueError('código inesperado em %#x' % addr)
        itcm[addr:addr + 4] = b_w(addr, syms[sym] & ~1)
    sdram = bytearray(sdram)
    for addr, orig, new in POKES:
        img, o = (sdram, addr - 0x80000000) if addr >= 0x80000000 else (itcm, addr)
        if img[o:o + len(orig)] != orig:
            raise ValueError('código inesperado em %#x' % addr)
        img[o:o + len(new)] = new
    for addr, orig, sym in POINTERS:
        o = addr - 0x80000000
        if struct.unpack_from('<I', sdram, o)[0] != orig:
            raise ValueError('ponteiro inesperado em %#x' % addr)
        assert syms[sym] & 1, sym                       # Thumb function
        struct.pack_into('<I', sdram, o, syms[sym])
    itcm += b'\0' * (PATCH_AT - len(itcm)) + blob
    sec = head + struct.pack('<6I', FLASH_BASE + 0x28, len(itcm), 0,
                             FLASH_BASE + 0x28 + len(itcm), len(sdram), 0x80000000) + itcm + sdram
    sec += b'\0' * (-len(sec) % 0x80)
    fw.sections[0] = bytes(sec)
    out = fw.pack()
    info = dict(blob=len(blob), mcu=len(fw.sections[0]), restarted=restarted,
                hooks=['%#x -> %s %#x' % (a, s, syms[s]) for a, o, s in HOOKS + POINTERS])
    return out, info


def main():
    features = DEFAULT_FEATURES
    args = []
    for a in sys.argv[1:]:
        if a.startswith('--features='):
            features = [f for f in a[11:].split(',') if f]
            bad = [f for f in features if f not in FEATURES]
            assert features and not bad, 'features: %s' % ', '.join(FEATURES)
        else:
            args.append(a)
    base = args[0] if len(args) > 0 else BASE if 'parallel' in features else ORIGINAL
    out = args[1] if len(args) > 1 else paths.build_path('QME-200_FW_USB_V1.2.2%s.bin' % default_name(features))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    data, info = patch(open(base, 'rb').read(), features)
    if info['restarted']:
        print('base com app ja patcheado: app original de %s' % os.path.basename(ORIGINAL))
    open(out, 'wb').write(data)
    ok = data[16:23] == b'QME-200' and data[:4] == b'FRMW' and sum(data[0x30:]) & 0xff == data[0x0e]
    print('patch [%s]: %d bytes at %#x-%#x (livre ate %#x); ganchos: %s' % (
        ', '.join(features), info['blob'], PATCH_AT, PATCH_AT + info['blob'], ITCM_END, ', '.join(info['hooks'])))
    print('%s: %d bytes, MCU %d bytes, checagens do editor %s' % (out, len(data), info['mcu'],
                                                                  'OK' if ok else 'FALHOU'))


if __name__ == '__main__':
    main()
