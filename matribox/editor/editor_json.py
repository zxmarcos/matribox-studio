"""Add the effects of a modified QME-200 firmware to the PC editor's effect list (alg_data.json).

The editor ("Matribox II PRO", Flutter) loads assets/alg/alg_data.json and identifies every effect by
(module index, fxid); presets carry the fxid. Factory JSON vs firmware library (V1.2.2): same order,
codeing == 'ValToStr_%03d' % f3 (866/866), Sync == N for f2 0x1N else -1 (865/866), and widgetType is
1 for format 0 (Off/On), 2 for option lists, 3 for format 0x5d (+/- dB), 0 otherwise.
Menus whose list does not come from the library (5 Cab, 6 User IR, 11 Clone) are left untouched.

usage: editor_json.py <firmware.bin> <alg_data.json in> <alg_data.json out>
"""
import json, os, re, sys
from matribox import paths
from matribox.firmware import fwtool

SPECIAL = {5, 6, 11}


def list_codes():
    codes = set()
    for line in open(os.path.join(paths.DATA, 'display_formats.txt'), encoding='utf8'):
        m = re.match(r'0x([0-9a-f]+)\s+list', line)
        if m:
            codes.add(int(m.group(1), 16))
    return codes


def num(x):
    return ('%d' % x) if float(x).is_integer() else ('%g' % x)


def widget_type(code, lists):
    if code == 0:
        return 1
    if code == 0x5d:
        return 3
    return 2 if code in lists else 0


def entry(e, index, lists, source):
    widgets = []
    for k, p in enumerate(e.params):
        f1, f2, f3 = p['flags']
        widgets.append({
            'name': p['name'], 'ID': str(p['idx']), 'defaultValue': num(p['default']),
            'valueRange': '%s-%s' % (num(p['min']), num(p['max'])),
            'min': num(p['min']), 'max': num(p['max']), 'step': num(p['step']),
            'codeing': 'ValToStr_%03d' % f3, 'widgetType': widget_type(f3, lists),
            'Sync': (f2 & 0xf) if f2 & 0xf0 == 0x10 else -1})
    para = '\n'.join(p['name'] for p in e.params)
    return {'fxid': e.id, 'index': index, 'fxtitle': e.name, 'name': e.name, 'type': e.category,
            'origin': source, 'mark': '', 'associationId': '', 'remark': '',
            'descriptionCN': source, 'descriptionEN': source, 'style': '',
            'paraCN': para, 'paraEN': para, 'widget': widgets}


def main():
    fw = fwtool.Firmware(open(sys.argv[1], 'rb').read())
    raw = open(sys.argv[2], 'rb').read()
    data = json.loads(raw.decode('utf-8-sig'))
    lists = list_codes()
    origins = {}
    for d in [os.path.join(paths.EXAMPLES, 'parallel')] + paths.setting('catalog_dirs', []):
        for f in os.listdir(d) if os.path.isdir(d) else []:
            if f.endswith('.json'):
                m = json.load(open(os.path.join(d, f)))
                if 'id' in m and 'name' in m:
                    origins[int(str(m['id']), 0)] = m.get('source', 'Matribox mod')
    added = 0
    lib = fw.library
    for mod in data['Modules']:
        menu = mod['index']
        if menu in SPECIAL or menu >= len(lib.cat_entries):
            continue
        have = {a['fxid']: a for a in mod['alg']}
        new = []
        for k, ei in enumerate(lib.cat_entries[menu]):
            e = lib.entries[ei]
            if e.id in have:
                a = have.pop(e.id)
            else:
                src = origins.get(e.id, 'Matribox mod')
                src = 'Valeton GP-200' if src.startswith('GP-200') else src
                a = entry(e, k, lists, src)
                added += 1
                print('  %-6s +%-18s [%s] %d params' % (mod['name'], e.name, e.category, len(e.params)))
            a['index'] = k
            new.append(a)
        new += list(have.values())          # JSON-only entries (none on factory data) keep their place at the end
        mod['alg'] = new
    out = json.dumps(data, ensure_ascii=False, indent=4).replace('\n', '\r\n')
    open(sys.argv[3], 'wb').write(b'\xef\xbb\xbf' + out.encode('utf8') if raw.startswith(b'\xef\xbb\xbf') else out.encode('utf8'))
    print('%d entries added -> %s' % (added, sys.argv[3]))


if __name__ == '__main__':
    main()
