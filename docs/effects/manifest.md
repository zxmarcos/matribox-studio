# Manifest reference

A manifest is a JSON file next to a module's `.axf`. `matribox fw add`, the Studio and the port recipes read it
to build the library entry. A folder of manifests is a **catalog**: the Studio lists every manifest of the
folders you register under "Bibliotecas".

```json
{
  "elf": "modecho.axf",
  "id": "0x0B000070",
  "name": "Mod Echo",
  "category": "Delay",
  "menu": 9,
  "params": [
    {"name": "Mix", "min": 0, "max": 100, "step": 1, "default": 30, "flags": [255, 0, 21]},
    {"name": "Time", "min": 20, "max": 4000, "step": 1, "default": 380, "flags": [255, 21, 25]},
    {"name": "Sync", "min": 0, "max": 1, "step": 1, "default": 0, "flags": [255, 33, 0]}
  ]
}
```

## Entry fields

| Field | Required | Meaning |
|---|---|---|
| `elf` | yes | the module, relative to the manifest |
| `id` | yes | effect id (number or `"0x…"` string); unique in the library; must equal the id in the module header |
| `name` | yes | name on the pedal, ≤ 19 latin-1 characters |
| `category` | yes | ≤ 19 characters; the label of the tab the effect goes into (an existing tab with the same label, or a new one) |
| `menu` | yes | 0–4 or 7–10 ([menus](../firmware/effect-library.md#menus)) |
| `params` | yes | up to 15 parameters, in the order the pedal shows them |
| `group` | no | force the subgroup (tab) number instead of choosing it by category |
| `position` | no | force the position in the menu list instead of the end of its tab |
| `link_id` | no | for amps: id of the default cabinet (default −1) |
| `hflags` | no | the three header flag bytes; the first is replaced by the subgroup (default `[0, 255, 0]`) |
| `source` | no | free text; the editor list generator uses it to label where an effect came from |

## Parameter fields

| Field | Meaning |
|---|---|
| `name` | ≤ 19 characters |
| `min`, `max`, `step`, `default` | the pedal quantises to `step` and clamps to `[min, max]`; `min ≤ default ≤ max` |
| `flags` | `[f1, f2, f3]`: `f1` = amp panel position or 255; `f2` = tempo sync link or 0; `f3` = display format. Default `[255, 0, 21]` |
| `idx` | position of the value in `knobs[]` (default: the parameter's order); unique and < 15 |
| `options` | list of option names for a text-list knob (needs the `textlist` app patch to be shown as text) |

### Display format (`f3`)

Common values: `21` (`0x15`, number), `0` (Off/On), `25` (`0x19`, ms), `34` (`0x22`, Hz with 2 decimals),
`93` (`0x5d`, number; the PC editor shows a ±dB slider), `116` (`0x74`, dB with Mute below −60), `117`
(`0x75`, pan), `148` (`0x94`, Mono L/Mono R/Stereo), `166` (`0xa6`, Normal/Invert). The full list of ~196
codes, with the factory option lists, is in [matribox/data/display_formats.txt](../../matribox/data/display_formats.txt).

### Tempo sync (`f2`)

A synced pair is two parameters pointing at each other by their order in `params` (0-based):

- the time/rate parameter has `f2 = 0x10 + N`, where N is the order of its Sync switch;
- the Sync switch has `f2 = 0x20 + M`, where M is the order of the time/rate parameter, and `f3 = 0` (Off/On).

In the Mod Echo above, Time is parameter 1 and Sync parameter 5: Time has `f2 = 0x15` (21) and Sync
`f2 = 0x21` (33). With Sync on, the module gets a note division index 0–10 instead of milliseconds:

```c
float ms = knobs[5] >= 0.5f ? mb_sync_ms(knobs[1], beat_ms) : knobs[1];
```

`Init` does not receive the tempo: take the synced value at the first `Process` instead of sliding to it.

### Text lists (`options`)

```json
{"name": "Mode", "options": ["Tremolo", "Vibrato", "Ring Mod", "Bitcrush", "Octave Fuzz"],
 "min": 0, "max": 4, "step": 1, "default": 0, "flags": [255, 0, 21]}
```

Options are 1–14 characters, without `|`; `min`/`max`/`step` must be 0 / n−1 / 1 (they are filled in if
omitted). The module receives the option index. With the `textlist` app patch the pedal shows the names;
without it, the index as a number (keep `f3 = 21` for that). The tools store the names in the entry's string
table and point the parameter's `head` field at them ([textlist](../app-patches.md#textlist-knobs-with-named-options)).

## Catalog folders

A catalog folder may have a `library.json` that names it in the Studio:

```json
{"name": "Tutorial", "color": "#38bdf8", "description": "The effect built step by step in docs/effects/tutorial.md"}
```

Without it the Studio uses the folder name and a colour derived from it.
