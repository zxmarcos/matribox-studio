"""Port Valeton GP-200 / Matribox II (QME-100) effects that the Matribox II Pro lacks into a QME-200 image.

  gpport.py export <GP-200|QME-100 fw> <QME-200 fw> <outdir> [skip_dir ...]
                                                                convert + emulate + write .axf/.json
                                                                (ids already exported in skip_dir are left out)
  gpport.py build  <QME-200 fw> <outdir[,outdir...]> <out.bin> [name ...]
                                                                add exported modules (all, or the named),
                                                                symbol names stripped (gp2qme.strip_names)

Menu mapping: GP menu -> QME menu, by category for the GP pre-FX/NR/wah menus (QME keeps gates and
boosts in menu 0, filters/pitch/special in 1, wahs in 2, drives in 3). Parameter records are copied
verbatim (same format in both firmwares: idx, panel slot, sync and display-format flags).
Needs unicorn on PYTHONPATH (RAM sizes are measured in matribox/module/effemu.py).

The export writes a library.json in the output folder (name, colour, and "port": the recipe's key), so the
folder is a ready catalog for the Studio, whose Ferramentas tab runs the same export.
"""
import hashlib, json, math, os, random, sys
from matribox.ports import gp200lib, gp2qme
from matribox.firmware import fwtool

GP_TO_QME = {2: 3, 3: 4, 6: 7, 7: 8, 8: 9, 9: 10}
BY_CATEGORY = {'Comp': 0, 'Boost': 0, 'Acoustic': 0, 'Gate': 0, 'Filter': 1, 'Pitch': 1, 'Special': 1,
               'Wah': 2, 'Auto Filter': 2, 'OD': 3}
SKIP_MENUS = {5, 10}                       # CAB IR engine and the volume/routing slot
# QME-100 menus: 0/1 pre-FX, 2 amp, 3 FX-loop send, 4 return, 5 NR, 6 CAB IR, 7 EQ, 8 mod, 9 delay, 10 reverb, 11 vol
Q100_TO_QME = {2: 4, 7: 7, 8: 8, 9: 9, 10: 10}
Q100_SKIP_MENUS = {3, 4, 6, 11}            # FX loop (jacks the QME-200 lacks), CAB IR engine, volume

# the recipes: the firmware each one was made for (memory windows in gp200lib / qme100lib.MENU_MEM)
PORTS = [
    dict(key='gp200', name='Valeton GP-200', model='GP-200', version='V1.8.2', file='GP-200_USB_Firmware_V1.8.2.bin',
         sha256='c368618773097b07c556c951eb10792f9d57be929323366648eb6e083d0d23af', color='#a78bfa', expect=39),
    dict(key='qme100', name='Matribox II (QME-100)', model='QME-100', version='V1.3.0',
         file='QME-100_FW_USB_V1.3.0.bin',
         sha256='da4c40a48673c5c4e6ec37717f9cb78d0dc7c461eb5c47757d0a5f89398e5561', color='#38bdf8', expect=2, after='gp200'),
]
PORT = {p['key']: p for p in PORTS}


def identify(data):
    """(recipe, version, verified) for a GP-200 / QME-100 image; verified: the exact file the recipe was made
    from (SHA-256). ValueError when it is neither pedal's USB firmware."""
    if data[:4] == b'HTFW' and data[0x0c:0x13] == b'QME-100':
        p, version = PORT['qme100'], 'V%d.%d.%d' % tuple(data[0x1d:0x20])
    elif data[:4] == b'FRMW' and data[0x10:0x17] == b'GP-200\0':
        p, version = PORT['gp200'], None           # the FRMW header has no readable version
    elif data[:4] == b'FRMW' and data[0x10:0x17] == b'QME-200':
        raise ValueError('este é um firmware do próprio QME-200')
    else:
        raise ValueError('não é um firmware USB do GP-200 nem do QME-100')
    verified = hashlib.sha256(data).hexdigest() == p['sha256']
    return p, version or (p['version'] if verified else '?'), verified


def qme_menu(e, q100=False):
    if e['category'] == 'Boost':
        return 0
    direct = Q100_TO_QME if q100 else GP_TO_QME
    if e['menu'] in direct:
        return direct[e['menu']]
    return BY_CATEGORY.get(e['category'])


def safe(name):
    return ''.join(c if c.isalnum() else '_' for c in name)


def sanity(elf, params):
    from matribox.module import effemu
    rnd = random.Random(3)
    audio = [(rnd.uniform(-.3, .3),) * 2 for _ in range(64 * 120)]
    res = []
    for pick in ('default', 'max', 'min'):
        knobs = [0.0] * 16
        for p in params:
            knobs[p['idx']] = p[pick]
        out = effemu.Emu(elf).run(knobs, audio)
        bad = sum(1 for l, r in out if not (math.isfinite(l) and math.isfinite(r)) or abs(l) > 50)
        rms = math.sqrt(sum(l * l for l, r in out) / len(out))
        res.append('%s rms=%.3f%s' % (pick, rms, ' BAD=%d' % bad if bad else ''))
    return res


def manifest_ids(d):
    """ids of the module manifests in folder d"""
    out = set()
    for f in os.listdir(d) if os.path.isdir(d) else []:
        if f.endswith('.json') and f != 'library.json':
            try:
                out.add(int(str(json.load(open(os.path.join(d, f)))['id']), 0))
            except (ValueError, KeyError, TypeError, OSError):
                pass
    return out


def folder_port(d):
    """the recipe key a folder was exported by (its library.json), or None"""
    try:
        return json.load(open(os.path.join(d, 'library.json'), encoding='utf-8')).get('port')
    except (OSError, ValueError, AttributeError):
        return None


def clear_export(d, key):
    """remove an earlier export of recipe `key` from d (its manifests and modules), so a new one does not leave
    stale modules behind; refuses a folder holding manifests of anything else"""
    if not os.path.isdir(d):
        return 0
    mans = [f for f in os.listdir(d) if f.endswith('.json') and f != 'library.json']
    if not mans:
        return 0
    if folder_port(d) != key:
        raise ValueError('a pasta %s já tem manifestos que não vieram deste port: escolha outra' % d)
    for f in mans:
        try:
            elf = json.load(open(os.path.join(d, f))).get('elf')
        except (OSError, ValueError, AttributeError):
            elf = None
        if elf and os.path.basename(elf) == elf and os.path.exists(os.path.join(d, elf)):
            os.remove(os.path.join(d, elf))
        os.remove(os.path.join(d, f))
    return len(mans)


def export(gpfw, qmfw, outdir, skip_dirs=(), log=print, on_item=None, cancel=None):
    """Convert every module of gpfw whose id the QME-200 image qmfw (and the folders skip_dirs) lack.
    log(line): the text report; on_item(record, done, total): each module as it is finished, record =
    dict(id, name, category, menu (QME), status 'ok' | 'failed' | 'skipped', reason, elf, relocs, iram, sram, checks);
    cancel(): stop before the next module when true. Returns the records."""
    lib = fwtool.Firmware(qmfw).library
    qids = {e.id for e in lib.entries}
    for d in skip_dirs:
        qids |= manifest_ids(d)
    q100 = gpfw[:4] == b'HTFW'
    src = 'Matribox II (QME-100) V%d.%d.%d' % tuple(gpfw[0x1d:0x20]) if q100 else 'GP-200 V1.8.2'
    skip_menus = Q100_SKIP_MENUS if q100 else SKIP_MENUS
    os.makedirs(outdir, exist_ok=True)
    rec = PORT['qme100' if q100 else 'gp200']
    meta = os.path.join(outdir, 'library.json')
    if not os.path.exists(meta) or folder_port(outdir) == rec['key']:
        json.dump(dict(name=rec['name'], color=rec['color'], description='Módulos portados do %s (%s)' % (
            rec['name'], src), port=rec['key']), open(meta, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    done, report, records = set(), [], []
    mods = gp2qme.all_modules(gpfw)
    # canonical copy first: the module in the menu its category maps to
    mods.sort(key=lambda m: (m.e['menu'] in ((1,) if q100 else (0, 1, 4))))
    total = len({m.e['id'] for m in mods if m.e['id'] not in qids and m.menu not in skip_menus})
    seen = set()

    def item(r):                                 # one record per id: a copy in another menu replaces a failure
        records[:] = [x for x in records if x['id'] != r['id']] + [r]
        if on_item:
            on_item(r, len(seen), total)

    for m in mods:
        e = m.e
        if e['id'] in qids or e['id'] in done or m.menu in skip_menus:
            continue
        if cancel and cancel():
            log('cancelado')
            break
        seen.add(e['id'])
        r = dict(id=e['id'], name=e['name'], category=e['category'], menu=None, status='failed', reason='', elf=0, relocs=0,
                 iram=0, sram=0, checks=[])
        menu = qme_menu(e, q100)
        if menu is None:
            report.append('%-16s skipped: no QME menu for category %r' % (e['name'], e['category']))
            item(dict(r, status='skipped', reason='nenhum menu do QME-200 para a categoria %r' % e['category']))
            continue
        r['menu'] = menu
        try:
            m.scan()
            if m.external:
                raise ValueError('references GP-200 app state %s' % sorted(m.external))
            hit = gp2qme.measure(m)
            _, top = m.sizes(m.scan())
            iram = gp2qme.align(max(hit.get(gp2qme.IRAM, 0), top.get(gp2qme.IRAM, -4) + 4), 0x10) or None
            sram = gp2qme.align(max(hit.get(gp2qme.SRAM, 0), top.get(gp2qme.SRAM, -4) + 4), 0x10) or None
            elf, sizes, _, nrel = m.to_elf(iram, sram)
            if len(elf) > gp2qme.MAX_ELF:
                raise ValueError('ELF %#x > 32 KB staging slot' % len(elf))
            checks = sanity(elf, e['params'])
        except Exception as ex:                  # keep going, report at the end
            report.append('%-16s FAILED: %s' % (e['name'], ex))
            item(dict(r, reason=str(ex) or type(ex).__name__))
            continue
        done.add(e['id'])
        base = safe(e['name'])
        open(os.path.join(outdir, base + '.axf'), 'wb').write(elf)
        man = dict(menu=menu, id='%#010x' % e['id'], name=e['name'], category=e['category'] or 'GP-200',
                   elf=base + '.axf', link_id=-1,
                   params=[dict(name=p['name'], min=p['min'], max=p['max'], step=p['step'], default=p['default'],
                                idx=p['idx'], flags=p['flags']) for p in e['params']],
                   source='%s menu %d.%d' % (src, e['menu'], e['index']),
                   ram=dict(iram=hex(sizes.get(gp2qme.IRAM, 0)), sram=hex(sizes.get(gp2qme.SRAM, 0))))
        json.dump(man, open(os.path.join(outdir, base + '.json'), 'w'), indent=1)
        report.append('%-16s -> menu %2d  elf=%5d relocs=%3d IRAM=%#7x SRAM=%#8x  %s' % (
            e['name'], menu, len(elf), nrel, sizes.get(gp2qme.IRAM, 0), sizes.get(gp2qme.SRAM, 0), ', '.join(checks)))
        log(report[-1])
        item(dict(r, status='ok', elf=len(elf), relocs=nrel, iram=sizes.get(gp2qme.IRAM, 0),
                  sram=sizes.get(gp2qme.SRAM, 0), checks=checks))
    log('\n'.join(r for r in report if 'FAILED' in r or 'skipped' in r))
    return records


def build(qmfw_path, outdirs, out, names):
    data = open(qmfw_path, 'rb').read()
    fw = fwtool.Firmware(data)
    mans = sorted((f, d) for d in outdirs.split(',') for f in os.listdir(d)
                  if f.endswith('.json') and f != 'library.json')
    added = 0
    for f, outdir in mans:
        m = json.load(open(os.path.join(outdir, f)))
        if names and m['name'] not in names:
            continue
        elf = gp2qme.strip_names(open(os.path.join(outdir, m['elf']), 'rb').read())
        group, pos = fw.library.place(m['menu'], m)
        e = fwtool.Entry.build(int(m['id'], 0), m['name'], m['category'], m['params'], elf,
                               (group, 0xff, 0), int(m.get('link_id', -1)))
        fw.library.add(m['menu'], e, pos)
        added += 1
        print('menu %2d group %d pos %3d  %s' % (m['menu'], group, pos, m['name']))
    img = fw.pack()
    open(out, 'wb').write(img)
    print('%d modules added; library %#x of %#x bytes; wrote %s' % (
        added, len(fw.library.pack()), fwtool.MAX_LIBRARY, out))


def main():
    a = sys.argv[1:]
    if len(a) >= 4 and a[0] == 'export':
        export(open(a[1], 'rb').read(), open(a[2], 'rb').read(), a[3], a[4:],
               log=lambda s: print(s, flush=True))
    elif len(a) >= 4 and a[0] == 'build':
        build(a[1], a[2], a[3], a[4:])
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
