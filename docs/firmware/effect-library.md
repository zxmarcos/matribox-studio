# The effect library (section 1)

The app knows nothing about individual effects. Every effect is an entry of the library: a header with its
id, name, category and parameter descriptions, followed by the module that runs it (an ELF, see
[the module ABI](../effects/module-abi.md)). Names, knob ranges and display formats on the pedal's screen all
come from here, which is why a new effect needs no change to the app.

The library is written to flash at `0x480000`; the partition ends at `0x700000`, so the whole section must
stay below **2.5 MB** (`0x280000`). The factory library is `0x20ba50` bytes (179 entries), which leaves about
470 KB for additions.

## Binary format

All values little-endian.

```
+0x00 u32 0
+0x04 u32 0x18 + 4 * ncat          (start of the menu lists)
+0x08 u32 size of the index        (<= 0x1000: the app copies the index to RAM at 0x81300000)
+0x0C u32 total size
+0x10 u32 0
+0x14 u32 ncat                     (16: the app supports no more)
+0x18 u32 list_offset[ncat]
list:  u32 n, u32 entry_offset[n]  (the menu's entries, in the order the pedal shows them)
entries follow the index
```

An entry:

```
+0x00 u32 total size of the entry
+0x04 u32 header size              (the module ELF starts here)
+0x08 u32 effect id
+0x0C i32 link_id                  (amps: id of their default cabinet; otherwise -1)
+0x10 u8  number of parameters     (<= 15)
+0x11 u8  subgroup                 (the menu tab, see below)
+0x12 u8  0xff, +0x13 u8 0         (flags, always these values)
+0x14 u16 offset of the name       (in the string table)
+0x16 u16 offset of the category
+0x18 parameter records, 24 bytes each
      string table (NUL-terminated latin-1 strings, shared)
      0xff padding to a multiple of 4
      the module ELF (<= 32 KB)
```

A parameter record:

| Offset | Type | Field |
|---|---|---|
| `+0x00` | i16 | `head`: −1 in every factory entry; the app never reads it. The [textlist patch](../app-patches.md#textlist-knobs-with-named-options) uses it for option names |
| `+0x02` | u16 | offset of the parameter name |
| `+0x04` | f32 × 4 | min, max, step, default |
| `+0x14` | u8 | `idx`: the position of the value in the module's `knobs[]` (< 15) |
| `+0x15` | u8 | `f1`: position on the amp panel (0–5), or `0xff`; not read on the pedal (`0xff` is safe) |
| `+0x16` | u8 | `f2`: tempo sync link (see below), 0 for none |
| `+0x17` | u8 | `f3`: display format (see below) |

`matribox.firmware.fwtool` parses and rebuilds this format (`Library`, `Entry`); rebuilding the 179 factory
entries from the parsed fields gives the original bytes.

## Menus

The library has 16 menus. Their meaning is fixed by the app:

| Menu | On the pedal | Factory entries | Tabs (subgroups) |
|---|---|---|---|
| 0 | DYN: dynamics, boost | 14 | Comp, Boost, Acoustic, Gate |
| 1 | FX: filter, pitch | 8 | Filter, Pitch, Special |
| 2 | WAH | 6 | Wah, Filter, Auto Filter |
| 3 | DRV | 24 | OD, Fuzz, Distortion, Bass Drive |
| 4 | AMP | 63 | Clean, Drive, Hi Gain, Bass, Acoustic |
| 5 | CAB | 1 (the engine) | — |
| 6 | User IR | 1 (the engine) | — |
| 7 | EQ | 5 | EQ |
| 8 | MOD | 23 | Chorus, Flanger, Vibrato, Phaser, Tremolo, Pitch, Special |
| 9 | DLY | 17 | Delay |
| 10 | RVB | 12 | Reverb |
| 11 | Clone | 1 (the engine) | — |
| 12–14 | FX Loop, SND, RTN | 1 each | — |
| 15 | Volume | 1 | — |

**Menus 5, 6 and 11 are special**: their lists on the pedal come from other data (the IR directory, the 20
user IR slots, the 10 Clone slots), and their single library entry is the engine that plays them. An effect
added there would never be shown. New effects go into menus 0–4 and 7–10. There cannot be a 17th menu.

## Tabs

The tabs of a menu are the subgroups (byte `+0x11` of each entry). Tab "ALL" lists everything; tab *k*
lists the entries of subgroup *k − 1*, labelled with the **category of the first entry** of that subgroup.
The app computes the number of tabs at boot from the **last two entries only**:
`max(subgroup[n−2], subgroup[n−1]) + 1`. So the end of a menu must keep its highest subgroup, and a new tab is
made by giving a new category the subgroup `max + 1`, at the end of the menu. Menus with more than 7 tabs
were never used by the factory firmware (8 has 7).

## Ids and presets

The high byte of an id is the family: `0x01` utility/EQ/routing, `0x03` drive, `0x04` modulation, `0x05` wah,
`0x07`/`0x08` amps, `0x0a` cabs, `0x0b` delay, `0x0c` reverb, `0x0f` Clone, `0x06` routing and volume.
Families are a convention; the app only needs ids to be **unique**.

Presets store, for each chain slot, the menu, the index in the menu and the id. On load the app looks the id
up again and takes its current index, and an unknown id falls back to entry 0 of the menu. Consequences:

- inserting an effect in the middle of a menu does not break existing presets;
- a preset that uses an added effect, loaded on a firmware without it, gets the menu's first effect instead.

## Parameters

- **At most 15 per effect.** A chain slot keeps 15 floats (`struct` of `0x44` bytes); `idx` must be < 15.
  Inserting an effect writes each parameter's default to `knobs[idx]`; positions the effect does not use
  keep whatever value they had.
- **Names**: effect, category and parameter names are copied into 20-byte buffers: **19 characters at most**.
- **Setting a value** (`FUN_000213cc`) quantises it to `step` and clamps it to `[min, max]`.
- **EXP pedal**: up to three targets (slot, parameter, min, max): any parameter of a new effect can be
  assigned to it.

### Display formats (`f3`)

`f3` selects how the value is printed (`FUN_0001b6ac`, a switch over ~196 codes): either a printf format or a
fixed list of option names. The full table is [matribox/data/display_formats.txt](../../matribox/data/display_formats.txt).
The most useful ones:

| `f3` | Shows | Typical use |
|---|---|---|
| `0x15` | `%.0f` | 0–100 knobs |
| `0x00` | Off / On | switches |
| `0x19` | `%.0fms` | times |
| `0x22` | `%.2fHz` | rates |
| `0x5d` | `%.0f` (the PC editor draws a ± dB slider) | ±dB |
| `0x74` | `Mute` below −60, else `%.0fdB` | levels |
| `0x75` | Center / R*n* / L*n* | pan |
| `0x94` | Mono (L) / Mono (R) / Stereo | input mode |
| `0xa6` | Normal / Invert | phase |

A value past the end of a list shows "Error". For option names of your own, use the textlist patch.

### Tempo sync (`f2`)

A parameter with `f2 = 0x1N` can follow the tempo: parameter number *N* (its order in the entry) is its Sync
switch, which has `f2 = 0x2M`, *M* being the synced parameter's number. With Sync on, the knob no longer
holds a time but an index of a note division (switching Sync on sets it to 4 = 1/4; switching it off restores
the default):

| Index | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Shown | 1/1 | 1/2 | 1/2D | 1/2T | 1/4 | 1/4D | 1/4T | 1/8 | 1/8D | 1/8T | 1/16 |
| × beat | 4 | 2 | 3 | 4/3 | 1 | 1.5 | 2/3 | 0.5 | 0.75 | 1/3 | 0.25 |

The module computes the period from the beat length the app passes to `Process` (`mb_sync_ms()` in the SDK).

## Limits checked by the tools

`fwtool.Library.place` (used by `matribox fw add`, the Studio and the port recipes) refuses anything that breaks
the rules above: special menus, duplicate ids, names over 19 characters, more than 15 parameters or repeated
`idx`, `min ≤ default ≤ max`, incomplete sync pairs, and positions that would hide tabs. It also picks the
subgroup (the existing tab with the same category, or a new one) and the position. `Library.pack` refuses an
index over `0x1000` bytes or a library over the partition, and `Entry.build` a module over 32 KB.
