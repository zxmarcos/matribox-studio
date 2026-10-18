# NAM on the pedal

[Neural Amp Modeler](https://github.com/sdatkinson/neural-amp-modeler) captures are neural networks. The
factory Clone does not run them: the PC editor fits a much simpler model to a capture and the pedal plays that
(see [the factory Clone engine](firmware/audio-engine.md#the-factory-clone-engine)). This SDK runs the network
itself, sample for sample the same as NAM's own code, within the pedal's CPU budget.

There are three ways to use it:

| | What | Captures come from | Example |
|---|---|---|---|
| NAM effect | a capture compiled into an effect of the AMP menu | the firmware image | [examples/nam_a2](../examples/nam_a2) |
| NAM Clone engine | the Clone menu plays NAM captures from its 10 slots | written over USB, any time | [examples/nam_clone](../examples/nam_clone) |
| Dual Clone engine | each Clone slot runs the factory engine or NAM, by what it holds | written over USB | `nam_clone` + app patches `clonedual` (and `clonetabs`) |

## The A2-Lite network

NAM's A2 architecture (NeuralAmpModelerCore 0.7, `NAM/wavenet/a2_fast.cpp`) is a WaveNet with one layer array
of 23 layers:

- kernels 6×14, 15, 15, 6×7; dilations {1, 3, 7, 17, 41, 101, 239} × 2, {1, 13} and {1, 3, …, 239};
- LeakyReLU(0.01), no gating, a 1×1 layer per layer, a head convolution of 16 taps with bias, and a final
  `head_scale` weight;
- **Lite = 3 channels: 1871 weights, about 1730 multiply-adds per sample.** Full A2 has 8 channels, too much
  for the M7.

An A2 `.nam` file is a `SlimmableContainer` holding both networks; `matribox/nam/nam_a2.py` extracts the
3-channel one, checks its shape the way the core's `is_a2_shape()` does, and packs the weights in the order the
C engine reads them. Only A2 captures work: the older "standard" A1 captures (16 channels) have no 3-channel
network.

## The engine: `nam/a2lite.c`

Portable C, with the same operation order per channel as `a2_fast`, so a PC build without fused
multiply-adds produces **the same samples as the official core**. `nam/test_a2.py model.nam` checks exactly
that against `ref_render` (a small program built from [nam/ref_render.cpp](../nam/ref_render.cpp) and the
NeuralAmpModelerCore sources, configured with `nam_ref_render` in `workspace.json`), and with `--emu` checks the
ARM build in the emulator (differences ≈ 1e-6, from the M7's FMA).

How it fits:

- **Rings instead of copies.** Each layer keeps the history of its input in a ring of `lookback + block`
  columns; tap pairs are cut only where their two taps wrap, so reads stay contiguous. The residual of one layer
  is written straight into the next layer's ring.
- **Hand-written M7 assembly for the hot loops** (ARM build only): `a2m7_pass2` runs two tap pairs over **two
  samples per iteration**, with the 18 weights in `s0`–`s17` and six independent `VFMA` chains (twice as many
  as one sample gives, enough to hide the FPU latency); `a2m7_post` does the mix-in, LeakyReLU (`vmaxnm`), head
  accumulation and residual, also two samples at a time. Bit-identical to the C build of the same engine.
- **Prefetch** (`PLD`) 96 bytes ahead on both streams.
- **Weights in the ITCM.** The weights are a writable array in a code section (`.text.weights`); the loader
  copies code sections to the ITCM pool, and the app's MPU leaves the ITCM writable.
- The "prewarm" is computed directly as the steady state of a silent input.

Cost on the pedal: the network takes **≈ 460 µs per block** (it was 680 µs before the assembly and the
memory placement); about 100 KB of OCRAM; 12 KB of code plus 7.5 KB of weights.

## The NAM effect (`examples/nam_a2`)

```sh
python examples/nam_a2/build_nam.py capture.nam --name "My Amp" [--id 0x07000101]
python -m matribox fw add firmware/QME-200_FW_USB_V1.2.2.bin examples/nam_a2/gen/my_amp.json my_amp_fw.bin
```

- Menu AMP, tab "NAM"; knobs Gain and Level in dB.
- Input `(L + R) / 2 × 0.45659`: the chain's full scale is ±2.19 and NAM expects ±1.0 at the interface's full
  scale. Output on both channels, normalised to −18 dB loudness like the plugin.
- Runs at 44.1 kHz: a 48 kHz capture sounds about 8% lower in frequency. The Clone engine resamples.
- Each capture is its own effect and its own id; the weights are inside the firmware.

## The NAM Clone engine (`examples/nam_clone`)

The module replaces the Clone menu's engine (entry 11.0, id `0x0F000000`) and keeps its header, name and five
knobs, so presets and the PC editor still match. At Init the app hands it the flash address of the slot's data
(`ctx[0]`); the module validates the blob (magic, version, number of weights, CRC-32) and loads the weights.
A slot that does not hold a NAM blob (a factory capture, or nothing) plays **dry**.

Knobs: Gain and Volume `(k − 50) × 0.4 dB` (Volume 0 = mute); Bass, Middle, Treble as a 100 Hz shelf, a 700 Hz
peak (Q 0.7) and a 3 kHz shelf of `(k − 50) × 0.24 dB`.

### Resampling 44.1 ↔ 48 kHz (`nam/rs48.c`)

NAM captures are trained at 48 kHz; the module runs the network at 48 kHz when the blob says so. Two rational
stages each way, through 73.5 kHz:

- up: 44.1 k × 5/3 (120 taps), then 73.5 k × 32/49 (1280-tap prototype, 70 dB);
- down: 48 k × 49/32, then 73.5 k × 3/5 (240 taps, 71 dB).

Flat within ±0.005 dB to 19 kHz, −0.01 dB at 20 kHz; latency 1.36 ms (60 samples); per-phase tables in OCRAM
(11.6 KB). The up stage produces 69 or 70 samples per block, so the engine is built with `A2_BLOCK=72`.
`nam/test_rs48.py` checks it against a float64 reference and the module in the emulator. A blob flag
(`--native-rate`) runs a 48 kHz capture at 44.1 kHz without resampling.

Cost on the pedal, 48 kHz capture with an IR in the chain: network 461 µs, resampler 88 µs, the Clone's
Process 559 µs, **the whole audio block 913 µs of the 1250 µs limit**.

### Captures in the Clone slots

The blob ([nam/a2blob.h](../nam/a2blob.h)) is 7620 bytes and fits the 8 KB of a slot:

```
0x00 "NAM2"   0x04 u16 version   0x06 u16 number of weights   0x08 f32 loudness   0x0c f32 sample rate
0x10 u32 CRC-32 of the weights   0x14 u32 flags (bit 0: run at 44.1 kHz)
0x18 zeros up to 0x88            0x88 f32 weights[1871]
```

The zeros matter: the factory Clone engine reads two IR lengths at `+0x7c` and `+0x84` and leaves the slot
silent when they are 0, so a NAM slot played by the factory engine is muted instead of crashing it.

```sh
python -m matribox nam ports                       # MIDI ports
python -m matribox nam list                        # names in the 10 slots
python -m matribox nam send capture.nam --slot 3 --name "My Amp"
python -m matribox nam cpu                         # live CPU meter of the module
```

Writing a slot **replaces the capture that was there**, and the pedal cannot give slot data back: keep your own
copies. The Studio's NAM tab does the same with a capture library browser and keeps a log of what went where.

## Factory and NAM captures side by side: `clonedual` and `clonetabs`

With the NAM engine alone, factory captures play dry. The `clonedual` app patch keeps both engines: the Clone
menu gets two library entries (11.0 = factory engine, 11.1 = NAM, id `0x0F100000`, same knobs), and the app
picks the entry **per slot**, by whether the slot's data starts with `"NAM2"`. UI, presets, ids and the USB
protocol do not change. `clonetabs` adds "Native" and "NAM A2" tabs to the Clone menu's effect list, listing
the slots of each kind. See [app patches](app-patches.md#clonedual-factory-and-nam-engines-per-slot).

In the Studio, "Gerar firmware" → "Motor do Clone" chooses factory, NAM or both (both requires a base with the
`clonedual` patch). From the command line:

```sh
python -m matribox patch-app --features=parallel,clonedual,clonetabs base.bin app.bin
python examples/nam_clone/build_clone.py --dual app.bin firmware_with_nam.bin
```
