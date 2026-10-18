# The audio engine

## The audio loop

Audio runs at **44.1 kHz in blocks of 64 frames** (1.451 ms). The DMA interrupt of SAI1 calls the audio loop
`FUN_00031e18` once per block. In order, it:

1. **Builds the input.** The guitar is the difference of the two inputs of the first ADC
   (`(p3/2 − p0/2) × input gain`), passed through a fixed biquad. The second ADC pair is the FX Loop return;
   the third is AUX IN. The guitar goes to both channels of `io`, an interleaved stereo float buffer
   (L, R, L, R…). USB playback channel 2 is added here (re-amping); the looper in "pre" mode and the preset
   change fade also act here.
2. **Runs the chain.** If the chain is ready (a flag the app clears while it loads a preset), it computes the
   beat length `60000 / BPM` (global or preset tempo) and calls the `Process` function of every module, **one
   after the other in chain order, all on the same `io` buffer**:

   ```c
   hdr[slot]->Process(io, 64, knobs[slot], aux, &on[slot], &beat_ms, extra);
   ```

   Then the preset volume is applied.
3. **After the chain**: the looper in "post" mode, USB recording (dry or processed), the global EQ (up to 7
   biquads), the drum machine, the meters, AUX IN and USB playback channels 0/1 (they never go through the
   effects), a final biquad, the MASTER VOLUME knob, and the output to the DAC frame
   `{L, FX send, R, 0}`. The tuner is fed with the dry guitar.
4. **Measures its own time** (see "System overload" below).

Notes on the chain:

- The app does not measure or reorder modules; it only calls them.
- With the output set to "cab on L only / R only", Cab and User IR slots (menus 5/6) are deferred and run after
  the whole chain on the chosen channel only.
- A chain has at most **12 slots**.

### The full-scale level

The ADC is scaled so that its full scale is about **±2.19** in the float chain (`1.0198e-9 × 2³¹`), not ±1.
Effects that model analog gear need to know this; the NAM module scales its input by `0.45659` so that
interface full scale is ±1.0 as in the NAM plugin.

### Buffers shared with every module

| Argument | Content | Used by |
|---|---|---|
| `io` | the chain signal, interleaved stereo, processed in place | everyone |
| `aux` | the guitar before the chain (with the looper "pre" and the fade) plus the second ADC pair | the gates, as sidechain |
| `extra[0]` | the FX Loop send, starts every block with a constant, has its own DAC channel | FX Loop / SND |
| `extra[1]` | the FX Loop return (second ADC pair) | FX Loop / RTN |
| `beat_ms` | pointer to the beat length in ms (only the low 16 bits are valid) | tempo-synced effects |

## Where modules live in memory

The app's loader copies each module's sections into four pools shared by all the effects of the preset:

| Module region | Pool | Address | Size |
|---|---|---|---|
| `ER_IROM` (code, module header) | ITCM | `0x58000` | 128 KB |
| `ER_SROM` (read-only data) | SDRAM | `0x81301000` | 128 KB |
| `ER_IRAM` (state) | OCRAM | `0x20204000` | 192 KB |
| `ER_SRAM` (large buffers) | SDRAM | `0x81321000` | ≈ 6.8 MB |

The ELF itself is first copied to a 32 KB staging slot per chain slot (`0x819f0000 + slot × 0x8000`). The
memory map set by the app's MPU makes the ITCM writable and executable, OCRAM and SDRAM cached write-back, and
the flash at `0x60000000` **uncached**: data read from flash every block is slow.

How fast each memory is matters more than how much there is. Measured on the pedal with the NAM module:
moving 7.5 KB of weights read every block from SDRAM to the ITCM saved 52 µs per block, and a resampler whose
tables were in SDRAM ran at 5.6 cycles per instruction because the NAM network had evicted the cache. Keep the
data you touch every block in OCRAM (`ER_IRAM`) or the ITCM; use SDRAM (`ER_SRAM`) for delay lines and other
data that is read sparsely.

There is no free TCM to grab: the ITCM holds about 343 KB of app code plus the 128 KB pool, and the DTCM's
32 KB are the stack.

## "System overload"

Two checks raise the flag that shows "CAUTION! System overload. Please try to reorganize your effect chain."

**CPU time, while playing.** The audio loop reads SysTick at its start and at its end (600 000 ticks per
ms) and stores the block time in ms at `0x20238198` (last, peak, max, min). A block **over 1.25 ms** (86% of the
1.451 ms block) increments a counter and any block under it clears the counter; **at the third block in a
row** the app switches every slot off (they are bypassed, not removed), marks the preset as modified and
shows the warning. The budget covers the whole loop: the app's own work (looper, global EQ, drums, USB,
meters) costs about 165 µs, so the modules get roughly **1 ms per block**, i.e. about 600 000 cycles for the
whole chain.

**Memory, when an effect is inserted.** Before inserting or replacing a block, the app measures the module's
sections with the loader and compares them with the free space of each pool (`0x202448d0`: four
{base, capacity, used}). If something does not fit the effect is refused and the warning is shown with flag 2.
For instance, the factory Cab/IR engine needs ≈ 70 KB of OCRAM and the factory Clone ≈ 74 KB, so a Clone and a
Cab already use 144 of the 192 KB.

## The Cab and User IR engine

One module (`CabIrInit` / `CabIrProcess`) serves menus 5 and 6. At Init the app passes, instead of the
`beat_ms` pointer, the address of the IR in the memory-mapped flash; the module reads it from there.

- An IR is **1024 int32 samples** (24 useful bits, scale 2⁻²³): 23.2 ms at 44.1 kHz, 4 KB, always that size.
- Convolution: uniform partitioned overlap-save with CMSIS-DSP, 16 partitions of 64 samples, 128-point FFTs;
  no added latency. It reads the **left channel only** and writes the same result to L and R: the cab is mono.
- Then Low Cut (19–2000 Hz) and High Cut (2000–20001 Hz) biquads and the Volume. 19 Hz and 20001 Hz mean off.
- The IR section (`0x700000`) has a directory of 91 entries of 72 bytes: 61 factory cabs, 20 "User IR n" and 10
  "Clone n" names. User IR data lives in its own file system at `0x753000`.

## The factory Clone engine

The factory Clone does **not** capture on the pedal. The PC editor imports a `.nam` file, runs it on a test
signal and fits a model of its own (a Wiener-Hammerstein structure) to the input/output pair; the pedal plays
that model:

1. an IIR filter (double precision);
2. a partitioned convolution (IR 1);
3. a static nonlinearity with 4× oversampling, an asymmetric exponential saturation
   `y = p·(1 − e^(−q·x))` for x > 0 and `y = −p'·(1 − e^(q'·x))` for x ≤ 0;
4. an IIR filter;
5. a second partitioned convolution (IR 2);

plus Bass/Middle/Treble biquads and Gain/Volume. The 10 captures live in the Clone file system at `0x40A000`
(0x2000 bytes per slot). See [NAM on the pedal](../nam.md) for running real NAM networks instead.
