# Matribox Studio and the command-line tools

## Matribox Studio

```sh
python -m matribox studio
```

A desktop app (Tkinter; its interface is in Portuguese) with six modes:

- **Firmware**: start from a base image (the official file, or a base made from it with app patches:
  "Gerar base do original"), remove factory effects, add modules from your catalogs (folders of manifests,
  "Bibliotecas"), rename them or move them to another tab, choose the Clone engine, and build. It checks every
  rule of [the effect library](firmware/effect-library.md), warns about presets that use removed effects, and
  can write the PC editor's `alg_data.json` for the result. Selections are saved as profiles; `matribox compose
  profile.json out.bin` builds a profile without the GUI. "Atualizar o pedal" flashes the result.
- **Pedal e presets**: reads the presets from the pedal (or from the Bank of an image), draws the chains
  (parallel sections in two rows), shows parameters formatted like the pedal, and edits the live preset:
  on/off, knobs, insert/move/remove/replace blocks by drag and drop, switch presets.
- **Capturas NAM**: the 10 Clone slots, a library of `.nam` files with their metadata and whether they run on
  the pedal, and writing a capture to a slot.
- **IRs**: the 20 user IR slots: write a WAV (converted like the editor does), rename, compare frequency
  responses.
- **Global**: the global EQ and global settings, like the editor's pages.
- **Ferramentas**: tools that used to be command-line scripts. For now, the [ports](ports.md): pick the
  official GP-200 or QME-100 firmware file, extract its effects into a folder (the same files as `matribox port
  export`) and add that folder to the Studio's libraries, ready for the Firmware mode.

The session is kept in `%APPDATA%\MatriboxStudio`.

### Windows build

`packaging/build_exe.py` makes a standalone folder with PyInstaller (`dist/MatriboxStudio/`): the executable,
the example catalogs (build them first with `examples/build_all.py`), unicorn for the ports, and next to the
executable `firmware\`,
`build\` and optionally `zig\` (`--zig DIR`), which play the role they have in a checkout. The GitHub workflow
`studio-windows` runs it on demand or for a `v*` tag, uploads `MatriboxStudio-windows.zip` as an artifact and,
for a tag, attaches it to a release.

## Command line

`python -m matribox` lists the commands; each one is also a module (`python -m matribox.firmware.fwtool …`).

| Command | Module | What it does |
|---|---|---|
| `studio` | `studio/fwstudio.py` | the GUI |
| `compose` | `studio/fwstudio_core.py` | build a firmware from a Studio profile |
| `fw` | `firmware/fwtool.py` | `list`, `roundtrip`, `add FW manifest OUT`, `swapelf FW MENU.POS module OUT` |
| `app-elf` | `firmware/mkappelf.py` | the app as an ELF, for Ghidra and the emulators |
| `build-module` | `module/buildmod.py` | `OUT.axf src.c …`: C to a loadable module |
| `emulate` | `module/effemu.py` | run a module in the emulated loader |
| `disasm` | `module/effdis.py` | disassemble a module with symbols and relocations |
| `patch-app` | `app/apppatch.py` | `--features=parallel,textlist,clonedual,clonetabs BASE OUT` |
| `ui-shot` | `app/uiemu.py` | screenshot of the original app in emulation |
| `icons` | `app/mkicons.py` | regenerate the routing icons |
| `editor-json` | `editor/editor_json.py` | add a firmware's effects to the editor's list |
| `presets` | `usb/qmepreset.py` | `list`, `show [N]`, `bank FW`, `activate`, `on`, `knob` over USB |
| `update` | `usb/qmeupdate.py` | `check`, `probe`, `jump`, `flash FW [--jump]` |
| `nam` | `usb/nam_clone.py` | `ports`, `list`, `send`, `pack`, `syx`, `cpu` |
| `nam-convert` | `nam/nam_a2.py` | `.nam` → A2-Lite weights as C |
| `ir` | `usb/irwav.py` | `info`, `pack`, `list`, `send`, `rename` |
| `midi` | `usb/midisend.py` | program change, CC, tempo, clock |
| `port` | `ports/gpport.py` | `export`, `build` ([ports](ports.md)) |
| `gp200`, `qme100` | `ports/gp200lib.py`, `ports/qme100lib.py` | list / unpack those pedals' libraries |

USB tools use Windows MIDI (WinMM through ctypes, no extra packages). Close the PC editor first: Windows MIDI
ports are exclusive.

## Configuration

Paths come from [matribox/paths.py](../matribox/paths.py): `firmware/` for the official images and `build/` for
outputs by default, overridable by `workspace.json` at the repository root or the environment
(`MATRIBOX_FW`, `MATRIBOX_FIRMWARE_DIR`, `MATRIBOX_BUILD_DIR`). Zig is found through `$ZIG` or the `PATH`.

## Tests

| Test | Needs | Checks |
|---|---|---|
| `tests/test_qmepreset.py [FW]` | unicorn, a firmware with the NAM Clone | the preset protocol against the app's own handlers in emulation |
| `tests/test_qmeupdate.py` | the editor's `HTUSBTools.dll` | the update protocol against the DLL's own state machine |
| `examples/parallel/test_parallel.py` | unicorn | Split / Path B / Merge around factory effects |
| `examples/nam_clone/test_dual.py FW`, `test_tabs.py FW` | unicorn | `clonedual` and `clonetabs` in the emulated app |
| `nam/test_a2.py model.nam [--emu axf]` | numpy, `ref_render` | A2-Lite against NeuralAmpModelerCore |
| `nam/test_rs48.py model.nam [--emu]` | numpy, `ref_render` | the resampler and the Clone module |
| `nam/test_clone.py model.nam` | numpy, unicorn | the Clone slot protocol through the app's code, the module against the host engine |
