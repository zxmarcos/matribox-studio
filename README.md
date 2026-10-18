# Matribox Studio

An SDK and a set of tools for the firmware of the **Sonicake Matribox II Pro (QME-200)**, version V1.2.2, built
by reverse engineering the official firmware image.

![Matribox Studio: a preset with a parallel chain, live on the pedal over USB](docs/images/studio.png)

With it you can:

- write **new effects in C** and add them to the pedal's effect library;
- **compose a firmware**: remove factory effects, add your own or ported ones, and get a matching effect list
  for the PC editor;
- **patch the original app**: parallel chains drawn as two rows (Split / Path B / Merge), knobs with named
  options, and a Clone menu that runs both the factory engine and a real NAM network;
- run **NAM A2-Lite captures** on the pedal, as an effect of their own or through the Clone slots;
- talk to the pedal over USB: read and edit presets, write NAM captures and IRs to their slots, change the
  global settings and flash firmware, all without the vendor's editor;
- port effects from the **Valeton GP-200** and the **Matribox II (QME-100)**, which share the platform, from
  their own firmware files, in the Studio's Ferramentas tab or on the command line.

Nothing from the vendors is in this repository. The tools work on the official firmware image that you
download yourself; everything they derive from it (the app as an ELF, factory modules, ported effects) is
generated on your machine.

> **Warning.** Flashing modified firmware is at your own risk. The pedal has a recovery mode (a footswitch
> combination at power-on starts the bootloader's update mode, and normal updates never touch the
> bootloader), so an image that fails to boot can be replaced by the official one.

## Matribox Studio for Windows

**New here?** The [step-by-step guide](docs/getting-started.md) goes from the official firmware to a custom one
with dual chains, text-list knobs and NAM captures, using only the Studio.

The [releases](https://github.com/zxmarcos/matribox-studio/releases) have a ready-to-run build of the Studio
(`MatriboxStudio-windows.zip`, made by the `studio-windows` workflow): unzip it, put the official
`QME-200_FW_USB_V1.2.2.bin` in its `firmware` folder and run `MatriboxStudio.exe`. It includes the example
effects, Zig and the emulator, so app patches can be built and effects ported from the GP-200 and QME-100
without installing anything.

## Getting started

Requirements: Python 3.10+, [Zig](https://ziglang.org/) 0.14 (its bundled clang and lld are the ARM
compiler and linker), and for the emulators and tests `pip install unicorn capstone numpy`.

```sh
git clone https://github.com/zxmarcos/matribox-studio.git
cd matribox-studio
pip install -e .
# put the official QME-200_FW_USB_V1.2.2.bin in firmware/ (or set MATRIBOX_FW)
python examples/build_all.py          # build the example modules
python -m matribox studio             # the GUI
python -m matribox                    # the list of command-line tools
```

A `workspace.json` at the repository root (not tracked) can move the firmware and build folders elsewhere;
see [matribox/paths.py](matribox/paths.py).

## Repository layout

| Path | What is there |
|---|---|
| [sdk/](sdk/) | the effect module ABI (`matribox_module.h`), its tiny runtime and the linker script |
| [examples/](examples/) | example effects with their manifests: Mod Echo, a multi-mode knob, the parallel-routing modules, NAM |
| [nam/](nam/) | NAM A2-Lite in plain C (with Cortex-M7 assembly for the hot loops) and the 44.1/48 kHz resampler |
| [matribox/firmware/](matribox/firmware/) | the firmware container and effect library (`fwtool`), the app as an ELF, stock artifacts |
| [matribox/module/](matribox/module/) | module builder (C to a loadable `.axf`), emulator of the device loader, disassembler |
| [matribox/app/](matribox/app/) | patches for the original app (C code in its free ITCM) and an emulator of its UI |
| [matribox/usb/](matribox/usb/) | the editor's USB-MIDI protocol: presets, firmware update, NAM and IR slots, MIDI |
| [matribox/ports/](matribox/ports/) | delinking of GP-200 / QME-100 modules into QME-200 modules |
| [matribox/studio/](matribox/studio/) | Matribox Studio, the GUI over all of the above |
| [tests/](tests/) | tests that run the original app's own code in emulation against our implementations |

## License

MIT, see [LICENSE](LICENSE). `nam/a2lite.c` is a port of
[NeuralAmpModelerCore](https://github.com/sdatkinson/NeuralAmpModelerCore) (MIT). The Studio's fonts are IBM
Plex, under the SIL Open Font License ([matribox/studio/fonts/OFL.txt](matribox/studio/fonts/OFL.txt)).
No NAM capture is distributed: the NAM examples work with captures you provide.
