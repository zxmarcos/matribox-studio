# Hardware and firmware image

## The board

| Part | What it is |
|---|---|
| MCU | NXP i.MX RT1062 (Cortex-M7 with FPv5 double precision) at **600 MHz**; one core does everything: UI, USB and the audio loop |
| Memory | ITCM ≈ 480 KB and DTCM 32 KB (FlexRAM, set up by the bootloader), OCRAM 512 KB, SDRAM 32 MB (`0x80000000`) |
| Flash | Winbond W25Q64, 8 MB QSPI, executed in place at `0x60000000` (uncached by the app's MPU setup) |
| Audio | three Everest ES7243E stereo ADCs on a bit-banged I²C bus, SAI1 in slave mode (32-bit I²S, 3 RX lines, 2 TX lines), eDMA |
| Display | 480×272 RGB panel with a Sitronix ST7701S controller, driven by eLCDIF; framebuffer XRGB8888 at `0x81F00000` |
| Touch | Goodix GT911 on a bit-banged I²C bus |
| Controls | buttons and LEDs on a software shift register, three endless encoders under the screen plus the MENU encoder (ENC1–4), MASTER VOLUME and two EXP inputs on ADC1 |
| USB | one device with USB audio (44.1 kHz) and USB-MIDI; the PC editor talks SysEx over the MIDI port |

The firmware's app is a Keil build on top of the NXP SDK and **SEGGER emWin 6.16c** for the UI.

## Flash map

| Offset | Size | Content | Written by |
|---|---|---|---|
| `0x000000` | 128 KB | bootloader (with an info block at `0x4000`) | never by a normal update |
| `0x020000` | — | the app (section 0 of the firmware image) | update |
| `0x40A000` | 216 KB | Clone captures, 10 slots, own small file system | USB (Clone import) |
| `0x440000` | 256 KB | factory presets: 120 presets, 3 per 4 KB sector | update (section 3) |
| `0x480000` | 2.5 MB | effect library (section 1) | update |
| `0x700000` | — | IR directory and factory cabinets (section 2) | update |
| `0x753000` | 180 KB | user IRs, 20 slots, own file system | USB (IR import) |
| `0x780000` | 512 KB | record store: global settings, the preset map, user presets | the pedal (SAVE) |

Clone captures and user IRs live outside the areas an update writes, so they survive firmware changes. User
presets are in the record store; the factory presets are rewritten by every update.

## The firmware file (`FRMW`)

The official file `QME-200_FW_USB_V1.2.2.bin` (6 335 008 bytes) is little-endian:

| Offset | Content |
|---|---|
| `0x00` | `FRMW` |
| `0x04` | payload size = file size − `0x30` |
| `0x08`–`0x2f` | `FIRMWARE_HEADER` fields: release type, version, encryption (0), **checksum** (u16 at `0x0e`), device name `QME-200` at `0x10` |
| `0x30` | 4 × {u32 offset relative to `0x30`, u32 size} |

The four sections, in file order:

| # | Name in the editor | Content |
|---|---|---|
| 0 | MCU | the app: ITCM image + SDRAM image |
| 1 | DSP | the effect library (see [the effect library](effect-library.md)) |
| 2 | IR | the IR directory and the factory cabinets |
| 3 | Bank | the factory presets |

The checksum is the 8-bit sum of every byte from `0x30` to the end of the file (only the low byte of the u16
is compared). There is **no signature and no encryption**. The PC editor checks the extension `.bin`, the device
name, the `FRMW` magic and this checksum, and nothing else; the bootloader checks a sum on each packet.
`matribox.firmware.fwtool` parses and rebuilds this container (`matribox fw roundtrip` gives back an
identical file) and always writes a correct checksum.

### Section 0: the app

```
+0x00  ff ff ff ff ff ff ff ff  "V122"  00 00 00 00
+0x10  (flash 0x20028, itcm_size, load address 0x00000000)       ITCM image
+0x1C  (flash 0x20028 + itcm_size, sdram_size, 0x80000000)        SDRAM image
+0x28  the ITCM image, then the SDRAM image
```

The flash address is `0x20000` + the offset inside the section. The bootloader copies both images and jumps
to the reset vector at `0`; it does not check the section further. The ITCM image of V1.2.2 is `0x53cb9`
bytes, so the ITCM between `0x53cc0` and `0x58000` (≈ 17 KB) is free: [app patches](../app-patches.md) put
their code there. From `0x58000` the ITCM is the code pool of the effect modules.

The SDRAM image starts with Keil's `__main`: a scatter-load table copies one region to OCRAM, decompresses the
initialised data (armlink's LZ77 variant) and zeroes the rest. `matribox.firmware.mkappelf` reproduces this
and writes the app as an ARM ELF, the input for Ghidra and for the emulators (`matribox app-elf`).

## Updating

The PC editor ("Matribox II PRO", Flutter + a native `HTUSBTools` library) updates the pedal over USB-MIDI;
`matribox.usb.qmeupdate` speaks the same protocol, so the editor is not needed (see
[the protocol page](protocol.md#firmware-update)):

1. In the app, the message `0x0500000E` makes it write `0x5A5A5A5A` to `0x400A4000` and reset through AIRCR;
   the bootloader starts in update mode. The bootloader answers the query `F0 7E 48 01 F7` with
   `"QME-200"`, its version (`V1.0.0`) and the packet size (`0xB4` = 180 bytes).
2. The sections go in the order MCU, IR, Bank, DSP, each as a header and data packets with a per-packet sum,
   every packet ACKed or NAKed; then a jump starts the new app.

**Recovery.** Holding a footswitch combination while powering the pedal on starts the bootloader in update
mode, whatever app is installed. Since updates never touch the bootloader, flashing the official image this
way always brings the pedal back. Back up your user presets before experimenting: they are not touched by an
update, but a bad app may corrupt the record store.
