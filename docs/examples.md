# The examples

Each folder in [examples/](../examples) is a catalog the Studio can load: modules with their manifests.
`python examples/build_all.py` builds all of them (the `.axf` files are not in git).

| Example | Id | Menu / tab | Shows |
|---|---|---|---|
| [gain](#gain) | `0x01000080` | DYN / Boost | the smallest complete effect: state, smoothing, bypass ([tutorial](effects/tutorial.md)) |
| [modecho](#modecho) | `0x0B000070` | DLY / Delay | a stereo delay: big buffers in SDRAM, tails in bypass, tempo sync, lookup tables |
| [multimode](#multimode) | `0x04000070` | MOD / Special | one knob with named modes (text list) |
| [parallel](#parallel) | `0x01000070`–`72` | EQ / Routing | parallel paths from three ordinary modules that find each other at run time |
| [nam_a2](#nam_a2) | `0x07000100` | AMP / NAM | a NAM A2-Lite capture compiled into an effect |
| [nam_clone](#nam_clone) | `0x0F000000` / `0x0F100000` | Clone | a replacement engine for the Clone menu that plays NAM captures from the Clone slots |

## gain

A boost: drive into a soft clipper, a one-pole low-pass for tone, an output level. About 60 lines, 3 KB of
module. It is the [tutorial](effects/tutorial.md) effect.

## modecho

"Mod Echo", the first module built with this SDK and run on the pedal. A 4-second stereo delay whose read
head is modulated by an LFO.

- **Big buffers in SDRAM.** `MB_SRAM static float dline[176400 * 2];` puts the 1.4 MB delay line in
  `ER_SRAM`; the small state stays in `ER_IRAM`. Init clears the line, because the loader does not.
- **Constant tables in `ER_SROM`.** `sine_tab.h` is a `const` table for the LFO, interpolated linearly.
- **Bypass with tails.** In bypass the input to the delay line is muted but the echoes keep playing out, like
  the factory delays.
- **Tempo sync.** Time (parameter 1, `f2 = 0x15`) and Sync (parameter 5, `f2 = 0x21`) are a pair; with Sync on
  `mb_sync_ms(knobs[1], beat_ms)` gives the period. Since Init does not know the tempo, the first Process snaps
  the delay time to the right value instead of sliding to it. Compared with the factory "Pure" delay at four
  tempos × 11 divisions, the period matches within one sample.
- Parameter smoothing per sample and a low-pass in the feedback path.

## multimode

"Multi Mode": tremolo, vibrato, ring modulator, bitcrusher and octave fuzz in one module, chosen by a Mode knob
whose manifest has `"options": ["Tremolo", "Vibrato", "Ring Mod", "Bitcrush", "Octave Fuzz"]`. With the
[textlist app patch](app-patches.md#textlist-knobs-with-named-options) the pedal shows those names; without it,
0–4. The module just rounds `knobs[0]` to a mode and resets the state of the new mode when it changes.

## parallel

Three modules give the pedal a parallel effect path **without changing the app**:

```
... -> [Split] -> A effects -> [Path B] -> B effects -> [Merge] -> ...
```

- **Split** (`0x01000070`, Mode: Mono L / Mono R / Stereo) copies the signal into its own buffer.
- **Path B** (`0x01000071`, Input in dB) swaps the chain signal with that buffer: the B effects process the
  split signal while A's output is parked. In bypass, B is silent in the mix.
- **Merge** (`0x01000072`, Level A/B, Pan A/B, Phase B) mixes A and B back, with per-block gain ramps. Without
  Path B, B is the dry signal (parallel compression, for instance). In bypass it sums A and B at 0 dB.

How it works, in [route.h](../examples/parallel/route.h): the app runs the chain's modules one after the
other on one buffer, so a module that saves and swaps that buffer can build a second path. Split publishes a
pointer to its state right after the standard module header. Path B and Merge scan the app's table of loaded
module headers (`0x20241f44`) for the Split's id and magic, and only act if Split ran **earlier in the same
block**: Split records the byte the app flips at every block (`0x20234025`) and a sequence number. A missing,
bypassed or misplaced Split leaves them transparent, so a broken order degrades to a plain serial chain.

Limits: one parallel section per preset; it takes 3 of the 12 slots; with the output in "cab on L/R only"
mode, a Cab/IR inside a path runs after the Merge. `test_parallel.py` checks 11 cases in the emulator (the
factory Pure delay on path A, Guitar EQ 1 on path B) against the effects run alone and mixed in Python.

The [parallel app patch](app-patches.md#parallel-two-row-chain-display) draws such a chain as two rows.

## nam_a2

A real [NAM](https://github.com/sdatkinson/neural-amp-modeler) network, A2-Lite (the 3-channel WaveNet of
NAM's A2 architecture), compiled into an amp effect:

```sh
python examples/nam_a2/build_nam.py your_capture.nam --name "My Amp"
```

No capture comes with the repository: use an A2 capture of your own (`examples/build_all.py` builds the first
`.nam` it finds in `examples/nam_a2/models/`).

`build_nam.py` extracts the 3-channel network from the `.nam` file (`matribox/nam/nam_a2.py`), writes its 1871
weights as a C array, and links them with [nam_mod.c](../examples/nam_a2/nam_mod.c) and the engine in
`nam/a2lite.c`. Knobs: Gain (input drive, dB) and Level (dB, after loudness normalisation to −18 dB like the
plugin). The model runs at 44.1 kHz. Details on the [NAM page](nam.md).

## nam_clone

A module that replaces the engine of the Clone menu: it reads a NAM A2-Lite network from the Clone slot the
block uses and plays it, resampling 44.1 ↔ 48 kHz so that 48 kHz captures sound right. Captures are written to
the slots over USB (`matribox nam send`), with no firmware change per capture. Slots that hold a factory
capture play dry, unless the `clonedual` app patch is used, which lets each slot run the engine its data needs.
`build_clone.py` builds the module and, optionally, a firmware with it; `test_dual.py` and `test_tabs.py` check
the dual engine and the Clone tabs in the emulated app. Details on the [NAM page](nam.md).
