# Verification status

"Pedal" means tested by a user on a real Matribox II Pro with firmware V1.2.2 as the base. "Emulation" means
checked in the emulators and tests of this repository only.

| Feature | Status |
|---|---|
| Modified effect library (rename, duplicate, new entries) | pedal |
| Effect modules built with this SDK (Mod Echo) | pedal |
| Parallel routing modules (Split / Path B / Merge) | pedal |
| Recovery: footswitch combination at power-on + official image | pedal |
| `parallel` app patch: main screen in two rows | pedal |
| `parallel` app patch: edit screen, connector lines, icons | pedal |
| `textlist` app patch, Multi Mode example | pedal |
| NAM A2-Lite effect (`nam_a2`) | pedal (no overload in a typical chain; input calibration confirmed) |
| NAM Clone engine, capture writes over USB, 48 kHz resampling, CPU numbers | pedal |
| `clonedual` app patch | pedal |
| `clonetabs` app patch | pedal |
| Firmware update over USB (`qmeupdate`, Studio) | pedal |
| Presets over USB: read, activate, on/off, knobs | pedal |
| Presets over USB: chain edits (insert, move, remove, replace) | pedal |
| User IR slots, global settings and global EQ (Studio) | pedal |
| GP-200 and QME-100 ports | pedal |
| Ports from the Studio's Ferramentas tab | same files as `matribox port export` (byte for byte) |
| Gain Boost example | emulation |

The firmware tools themselves are checked by reproduction: rebuilding the factory library from its parsed
entries and re-packing the official image give identical bytes, and the app ELF and the 179 factory modules
derived by `matribox.firmware.stock` are identical to the ones the research was done on.
