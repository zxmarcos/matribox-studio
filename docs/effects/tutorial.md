# Tutorial: a new effect from scratch

This walks through [examples/gain](../../examples/gain): a boost with soft clipping, a tone control and an
output level, added to the DYN menu next to the factory boosts. The same steps make any effect.

## 0. What you need

- Python 3.10+ and this repository installed with `pip install -e .`
- [Zig](https://ziglang.org/) 0.14 on the `PATH` (or in `$ZIG`): its clang compiles for the Cortex-M7 and its
  lld links
- the official `QME-200_FW_USB_V1.2.2.bin` in `firmware/`
- for the emulator: `pip install unicorn capstone`

## 1. The C file

An effect is a C file that includes the SDK header and declares its two entry points.

```c
#include "matribox_module.h"

static float gain_s, level_s, coef_s;    /* smoothed gain, level and low-pass coefficient */
static float lp_l, lp_r;                 /* tone filter state, one per channel */
```

Every `static` variable becomes module state in `ER_IRAM` (fast OCRAM). The loader does not clear this memory
and leaves whatever the previous preset had in it, so **Init has to set every variable**:

```c
static void GainInit(const float *knobs, void *ctx)
{
    (void)ctx;
    gain_s = mb_db_to_gain(knobs[0]);
    level_s = mb_level_gain(knobs[2]);
    coef_s = tone_coef(knobs[1]);
    lp_l = lp_r = 0.0f;
}
```

`knobs[i]` is the value of the parameter whose `idx` is `i` in the manifest (step 3). Starting the smoothed
values at their targets avoids a fade-in every time the chain is reloaded.

`Process` is called every 1.45 ms with 64 stereo frames:

```c
static void GainProcess(float *io, uint16_t nframes, const float *knobs, float *aux,
                        const uint8_t *enabled, const uint32_t *beat_ms, float **extra)
{
    (void)aux; (void)beat_ms; (void)extra;
    if (!*enabled)                       /* bypass: leave the signal as it came in */
        return;

    const float gain_t = mb_db_to_gain(mb_clampf(knobs[0], 0.0f, 24.0f));
    const float level_t = mb_level_gain(mb_clampf(knobs[2], -61.0f, 6.0f));
    const float coef_t = tone_coef(knobs[1]);

    for (uint16_t n = 0; n < nframes; n++) {
        gain_s += 0.002f * (gain_t - gain_s);
        level_s += 0.002f * (level_t - level_s);
        coef_s += 0.002f * (coef_t - coef_s);

        float l = soft_clip(io[2 * n] * gain_s);
        float r = soft_clip(io[2 * n + 1] * gain_s);
        lp_l += coef_s * (l - lp_l);
        lp_r += coef_s * (r - lp_r);
        io[2 * n] = lp_l * level_s;
        io[2 * n + 1] = lp_r * level_s;
    }
}
```

Things to notice:

- **Bypass is your job.** The app calls `Process` even when the block is off; returning without touching `io`
  passes the signal through. An effect with tails (delay, reverb) keeps running and only stops feeding new
  input.
- **Expensive math once per block, cheap math per sample.** The targets are computed once per block, then each
  sample moves towards them with a one-pole smoother, so turning a knob does not click.
- **No C library.** `mb_db_to_gain` (dB to linear) and `mb_clampf` come from the SDK header; the tone
  coefficient uses `mb_exp2f` instead of `expf`:

  ```c
  static float tone_coef(float tone)     /* cutoff 800 Hz (0) .. 12.8 kHz (100) */
  {
      float fc = 800.0f * mb_exp2f(mb_clampf(tone, 0.0f, 100.0f) * 0.04f);
      return 1.0f - mb_exp2f(-TWO_PI_OVER_LN2 * fc / MB_SAMPLE_RATE);
  }
  ```

- **Mind the level.** The chain signal reaches about ±2.19 at the ADC's full scale. The drive curve here is a
  rational approximation of `tanh`, linear for small signals and flat beyond ±3.

The last line puts the module header (with the id) at the start of the code:

```c
MB_MODULE(0x01000080, GainInit, GainProcess);
```

## 2. Pick an id and a menu

The id must be unique in the library; `matribox fw list firmware/QME-200_FW_USB_V1.2.2.bin` lists the factory
ones. The high byte is the family by convention (`0x01` here). The menu decides where the effect appears:
0 DYN, 1 FX, 2 WAH, 3 DRV, 4 AMP, 7 EQ, 8 MOD, 9 DLY, 10 RVB (5, 6 and 11 cannot list new effects). See
[the effect library](../firmware/effect-library.md#menus).

## 3. The manifest

[gain.json](../../examples/gain/gain.json) describes the library entry:

```json
{
  "elf": "gain.axf",
  "id": "0x01000080",
  "name": "Gain Boost",
  "category": "Boost",
  "menu": 0,
  "params": [
    {"name": "Gain", "min": 0, "max": 24, "step": 1, "default": 6, "flags": [255, 0, 116]},
    {"name": "Tone", "min": 0, "max": 100, "step": 1, "default": 70, "flags": [255, 0, 21]},
    {"name": "Level", "min": -61, "max": 6, "step": 1, "default": 0, "flags": [255, 0, 116]}
  ]
}
```

- `category` is also the tab: "Boost" already exists in menu 0, so the effect joins that tab. A new category
  makes a new tab.
- Parameters get `idx` 0, 1, 2 in this order (set `"idx"` to place them elsewhere in `knobs[]`).
- `flags` = `[f1, f2, f3]`: `f3 = 116` (`0x74`) shows dB with "Mute" below −60, `21` (`0x15`) a plain
  number. All fields: [manifest reference](manifest.md).

## 4. Build

```sh
python examples/build_all.py gain
# or, for any module:
python -m matribox build-module examples/gain/gain.axf examples/gain/gain.c
```

```
examples/gain/gain.axf: 3020 bytes, regions {'ER_IROM': '0x8f8', 'ER_SROM': '0x4', 'ER_IRAM': '0x14'}, 13 ABS32 relocs
```

The report shows what the effect costs in each pool: 2.3 KB of ITCM code and 20 bytes of state.

## 5. Run it in the emulator

`matribox emulate` loads the `.axf` with a Python port of the app's loader and runs `Init` and `Process` on a
Cortex-M7 emulator (unicorn), with the knob values given on the command line, on a test signal (an impulse,
then a 220 Hz burst):

```sh
python -m matribox emulate examples/gain/gain.axf 6 70 0
```

```
id=0x01000080 init=0x58029 process=0x58311 relocs=13 pools={0: '0x8f8', 1: '0x4', 2: '0x14', 3: '0x0'}
  t=0.25s rms=0.58354
```

This catches the mistakes that would otherwise crash the pedal: a bad relocation, an uninitialised pointer, a
hard fault. For real tests, drive the module from Python with `matribox.module.effemu.Emu` and compare it with a
reference model ([testing and performance](testing.md)).

## 6. Put it in a firmware

From the command line:

```sh
python -m matribox fw add firmware/QME-200_FW_USB_V1.2.2.bin examples/gain/gain.json gain_fw.bin
```

```
menu 0, subgroup 1 (Boost), position 9
```

Or in the Studio (`python -m matribox studio`): add `examples/gain` to the libraries, tick "Gain Boost", and
build. The Studio can also remove factory effects to make room, apply app patches, and write the matching
`alg_data.json` for the PC editor.

Only section 1 (the library) changes; the app, the IRs and the presets are the original ones.

## 7. Flash and play

Flash `gain_fw.bin` with the Studio ("Atualizar o pedal"), `python -m matribox update flash gain_fw.bin`,
or the official PC editor. The effect is in DYN, tab Boost. If anything goes wrong, power the pedal on with the
recovery footswitch combination and flash the official file.

Before relying on a new effect in a big preset, check its CPU cost: the whole chain must stay under about
1 ms per block ([testing and performance](testing.md)).
