"""Render the original app's screen in emulation (for UI patches).

Runs main (FUN_000424d4) like mmiotrace.py, without the trace file, up to the GUI loop, then calls the loop
body (FUN_0002c758) a number of times and saves the LCDIF framebuffer as a PNG.

The QSPI flash (0x60000000) holds the firmware's DSP / IR / Bank sections at their partitions. Instead of
FUN_000399f0 (boot), only its storage, settings, effect-library and preset loading calls are made, one by
one, from Python; the device check in there is not run.

usage: uiemu.py firmware.bin out.png [loop_iterations]   (requires unicorn + capstone)
"""
import os, sys, struct
from unicorn import *
from unicorn.arm_const import *
from matribox.app import mmiotrace
from matribox.pngio import png
from matribox.firmware.fwtool import Firmware

W, H = 480, 272
LCDIF_CUR_BUF = 0x402b8040
FLASH = 0x60000000
PARTS = {1: 0x480000, 2: 0x700000, 3: 0x440000}   # FRMW section -> flash offset (Bank = factory presets;
                                                   # the user store at 0x780000 starts erased)
BOOT = 0x399f0
FLASH_READ, FLASH_PROGRAM, FLASH_ERASE = 0x26868, 0x267a4, 0x26688
FLASH_OPS = (FLASH_READ, FLASH_PROGRAM, FLASH_ERASE)
ASSERT_PRINT = 0x1c7c                               # prints two strings, then semihosting bkpt
SCRATCH = 0x2001e000                                # argument structs for the calls made from Python
GOTO_SCREEN = 0x3cad8                               # FUN_0003cad8(screen, param)
SDRAM_CODE = 0x32a74c                              # execution region at 0x80000000 (the rest is load data)


class Ui(mmiotrace.Tracer):
    def __init__(self, fw):
        super().__init__(os.devnull)
        self.boot_lr = None
        uc = self.uc
        # code from the firmware's MCU section (a patched app included); the RW data that __main's scatterload
        # would unpack comes from the stock app ELF (matribox.firmware.stock.app_elf), so only the SDRAM image's code/const part is replaced
        from matribox.app.apppatch import split_mcu
        head, itcm, sdram = split_mcu(fw.sections[0])
        uc.mem_write(0, bytes(itcm))
        uc.mem_write(0x80000000, bytes(sdram[:SDRAM_CODE]))
        uc.mem_write(FLASH, b'\xff' * 0x800000)
        for s, off in PARTS.items():
            uc.mem_write(FLASH + off, fw.sections[s])
        for a in FLASH_OPS:
            uc.hook_add(UC_HOOK_CODE, self.flash_op, begin=a, end=a)
        self.asserts = set()
        uc.hook_add(UC_HOOK_CODE, self.assert_msg, begin=ASSERT_PRINT, end=ASSERT_PRINT)

    def cstr(self, a):
        return bytes(self.uc.mem_read(a, 80)).split(b'\0')[0].decode('latin-1') if a else ''

    def assert_msg(self, uc, addr, size, ud):
        m = '%s %s' % (self.cstr(uc.reg_read(UC_ARM_REG_R0)), self.cstr(uc.reg_read(UC_ARM_REG_R1)))
        if m not in self.asserts:
            self.asserts.add(m)
            print('  assert (lr=%#x): %s' % (uc.reg_read(UC_ARM_REG_LR), m))

    def flash_op(self, uc, addr, size, ud):
        """FlexSPI NOR primitives, done on the memory-mapped copy: read/program(dev, offset, buf, len), erase(dev, offset)"""
        r = [uc.reg_read(x) for x in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        off = FLASH + (r[1] & 0xffffff)
        if addr == FLASH_READ:
            uc.mem_write(r[2], bytes(uc.mem_read(off, r[3])))
        elif addr == FLASH_PROGRAM:
            old, new = uc.mem_read(off, r[3]), uc.mem_read(r[2], r[3])
            uc.mem_write(off, bytes(a & b for a, b in zip(old, new)))
        else:
            uc.mem_write(off & ~0xfff, b'\xff' * 0x1000)
        uc.reg_write(UC_ARM_REG_R0, 0)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR) | 1)

    # FUN_000399f0 is replaced by boot(): stop there and remember where main continues
    def skip(self, uc, addr, size, ud):
        self.boot_lr = uc.reg_read(UC_ARM_REG_LR)
        uc.emu_stop()

    def lit(self, a):
        return struct.unpack('<I', self.uc.mem_read(a, 4))[0]

    def exec(self, pc, count=200_000_000):
        """emu_start, skipping the double-precision VFP instructions unicorn's M7 lacks"""
        uc, skipped = self.uc, 0
        while True:
            try:
                uc.emu_start(pc | 1, mmiotrace.STOP, count=count)
                return True
            except UcError as ex:
                pc = uc.reg_read(UC_ARM_REG_PC)
                hw = struct.unpack('<H', uc.mem_read(pc, 2))[0]
                if ex.errno == UC_ERR_INSN_INVALID and (hw & 0xee00) == 0xee00 and skipped < 1000000:
                    skipped += 1
                    pc += 4
                    continue
                if hw == 0xbeab:                    # semihosting (assert/printf output): ignore
                    pc += 2
                    continue
                print('  erro %s pc=%#x lr=%#x' % (ex, pc, uc.reg_read(UC_ARM_REG_LR)))
                return False

    def fn(self, f, *args, sp=0x20007000):
        uc = self.uc
        for r, v in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
            uc.reg_write(r, v & 0xffffffff)
        uc.reg_write(UC_ARM_REG_SP, sp)
        uc.reg_write(UC_ARM_REG_LR, mmiotrace.STOP | 1)
        ok = self.exec(f)
        pc = uc.reg_read(UC_ARM_REG_PC) & ~1
        if not ok or pc != mmiotrace.STOP:
            print('  FUN_%08x nao retornou (pc=%#x)' % (f, pc))
        return uc.reg_read(UC_ARM_REG_R0)

    def boot(self, sp):
        """the loading part of FUN_000399f0"""
        uc, sp = self.uc, sp - 0x400
        base = self.lit(0x39cc0)
        st = self.lit(0x39cc4)                      # global settings (0x238 bytes, magic 0x5aa5) + more state
        uc.mem_write(base + 8, struct.pack('<II', 0x238, 0x48c))
        dev = st + 0xc04
        self.fn(0x26714, dev, 0, sp=sp)
        self.fn(0x25d5c, dev, sp=sp)
        uc.mem_write(SCRATCH, struct.pack('<III', dev, 0x780000, 0x80000))
        self.fn(0x26000, SCRATCH, sp=sp)
        uc.mem_write(SCRATCH, struct.pack('<8I', dev, 0x700000, 0x53000, self.lit(0x39cc8), 0x2d000,
                                          self.lit(0x39ccc), 0x36000, 0x1000))
        self.fn(0x25258, SCRATCH, sp=sp)
        self.fn(0x2b030, sp=sp)
        r = self.fn(0x260dc, 0, st, 0x238, sp=sp)
        magic = struct.unpack('<H', uc.mem_read(st, 2))[0]
        if r == 0 or magic != 0x5aa5:               # empty user store: factory settings, preset i -> record i + 2
            uc.mem_write(st, bytes(uc.mem_read(self.lit(0x39cf4), 0x238)))
            uc.mem_write(st + 0x6c4, b''.join(struct.pack('<h', i + 2) for i in range(0xf0)))
            self.fn(0x262f4, 1, st + 0x6c4, 0x1e0, sp=sp)
            self.fn(0x262f4, 0, st, 0x238, sp=sp)
            print('configuracoes globais: padrao de fabrica')
        self.fn(0x260dc, 1, st + 0x6c4, 0x1e0, sp=sp)
        preset = struct.unpack('<h', uc.mem_read(st + 12, 2))[0]
        self.fn(0x21018, preset, st + 0x238, sp=sp)
        b = bytearray(uc.mem_read(st, 0xc04))
        def put(o, v): b[o] = v & 0xff
        for o in (0x8c4, 0x8c5, 0x8c6, 0x8c7, 0xbdb, 0xbdc, 0xbd9, 0xbf0, 0xbf1, 0xbfc, 0xbfd, 0xbff,
                  0xc00, 0xc01, 0xc02, 0xc03):
            put(o, 0)
        put(0xbdf, 2); put(0xbdd, 1); put(0xbda, 1)
        for k in range(6):
            put(0x899 + 0x40 + 0x80 * k, 0xff); put(0x899 + 0x80 * (k + 1), 0xff)
        uc.mem_write(st, bytes(b))
        g = self.lit(0x39cfc)
        v = self.lit(0x39cf8)
        uc.mem_write(g + 0x3e0, struct.pack('<I', v)); uc.mem_write(g + 1000, struct.pack('<I', v))
        uc.mem_write(g + 0x3f4, struct.pack('<II', 0x3f800000, 0x3f800000))
        self.fn(0x20b7c, sp=sp)
        r = self.fn(0x1b180, self.lit(0x39d04), 0x1000, self.lit(0x39d00), sp=sp)
        if r == 0:
            self.fn(0x1b628, sp=sp)
            n = min(self.fn(0x1b354, sp=sp), 0x10)
            uc.mem_write(st + 0x8c4, struct.pack('<I', n))
            for m in range(n):                      # per menu: highest model number + 1 (5, 6, 11: 1)
                if m in (5, 6, 11):
                    uc.mem_write(st + 2 * (0x452 + m), struct.pack('<H', 1))
                    continue
                prev = 0
                for k in range(self.fn(0x1b2cc, m, sp=sp)):
                    v = self.fn(0x1b690, m, k, sp=sp)
                    uc.mem_write(st + 2 * (0x452 + m), struct.pack('<H', (max(prev, v) + 1) & 0xffff))
                    prev = v
            print('biblioteca de efeitos: %d menus' % n)
        else:
            print('biblioteca de efeitos: falhou (%d)' % r)
        self.fn(0x21660, preset, sp=sp)
        self.fn(0x361ac, uc.mem_read(st + 0x230, 1)[0], sp=sp)
        print('preset atual: %d' % preset)

    def start(self):
        uc = self.uc
        uc.reg_write(UC_ARM_REG_SP, 0x20008000)
        uc.reg_write(UC_ARM_REG_LR, mmiotrace.STOP | 1)
        pc = mmiotrace.MAIN
        while True:
            self.exec(pc)
            if self.boot_lr is None:
                break
            ctx = uc.context_save()
            lr, self.boot_lr = self.boot_lr, None
            self.boot(uc.reg_read(UC_ARM_REG_SP))
            uc.context_restore(ctx)
            uc.reg_write(UC_ARM_REG_R0, 0)
            pc = lr & ~1
        print('main ate o laco da interface: pc=%#x' % uc.reg_read(UC_ARM_REG_PC))

    def loop(self, n):
        uc = self.uc
        for i in range(n):
            uc.reg_write(UC_ARM_REG_SP, 0x20008000)
            uc.reg_write(UC_ARM_REG_LR, mmiotrace.STOP | 1)
            if not self.exec(mmiotrace.GUI_LOOP, 20_000_000):
                return False
        return True

    def shot(self, path):
        fb = self.regs.get(LCDIF_CUR_BUF) or 0x81f00000
        px = bytes(self.uc.mem_read(fb, W * H * 4))
        rgb = bytearray(W * H * 3)
        rgb[0::3] = px[2::4]
        rgb[1::3] = px[1::4]
        rgb[2::3] = px[0::4]
        png(path, bytes(rgb), W, H)
        print('framebuffer %#x -> %s' % (fb, path))


SETPOS_CALL = 0x8000b376          # FUN_8000afc4: WM_SetWindowPos(block i, x, 0x46, 32, 44); r6 = i, r7 = blocks
SPLIT, PATHB, MERGE = 0x01000070, 0x01000071, 0x01000072


def parallel_layout(ids, y0=70, dy=21, py0=79):
    """(x, y) of each chain position: Split ... [A] ... Path B ... [B] ... Merge drawn as two rows"""
    n = len(ids)
    serial = [(243 - 19 * n + 38 * i, y0) for i in range(n)]
    if SPLIT not in ids or MERGE not in ids:
        return serial
    s, m = ids.index(SPLIT), ids.index(MERGE)
    b = ids.index(PATHB) if PATHB in ids and s < ids.index(PATHB) < m else m
    if not s < m:
        return serial
    a_len, b_len = b - s - 1, m - b               # row B starts with the Path B block itself
    k = max(a_len, b_len, 1)
    cols = s + 1 + k + (n - m)
    x = lambda c: 243 - 19 * cols + 38 * c
    pos = []
    for i in range(n):
        if i <= s:
            pos.append((x(i), py0))
        elif i < b:
            pos.append((x(i), py0 - dy))
        elif i < m:
            pos.append((x(s + 1 + i - b), py0 + dy))
        else:
            pos.append((x(s + 1 + k + i - m), py0))
    return pos


class Patched(Ui):
    """the UI layout patch, prototyped as a hook on the SetWindowPos call"""
    def __init__(self, fw):
        super().__init__(fw)
        self.uc.hook_add(UC_HOOK_CODE, self.setpos, begin=SETPOS_CALL, end=SETPOS_CALL)

    def chain_ids(self, n):
        st = self.lit(0x39cc4)
        order = bytes(self.uc.mem_read(st + 0x238 + 0x22, 12))
        return [self.lit(st + 0x268 + 0x44 * order[i] + 4) for i in range(n)]

    def setpos(self, uc, addr, size, ud):
        i, n = uc.reg_read(UC_ARM_REG_R6), uc.reg_read(UC_ARM_REG_R7)
        x, y = parallel_layout(self.chain_ids(n))[i]
        uc.reg_write(UC_ARM_REG_R1, x & 0xffffffff)
        uc.reg_write(UC_ARM_REG_R2, y & 0xffffffff)


def set_chain(fw, spec):
    """rewrite factory preset 0's 12 slots; spec = 'menu:index,...' ('-' = empty), e.g. '0:11,7:5,3:5,7:6,7:7'.
    Ids come from the effect library; a slot's parameters come from the factory slot with the same menu."""
    bank = bytearray(fw.sections[3])
    L = fw.library
    old = [bank[0x30 + 0x44 * i:0x30 + 0x44 * (i + 1)] for i in range(12)]
    items = [x for x in spec.split(',')] + ['-'] * 12
    for i in range(12):
        o = 0x30 + 0x44 * i
        if items[i] == '-':
            bank[o:o + 0x44] = struct.pack('<BbhI', 0, -1, -1, 0) + bytes(0x3c)
            continue
        en = 0 if items[i].startswith('!') else 1      # '!menu:id' = bypassed
        m, eid = (int(x, 0) for x in items[i].lstrip('!').split(':'))
        ents = [L.entries[e] if isinstance(e, int) else e for e in (L.cat_entries[m] if m < len(L.cat_entries) else [])]
        k = next((n for n, e in enumerate(ents) if e.id == eid), 0)   # the app recomputes it from the id
        src = next((s for s in old if s[1] == m and struct.unpack_from('<I', s, 4)[0] == eid), None) \
            or next((s for s in old if s[1] == m), None)
        e = next((e for e in ents if e.id == eid), None)
        params = bytes(src[8:]) if src else b''.join(struct.pack('<f', p['default']) for p in e.params)
        bank[o:o + 0x44] = (struct.pack('<BbhI', en, m, k, eid) + params + bytes(0x3c))[:0x44]
    n = sum(1 for x in items[:12] if x != '-')
    bank[0x22:0x2e] = bytes(range(n)) + b'\xff' * (12 - n)     # chain order: slot of each position
    fw.sections[3] = bytes(bank)


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    fw = Firmware(open(args[0], 'rb').read())
    for a in sys.argv[1:]:
        if a.startswith('--chain='):
            set_chain(fw, a[8:])
    ui = (Patched if '--proto' in sys.argv else Ui)(fw)
    ui.start()
    ui.loop(int(args[2]) if len(args) > 2 else 400)
    for a in sys.argv[1:]:
        if a.startswith('--screen='):                # FUN_0003cad8(screen, 0): 1 = main, 2 = chain edit
            ui.fn(GOTO_SCREEN, int(a[9:], 0), 0)
            ui.loop(60)
    ui.shot(args[1])
