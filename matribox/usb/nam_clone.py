"""Load NAM A2 captures into the Matribox II Pro Clone slots (firmware with examples/nam_clone).

    nam_clone.py ports                                  list the MIDI ports
    nam_clone.py list [--port P]                        names in the 10 Clone slots
    nam_clone.py send model.nam --slot N [--name X] [--port P]
    nam_clone.py pack model.nam out.bin                 just write the slot blob (nam/a2blob.h)
    nam_clone.py syx model.nam --slot N out.syx         SysEx file for other MIDI tools (MIDI-OX etc.)

The A2-Lite network of the .nam (matribox/nam/nam_a2.py) goes into the slot as an a2_blob; the PC editor must
be closed (Windows MIDI ports are exclusive). Same command the editor uses for its Clone import:
0x18000001 {u32 slot, u32 0, u32 byte sum, char name[20], data}; the pedal stores 0x2000 bytes in the
slot (FUN_00025d30), re-initialises the chain and answers 0x18000003 {slot, 1 = written, 0}.
"""
import argparse, json, os, struct, sys, zlib

from matribox.nam import nam_a2
from matribox.usb import htmidi

CMD_CLONE_LIST, CMD_CLONE_DATA, CMD_CLONE_ACK = 0x18000000, 0x18000001, 0x18000003
A2B_MAGIC, A2B_VERSION, A2B_HEADER, A2B_NATIVE_RATE = 0x324D414E, 1, 0x88, 1   # nam/a2blob.h
SLOTS = 10
NAME_MAX = 16               # the pedal keeps 17 bytes with the terminator (FUN_00025d30)
DATA_MAX = 0x2000 - 8 - 40  # decompression buffer 0x2023ade8..0x2023cde0 minus the command fields


def load(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _arch(m):
    """short description of one network of a .nam document"""
    a = m.get('architecture') or '?'
    if a == 'WaveNet':
        try:
            ch = sorted({la.get('channels') for la in m['config']['layers']})
            return 'WaveNet %s canais' % '/'.join(str(c) for c in ch)
        except Exception:
            return 'WaveNet'
    return a


def describe(path, m=None):
    """what the Studio shows about a .nam file: metadata and whether it fits a Clone slot (an A2-Lite net)"""
    m = m if m is not None else load(path)
    meta = dict(m.get('metadata') or {})
    lite = nam_a2.find_lite(m)
    if not meta and lite:
        meta = dict(lite.get('metadata') or {})
    subs = []
    if m.get('architecture') == 'SlimmableContainer':
        for s in m.get('config', {}).get('submodels', []):
            sm = s.get('model', {})
            subs.append(dict(arch=_arch(sm), weights=len(sm.get('weights') or []), max_value=s.get('max_value'),
                             lite=sm is lite))
    d = dict(path=path, file=os.path.basename(path), meta=meta, version=m.get('version'), arch=_arch(m),
             submodels=subs, lite=bool(lite), sample_rate=float(m.get('sample_rate') or 48000),
             name=(meta.get('name') or os.path.splitext(os.path.basename(path))[0]))
    if lite:
        d['sample_rate'] = float(lite.get('sample_rate') or d['sample_rate'])
        lm = lite.get('metadata') or {}
        d['loudness'] = lm.get('loudness', meta.get('loudness'))
        d['weights'] = len(lite.get('weights') or [])
        d['reason'] = ('rede A2-Lite (3 canais) dentro do container' if subs else 'rede A2-Lite (3 canais)')
    else:
        d['loudness'] = meta.get('loudness')
        if subs:
            d['reason'] = 'o container não tem a rede A2-Lite (tem %s)' % ', '.join(s['arch'] for s in subs)
        elif d['arch'] == 'WaveNet 3 canais':
            d['reason'] = 'WaveNet de 3 canais, mas não no formato A2 (camadas, dilatações ou ativações diferem)'
        elif m.get('architecture') == 'WaveNet':
            d['reason'] = '%s: só a A2-Lite (3 canais) roda no pedal' % d['arch']
        else:
            d['reason'] = 'arquitetura %s: só a WaveNet A2-Lite roda no pedal' % d['arch']
    return d


def pack_blob(path, native_rate=False, m=None):
    """-> (blob bytes, info dict); native_rate: run a 48 kHz model at 44.1 kHz (no resampling)"""
    m = m if m is not None else load(path)
    lite = nam_a2.find_lite(m)
    if not lite:
        raise ValueError('%s: no A2-Lite (3-channel A2 WaveNet) network found' % path)
    w = nam_a2.pack(lite['weights'])
    meta = lite.get('metadata') or m.get('metadata') or {}
    loud = meta.get('loudness')
    fs = float(lite.get('sample_rate') or m.get('sample_rate') or 48000)
    wb = struct.pack('<%df' % len(w), *w)
    hdr = struct.pack('<IHHffII', A2B_MAGIC, A2B_VERSION, len(w), float(loud) if loud is not None else -18.0,
                      fs, zlib.crc32(wb), A2B_NATIVE_RATE if native_rate else 0).ljust(A2B_HEADER, b'\0')
    blob = hdr + wb
    assert len(hdr) == A2B_HEADER and len(blob) <= DATA_MAX
    at48 = 47000 < fs < 49000 and not native_rate
    return blob, {'weights': len(w), 'loudness': loud, 'sample_rate': fs, 'bytes': len(blob),
                  'runs_at': '48 kHz (resampled)' if at48 else '44.1 kHz'}


# what was written to each slot (the pedal only reports names): shared with Matribox Studio's Capturas NAM tab
LOG_PATH = os.path.join(os.environ.get('APPDATA', os.path.dirname(os.path.abspath(__file__))), 'MatriboxStudio',
                        'nam_slots.json')


def load_log(path=LOG_PATH):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def record(slot, name, model, info, blob, path=LOG_PATH):
    """note that Clone slot `slot` (0-based) now holds `model` under `name`"""
    import datetime
    log = load_log(path)
    log[str(slot)] = dict(name=name, file=os.path.basename(model), path=os.path.abspath(model),
                          date=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'),
                          runs_at='48 kHz, com resampling' if info['runs_at'].startswith('48') else '44,1 kHz',
                          weights=info['weights'], bytes=info['bytes'], sample_rate=info['sample_rate'],
                          crc32='%08x' % zlib.crc32(blob))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(log, f, indent=1, ensure_ascii=False)
    return log


def clone_payload(slot, name, blob):
    nb = name.encode('latin1', 'replace')[:NAME_MAX]
    return struct.pack('<III', slot, 0, sum(blob) & 0xffffffff) + nb.ljust(20, b'\0') + blob


def default_name(path):
    return os.path.splitext(os.path.basename(path))[0][:NAME_MAX]


def cmd_list(port):
    data = htmidi.transact(port, CMD_CLONE_LIST, flag=htmidi.FLAG_REQ, want=CMD_CLONE_LIST)
    names = []
    for i in range(SLOTS):
        raw = data[80 + 17 * i:80 + 17 * (i + 1)]
        names.append(raw.split(b'\0')[0].decode('latin1'))
    return names


CMD_GLOBAL_SETTING = 0x16000002     # flag 0x11 {u32 idx}: reply {idx, u32 bits of app+0x7c+4*idx}
DIAG_IDX = 31                       # app+0xf8: the CPU meter of examples/nam_clone (see nam_clone.c)
BLOCK_US, LIMIT_US = 64e6 / 44100, 1250


def read_setting(port, idx):
    r = htmidi.transact(port, CMD_GLOBAL_SETTING, struct.pack('<I', idx), flag=htmidi.FLAG_REQ,
                        want=CMD_GLOBAL_SETTING, timeout=2)
    i, v = struct.unpack_from('<II', r, 0)
    return v if i == idx else None


def cmd_cpu(port, seconds, names=None):
    import time
    names = names or ['Clone', 'NAM', 'resampler', 'bloco do app', 'pico Clone', 'pico bloco']
    val = {}
    t_end = time.time() + seconds
    t_print = time.time() + 1.0
    while time.time() < t_end:
        try:
            w = read_setting(port, DIAG_IDX)
            read_setting(port, 30)          # alternate the index: the app holds back identical repeats
        except TimeoutError:
            continue
        if w is not None and w >> 28 < len(names):
            val[w >> 28] = w & 0x0fffffff
        if time.time() >= t_print and val:
            t_print += 1.0
            print('  '.join('%s %4d us (%2.0f%%)' % (names[k], v, 100.0 * v / BLOCK_US)
                            for k, v in sorted(val.items())) +
                  '   [bloco = %.0f us, overload acima de %d us]' % (BLOCK_US, LIMIT_US))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('ports')
    p = sub.add_parser('list'); p.add_argument('--port')
    p = sub.add_parser('cpu', help='live CPU meter of the NAM Clone module (worst case per ~0.75 s)')
    p.add_argument('--port'); p.add_argument('--seconds', type=float, default=30)
    p.add_argument('--names', help='comma-separated labels of the 6 values, for another module that writes the same '
                                    'meter (default: those of the NAM Clone module)')
    p = sub.add_parser('send'); p.add_argument('model'); p.add_argument('--slot', type=int, required=True)
    p.add_argument('--name'); p.add_argument('--port')
    p = sub.add_parser('pack'); p.add_argument('model'); p.add_argument('out')
    p = sub.add_parser('syx'); p.add_argument('model'); p.add_argument('--slot', type=int, required=True)
    p.add_argument('--name'); p.add_argument('out')
    for name in ('send', 'pack', 'syx'):
        sub.choices[name].add_argument('--native-rate', action='store_true',
                                       help='run a 48 kHz model at 44.1 kHz (no resampling, ~4%% less CPU, '
                                            'response ~8%% lower in frequency)')
    a = ap.parse_args()

    if a.cmd == 'ports':
        ins, outs = htmidi.list_ports()
        print('in: ', ins); print('out:', outs)
        return
    if a.cmd == 'list':
        with htmidi.Port(a.port) as port:
            for i, n in enumerate(cmd_list(port)):
                print('%2d  %s' % (i + 1, n))
        return
    if a.cmd == 'cpu':
        with htmidi.Port(a.port) as port:
            cmd_cpu(port, a.seconds, a.names.split(',') if a.names else None)
        return

    blob, info = pack_blob(a.model, a.native_rate)
    print('%s: A2-Lite, %d weights, trained at %.0f Hz, runs at %s, loudness %s dB, blob %d bytes' % (
        a.model, info['weights'], info['sample_rate'], info['runs_at'], info['loudness'], info['bytes']))
    if a.cmd == 'pack':
        open(a.out, 'wb').write(blob)
        return
    if not 1 <= a.slot <= SLOTS:
        sys.exit('--slot must be 1..%d' % SLOTS)
    name = (a.name or default_name(a.model))[:NAME_MAX]
    payload = clone_payload(a.slot - 1, name, blob)
    if a.cmd == 'syx':
        pk = htmidi.packets(htmidi.message(CMD_CLONE_DATA, payload))
        open(a.out, 'wb').write(b''.join(pk))
        print('wrote %s: %d packets (send them ~40 ms apart)' % (a.out, len(pk)))
        return
    with htmidi.Port(a.port) as port:
        print('port %s: writing slot %d "%s"...' % (port.name, a.slot, name))
        r = htmidi.transact(port, CMD_CLONE_DATA, payload, want=CMD_CLONE_ACK, timeout=15)
        slot, ok = struct.unpack_from('<II', r, 0)
        if slot != a.slot - 1 or ok != 1:
            sys.exit('pedal answered slot %d, status %d' % (slot + 1, ok))
        record(slot, name, a.model, info, blob)
        print('written (noted in %s); Clone slots now:' % LOG_PATH)
        for i, n in enumerate(cmd_list(port)):
            print('%2d  %s' % (i + 1, n))


if __name__ == '__main__':
    main()
