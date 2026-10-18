"""Valeton GP-200 effect library (FRMW section 1): pre-linked module images.

Library header: +00 ?, +04 0x18+4*ncat, +08 index size, +0C total, +10 1, +14 ncat, +18 list offsets
(same index layout as the QME-200). Each entry is the module's ER_IROM image, already linked to the
fixed per-menu addresses of the app (table at 0x2025e168 in GP-200 V1.8.2):

  +00 0 (host writes 0xFFAA0000|index)   +04 total size   +08 code offset   +0C code size
  +10 0x34 (offset of the metadata)      +14 id           +18 SROM base     +1C Init   +20 Process
  +24 0xFFAA55AA                         +28 12-byte stub (movw/movt r0; bx lr)
  +34 id  +38 name_off  +3C category_off  +40 link_id  +44 u8 nparams, u8 flags[3]  +48 params_off
  params: 24 bytes {u32 name_off, f32 min, max, step, default, u8 idx, f1, f2, f3}   (offsets entry-relative)
  [code_off, code_off+code_size) -> copied to the menu's ITCM code address
  [code_off+code_size, total)    -> copied to the menu's SRAM address (const data)

usage: python -m matribox gp200 <GP-200 fw> [outdir]
"""
import json, os, struct, sys

# per-menu fixed memory (GP-200 V1.8.2, app table 0x2025e168): code, code_cap, iram, iram_size, sram, sram_size
MENU_MEM = [
    (0x33200, 0x4000, 0x20204000, 0xe400, 0x815ba000, 0x56400),
    (0x37200, 0x2000, 0x20212400, 0x4400, 0x81610400, 0x0),
    (0x39200, 0x5400, 0x20216800, 0x22800, 0x81610400, 0x23000),
    (0x3e600, 0x27c00, 0x20239000, 0x0, 0x81633400, 0x23000),
    (0x66200, 0x2000, 0x20239000, 0x4000, 0x81656400, 0x0),
    (0x68200, 0x3400, 0x2023d000, 0x11800, 0x81656400, 0x1d800),
    (0x6b600, 0x2800, 0x2024e800, 0x400, 0x81673c00, 0x0),
    (0x6de00, 0x4000, 0x2024ec00, 0xe400, 0x81673c00, 0x56400),
    (0x71e00, 0x3000, 0x2025d000, 0xc00, 0x816ca000, 0x409c00),
    (0x74e00, 0x3000, 0x2025dc00, 0x400, 0x81ad3c00, 0x96000),
    (0x77e00, 0x200, 0x2025e000, 0x0, 0x81b69c00, 0x0),
]


def u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def cstr(b, o):
    return b[o:b.index(b'\0', o)].decode('latin1')


def library(fw):
    assert fw[:4] == b'FRMW'
    off, size = struct.unpack_from('<II', fw, 0x38)
    return fw[0x30 + off:0x30 + off + size]


def parse_entry(raw):
    total, code_off, code_size, meta = struct.unpack_from('<4I', raw, 4)
    eid, srom, init, process, magic = struct.unpack_from('<5I', raw, 0x14)
    assert magic == 0xFFAA55AA and total == len(raw)
    id2, name_o, cat_o, link = struct.unpack_from('<IIIi', raw, 0x34)
    nparams = raw[0x44]
    params_o = u32(raw, 0x48)
    params = []
    for i in range(nparams):
        po, mn, mx, st, df, idx, f1, f2, f3 = struct.unpack_from('<I4f4B', raw, params_o + 24 * i)
        params.append(dict(name=cstr(raw, po), min=mn, max=mx, step=st, default=df, idx=idx, flags=[f1, f2, f3]))
    return dict(id=eid, id2=id2, name=cstr(raw, name_o), category=cstr(raw, cat_o), link_id=link,
                hflags=list(raw[0x45:0x48]), srom=srom, init=init, process=process,
                code_off=code_off, code_size=code_size, const_size=total - code_off - code_size,
                total=total, params=params)


def entries(lib):
    ncat = u32(lib, 0x14)
    out = []
    for c in range(ncat):
        lo = u32(lib, 0x18 + 4 * c)
        for k in range(u32(lib, lo)):
            eo = u32(lib, lo + 4 + 4 * k)
            raw = lib[eo:eo + u32(lib, eo + 4)]
            e = parse_entry(raw)
            e.update(menu=c, index=k, lib_off=eo)
            out.append((e, raw))
    return out


def main():
    fw = open(sys.argv[1], 'rb').read()
    lib = library(fw)
    print('library %#x bytes, hdr %s' % (len(lib), [hex(u32(lib, o)) for o in range(0, 0x18, 4)]))
    es = entries(lib)
    for e, raw in es:
        print('%2d.%-2d id=%08x %-18s [%-10s] code=%#06x const=%#06x init=%#x proc=%#x srom=%#x  %s' % (
            e['menu'], e['index'], e['id'], e['name'], e['category'], e['code_size'], e['const_size'],
            e['init'], e['process'], e['srom'], ', '.join(p['name'] for p in e['params'])))
    if len(sys.argv) > 2:
        out = sys.argv[2]
        os.makedirs(out, exist_ok=True)
        for e, raw in es:
            fn = '%02d_%02d_%s.bin' % (e['menu'], e['index'], ''.join(c if c.isalnum() else '_' for c in e['name']))
            open(os.path.join(out, fn), 'wb').write(raw)
        json.dump([e for e, _ in es], open(os.path.join(out, 'effects.json'), 'w'), indent=1)


if __name__ == '__main__':
    main()
