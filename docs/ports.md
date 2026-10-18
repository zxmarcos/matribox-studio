# Porting effects from sibling pedals

The **Valeton GP-200** (firmware V1.8.2) and the **Sonicake Matribox II (QME-100)** (V1.3.0) are built on the
same platform as the Matribox II Pro: the same Keil app layout and the same algorithm library, with effects
the QME-200 does not have. Their modules can be turned into QME-200 modules.

This repository contains **only the recipe**: the tools read the other pedal's official firmware file, which
you download yourself, and generate the modules on your machine. No module of either pedal is distributed
here.

## In the Studio

The **Ferramentas** tab has one page per port. Choose the official firmware file (it is checked against the
SHA-256 of the file the recipe was made for; another version can be tried, with a warning) and an output
folder, and click "Extrair efeitos". It takes about a minute and a half for the GP-200; the table fills in as
each effect is converted, with its menu, size and RAM, or the reason it failed. The folder gets a
`library.json` and is added to the Studio's libraries, so the effects show up in the Firmware mode. The
QME-100 page leaves out what the GP-200 page already extracted, as the recipe below does. Nothing is compiled,
so Zig is not needed; the emulator (unicorn) is, and the Windows build includes it.

## On the command line

```sh
python -m matribox port export GP-200_USB_Firmware_V1.8.2.bin firmware/QME-200_FW_USB_V1.2.2.bin ports/gp200
python -m matribox port export QME-100_FW_USB_V1.3.0.bin firmware/QME-200_FW_USB_V1.2.2.bin ports/qme100 ports/gp200
python -m matribox port build firmware/QME-200_FW_USB_V1.2.2.bin ports/gp200,ports/qme100 out.bin ["Name" ...]
```

`export` converts every effect whose id the QME-200 lacks, measures its RAM, runs a sanity check in the
emulator and writes `.axf` + manifest pairs and a `library.json` (a trailing folder argument skips ids already
exported there).
`build` adds them, all or by name, to a QME-200 image. The exported folders are ordinary catalogs: the Studio
can add them with any other effects. Exporting needs `unicorn`.

What you get: 39 effects from the GP-200 (Bad-KT, Power LD, Silver Twin, Scream OD, Penesas, A-Chorus, Vibrato T,
Digital/Tape Delay S, Ice Delay, Broken Delay, Ambience 1/2, Tube/Amp Spring, Arena…) and 2 from the QME-100
(Digi Octa, Leslee). They do not all fit at once: the library partition is 2.5 MB. 37 of the GP-200 effects plus
the two QME-100 ones fit together with the routing modules, because `build` strips the symbol names from the
modules (23.6 KB saved). Some delays need a lot of SDRAM (Broken Delay 4 MB of the 6.8 MB pool).

## How it works

The other pedals store their modules **pre-linked**: each library entry is the module's code already linked
at a fixed address of its menu's memory window (one effect per menu), with the parameter records in the same
24-byte format as the QME-200. Turning one into a relocatable QME-200 module is **delinking**
(`matribox/ports/gp2qme.py`):

1. The windows of each menu are known from the other app's tables (`gp200lib.MENU_MEM`, `qme100lib.MENU_MEM`),
   so every address in the code can be classified: code (ITCM), constants, state, big buffers.
2. Every absolute reference is found: literal-pool words (`ABS32`, or `REL32` when the word is PC-relative and
   lands in another region), `MOVW`/`MOVT` pairs and pointers inside constant data. References to the other
   app (outside the windows) are refused.
3. A QME-200 ELF is written with the four `ER_*` sections and relocations in the form the QME-200 loader uses.
4. The RAM sizes (not recorded by the other pedals) are measured by running Init and Process in the emulator
   with the windows filled with a sentinel byte and taking the highest byte written.

Validation: on the 16 modules whose code is identical in both firmwares the relocations come out identical to
the QME-200's own; factory effects converted from the QME-100 or GP-200 produce output bit-exact with the
QME-200's factory modules; ported modules loaded behind other modules (non-zero offsets in every pool) give
the same output as alone.

The QME-100 uses another container (`HTFW`, not compressed) and keeps constants inside the code block;
`qme100lib.py` reads it (`matribox qme100 QME-100_FW_USB_V1.3.0.bin outdir` also writes its app as an ELF).
