"""Build a NAM A2-Lite module from a .nam capture.

    build_nam.py model.nam --name "Display Name" [--id 0x07000100]

Writes examples/nam_a2/gen/<slug>.axf and <slug>.json (manifest for `matribox fw add` / the Studio).
"""
import os, sys, json, subprocess, re

HERE = os.path.dirname(os.path.abspath(__file__))
NAM = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'nam')   # A2-Lite sources
GEN = os.path.join(HERE, 'gen')

MENU_AMP = 4
PARAMS = [
    {'name': 'Gain', 'min': -24, 'max': 24, 'step': 1, 'default': 0, 'flags': [255, 0, 0x74]},
    {'name': 'Level', 'min': -24, 'max': 24, 'step': 1, 'default': 0, 'flags': [255, 0, 0x74]},
]


def main():
    src = sys.argv[1]
    name = sys.argv[sys.argv.index('--name') + 1] if '--name' in sys.argv else os.path.splitext(os.path.basename(src))[0]
    eid = int(sys.argv[sys.argv.index('--id') + 1], 0) if '--id' in sys.argv else 0x07000100
    if len(name) > 19:
        sys.exit('name too long (19 chars max)')
    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    os.makedirs(GEN, exist_ok=True)
    wpre = os.path.join(GEN, slug + '_w')
    subprocess.run([sys.executable, '-I', '-m', 'matribox.nam.nam_a2', src, wpre, '--name', name], check=True)
    axf = os.path.join(GEN, slug + '.axf')
    env = dict(os.environ, MB_CFLAGS='-I %s -DNAM_EFFECT_ID=0x%08Xu' % (NAM, eid))
    subprocess.run([sys.executable, '-I', '-m', 'matribox.module.buildmod', axf,
                    os.path.join(HERE, 'nam_mod.c'), os.path.join(NAM, 'a2lite.c'), wpre + '.c'], check=True, env=env)
    man = {'elf': slug + '.axf', 'id': '0x%08X' % eid, 'name': name, 'category': 'NAM', 'menu': MENU_AMP,
           'params': PARAMS}
    json.dump(man, open(os.path.join(GEN, slug + '.json'), 'w'), indent=2)
    print('manifest', os.path.join(GEN, slug + '.json'))


if __name__ == '__main__':
    main()
