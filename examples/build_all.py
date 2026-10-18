"""Build every example module (.axf next to its manifest), as the Studio and the firmware recipes expect them.

    python examples/build_all.py [name ...]        default: all

Needs Zig (the ARM compiler) and the package installed (pip install -e .). nam_a2 is built from the first .nam
capture found in examples/nam_a2/models/ (none is distributed: put an A2 capture of your own there), and skipped
when there is none.
"""
import glob, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = [sys.executable, '-I']
BUILDMOD = PY + ['-m', 'matribox.module.buildmod']


def module(example, name, *srcs):
    d = os.path.join(HERE, example)
    return BUILDMOD + [os.path.join(d, name + '.axf')] + [os.path.join(d, s) for s in srcs]


EXAMPLES = {
    'gain': [module('gain', 'gain', 'gain.c')],
    'modecho': [module('modecho', 'modecho', 'modecho.c')],
    'multimode': [module('multimode', 'multimode', 'multimode.c')],
    'parallel': [module('parallel', m, m + '.c') for m in ('split', 'pathb', 'merge')],
    'nam_a2': [],                       # filled in by nam_a2_commands()
    'nam_clone': [PY + [os.path.join(HERE, 'nam_clone', 'build_clone.py')]],
}


def nam_a2_commands():
    models = sorted(glob.glob(os.path.join(HERE, 'nam_a2', 'models', '*.nam')))
    if not models:
        print('nam_a2: no capture in examples/nam_a2/models/, skipped')
        return []
    return [PY + [os.path.join(HERE, 'nam_a2', 'build_nam.py'), models[0], '--name', 'NAM A2 Demo']]


def main():
    EXAMPLES['nam_a2'] = nam_a2_commands()
    names = sys.argv[1:] or [n for n in EXAMPLES if os.path.isdir(os.path.join(HERE, n))]
    for n in names:
        for cmd in EXAMPLES[n]:
            subprocess.run(cmd, check=True)


if __name__ == '__main__':
    main()
