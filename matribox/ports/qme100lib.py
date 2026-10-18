"""Sonicake Matribox II (QME-100) USB firmware: uncompressed 'HTFW' container + pre-linked effect library.

Container (QME-100 V1.3.0):
  +00 'HTFW'  +04 ?  +08 file size  +0C model "QME-100"  +1C 'V',1,3,0  +24 payload size
  +3C 4 x {u32 flash address, u32 offset, u32 size, u32 tag}   (offsets from file 0x78)
  sec0 app (flash 0x20000: same header + Keil ITCM/SDRAM layout as the QME-200 app)
  sec1 cab IRs (0x700000)   sec2 factory presets (0x4e0000)   sec3 algorithm library (0x500000)
Library: same index as the QME-200/GP-200 (12 menus) and the same pre-linked entry layout as the GP-200
(matribox/ports/gp200lib.py), except that the const data (ER_SROM) stays inside the code blob (const tail = 0).

usage: python -m matribox qme100 <QME-100 fw> [outdir]
"""
import struct, sys

DATA = 0x78

# per-menu fixed memory (QME-100 V1.3.0, app table 0x20258850): code, code_cap, iram, iram_size, sram, sram_size
# menus: 0/1 pre-FX (two slots, same list), 2 amp, 3 SND, 4 RTN, 5 NR, 6 CAB IR, 7 EQ, 8 mod, 9 delay,
# 10 reverb, 11 volume
MENU_MEM = [
    (0x4a600, 0xa400, 0x20204000, 0x12c00, 0x81497000, 0x23000),
    (0x54a00, 0xa400, 0x20216c00, 0x12c00, 0x814ba000, 0x23000),
    (0x5ee00, 0x5400, 0x20229800, 0x12c00, 0x814dd000, 0x23000),
    (0x64200, 0x1000, 0x2023c400, 0x800, 0x81500000, 0x0),
    (0x65200, 0x1000, 0x2023cc00, 0x800, 0x81500000, 0x0),
    (0x66200, 0x2000, 0x2023d400, 0x400, 0x81500000, 0x0),
    (0x68200, 0x3400, 0x2023d800, 0x11800, 0x81500000, 0x4e000),
    (0x6b600, 0x2800, 0x2024f000, 0x400, 0x8154e000, 0x0),
    (0x6de00, 0x4000, 0x2024f400, 0x8400, 0x8154e000, 0x0),
    (0x71e00, 0x3000, 0x20257800, 0xc00, 0x8154e000, 0x480000),
    (0x74e00, 0x3000, 0x20258400, 0x400, 0x819ce000, 0x7d400),
    (0x77e00, 0x200, 0x20258800, 0x0, 0x81a4b400, 0x0),
]


def sections(fw):
    assert fw[:4] == b'HTFW' and fw[0x0c:0x13] == b'QME-100'
    out = []
    for i in range(4):
        flash, off, size, tag = struct.unpack_from('<4I', fw, 0x3c + 16 * i)
        out.append((flash, DATA + off, size, tag))
    return out


def library(fw):
    flash, off, size, tag = sections(fw)[3]
    return fw[off:off + size]


def app(fw):
    """Section 0 at QME-200 file offsets (header at 0x60, ITCM at 0x78), for matribox/firmware/mkappelf.py."""
    flash, off, size, tag = sections(fw)[0]
    return b'\0' * 0x50 + fw[off:off + size]


def main():
    from matribox.ports import gp200lib
    fw = open(sys.argv[1], 'rb').read()
    for i, (flash, off, size, tag) in enumerate(sections(fw)):
        print('sec%d flash=%#08x file=%#08x size=%#08x tag=%#010x' % (i, flash, off, size, tag))
    for e, raw in gp200lib.entries(library(fw)):
        print('%2d.%-2d id=%08x %-16s [%-11s] code=%#06x init=%#x proc=%#x srom=%#x  %s' % (
            e['menu'], e['index'], e['id'], e['name'], e['category'], e['code_size'], e['init'],
            e['process'], e['srom'], ', '.join(p['name'] for p in e['params'])))
    if len(sys.argv) > 2:
        import os
        from matribox.firmware import mkappelf
        os.makedirs(sys.argv[2], exist_ok=True)
        a = app(fw)
        segs, zero, hdr = mkappelf.build(a)
        mkappelf.write_elf(os.path.join(sys.argv[2], 'app.elf'), segs, zero, struct.unpack_from('<I', a, 0x7c)[0])


if __name__ == '__main__':
    main()
