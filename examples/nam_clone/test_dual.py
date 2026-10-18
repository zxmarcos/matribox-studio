"""Emulation check of the "clonedual" firmware (app patch clone_dual.c + library entry 11.1 = NAM A2).

    test_dual.py [fw.bin]      (default <build>/QME-200_FW_USB_V1.2.2_parallel_ui_clonedual.bin; needs unicorn)

The app boots in matribox/app/uiemu.py with factory preset 0 rewritten to two Clone blocks (slots A and B) and an
empty Clone store. Checks, against the ELFs of entries 11.0 (factory) and 11.1 (NAM):
1. after the preset load (FUN_00020424(3) copies the modules to the staging buffers) both blocks hold 11.0;
2. writing a NAM blob to slot A through the app's own Clone-write handler (0x395e8, the 0x18000001 command)
   reloads the chain in mode 3 (patched from 2) and block A then holds 11.1, block B still 11.0; the
   audio-side load (FUN_000204f4) places both modules in the pools;
3. the memory checks before inserting (FUN_0001b4f4) and replacing (FUN_0001b598) a block look at the
   engine the slot needs;
4. clearing slot A (FUN_0002d344 -> FUN_00024974, mode 2 patched to 3) brings block A back to 11.0.
"""
import os, sys, struct

HERE = os.path.dirname(os.path.abspath(__file__))
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R4, UC_ARM_REG_R7, UC_ARM_REG_SP
from matribox import paths
from matribox.firmware.fwtool import Firmware
from matribox.app import uiemu

FW = paths.build_path('QME-200_FW_USB_V1.2.2_parallel_ui_clonedual.bin')
CLONE_MENU, CLONE_BASE = 11, 0x0F000000
SLOT_A, SLOT_B = 3, 6                       # Clone slots used by chain blocks 0 and 1
STAGING = 0x819f0000                        # + chain slot * 0x8000 (FUN_0001b538)
WRITE_AT, WRITE_END = 0x395e8, 0x39612      # Clone-write case of the message handler, up to after the reload
CLEAR = 0x2d344                             # (?, slot, ?, ?): FUN_00024974(slot), then the chain reload
RELOAD, SIZES = 0x20424, 0x4029c            # FUN_00020424(mode); FUN_0004029c(sizes, elf): ELF read by the checks
LOAD, LOADED = 0x204f4, 0x1b320             # chain (re)load on the audio side; FUN_0001b2ec after its FUN_00040510
MSG = 0x81c00000                            # message buffer for the write handler (free SDRAM in emulation)
NAM_MAGIC = 0x324D414E


def clone_chain(fw):
    """factory preset 0 -> two Clone blocks (slots A, B), knobs at 50, the rest empty"""
    bank = bytearray(fw.sections[3])
    for i in range(12):
        o = 0x30 + 0x44 * i
        if i < 2:
            k = (SLOT_A, SLOT_B)[i]
            rec = struct.pack('<BbhI', 1, CLONE_MENU, k, CLONE_BASE + k) + struct.pack('<5f', *[50.0] * 5)
        else:
            rec = struct.pack('<BbhI', 0, -1, -1, 0)
        bank[o:o + 0x44] = rec.ljust(0x44, b'\0')
    bank[0x22:0x2e] = bytes([0, 1]) + b'\xff' * 10
    fw.sections[3] = bytes(bank)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else FW
    fw = Firmware(open(path, 'rb').read())
    lib = fw.library
    ents = [lib.entries[e] for e in lib.cat_entries[CLONE_MENU]]
    assert len(ents) == 2, 'menu 11 tem %d entradas (esperado: fabrica + NAM)' % len(ents)
    elf = {0: ents[0].elf, 1: ents[1].elf}
    name = {0: 'fabrica (11.0)', 1: 'NAM (11.1)'}
    clone_chain(fw)
    ui = uiemu.Ui(fw)
    uc = ui.uc
    reloads, sizes_elf = [], []
    uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, d: reloads.append(uc.reg_read(UC_ARM_REG_R0)),
                begin=RELOAD, end=RELOAD)
    uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, d: sizes_elf.append(uc.reg_read(UC_ARM_REG_R1)),
                begin=SIZES, end=SIZES)
    loads = []
    uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, d: loads.append(uc.reg_read(UC_ARM_REG_R0) - (1 << 32)
                                                               if uc.reg_read(UC_ARM_REG_R0) >> 31 else
                                                               uc.reg_read(UC_ARM_REG_R0)),
                begin=LOADED, end=LOADED)
    ui.start()

    def staged(slot):
        got = bytes(uc.mem_read(STAGING + slot * 0x8000, 0x200))
        hit = [k for k, e in elf.items() if e[:0x200] == got]
        return hit[0] if hit else None

    def engines(step, want):
        got = (staged(0), staged(1))
        print('%-34s bloco A (slot %d): %-15s bloco B (slot %d): %s' % (
            step, SLOT_A, name.get(got[0], '?'), SLOT_B, name.get(got[1], '?')))
        assert got == want, 'esperado %s' % (want,)

    def checked(fn, slot):
        sizes_elf.clear()
        ui.fn(fn, 2, CLONE_MENU, slot)
        assert sizes_elf, 'FUN_%08x nao leu ELF' % fn
        got = bytes(uc.mem_read(sizes_elf[-1], 0x200))
        return next((k for k, e in elf.items() if e[:0x200] == got), None)

    engines('preset carregado:', (0, 0))
    print('checagem de memoria (inserir / trocar), slot %d: %s / %s' % (
        SLOT_A, name[checked(0x1b4f4, SLOT_A)], name[checked(0x1b598, SLOT_A)]))
    assert checked(0x1b4f4, SLOT_A) == 0 and checked(0x1b598, SLOT_A) == 0

    # 0x18000001 {u32 slot, u32 0, u32 byte sum, name[20], data}: a NAM blob ("NAM2", rest irrelevant here)
    blob = struct.pack('<I', NAM_MAGIC).ljust(7620, b'\0')
    body = struct.pack('<III', SLOT_A, 0, sum(blob) & 0xffffffff) + b'Teste NAM'.ljust(20, b'\0') + blob
    uc.mem_write(MSG, struct.pack('<II', 0x18000001, len(body)) + body)
    reloads.clear()
    uc.reg_write(UC_ARM_REG_R4, MSG)
    uc.reg_write(UC_ARM_REG_R7, 0)
    uc.reg_write(UC_ARM_REG_SP, 0x20007000)
    uc.emu_start(WRITE_AT | 1, WRITE_END, count=200_000_000)
    data = ui.fn(0x249ec, SLOT_A)
    magic = struct.unpack('<I', uc.mem_read(data, 4))[0]
    print('gravacao do slot %d: dados em %#x (magic %#x), recarga da cadeia no modo %s' % (
        SLOT_A, data, magic, reloads))
    assert magic == NAM_MAGIC and reloads == [3]
    engines('depois de gravar NAM no slot A:', (1, 0))
    loads.clear()
    ui.fn(LOAD)
    print('recarga no lado do audio (FUN_000204f4): loader por bloco %s (-2 = sem memoria)' % loads)
    assert loads == [0, 0]
    a = (checked(0x1b4f4, SLOT_A), checked(0x1b598, SLOT_A), checked(0x1b4f4, SLOT_B), checked(0x1b598, SLOT_B))
    print('checagem de memoria (inserir / trocar): slot %d %s / %s, slot %d %s / %s' % (
        SLOT_A, name[a[0]], name[a[1]], SLOT_B, name[a[2]], name[a[3]]))
    assert a == (1, 1, 0, 0)

    reloads.clear()
    ui.fn(CLEAR, 0, SLOT_A, 0, 0)
    data = ui.fn(0x249ec, SLOT_A)
    magic = struct.unpack('<I', uc.mem_read(data, 4))[0]
    print('slot %d apagado: dados em %#x (magic %#x), recarga no modo %s' % (SLOT_A, data, magic, reloads))
    assert reloads == [3] and magic != NAM_MAGIC
    engines('depois de apagar o slot A:', (0, 0))
    print('OK')


if __name__ == '__main__':
    main()
