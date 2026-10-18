"""matribox <command> [args...]: one entry point for the SDK tools.

Every command runs a module of the package as a script (`matribox fw list x.bin` is the same as
`python -m matribox.firmware.fwtool list x.bin`); `matribox <command> -h` shows that tool's own help.
"""
import runpy, sys

COMMANDS = [
    ('studio', 'matribox.studio.fwstudio', 'Matribox Studio (GUI): firmware, presets, NAM captures, IRs, global settings'),
    ('compose', 'matribox.studio.fwstudio_core', 'build a firmware from a Studio profile, without the GUI'),
    ('fw', 'matribox.firmware.fwtool', 'firmware image: list, roundtrip, add a module, swap a module ELF'),
    ('app-elf', 'matribox.firmware.mkappelf', 'rebuild the original app as an ELF (Ghidra, emulators)'),
    ('build-module', 'matribox.module.buildmod', 'compile C sources into an effect module (.axf)'),
    ('emulate', 'matribox.module.effemu', 'run a module through the emulated device loader (unicorn)'),
    ('disasm', 'matribox.module.effdis', 'disassemble a module with symbols and relocations (capstone)'),
    ('patch-app', 'matribox.app.apppatch', 'patch the original app: parallel, textlist, clonedual, clonetabs'),
    ('ui-shot', 'matribox.app.uiemu', "render the original app's screen in emulation (unicorn)"),
    ('icons', 'matribox.app.mkicons', 'regenerate the Split / Path B / Merge icons of the parallel patch'),
    ('editor-json', 'matribox.editor.editor_json', "add a firmware's effects to the PC editor's alg_data.json"),
    ('presets', 'matribox.usb.qmepreset', 'presets over USB, or from the Bank of a firmware image'),
    ('update', 'matribox.usb.qmeupdate', 'flash a firmware image over USB, like the PC editor'),
    ('nam', 'matribox.usb.nam_clone', 'NAM A2 captures in the Clone slots'),
    ('nam-convert', 'matribox.nam.nam_a2', '.nam file -> A2-Lite weights as C source'),
    ('ir', 'matribox.usb.irwav', 'WAV files -> User IR slots'),
    ('midi', 'matribox.usb.midisend', 'send MIDI messages to the pedal'),
    ('port', 'matribox.ports.gpport', 'port Valeton GP-200 / Matribox II effects from their firmware'),
    ('gp200', 'matribox.ports.gp200lib', 'list the effect library of a Valeton GP-200 firmware'),
    ('qme100', 'matribox.ports.qme100lib', 'unpack a Matribox II (QME-100) firmware'),
]


def usage():
    w = max(len(c) for c, _, _ in COMMANDS)
    return __doc__ + '\ncommands:\n' + '\n'.join('  %-*s  %s' % (w, c, h) for c, _, h in COMMANDS)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(usage())
        return
    mods = {c: m for c, m, _ in COMMANDS}
    if argv[0] not in mods:
        sys.exit('unknown command %r\n\n%s' % (argv[0], usage()))
    sys.argv = ['matribox ' + argv[0]] + argv[1:]
    runpy.run_module(mods[argv[0]], run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    main()
