"""Trace the peripheral register accesses of the original app's start-up (main = FUN_000424d4).

Runs the original app (matribox.firmware.stock.app_elf) in unicorn as a Cortex-M7 from main, with every peripheral (0x4xxxxxxx) and
private-peripheral (0xExxxxxxx) access hooked. Registers keep the last written value; a read that keeps
being polled from the same instruction alternates 0xffffffff / 0 so busy-wait loops finish. Functions
that need real hardware/flash content are skipped (return 0). Output: one line per access, tagged with
the top-level main() call it belongs to.

usage: mmiotrace.py out.txt [max_instructions]   (requires unicorn + capstone)
"""
import sys, struct, collections
import capstone
from unicorn import *
from unicorn.arm_const import *
from matribox.firmware import stock

MAIN = 0x424d4
MAIN_END = 0x42530
SKIP = {0x399f0: 'boot/flash/integrity'}  # not emulated
GUI_LOOP = 0x2c758              # main's endless loop body: tracing stops when main calls it
GPIO_PININIT = 0x26d70          # GPIO_PinInit(base, pin, &{direction, outputLogic, interruptMode})
GPIO_PINWRITE = 0x26e70         # GPIO_PinWrite(base, pin, value)
SHIFT_XFER = 0x35750            # 4-byte software shift register transfer (buttons/LEDs)
REGIONS = [(0x0, 0x80000), (0x20000000, 0x20000), (0x20200000, 0x80000), (0x80000000, 0x2000000),
           (0x60000000, 0x1000000)]
MMIO = [(0x40000000, 0x2400000), (0xe0000000, 0x100000)]
STOP = 0x7fff0
SYSTICK_TICKS = 0x202364a0      # incremented by the SysTick ISR (FUN_0003592c / FUN_00035868)
DELAY_MS = 0x35868              # delay(ms) busy-waits on SysTick


def load(uc):
    e = open(stock.app_elf(), 'rb').read()
    phoff, = struct.unpack_from('<I', e, 28)
    n, = struct.unpack_from('<H', e, 44)
    for i in range(n):
        t, off, va, pa, fs, ms, fl, al = struct.unpack_from('<8I', e, phoff + 32 * i)
        if fs:
            uc.mem_write(va, e[off:off + fs])


class Tracer:
    def __init__(self, out):
        self.out = open(out, 'w')
        self.regs = {}
        self.poll = collections.Counter()
        self.phase = 'main'
        self.md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
        self.cyc = 0
        uc = self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        uc.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M7)
        for b, s in REGIONS:
            uc.mem_map(b, s)
        for b, s in MMIO:
            uc.mem_map(b, s)
        load(uc)
        uc.mem_write(STOP, b'\xfe\xe7')
        uc.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, self.mmio, begin=0x40000000, end=0x423fffff)
        uc.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, self.mmio, begin=0xe0000000, end=0xe00fffff)
        uc.hook_add(UC_HOOK_CODE, self.main_code, begin=MAIN, end=MAIN_END)
        for a in SKIP:
            uc.hook_add(UC_HOOK_CODE, self.skip, begin=a, end=a)
        uc.hook_add(UC_HOOK_MEM_READ, self.ticks, begin=SYSTICK_TICKS, end=SYSTICK_TICKS + 3)
        uc.hook_add(UC_HOOK_CODE, self.delay, begin=DELAY_MS, end=DELAY_MS)
        uc.hook_add(UC_HOOK_CODE, self.api, begin=GPIO_PININIT, end=GPIO_PININIT)
        uc.hook_add(UC_HOOK_CODE, self.api, begin=GPIO_PINWRITE, end=GPIO_PINWRITE)
        uc.hook_add(UC_HOOK_CODE, self.api, begin=SHIFT_XFER, end=SHIFT_XFER)

    def api(self, uc, addr, size, ud):
        r = [uc.reg_read(x) for x in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        lr = uc.reg_read(UC_ARM_REG_LR) & ~1
        if addr == GPIO_PININIT:
            d, o, i = bytes(uc.mem_read(r[2], 3))
            self.out.write('I %08x %d dir=%d out=%d irq=%d lr=%05x %s\n' % (r[0], r[1], d, o, i, lr, self.phase))
        elif addr == GPIO_PINWRITE:
            self.out.write('P %08x %d %d lr=%05x %s\n' % (r[0], r[1], r[2] & 0xff, lr, self.phase))
        else:
            self.out.write('S %s lr=%05x %s\n' % (bytes(uc.mem_read(r[1], r[3])).hex(), lr, self.phase))

    def delay(self, uc, addr, size, ud):
        self.out.write('D %d ms lr=%05x %s\n' % (uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_LR) & ~1, self.phase))

    def ticks(self, uc, access, addr, size, value, ud):
        """time passes whenever someone looks at the SysTick tick counter"""
        t = struct.unpack('<I', uc.mem_read(SYSTICK_TICKS, 4))[0]
        uc.mem_write(SYSTICK_TICKS, struct.pack('<I', (t + 1) & 0xffffffff))

    def main_code(self, uc, addr, size, ud):
        ins = next(self.md.disasm(bytes(uc.mem_read(addr, size)), addr), None)
        if ins and ins.mnemonic == 'bl':
            target = int(ins.op_str.lstrip('#'), 16)
            if target == GUI_LOOP:
                uc.emu_stop()
                return
            self.phase = 'FUN_%08x' % target
            self.out.write('# ---- %s\n' % self.phase)
            self.out.flush()

    def skip(self, uc, addr, size, ud):
        self.out.write('# skipped %s @%#x\n' % (SKIP[addr], addr))
        uc.reg_write(UC_ARM_REG_R0, 0)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR) | 1)

    def mmio(self, uc, access, addr, size, value, ud):
        pc = uc.reg_read(UC_ARM_REG_PC)
        a4 = addr & ~3
        if access == UC_MEM_WRITE:
            old = self.regs.get(a4, 0)
            sh = (addr & 3) * 8
            mask = ((1 << (8 * size)) - 1) << sh
            new = (old & ~mask) | ((value << sh) & mask)
            self.regs[a4] = new
            uc.mem_write(a4, struct.pack('<I', new))
            self.out.write('W %08x %08x sz%d pc=%05x %s\n' % (addr, value & ((1 << 8 * size) - 1), size, pc, self.phase))
            self.poll.clear()
            return
        v = self.regs.get(a4, 0)
        if a4 == 0xe0001004:                    # DWT_CYCCNT keeps running
            self.cyc = (self.cyc + 50000) & 0xffffffff; v = self.cyc
        elif a4 == 0xe000e010:                  # SysTick CTRL: COUNTFLAG
            v |= 0x10000
        elif a4 == 0xe000e018:                  # SysTick VAL counts down; a wrap = one SysTick ISR
            v = self.regs.get(a4, 0xffffff) - 0x180000
            if v < 0:
                v &= 0xffffff
                t = struct.unpack('<I', uc.mem_read(SYSTICK_TICKS, 4))[0]
                uc.mem_write(SYSTICK_TICKS, struct.pack('<I', (t + 1) & 0xffffffff))
            self.regs[a4] = v
        elif a4 in (0x401b8000, 0x401bc000, 0x401c0000, 0x401b8008, 0x401bc008, 0x401c0008):
            # GPIO data/pad status behave like pins, not like busy-wait flags: no alternation; a pin
            # switched to input reads 0 (I2C ACK from the codecs / touch controller)
            base = a4 & ~0xf
            v = self.regs.get(base, 0) & self.regs.get(base + 4, 0)
        else:
            k = (pc, a4)
            self.poll[k] += 1
            if self.poll[k] > 20:
                v = 0xffffffff if self.poll[k] & 1 else 0
        uc.mem_write(a4, struct.pack('<I', v & 0xffffffff))
        if self.poll[(pc, a4)] <= 3:
            self.out.write('R %08x %08x sz%d pc=%05x %s\n' % (addr, v, size, pc, self.phase))

    def call(self, fn, *args, data=b''):
        """trace one more function after main (args are ints; data is copied to scratch, pass DATA)"""
        uc = self.uc
        DATA = 0x2001f000
        uc.mem_write(DATA, data or b'\0')
        regs = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
        for r, v in zip(regs, args):
            uc.reg_write(r, DATA if v == 'DATA' else v)
        self.phase = 'FUN_%08x' % fn
        self.out.write('# ---- %s (chamada extra)\n' % self.phase)
        uc.reg_write(UC_ARM_REG_SP, 0x20008000)
        uc.reg_write(UC_ARM_REG_LR, STOP | 1)
        try:
            uc.emu_start(fn | 1, STOP, count=10_000_000)
        except UcError as ex:
            self.out.write('# extra call error %s pc=%#x\n' % (ex, uc.reg_read(UC_ARM_REG_PC)))

    def run(self, budget):
        uc = self.uc
        uc.reg_write(UC_ARM_REG_SP, 0x20008000)
        uc.reg_write(UC_ARM_REG_LR, STOP | 1)
        pc, skipped = MAIN, 0
        while True:
            try:
                uc.emu_start(pc | 1, STOP, count=budget)
                why = 'stopped'
                break
            except UcError as ex:
                pc = uc.reg_read(UC_ARM_REG_PC)
                hw = struct.unpack('<H', uc.mem_read(pc, 2))[0]
                # unicorn's Cortex-M7 lacks double-precision VFP (vcvt.f64 & co.): those only compute filter
                # coefficients here, so skip the 32-bit coprocessor instruction and carry on
                if ex.errno == UC_ERR_INSN_INVALID and (hw & 0xee00) == 0xee00 and skipped < 100000:
                    skipped += 1
                    pc += 4
                    continue
                why = 'error %s' % ex
                break
        self.out.write('# %d double-precision VFP instructions skipped\n' % skipped)
        pc = uc.reg_read(UC_ARM_REG_PC)
        self.out.write('# end: %s pc=%#x phase=%s\n' % (why, pc, self.phase))
        print('end: %s pc=%#x phase=%s' % (why, pc, self.phase))
        # backlight PWM, called from the skipped FUN_000399f0: FUN_00033dc0(2, &{2000 Hz, 50 %})
        self.call(0x33dc0, 2, 'DATA', data=struct.pack('<II', 2000, 50))
        self.out.write('# end extra\n')
        self.out.close()


if __name__ == '__main__':
    Tracer(sys.argv[1]).run(int(sys.argv[2]) if len(sys.argv) > 2 else 50_000_000)
