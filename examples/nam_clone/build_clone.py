"""Build the NAM-from-Clone-slot module and, optionally, a firmware with it.

    build_clone.py [base_fw.bin out_fw.bin]
    build_clone.py --dual base_fw.bin out_fw.bin

Writes examples/nam_clone/gen/nam_clone.axf. With firmware paths, entry 11.0 (Clone) of base_fw gets
this module; its id, name and the five Clone knobs stay as they are (presets and the editor still match).
With --dual, 11.0 runs the factory Clone and the module is added as entry 11.1 (fwtool.set_clone, same knobs):
base_fw must carry the app patch "clonedual" (matribox/app/apppatch.py), which runs 11.1 for the slots holding a
NAM blob and 11.0 for the others. Without that patch, Clone slot 1 would always run the NAM module.
"""
import os, sys, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
NAM = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'nam')   # A2-Lite + resampler sources
GEN = os.path.join(HERE, 'gen')
CLONE_ENTRY = '11.0'
DUAL_HOOK = 0x1b538                     # first hook of the clonedual app patch (FUN_0001b538)


def build_module():
    os.makedirs(GEN, exist_ok=True)
    axf = os.path.join(GEN, 'nam_clone.axf')
    env = dict(os.environ, MB_CFLAGS='-I %s -DA2_BLOCK=72' % NAM)
    subprocess.run([sys.executable, '-I', '-m', 'matribox.module.buildmod', axf,
                    os.path.join(HERE, 'nam_clone.c'), os.path.join(NAM, 'a2lite.c'), os.path.join(NAM, 'rs48.c')],
                   check=True, env=env)
    return axf


def add_dual(base, axf, out):
    from matribox.firmware import fwtool, stock
    from matribox.app import apppatch
    fw = fwtool.Firmware(open(base, 'rb').read())
    _, itcm, _ = apppatch.split_mcu(fw.sections[0])
    orig = dict((a, o) for a, o, s in apppatch.FEATURES['clonedual']['hooks'])[DUAL_HOOK]
    if itcm[DUAL_HOOK:DUAL_HOOK + len(orig)] == orig:
        raise SystemExit('%s: app sem o patch "clonedual" (matribox patch-app --features=...,clonedual)' % base)
    lib = fw.library
    factory = fwtool.clone_elfs(lib).get('factory') or fwtool.clone_elfs(stock.firmware().library)['factory']
    elf = open(axf, 'rb').read()
    fwtool.set_clone(lib, 'dual', factory, elf)
    f = lib.entries[lib.cat_entries[fwtool.CLONE_MENU][0]]
    data = fw.pack()
    open(out, 'wb').write(data)
    print('11.0 %08x %s (fabrica) + 11.1 %08x %s (%d bytes de modulo) -> %s' % (
        f.id, f.name, fwtool.CLONE_NAM_ID, fwtool.CLONE_NAM_NAME, len(elf), out))


def main():
    args = [a for a in sys.argv[1:] if a != '--dual']
    dual = len(args) != len(sys.argv) - 1
    axf = build_module()
    if dual:
        if len(args) != 2:
            raise SystemExit(__doc__)
        add_dual(args[0], axf, args[1])
    elif len(args) == 2:
        subprocess.run([sys.executable, '-I', '-m', 'matribox.firmware.fwtool', 'swapelf',
                        args[0], CLONE_ENTRY, axf, args[1]], check=True)


if __name__ == '__main__':
    main()
