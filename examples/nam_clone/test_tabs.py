"""Emulation check of the "clonetabs" app patch (Clone menu tabs "Native" / "NAM A2", needs "clonedual").

    test_tabs.py [fw.bin] [png_prefix]   (default <build>/QME-200_FW_USB_V1.2.2_parallel_ui_clonetabs.bin; needs unicorn)

The app boots in matribox/app/uiemu.py with factory preset 0 rewritten to one Clone block (slot 3) and an empty Clone
store, then opens the effect-select screen of that block. Checks:
1. only factory slots: subgroup 0 for every slot, one tab after ALL ("Native");
2. NAM blobs written to slots 2 and 7 through the app's own Clone-write handler: those two get subgroup 1,
   two tabs ("Native", "NAM A2");
3. on the screen (encoder path: FUN_800018f0 + FUN_80001b34): tab +1 from the slot's own tab goes to
   "NAM A2" and loads its first slot (2); row +1 there loads slot 7; tab back to "Native" loads slot 0;
   the list of each tab holds only its slots;
4. the other menus keep their subgroups and tab counts (compared with the unpatched functions' table).
Screenshots of the screen go to <png_prefix>_<step>.png when a prefix is given.
"""
import os, sys, struct

HERE = os.path.dirname(os.path.abspath(__file__))
from unicorn.arm_const import UC_ARM_REG_R4, UC_ARM_REG_R7, UC_ARM_REG_SP
from matribox import paths
from matribox.firmware.fwtool import Firmware
from matribox.app import uiemu

FW = paths.build_path('QME-200_FW_USB_V1.2.2_parallel_ui_clonetabs.bin')
CLONE_MENU, CLONE_BASE = 11, 0x0F000000
SLOT = 3                                    # Clone slot of chain block 0
NAM_SLOTS = (2, 7)
WRITE_AT, WRITE_END = 0x395e8, 0x39612      # Clone-write case of the message handler (see test_dual.py)
MSG = 0x81c00000
NAM_MAGIC = 0x324D414E
SUBGROUP, TAB_COUNT, TAB_NAME = 0x1ebd2, 0x270c8, 0x1eb96
SCREENS = 0x20241bb8                        # {create, destroy, update} per screen (FUN_0003c2bc)
SELECT_CREATE = 0x8000153d                  # effect-select screen: FUN_8000153c(state, &block)
STATE_PTR = 0x8000183c                      # literal: address of the screen's state pointer
ENCODER, PRESS = 0x800018f0, 0x80001b34     # FUN_800018f0(state, event), FUN_80001b34(state)
EVENT = 0x2001e100
BLOCKS = 0x268                              # chain blocks: global state + 0x268 + 0x44 * n (+1 menu, +2 idx)


def clone_chain(fw):
    bank = bytearray(fw.sections[3])
    for i in range(12):
        o = 0x30 + 0x44 * i
        rec = (struct.pack('<BbhI', 1, CLONE_MENU, SLOT, CLONE_BASE + SLOT) + struct.pack('<5f', *[50.0] * 5)
               if i == 0 else struct.pack('<BbhI', 0, -1, -1, 0))
        bank[o:o + 0x44] = rec.ljust(0x44, b'\0')
    bank[0x22:0x2e] = bytes([0]) + b'\xff' * 11
    fw.sections[3] = bytes(bank)


def main():
    args = sys.argv[1:]
    path = args[0] if args else FW
    prefix = args[1] if len(args) > 1 else None
    fw = Firmware(open(path, 'rb').read())
    clone_chain(fw)
    ui = uiemu.Ui(fw)
    uc = ui.uc
    ui.start()
    st = ui.lit(0x39cc4)
    buf = 0x2001e200

    def tabs():
        n = ui.fn(TAB_COUNT, CLONE_MENU) & 0xffff
        sub = [ui.fn(SUBGROUP, CLONE_MENU, k) for k in range(10)]
        names = []
        for t in range(n):
            k = sub.index(t)
            uc.mem_write(buf, b'\xee' * 20)
            ui.fn(TAB_NAME, CLONE_MENU, k, buf, 20)
            names.append(ui.cstr(buf))
        return sub, names

    # 4. other menus: subgroups from the library (FUN_0001b690), counts from the boot table
    for m in range(16):
        if m in (5, 6, CLONE_MENU):
            continue
        cnt = struct.unpack('<H', uc.mem_read(st + 2 * (0x452 + m), 2))[0]
        assert ui.fn(TAB_COUNT, m) & 0xffff == cnt, m
        for k in range(ui.fn(0x1b2cc, m)):
            assert ui.fn(SUBGROUP, m, k) == ui.fn(0x1b690, m, k), (m, k)
    assert ui.fn(SUBGROUP, 5, 0) == 0 and ui.fn(SUBGROUP, 6, 3) == 0
    print('outros menus: subgrupos e abas iguais aos de fabrica')

    sub, names = tabs()
    print('so capturas de fabrica: subgrupos %s, abas ALL + %s' % (sub, names))
    assert sub == [0] * 10 and names == ['Native']

    for k in NAM_SLOTS:
        blob = struct.pack('<I', NAM_MAGIC).ljust(7620, b'\0')
        body = struct.pack('<III', k, 0, sum(blob)) + ('NAM %d' % k).encode().ljust(20, b'\0') + blob
        uc.mem_write(MSG, struct.pack('<II', 0x18000001, len(body)) + body)
        uc.reg_write(UC_ARM_REG_R4, MSG)
        uc.reg_write(UC_ARM_REG_R7, 0)
        uc.reg_write(UC_ARM_REG_SP, 0x20007000)
        uc.emu_start(WRITE_AT | 1, WRITE_END, count=200_000_000)
    sub, names = tabs()
    print('NAM nos slots %s: subgrupos %s, abas ALL + %s' % (NAM_SLOTS, sub, names))
    assert sub == [1 if k in NAM_SLOTS else 0 for k in range(10)] and names == ['Native', 'NAM A2']

    # the effect-select screen of chain block 0
    scr = next(n for n in range(0x20) if ui.lit(SCREENS + 12 * n) == SELECT_CREATE)
    uc.mem_write(EVENT, bytes(16))
    ui.fn(uiemu.GOTO_SCREEN, scr, EVENT)        # event byte 0 = block 0
    ui.loop(40)
    s = ui.lit(ui.lit(STATE_PTR))

    def h(o):
        return struct.unpack('<H', uc.mem_read(s + o, 2))[0]

    def block():
        return uc.mem_read(st + BLOCKS + 1, 1)[0], struct.unpack('<h', uc.mem_read(st + BLOCKS + 2, 2))[0]

    def show(step):
        ui.loop(20)
        print('%-26s aba %d, linha %d, bloco 0 = menu %d slot %d' % (step, h(0x8a), h(0x92), *block()))
        if prefix:
            ui.shot('%s_%s.png' % (prefix, step))

    def turn(field, d):
        uc.mem_write(s + 0x9a, bytes([field]))
        uc.mem_write(s + 0x9c, bytes([1]))
        uc.mem_write(EVENT + 4, struct.pack('<i', d))
        ui.fn(ENCODER, s, EVENT)
        ui.fn(PRESS, s)

    show('abrir')
    assert h(0x82) == CLONE_MENU and h(0x8a) == 1 and block() == (CLONE_MENU, SLOT)   # slot 3 = Native
    turn(3, 1)
    show('aba_nam')
    assert h(0x8a) == 2 and h(0x92) == 0 and block() == (CLONE_MENU, NAM_SLOTS[0])
    turn(4, 1)
    show('nam_linha2')
    assert h(0x92) == 1 and block() == (CLONE_MENU, NAM_SLOTS[1])
    turn(4, 1)                                  # wraps to the first row
    assert h(0x92) == 0 and block() == (CLONE_MENU, NAM_SLOTS[0])
    turn(3, -1)
    show('aba_native')
    assert h(0x8a) == 1 and block() == (CLONE_MENU, 0)
    for _ in range(7):
        turn(4, 1)
    assert block() == (CLONE_MENU, 9)           # rows of Native: 0 1 3 4 5 6 8 9
    turn(3, -1)
    show('aba_all')
    assert h(0x8a) == 0 and block() == (CLONE_MENU, 0)
    turn(3, -1)                                 # ALL - 1 wraps to the last tab
    assert h(0x8a) == 2 and block() == (CLONE_MENU, NAM_SLOTS[0])
    print('OK')


if __name__ == '__main__':
    main()
