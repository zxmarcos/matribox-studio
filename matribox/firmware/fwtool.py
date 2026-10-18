"""Rebuild tool for Sonicake Matribox II Pro (QME-200) USB firmware images.

Container layout (all little-endian):
  0x00  'FRMW'
  0x04  u32 payload size (= file size - 0x30)
  0x08..0x2f  version/model fields (kept verbatim)
  0x30  4 x (u32 offset, u32 size), offsets relative to 0x30:
        [0] main app (ITCM + SDRAM images)   -> flash ?
        [1] effect library                   -> flash 0x480000 (max 0x280000)
        [2] cabinet IR library               -> flash 0x700000
        [3] presets / system settings        -> flash 0x780000

Effect library (section 1):
  +0x00 u32 0
  +0x04 u32 offset of category table (= 0x18 + 4*ncat; checked by the app)
  +0x08 u32 size of the index (copied to RAM, <= 0x1000) == offset of first entry
  +0x0c u32 total library size
  +0x10 u32 0
  +0x14 u32 ncat (app clamps to 16)
  +0x18 u32 cat_list_off[ncat]
  lists: u32 count, u32 entry_off[count]
  entries: u32 total, u32 hdr_size, u32 id, i32 -1, u8 nparams, u8 0, u8 0xff, u8 0,
           u16 name_str, u16 category_str, nparams x 24-byte param records, strings,
           0xff padding to 4, then the module ELF (<= 0x8000 bytes).
"""
import struct, sys, json, argparse

FRMW_SECTIONS = 4
MAX_ELF = 0x8000
MAX_INDEX = 0x1000
MAX_LIBRARY = 0x700000 - 0x480000
MAX_PARAMS = 15                 # knob slots per chain slot (slot struct 0x44 bytes)
MAX_NAME = 19                   # UI copies names into 20-byte buffers
MAX_OPTION = 14                 # value texts go into 15-byte buffers
OPTION_SEP = '|'                # text-list parameters (app patch "textlist"): head = offset of "A|B|C"
SPECIAL_MENUS = {5, 6, 11}      # Cab, User IR, Clone: menu items come from other data


class Entry:
    def __init__(self, raw):
        self.raw = bytes(raw)
        total, hdr = struct.unpack_from('<II', raw, 0)
        assert total == len(raw)
        self.hdr_size = hdr
        self.id, self.link_id = struct.unpack_from('<Ii', raw, 8)   # link_id: default cab for amps, else -1
        self.nparams = raw[16]
        no, co = struct.unpack_from('<HH', raw, 20)
        strtab = 24 + 24 * self.nparams
        s = lambda o: raw[strtab + o:raw.index(b'\0', strtab + o)].decode('latin1')
        self.name, self.category = s(no), s(co)
        self.params = []
        for i in range(self.nparams):
            u, po, mn, mx, st, df, idx, f1, f2, f3 = struct.unpack_from('<hH4f4B', raw, 24 + 24 * i)
            self.params.append(dict(name=s(po), min=mn, max=mx, step=st, default=df, idx=idx,
                                    flags=[f1, f2, f3], head=u))
            if u >= 0:                                                  # text list (factory entries: always -1)
                self.params[-1]['options'] = s(u).split(OPTION_SEP)
        self.hflags = list(raw[17:20])                                # UI/class flags, usually [0, 0xff, 0]
        self.elf = raw[hdr:]

    @classmethod
    def build(cls, effect_id, name, category, params, elf, hflags=(0, 0xff, 0), link_id=-1):
        """Create a new library entry. params: list of dicts {name,min,max,step,default[,flags][,options]}.
        options = list of value texts (shown by the "textlist" app patch; the value is the option index)."""
        strings = bytearray()
        seen = {}
        def addstr(t):
            if t in seen: return seen[t]
            o = seen[t] = len(strings); strings.extend(t.encode('latin1') + b'\0'); return o
        name_o = addstr(name)
        cat_o = addstr(category)
        recs = bytearray()
        for i, p in enumerate(params):
            po = addstr(p['name'])
            f = p.get('flags', [0xff, 0x00, 0x15])
            head = addstr(OPTION_SEP.join(p['options'])) if p.get('options') else p.get('head', -1)
            recs += struct.pack('<hH4f4B', head, po, p['min'], p['max'], p['step'], p['default'],
                                p.get('idx', i), *f)
        hdr = bytearray(struct.pack('<IIIiBBBBHH', 0, 0, effect_id, link_id, len(params), *hflags, name_o, cat_o))
        hdr += recs + strings
        while len(hdr) % 4: hdr.append(0xff)
        if len(elf) > MAX_ELF:
            raise ValueError('module ELF is %#x bytes, device staging buffer is %#x' % (len(elf), MAX_ELF))
        struct.pack_into('<II', hdr, 0, len(hdr) + len(elf), len(hdr))
        return cls(bytes(hdr) + bytes(elf))

    def with_elf(self, elf):
        """same header (id, name, params), different module"""
        if len(elf) > MAX_ELF:
            raise ValueError('module ELF is %#x bytes, device staging buffer is %#x' % (len(elf), MAX_ELF))
        hdr = bytearray(self.raw[:self.hdr_size])
        struct.pack_into('<I', hdr, 0, len(hdr) + len(elf))
        return Entry(bytes(hdr) + bytes(elf))


class Library:
    def __init__(self, blob):
        z0, cat_off, idx_size, total = struct.unpack_from('<4I', blob, 0)
        assert total == len(blob), 'library size mismatch'
        ncat = struct.unpack_from('<I', blob, 0x14)[0]
        assert cat_off == 0x18 + 4 * ncat
        self.cats = []                          # list of lists of entry offsets
        for c in range(ncat):
            lo = struct.unpack_from('<I', blob, 0x18 + 4 * c)[0]
            n = struct.unpack_from('<I', blob, lo)[0]
            self.cats.append(list(struct.unpack_from('<%dI' % n, blob, lo + 4)))
        # entries in physical order
        offs = sorted({o for l in self.cats for o in l})
        assert offs[0] == idx_size
        self.entries = []
        self._by_off = {}
        for o in offs:
            e = Entry(blob[o:o + struct.unpack_from('<I', blob, o)[0]])
            self._by_off[o] = len(self.entries)
            self.entries.append(e)
        self.cat_entries = [[self._by_off[o] for o in l] for l in self.cats]
        self.original = blob

    def add(self, cat, entry, position=None):
        self.entries.append(entry)
        lst = self.cat_entries[cat]
        lst.insert(len(lst) if position is None else position, len(self.entries) - 1)

    def groups(self, menu):
        """subgroup index -> category string (UI tab label = category of the group's first entry)"""
        g = {}
        for ei in self.cat_entries[menu]:
            e = self.entries[ei]
            g.setdefault(e.hflags[0], e.category)
        return g

    def place(self, menu, m):
        """Validate a manifest against the UI rules found in the app; returns (group, position)."""
        if not 0 <= menu < len(self.cat_entries):
            raise ValueError('menu must be 0..%d (the app supports at most 16 menus)' % (len(self.cat_entries) - 1))
        if menu in SPECIAL_MENUS:
            raise ValueError('menu %d lists cab/IR/clone data, not library entries: the effect would never show' % menu)
        eid = int(str(m['id']), 0)
        if any(e.id == eid for e in self.entries):
            raise ValueError('id %#010x already used' % eid)
        for key in ('name', 'category'):
            if len(m[key]) > MAX_NAME:
                raise ValueError('%s %r longer than %d chars (UI buffer is 20 bytes)' % (key, m[key], MAX_NAME))
        ps = m['params']
        if len(ps) > MAX_PARAMS:
            raise ValueError('%d params: a chain slot stores at most %d knob values' % (len(ps), MAX_PARAMS))
        idxs = [p.get('idx', i) for i, p in enumerate(ps)]
        if len(set(idxs)) != len(idxs) or max(idxs, default=0) >= MAX_PARAMS:
            raise ValueError('param idx values must be unique and < %d' % MAX_PARAMS)
        for i, p in enumerate(ps):
            if len(p['name']) > MAX_NAME:
                raise ValueError('param name %r too long' % p['name'])
            if 'options' in p:                      # text list: value = option index 0..n-1
                o = p['options']
                if not o or any(not x or OPTION_SEP in x or len(x) > MAX_OPTION for x in o):
                    raise ValueError('param %r: options must be 1..%d chars, without %r' % (p['name'], MAX_OPTION, OPTION_SEP))
                p.setdefault('min', 0); p.setdefault('max', len(o) - 1); p.setdefault('step', 1)
                p.setdefault('default', 0)
                if (p['min'], p['max'], p['step']) != (0, len(o) - 1, 1):
                    raise ValueError('param %r: options need min 0, max %d, step 1' % (p['name'], len(o) - 1))
            if not (p['min'] <= p['default'] <= p['max']) or p['step'] <= 0:
                raise ValueError('param %r: need min <= default <= max and step > 0' % p['name'])
            f2 = p.get('flags', [0xff, 0, 0x15])[1]
            if f2 & 0xf0 in (0x10, 0x20):           # tempo-sync pair: 0x1N on the value, 0x2N on its Sync switch
                n = f2 & 0xf
                want = (0x20 if f2 & 0xf0 == 0x10 else 0x10) | i
                if n >= len(ps) or ps[n].get('flags', [0, 0, 0])[1] != want:
                    raise ValueError('param %r: sync flag %#x needs param %d with flag %#x' % (p['name'], f2, n, want))
        groups = self.groups(menu)
        lst = self.cat_entries[menu]
        if 'group' in m:
            group = m['group']
        else:
            match = [g for g, c in groups.items() if c == m['category']]
            group = match[0] if match else (max(groups) + 1 if groups else 0)
        if group in groups and groups[group] != m['category']:
            raise ValueError('group %d is labelled %r in this menu' % (group, groups[group]))
        if 'position' in m:
            pos = m['position']
        elif not groups or group >= max(groups):
            pos = len(lst)
        else:                                       # right after the group's last entry, keeps the menu tail intact
            pos = max(k for k, ei in enumerate(lst) if self.entries[ei].hflags[0] == group) + 1
        # the app derives the tab count from the last two entries only: max(g[n-2], g[n-1]) + 1
        gl = [self.entries[ei].hflags[0] for ei in lst]
        gl.insert(pos, group)
        if max(gl[-2:]) != max(gl):
            raise ValueError('position %d would hide subgroup tabs (menu tail must keep the highest group)' % pos)
        return group, pos

    def remove(self, eis):
        """drop entries (indices into self.entries) from the menus and from the image"""
        eis = set(eis)
        keep = [ei for ei in range(len(self.entries)) if ei not in eis]
        new = {ei: k for k, ei in enumerate(keep)}
        self.entries = [self.entries[ei] for ei in keep]
        self.cat_entries = [[new[ei] for ei in l if ei in new] for l in self.cat_entries]

    def pack(self):
        ncat = len(self.cat_entries)
        idx = bytearray(struct.pack('<6I', 0, 0x18 + 4 * ncat, 0, 0, 0, ncat))
        idx += b'\0' * (4 * ncat)
        list_pos = []
        for l in self.cat_entries:
            list_pos.append(len(idx))
            idx += struct.pack('<I', len(l)) + b'\0' * (4 * len(l))
        if len(idx) > MAX_INDEX:
            raise ValueError('index %#x bytes exceeds the %#x RAM copy' % (len(idx), MAX_INDEX))
        offs, pos = [], len(idx)
        for e in self.entries:
            offs.append(pos); pos += len(e.raw)
        for c, l in enumerate(self.cat_entries):
            struct.pack_into('<I', idx, 0x18 + 4 * c, list_pos[c])
            for k, ei in enumerate(l):
                struct.pack_into('<I', idx, list_pos[c] + 4 + 4 * k, offs[ei])
        struct.pack_into('<II', idx, 8, len(idx), pos)
        out = bytes(idx) + b''.join(e.raw for e in self.entries)
        if len(out) > MAX_LIBRARY:
            raise ValueError('library %#x bytes exceeds flash partition %#x' % (len(out), MAX_LIBRARY))
        return out


class Firmware:
    def __init__(self, data):
        assert data[:4] == b'FRMW'
        assert struct.unpack_from('<I', data, 4)[0] == len(data) - 0x30
        self.header = bytearray(data[:0x30])
        self.sections = []
        for i in range(FRMW_SECTIONS):
            off, size = struct.unpack_from('<II', data, 0x30 + 8 * i)
            self.sections.append(data[0x30 + off:0x30 + off + size])
        self.library = Library(self.sections[1])

    def pack(self):
        secs = list(self.sections)
        secs[1] = self.library.pack()
        table = bytearray()
        pos = 8 * FRMW_SECTIONS
        for s in secs:
            table += struct.pack('<II', pos, len(s)); pos += len(s)
        body = bytes(table) + b''.join(secs)
        hdr = bytearray(self.header)
        struct.pack_into('<I', hdr, 4, len(body))
        # FIRMWARE_HEADER.nChecksum (u16 @0x0e) = 8-bit sum of every byte from 0x30 to EOF
        struct.pack_into('<H', hdr, 0x0e, sum(body) & 0xff)
        return bytes(hdr) + body


# Clone menu engines. 'factory': 11.0 = factory Clone; 'nam': 11.0 = NAM A2 module (examples/nam_clone, same
# id / name / knobs); 'dual': 11.0 factory + 11.1 NAM, which needs the app patch "clonedual" (apppatch.py):
# without it, Clone slot 1 always runs 11.1 and the other slots 11.0.
CLONE_MENU = 11
CLONE_NAM_ID, CLONE_NAM_NAME = 0x0F100000, 'NAM A2'
CLONE_MODES = ('factory', 'nam', 'dual')


def clone_engine(elf):
    return 'factory' if b'ToneCloneInit' in elf else 'nam'


def clone_mode(lib):
    """engines of the Clone menu: 'factory', 'nam', 'dual' or '?' (anything else)"""
    es = [clone_engine(lib.entries[ei].elf) for ei in lib.cat_entries[CLONE_MENU]]
    return {('factory',): 'factory', ('nam',): 'nam', ('factory', 'nam'): 'dual'}.get(tuple(es), '?')


def clone_elfs(lib):
    """{'factory': elf, 'nam': elf} found in the Clone menu"""
    return {clone_engine(lib.entries[ei].elf): lib.entries[ei].elf for ei in lib.cat_entries[CLONE_MENU]}


def set_clone(lib, mode, factory_elf, nam_elf):
    """rebuild the Clone menu for mode; 11.0 keeps its id, name and knobs, 11.1 (dual) copies them"""
    assert mode in CLONE_MODES, mode
    lst = lib.cat_entries[CLONE_MENU]
    first = lib.entries[lst[0]]
    lib.entries[lst[0]] = first.with_elf(nam_elf if mode == 'nam' else factory_elf)
    lib.remove(lst[1:])
    if mode == 'dual':
        if any(e.id == CLONE_NAM_ID for e in lib.entries):
            raise ValueError('id %#010x already used' % CLONE_NAM_ID)
        lib.add(CLONE_MENU, Entry.build(CLONE_NAM_ID, CLONE_NAM_NAME, first.category, first.params, nam_elf,
                                        first.hflags, first.link_id))


def load_params(path):
    m = json.load(open(path))
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('roundtrip'); p.add_argument('fw'); p.add_argument('out')
    p = sub.add_parser('list'); p.add_argument('fw')
    p = sub.add_parser('add', help='add a module described by a JSON manifest')
    p.add_argument('fw'); p.add_argument('manifest'); p.add_argument('out')
    p = sub.add_parser('swapelf', help='replace the module of entry MENU.POS, keeping its id, name and params')
    p.add_argument('fw'); p.add_argument('entry'); p.add_argument('elf'); p.add_argument('out')
    a = ap.parse_args()
    data = open(a.fw, 'rb').read()
    fw = Firmware(data)
    if a.cmd == 'roundtrip':
        out = fw.pack()
        open(a.out, 'wb').write(out)
        print('identical' if out == data else 'DIFFERENT', len(out))
    elif a.cmd == 'list':
        for c, l in enumerate(fw.library.cat_entries):
            for k, ei in enumerate(l):
                e = fw.library.entries[ei]
                print('%2d.%-2d %08x %-16s %-12s %s' % (c, k, e.id, e.name, e.category,
                                                       ', '.join(p['name'] for p in e.params)))
    elif a.cmd == 'add':
        m = json.load(open(a.manifest))
        import os
        elf = open(os.path.join(os.path.dirname(os.path.abspath(a.manifest)), m['elf']), 'rb').read()
        group, pos = fw.library.place(m['menu'], m)
        hflags = list(m.get('hflags', (0, 0xff, 0)))
        hflags[0] = group
        e = Entry.build(int(str(m['id']), 0), m['name'], m['category'], m['params'], elf, hflags,
                        int(str(m.get('link_id', -1)), 0))
        fw.library.add(m['menu'], e, pos)
        print('menu %d, subgroup %d (%s), position %d' % (m['menu'], group, m['category'], pos))
        out = fw.pack()
        open(a.out, 'wb').write(out)
        print('wrote %s (%d bytes, library %#x bytes)' % (a.out, len(out), len(fw.library.pack())))
    elif a.cmd == 'swapelf':
        menu, pos = (int(x) for x in a.entry.split('.'))
        ei = fw.library.cat_entries[menu][pos]
        e = fw.library.entries[ei]
        elf = open(a.elf, 'rb').read()
        fw.library.entries[ei] = e.with_elf(elf)
        print('%d.%d %08x %s: module %d -> %d bytes' % (menu, pos, e.id, e.name, len(e.elf), len(elf)))
        out = fw.pack()
        open(a.out, 'wb').write(out)
        print('wrote %s (%d bytes, library %#x bytes)' % (a.out, len(out), len(fw.library.pack())))


if __name__ == '__main__':
    main()




