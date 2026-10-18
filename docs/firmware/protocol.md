# USB protocol and presets

The pedal is one USB device with an audio interface and a MIDI port. The PC editor talks to the app with
SysEx messages over that MIDI port; the same port, after a reset into the bootloader, carries firmware updates.
Implementations: `matribox/usb/htmidi.py` (packets, LZO, Windows MIDI through WinMM with ctypes),
`qmepreset.py` (presets and settings), `nam_clone.py` and `irwav.py` (capture and IR slots), `qmeupdate.py`
(updates). Windows MIDI ports are exclusive: close the PC editor before using them.

## Packets and messages

A packet:

```
F0 21 25 4D 50 00 00 cs flag len_lo len_hi off_lo off_hi <data as 4-bit nibbles> F7
```

- `flag` 0x11 = request, 0x12 = data, 0x13 = NAK (offset out of order or bad checksum).
- Up to 0xB8 bytes of data per packet; `cs` = sum of the nibbles & 0x7F.
- The pedal reassembles packets in order in a 16 KB buffer; offset 0 starts a new message.

A message is `u32 cmd | u32 n | n bytes compressed with LZO1X` (miniLZO 2.10 on both sides; requests have
n = 0). The dispatcher (`FUN_0003a084`) routes by group `(cmd >> 24) & 0xf` and sub `cmd & 0xf`. The pedal
also sends messages on its own: preset changes, chain edits, knob moves, on/off, the "modified" flag.

Note that `FUN_0003568c`, which sends some replies, holds back a reply identical to the previous one: to read
the same value twice, ask something else in between.

## Presets

| Request (flag 0x11) | Reply |
|---|---|
| `0x10000000 {u32 i}` | the preset **being edited**, unsaved changes included |
| `0x10000002 {u32 i}` | the **stored** preset `i`, without switching to it |
| `0x10000001 {0x010a0000}` | the list: `{sub, i32 pos[240], name[240][17], style[240][10]}` |
| `0x10000001 {0x010a0001}` | the current preset `{sub, u32 i}` |

A preset reply is `{u32 i}` followed by blocks `{u32 cmd, u32 size, u32 kind << 16, size − 4 bytes}`:

| Kind | Content |
|---|---|
| `0x0100` | dimensions `{4 footswitches, 3 combos, 12 slots, 1, 5 controls, 1, 15 params per slot}` |
| `0x0101` | `{u32 version, name[17], author[20], info[50], style[10]}` |
| `0x0104` | the chain: `{i8 order[12]` (slot shown at each position, −1 = end)`, i8 menu[12], u32 id[12]}` |
| `0x0105` | `{f32 knobs[12][15], u8 on[12], u16 volume, u16 BPM, i8 pan, u8 fx loop}` |
| `0x0102`, `03`, `07`, `08`, `09` | quick knobs, footswitches, EXP, CTRL, version |

The stored format (0x48C bytes, three per 4 KB sector): `+0` `0x5AA5`, `+0x0a` name, `+0x1e` volume, `+0x1f`
pan, `+0x20` BPM, `+0x22` order[12], `+0x30` 12 slots of `{u8 on, i8 menu, i16 index, u32 id, f32 knobs[15]}`,
`+0x481` style.

### Editing the live preset

These act on the edit buffer only; nothing reaches flash until the preset is saved on the pedal.

| Message (flag 0x12) | Effect |
|---|---|
| `{0x010a0001, u32 i}` | load preset `i`, **discarding unsaved changes** |
| `{0x01050002, u32 slot, u32 on}` | switch a block on/off |
| `{0x01050001, u32 slot, u32 k, f32 v}` | set a knob; `k` is the parameter's **order in the library entry**, the pedal converts it to the `knobs[]` position (they differ for the Cab: orders 0, 1, 2 are positions 1, 5, 6) |
| `{0x01040001, i8 old, i8 new, i8 menu, u32 id}` | chain edit: (−1, p) insert at p, (p, −1) remove, (p, q) move, (p, p) replace the effect |
| `0x11000001 {0x0800}` (flag 0x11) | read the "modified" flag |

Insert and replace check memory first; with no memory, or with 12 blocks, the pedal refuses and sends
`0x11000001 {0x0700, 2}`. An id the pedal's library does not have leaves an empty "phantom" slot in the chain:
use ids from the firmware that is actually installed.

## NAM captures and IRs

The Clone slots (group 8) and the user IR slots (group 4) work the same way:

| | Clone | User IR |
|---|---|---|
| list | `0x18000000` → `{10 u32, 10 u32, 10 names × 17}` | `0x14000000` → `{20 u32, 20 u32, 20 names × 17}` |
| write | `0x18000001 {u32 slot, u32 0, u32 checksum, name[20], data}` | `0x14000001 {u32 slot, u32 0, u32 checksum, name[20], int32 ir[1024]}` |
| reply | `0x18000003 {slot, 1 = written, 0}` | `0x14000003 {slot, 1, 0}` |
| rename | — | `0x14000002 {u32 slot, name}` (no reply) |

The pedal keeps 0x2000 bytes per Clone slot and 16 characters of a name. There is no command to read slot
data back. `matribox nam send` and `matribox ir send` write slots; the Studio keeps a log of what it wrote
(`%APPDATA%\MatriboxStudio\nam_slots.json`, `ir_slots.json`). Writing a slot replaces what was there for good.

The editor converts any WAV to the IR format by reading the first 4096 bytes of a 24-bit/44.1 kHz file as
1024 samples; `matribox/usb/irwav.py` does the same conversion in Python (any PCM or float WAV, resampling,
optional normalisation) and gives identical bytes for 24-bit/44.1 kHz files.

## Global settings and global EQ

| Message | |
|---|---|
| `0x11000000` | read the 52 bytes of global settings |
| `0x11000001 {u32 type, i32 value}` | change one setting; `type = 0xB000 | (field + 1) << 8 | index`; the pedal echoes the message |
| `0x16000000` | read the 31 floats of the global EQ |
| `0x16000001` | EQ on/off |
| `0x16000002 {u32 index, f32 value}` | set one EQ parameter (Lo Cut, Lo Shelf, bands 1–4, Hi Shelf, Hi Cut, Level) |

Every change is applied and kept by the pedal immediately. `matribox.usb.qmepreset.GLOBAL_TYPES` lists the 22
settings; the Studio's Global tab edits them.

## Firmware update

To update, the app is asked to reboot into the bootloader with `0x0500000E` (flag 0x12). The bootloader
speaks a different SysEx dialect, `F0 7E 48 … F7`, with ACK `03 7F` and NAK `03 7E`:

| Step | Message |
|---|---|
| query | `01`; the reply has the device name, the bootloader version and the packet size P (bytes of image per packet) |
| version check | `0B 01 'V' d d d 00` (from the image header); **required** before the first section header |
| location | `7F <loc>`: MCU 0, IR 1, Bank 2, DSP 0x10; echoed back |
| section header | `02 <size as 8 nibbles> <number of packets as 8 nibbles>` |
| data | `04 <index & 0x7f> <2P nibbles, padded with 0x0F> <8-bit sum of those nibbles as 2 nibbles>` |
| end of section | `05 7B` |
| finish | `07` (read the MCU id), then `09` (start the new app) |

Sections go in the order MCU → IR → Bank → DSP; each packet waits for its ACK and is resent on NAK. The
protocol was ported from the editor's `HTUSBTools.dll`, and `tests/test_qmeupdate.py` checks it against the
DLL's own state machine (called through ctypes, needs a copy of the DLL). `matribox update flash FW.bin` and the
Studio's "Atualizar o pedal" dialog run the whole sequence and log it to `%APPDATA%\MatriboxStudio`.

## The PC editor's effect list

The editor does not ask the pedal which effects exist: it reads `assets/alg/alg_data.json`, with one module
per menu and every effect by `fxid`. Effects you add are invisible to it (presets using them show the id)
until they are added to that file: `matribox editor-json FW.bin alg_data.json out.json` adds the missing
entries with the editor's own rules (`codeing = ValToStr_%03d % f3`, Sync from `f2`, widget type from the
format). The Studio does it when it builds a firmware.
