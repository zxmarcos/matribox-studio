# The module ABI

An effect module is an ARM ELF that the app's own loader relocates into its memory pools and calls through two
function pointers. The factory modules are Keil builds (`Algorithm.axf`); the SDK builds the same kind of file
with Zig's clang and lld (`matribox/module/buildmod.py`). Everything below is what the loader and the audio loop
of V1.2.2 expect. [sdk/matribox_module.h](../../sdk/matribox_module.h) encodes it.

## The two entry points

```c
void Init(const float *knobs, void *ctx);
void Process(float *io, uint16_t nframes, const float *knobs, float *aux,
             const uint8_t *enabled, const uint32_t *beat_ms, float **extra);
```

**Init** runs whenever the app (re)loads the chain: preset change, inserting, removing or moving a block, and
a few other edits. It gets the current knob values. `ctx` is only meaningful for the engines of the special
menus (Cab, User IR, Clone get the flash address of their IR or capture in `ctx[0]`); other effects ignore it.

**Process** runs once per audio block, for every slot of the chain, **whether the block is on or off**:

| Argument | Meaning |
|---|---|
| `io` | `nframes` stereo frames, interleaved (L, R, L, R…), 32-bit float; read and write in place |
| `nframes` | 64 |
| `knobs` | the 15 parameter values of this slot, indexed by each parameter's `idx` |
| `aux` | the dry guitar before the chain (sidechain); see [the audio engine](../firmware/audio-engine.md) |
| `enabled` | `*enabled == 0` when the block is bypassed |
| `beat_ms` | `*beat_ms & 0xffff` = length of a beat in ms at the current tempo |
| `extra` | FX loop send and return buffers; only the FX Loop modules use them |

**Bypass is the module's job.** When `*enabled` is 0 the output must be the dry input. The module can keep
running underneath (a delay can let its repeats ring out, as `examples/modecho` does) or simply return.

The sample rate is **44.1 kHz** and the signal's full scale is about ±2.19 (see
[the full-scale level](../firmware/audio-engine.md#the-full-scale-level)).

## The module header

The first bytes of the code region are a header the app reads:

| Offset | Field |
|---|---|
| `+0x00` | written by the app: `0xFFAA0000 | instance` |
| `+0x04`–`+0x13` | reserved (0) |
| `+0x14` | effect id: **must equal the library entry's id** |
| `+0x18` | address of the read-only data (`ER_SROM` base) |
| `+0x1C` | `Init` |
| `+0x20` | `Process` |
| `+0x24` | magic `0xFFAA55AA` |

In C, one line declares it:

```c
MB_MODULE(0x01000080, GainInit, GainProcess);
```

The app keeps a table of the headers of the loaded modules (`0x20241f44`, 12 entries); this is how modules can
find each other ([the parallel routing modules](../examples.md#parallel) do).

## Memory regions

The loader picks a pool for each section **by its name** and relocates by the high nibble of the link
address:

| Section | Link address | Goes to | Pool size | What to put there |
|---|---|---|---|---|
| `ER_IROM` | `0x0xxxxxxx` | ITCM | 128 KB | code and the header (`.text`) |
| `ER_SROM` | `0x1xxxxxxx` | SDRAM | 128 KB | constant tables (`const`, `.rodata`) |
| `ER_IRAM` | `0x2xxxxxxx` | OCRAM | 192 KB | state: every `static` variable (`.data`, `.bss`) |
| `ER_SRAM` | `0x3xxxxxxx` | SDRAM | ≈ 6.8 MB | big buffers, marked `MB_SRAM` (`.bss.sram`) |

The pools are **shared by every module of the preset**, so your sizes reduce what the other effects can use;
the app refuses to insert an effect that does not fit ([memory check](../firmware/audio-engine.md#system-overload)).
Each slot loads its own copy of the module: two instances of the same effect have separate state.

Two things the loader does **not** do:

- **It does not zero RAM.** Static variables start with whatever the previous preset left. `Init` must
  initialise every piece of state, delay lines included.
- **It does not run constructors** (`.init_array` is discarded).

## Relocations and code generation

The loader understands `SHT_REL` relocations against the four regions: `R_ARM_ABS32` and `R_ARM_REL32` take
the addend stored in place, and `R_ARM_MOVW_ABS_NC`/`R_ARM_MOVT_ABS` **ignore the addend** (it uses the symbol
value only). The SDK therefore compiles with `-mcpu=cortex_m7+no_movt`: addresses are loaded from literal
pools. `buildmod.py` links with `--emit-relocs`, keeps the four `ER_*` sections, turns every absolute reference
into an `ABS32` against a per-region section symbol with the offset in place, drops PC-relative references
that stay inside a region, and rejects anything else (references between regions by PC-relative code,
unknown relocation types, undefined symbols).

Rules that follow:

- **No imports.** A module cannot call the app or a C library. `sdk/mb_rt.c` provides `memset`/`memcpy` and the
  `__aeabi_mem*` helpers the compiler emits; `matribox_module.h` has `mb_exp2f`, `mb_db_to_gain`,
  `mb_level_gain`, `mb_clampf` and `mb_sync_ms`. Write or copy the math you need (sine tables, polynomial
  approximations); `sinf`/`expf` from a libm are not available.
- **Hard-float ABI, FPv5 with double precision** (`-mfloat-abi=hard`, `fp_armv8d16`). Single precision is
  what the hardware is fast at; use `float` literals (`0.5f`).
- **The packed ELF must be ≤ 32 KB**: the app copies it to a 32 KB staging buffer per slot before loading.
  Tables count; large constant tables can be computed in `Init` into `ER_SRAM` or `ER_IRAM` instead.
- A source file can add compiler flags for itself with a line `/* buildmod-cflags: -Os */`; `MB_CFLAGS` in the
  environment adds flags to every file (the NAM examples pass `-I nam` and defines this way).

## Talking to the app (advanced)

A module can read app memory directly; the examples do it in two places, both specific to V1.2.2:

- the table of loaded module headers at `0x20241f44` and the byte at `0x20234025` that flips at every audio
  block (parallel routing);
- the block time at `0x20238198` (float ms) and a free word at `0x202382A0` that the PC can read as global
  setting 31 (the NAM Clone module's CPU meter).

Anything like this ties the module to that firmware version; keep it in a header such as
[examples/parallel/route.h](../../examples/parallel/route.h).
