"""Rebuild the QME-200 main application as an ARM ELF (for Ghidra) from the USB firmware image.

Layout (from section-0 header at file 0x60 and the Keil __scatterload table):
  ITCM  load image  : file 0x78,     size hdr[0x64]  -> 0x00000000
  SDRAM load image  : follows ITCM,  size hdr[0x70]  -> hdr[0x74] (0x80000000)
  scatter regions   : copy / decompress / zero-init per region table
"""
import sys, struct

def decompress(src, so, n):
    """armlink __decompress (LZ77 variant) — port of the handler found at 0x8000003c."""
    out = bytearray()
    i = so
    r3 = src[i]; i += 1
    while True:
        r4 = r3 & 3
        if r4 == 0: r4 = src[i]; i += 1
        r5 = r3 >> 4
        if r5 == 0: r5 = src[i]; i += 1
        for _ in range(r4 - 1):
            out.append(src[i]); i += 1
        if r5:
            back = src[i]; i += 1
            if (r3 & 0xc) == 0xc:
                back += src[i] << 8; i += 1
            else:
                back += (r3 & 0xc) << 6
            p = len(out) - back
            for _ in range(r5 + 2):
                out.append(out[p]); p += 1
        if len(out) >= n: break
        r3 = src[i]; i += 1
    return bytes(out[:n])

def build(fw, quiet=False):
    hdr = struct.unpack_from('<6I', fw, 0x60)
    itcm_size, sd_size, sd_base = hdr[1], hdr[4], hdr[5]
    itcm = fw[0x78:0x78 + itcm_size]
    sdo = 0x78 + itcm_size
    sdram = fw[sdo:sdo + sd_size]
    segs = [(0x0, itcm, 5), (sd_base, sdram, 7)]
    # find scatterload table: __main is at sd_base; adr at +8 points to the (start,end) offset pair
    adr = sdram[8]
    tp = ((8 + 4) & ~3) + adr * 4
    a, b = struct.unpack_from('<ii', sdram, tp)
    zero = []
    for p in range(tp + a, tp + b, 16):
        load, exe, size, h = struct.unpack_from('<4I', sdram, p)
        lo = load - sd_base
        kind = {0x800000a0: 'copy', 0x8000003c: 'decompress', 0x800000bc: 'zero'}.get(h, hex(h))
        if not quiet:
            print('region load=%08x exec=%08x size=%08x %s' % (load, exe, size, kind), file=sys.stderr)
        if load == exe and kind != 'zero':
            continue                      # in-place region (GP-200): already part of the SDRAM image
        if kind == 'copy': segs.append((exe, sdram[lo:lo + size], 6))
        elif kind == 'decompress': segs.append((exe, decompress(sdram, lo, size), 6))
        elif kind == 'zero': zero.append((exe, size))
        if load - sd_base < len(segs[1][1]):  # region init data marks end of SDRAM execution region
            segs[1] = (sd_base, sdram[:load - sd_base], 7)
    return segs, zero, hdr

def write_elf(path, segs, zero, entry):
    phs = [(va, data, len(data), fl) for va, data, fl in segs] + [(va, b'', sz, 6) for va, sz in zero]
    phoff = 52
    off = phoff + 32 * len(phs)
    body = bytearray()
    ph_bytes = bytearray()
    for va, data, memsz, fl in phs:
        o = off + len(body)
        ph_bytes += struct.pack('<8I', 1, o if data else 0, va, va, len(data), memsz, fl, 4)
        body += data
        while len(body) % 4: body += b'\0'
    eh = b'\x7fELF\x01\x01\x01' + b'\0' * 9 + struct.pack('<HHIIIIIHHHHHH', 2, 40, 1, entry, phoff, 0,
                                                            0x05000400, 52, 32, len(phs), 40, 0, 0)
    open(path, 'wb').write(eh + ph_bytes + body)

def make(fw, path, quiet=False):
    """write the ELF of firmware image `fw` (bytes) to `path`"""
    segs, zero, hdr = build(fw, quiet)
    entry = struct.unpack_from('<I', fw, 0x78 + 4)[0]
    write_elf(path, segs, zero, entry)
    return segs, zero


def main():
    if len(sys.argv) != 3:
        sys.exit('usage: mkappelf.py firmware.bin app.elf')
    segs, zero = make(open(sys.argv[1], 'rb').read(), sys.argv[2])
    for va, data, fl in segs: print('seg %08x size %#x' % (va, len(data)))
    for va, sz in zero: print('bss %08x size %#x' % (va, sz))


if __name__ == '__main__':
    main()
