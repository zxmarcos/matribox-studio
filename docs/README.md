# Documentation

Everything here describes the **Sonicake Matribox II Pro (QME-200), firmware V1.2.2**. Addresses such as
`FUN_00031e18` or `0x202381a8` are those of that version; the names follow Ghidra's convention (`FUN_` +
address). Facts were found by reverse engineering and checked in emulation, and most of them on the pedal; the
[status page](status.md) says which.

## How the firmware works

1. [Hardware and firmware image](firmware/overview.md): the board, the memory map, the `FRMW` file and its four
   sections, how an update reaches the pedal and how to recover from a bad one.
2. [The effect library](firmware/effect-library.md): the binary format of section 1, menus, tabs, parameter
   records, display formats, ids and every limit the app enforces.
3. [The audio engine](firmware/audio-engine.md): the audio loop, how the chain runs its modules, memory pools,
   the CPU budget and "System overload", and the Cab/IR and Clone engines.
4. [USB protocol and presets](firmware/protocol.md): the editor's SysEx protocol, the preset format, slots for
   NAM captures and IRs, global settings, and the bootloader's update protocol.

## Writing effects

1. [The module ABI](effects/module-abi.md): what an effect module is, how the app loads and calls it, and the
   rules your C code has to follow.
2. [Tutorial: a new effect from scratch](effects/tutorial.md): `examples/gain`, from an empty file to the pedal.
3. [Manifest reference](effects/manifest.md): the JSON that puts a module into the library (parameters,
   display formats, tempo sync, text lists).
4. [Testing and performance](effects/testing.md): the emulated loader, measuring cost, memory and CPU budgets.
5. [The examples](examples.md): what each example in `examples/` shows.
6. [NAM on the pedal](nam.md): the A2-Lite engine, the NAM effect, the NAM Clone engine and capture slots.

## Changing the app and composing firmware

- [App patches](app-patches.md): parallel chains in two rows, text-list knobs, the dual-engine Clone menu and its
  tabs; how patches are applied and how to port them to another firmware version.
- [Porting effects from sibling pedals](ports.md): Valeton GP-200 and Matribox II (QME-100) recipes.
- [Matribox Studio and the command-line tools](tools.md).
- [Verification status](status.md): what has been confirmed on the pedal and what only in emulation.
