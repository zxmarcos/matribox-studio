# Testing and performance

## The emulated loader

`matribox/module/effemu.py` is a Python port of the app's module loader (`FUN_0004029c`, `FUN_000405c8`,
`FUN_00040b1c`) on top of the unicorn emulator, running the module's own Thumb-2 code as a Cortex-M7:

```python
from matribox.module.effemu import Emu

emu = Emu(open('examples/gain/gain.axf', 'rb').read())   # load, relocate, find Init/Process
out = emu.run(knobs=[6, 70, 0] + [0] * 13,               # Init, then Process block by block
              audio=[(x, x) for x in signal],            # list of (L, R) frames
              beat_ms=500, enabled=1)
```

- Pools have the device's addresses and sizes; `Emu(fill=0xA5)` fills RAM with garbage first, which catches
  state that `Init` forgets to set.
- `emu.load(elf)` loads more modules after the first, at non-zero offsets in every pool (the way a chain is
  loaded), and registers them in the module header table; `emu.run_chain(...)` runs several modules in series.
  [examples/parallel/test_parallel.py](../../examples/parallel/test_parallel.py) uses it to check Split / Path
  B / Merge around factory effects.
- `ctx` passes the bytes the app gives to `Init` (Cab, User IR and Clone engines read a flash pointer there).
- Factory modules for comparisons come from your own firmware file:
  `matribox.firmware.stock.factory_module(menu, slot)` / `factory_entry(menu, slot)` (with the parameter
  defaults), and the whole app as an ELF from `stock.app_elf()`.

### Bit-exact testing

The pattern the examples and ports use: compile the same C source for the PC (with `-ffp-contract=off`, so no
fused multiply-adds) and compare its output with a reference, then compare the ARM build in the emulator with
the PC build. `nam/test_a2.py` compares the A2-Lite engine with the official NeuralAmpModelerCore this way and
reaches identical samples on the PC; the ARM build differs by ~1e-6 because the M7 uses FMA.

## CPU budget

The audio block is 1.451 ms. The app raises "System overload" when **the whole audio loop** takes more than
**1.25 ms in three consecutive blocks**; the app itself takes about 165 µs, so the chain has about **1 ms per
block**, shared by all of its effects. For reference, measured on the pedal: a factory Cab/IR ≈ 190 µs, the NAM
A2-Lite network ≈ 460 µs.

To estimate a module's cost, count the instructions of `Process` in the emulator with a code hook (as
`nam/test_a2.py` does) and divide by 64 frames. On the pedal the M7 ran at about **1.4–1.5 cycles per
emulated instruction** for compute-bound code; code that streams through SDRAM or flash data is much slower
(5+ cycles per instruction were measured). 600 MHz × 1 ms = 600 000 cycles per block for the chain.

Where the time goes, from experience with the NAM module:

- **Memory traffic dominates** once the FPU is busy. The D-cache is 32 KB and the M7 has no data prefetcher:
  data read every block should be small and in OCRAM (`ER_IRAM`) or the ITCM (writable code-region data, see
  [the NAM page](../nam.md)); `PLD` hints help with streams.
- Independent accumulator chains hide the FPU's latency: processing two samples per loop iteration (six
  `VFMA` chains instead of three) cut the NAM network by 30%.
- Per-block work (coefficients, ramps) is free compared with per-sample work.

## Measuring on the pedal

The app measures the block time itself and keeps it at `0x20238198` (float ms: last, peak, max, min). A module
can read it, and its own time with SysTick (`0xE000E018`, counting down at 600 MHz, reload every 1 ms), and
publish them in the free word `0x202382A0`: the PC reads that word as global setting 31 (`matribox nam cpu`
does this for the NAM Clone module, see `examples/nam_clone/nam_clone.c`).

## Memory budget

| Pool | Size | Typical users |
|---|---|---|
| ITCM (`ER_IROM`) | 128 KB | code; ~2–30 KB per effect |
| OCRAM (`ER_IRAM`) | 192 KB | Cab/IR ≈ 70 KB, factory Clone ≈ 74 KB, NAM ≈ 100–115 KB |
| SDRAM const (`ER_SROM`) | 128 KB | tables |
| SDRAM (`ER_SRAM`) | ≈ 6.8 MB | delay lines: the factory 4 s stereo delays use 1.4 MB |

The build report of `buildmod` prints the size of every region; `effemu` prints how much of each pool the
module used.
