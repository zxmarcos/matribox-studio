"""Matribox Studio core: compose a QME-200 firmware from a base image + a module catalog.

The base image (by default the parallel-routing + two-row UI build) supplies the MCU (app), IR and Bank
sections untouched; only the DSP section (effect library) is rebuilt:
  * base entries can be kept or removed (menus whose list is not library-driven and the routing modules the
    UI patch draws are locked, and every menu keeps at least one entry: presets fall back to entry 0);
  * catalog modules (JSON manifests + .axf, as written by gpport.py / mk_*_json.py) can be added, with an
    optional new name / tab (category). Nothing about the catalog is built in: it is the list of library
    folders the caller passes (the GUI keeps the user's list), each optionally describing itself in a
    library.json ({"name", "color", "description"}).
Subgroups (UI tabs) are renumbered after removals and new modules are placed with fwtool.Library.place, so
the app's tab rules hold (tab label = category of the group's first entry, tab count from the menu tail).

  fwstudio_core.py <profile.json> <out.bin>          headless build from a profile saved by the GUI
"""
import json, os, struct, sys
from matribox import paths
from matribox.firmware import fwtool

ROOT = paths.SDK_ROOT                              # profiles keep paths relative to the repository
DEFAULT_BASE = paths.build_path('QME-200_FW_USB_V1.2.2_parallel_ui.bin')
# where the base chooser looks for QME-200 images
BASE_DIRS = [paths.FIRMWARE_DIR, paths.BUILD_DIR] + paths.setting('base_dirs', [])
DEFAULT_EDITOR_JSON = paths.default_editor_json()

MENUS = {0: 'Dinâmica / Boost', 1: 'Filtro / Pitch', 2: 'Wah', 3: 'Drive', 4: 'Amp', 5: 'Cab', 6: 'User IR',
         7: 'EQ / Roteamento', 8: 'Modulação', 9: 'Delay', 10: 'Reverb', 11: 'Clone', 12: 'FX Loop',
         13: 'FX Send', 14: 'FX Return', 15: 'Volume'}
MENU_SHORT = {0: 'DYN', 1: 'FX', 2: 'WAH', 3: 'DRV', 4: 'AMP', 5: 'CAB', 6: 'IR', 7: 'EQ', 8: 'MOD', 9: 'DLY',
              10: 'RVB', 11: 'CLN', 12: 'LOOP', 13: 'SND', 14: 'RTN', 15: 'VOL'}
FIXED_MENUS = {5, 6, 11, 12, 13, 14, 15}         # engine / single-slot menus: list not driven by the library
ROUTING_IDS = {0x01000070, 0x01000071, 0x01000072}  # Split / Path B / Merge (drawn by the UI patch)
FACTORY_MAX_TABS = 7                              # most subgroups a factory menu has (menu 8)

ORIGINAL_FW = paths.original_firmware()                                       # factory Clone module
NAM_CLONE_ELF = os.path.join(paths.EXAMPLES, 'nam_clone', 'gen', 'nam_clone.axf')      # build_clone.py
CLONE_DUAL_HOOK = (0x1b538, bytes.fromhex('144b10b5'))   # first hook of the app patch "clonedual" (original bytes)
CLONE_LABELS = {'factory': 'Fábrica', 'nam': 'NAM A2', 'dual': 'Fábrica + NAM A2 (automático por slot)',
                '?': 'outro'}

# bases made from the original firmware (make_base): app patches of apppatch.py + the routing modules
APP_PATCHES = [('parallel', 'GUI paralela', 'Split / Path B / Merge desenhados em duas fileiras (tela principal e '
                'de edição). Exige os módulos de roteamento.'),
               ('textlist', 'Listas de texto', 'Parâmetros com opções nomeadas (campo "options" dos manifestos) '
                'aparecem como texto em vez de número.'),
               ('clonedual', 'Clone com dois motores', 'Cada slot do Clone roda o motor da captura que guarda '
                '(fábrica ou NAM A2). O motor NAM é escolhido depois, em "Gerar firmware".'),
               ('clonetabs', 'Abas do Clone', 'O menu Clone ganha as abas Native e NAM A2 ao lado de ALL, cada uma '
                'com os slots daquele tipo. Exige o Clone com dois motores.')]
APP_PATCHES_REQUIRE = {'clonetabs': ['clonedual']}     # as in apppatch.FEATURES[..]['requires']
ROUTING_MANIFESTS = [os.path.join(paths.EXAMPLES, 'parallel', f) for f in ('split.json', 'pathb.json', 'merge.json')]

LIBRARY_META = 'library.json'      # optional in a library folder: {"name", "color": "#rrggbb", "description"}
FACTORY = 'Fábrica'                # origin of base entries that no library provides


def rel(path):
    try:
        r = os.path.relpath(os.path.abspath(path), ROOT)
    except ValueError:
        return os.path.abspath(path)
    return os.path.abspath(path) if r.startswith('..') else r.replace('\\', '/')


def absp(path):
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


class Item:
    """One effect: either an entry of the base library ('base') or a catalog manifest ('lib')."""
    def __init__(self, kind, menu, eid, name, category, params, origin):
        self.kind, self.menu, self.id = kind, menu, eid
        self.name, self.category, self.params, self.origin = name, category, params, origin
        self.orig_name, self.orig_category = name, category
        self.selected = kind == 'base'
        self.locked = ''                 # reason, when the user may not change the selection
        self.size = 0                    # bytes in the library
        self.elf_size = 0
        self.ram = {}
        self.source = ''
        self.presets = 0                 # factory presets (base Bank) using it
        self.preset_ids = []             # their indices in the Bank (qmepreset.preset_label numbering)
        self.problem = ''                # catalog item that cannot be added
        self.entry = None                # base: fwtool.Entry
        self.manifest = None             # lib: dict
        self.path = ''                   # lib: manifest path
        self.elf = b''

    @property
    def key(self):
        return ('base:%d:%08x' % (self.menu, self.id)) if self.kind == 'base' else 'lib:' + rel(self.path)

    @property
    def edited(self):
        return self.name != self.orig_name or self.category != self.orig_category


def manifests(d):
    """(path, manifest) for every module manifest directly inside library folder d"""
    d = absp(d)
    if not os.path.isdir(d):
        return
    for f in sorted(os.listdir(d)):
        if not f.endswith('.json') or f == LIBRARY_META:
            continue
        p = os.path.join(d, f)
        try:
            m = json.load(open(p, encoding='utf-8'))
        except Exception:
            continue
        if isinstance(m, dict) and all(k in m for k in ('elf', 'id', 'menu', 'name', 'params')):
            yield p, m


def library_meta(d):
    """what a library folder says about itself (library.json), with the folder name as the default name"""
    d = absp(d)
    try:
        meta = json.load(open(os.path.join(d, LIBRARY_META), encoding='utf-8'))
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    color = str(meta.get('color', ''))
    ok = len(color) == 7 and color[0] == '#' and all(c in '0123456789abcdefABCDEF' for c in color[1:])
    return dict(name=str(meta.get('name') or os.path.basename(os.path.normpath(d))).strip(),
                color=color.lower() if ok else '', description=str(meta.get('description', '')))


def scan_catalog(sources):
    """[(dir, label)] -> list of 'lib' Items (manifest problems are recorded, not raised); a manifest reachable
    from two libraries is listed once, under the first"""
    items, seen = [], set()
    for d, label in sources:
        d = absp(d)
        for p, m in manifests(d):
            k = os.path.normcase(os.path.abspath(p))
            if k in seen:
                continue
            seen.add(k)
            it = Item('lib', int(m['menu']), int(str(m['id']), 0), m['name'], m.get('category', label),
                      m['params'], label)
            it.manifest, it.path, it.selected = m, p, False
            it.source = m.get('source', '')
            it.ram = {k: int(str(v), 0) for k, v in m.get('ram', {}).items()}
            ep = os.path.join(d, m['elf'])
            try:
                it.elf = fwtool_strip(open(ep, 'rb').read())
                it.elf_size = len(it.elf)
                it.size = len(build_entry(it, 0).raw)
            except FileNotFoundError:
                it.problem = 'módulo %s não encontrado' % m['elf']
            except Exception as ex:
                it.problem = str(ex)
            items.append(it)
    return items


def fwtool_strip(elf):
    """symbol names out (as gpport build does); the loader only reads value/type/section"""
    from matribox.ports import gp2qme
    try:
        return gp2qme.strip_names(elf)
    except Exception:
        return elf


def build_entry(it, group):
    m = it.manifest
    return fwtool.Entry.build(it.id, it.name, it.category, m['params'], it.elf, (group, 0xff, 0),
                              int(str(m.get('link_id', -1)), 0))


def preset_usage(bank):
    """(menu, id) -> indices of the base presets using it (preset r at r*0x500 + (r/3)*0x100, 12 slots of 0x44
    bytes at +0x30: u8 on, i8 menu, i16 index, u32 id, 15 floats)"""
    use = {}
    r = 0
    while True:
        o = r * 0x500 + (r // 3) * 0x100
        if o + 0x500 > len(bank):
            break
        seen = set()
        for i in range(12):
            on, m, k, eid = struct.unpack_from('<BbhI', bank, o + 0x30 + 0x44 * i)
            if 0 <= m < 16 and (m, eid) not in seen:
                seen.add((m, eid))
                use.setdefault((m, eid), []).append(r)
        r += 1
    return use


def app_kind(mcu):
    """'original', 'patched' (UI patch linked after the original ITCM image) or 'own' (a smaller app of our own,
    e.g. the POCs and waveod)"""
    try:
        n0 = struct.unpack_from('<I', mcu, 0x14)[0]
        return 'patched' if n0 > 0x53cb9 + 0x40 else 'own' if n0 < 0x53cb9 else 'original'
    except struct.error:
        return '?'


def app_clonedual(mcu):
    """True when the app carries the patch "clonedual" (Clone engine chosen per slot)"""
    try:
        n0 = struct.unpack_from('<I', mcu, 0x14)[0]
    except struct.error:
        return False
    a, orig = CLONE_DUAL_HOOK
    return n0 > 0x53cb9 + 0x40 and mcu[0x28 + a:0x28 + a + len(orig)] not in (orig, b'')


def app_patches(mcu):
    """features of apppatch.py whose first hook is in the app (in APP_PATCHES order)"""
    from matribox.app import apppatch
    try:
        n0 = struct.unpack_from('<I', mcu, 0x14)[0]
    except struct.error:
        return []
    if n0 <= 0x53cb9 + 0x40:
        return []
    out = []
    for f, _, _ in APP_PATCHES:
        a, orig, _ = apppatch.FEATURES[f]['hooks'][0]
        if mcu[0x28 + a:0x28 + a + len(orig)] not in (orig, b''):
            out.append(f)
    return out


def find_bases(extra=()):
    """QME-200 images in BASE_DIRS plus `extra` (recently used files) -> absolute paths, without duplicates"""
    cands = []
    for d in BASE_DIRS:
        d = absp(d)
        if os.path.isdir(d):
            cands += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.lower().endswith('.bin')]
    out, seen = [], set()
    for p in cands + list(extra):
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen and is_base(p):
            seen.add(k)
            out.append(os.path.abspath(p))
    return out


def is_base(path):
    """True for a file with a QME-200 FRMW header"""
    try:
        with open(path, 'rb') as f:
            h = f.read(0x30)
    except OSError:
        return False
    return h[:4] == b'FRMW' and h[16:23] == b'QME-200'


def probe_base(path):
    """summary of a candidate base: app kind, effects, library size, parallel routing; 'error' if unusable"""
    st = os.stat(path)
    info = dict(path=path, size=st.st_size, mtime=st.st_mtime, app='?', effects=0, library=0, routing=False,
                patches=[], error='')
    try:
        fw = fwtool.Firmware(open(path, 'rb').read())
        lib = fw.library
        info.update(app=app_kind(fw.sections[0]), effects=len(lib.entries), library=len(fw.sections[1]),
                    routing=any(e.id in ROUTING_IDS for e in lib.entries), patches=app_patches(fw.sections[0]))
    except Exception as ex:
        info['error'] = str(ex) or type(ex).__name__
    return info


def base_name(src, features, routing):
    """default output name for make_base: the source's name + the suffix apppatch.py would use"""
    from matribox.app import apppatch
    stem = os.path.splitext(os.path.basename(src))[0]
    suffix = apppatch.default_name(features) if features else '_parallel' if routing else '_copia'
    return paths.build_path(stem + suffix + '.bin')


def make_base(src, out, features, routing=True):
    """a base image from the original firmware `src`: the routing modules (Split / Path B / Merge, always
    with 'parallel') added to the DSP section, then the app patched with `features` (apppatch.py, built with
    Zig). IR and Bank come from src unchanged."""
    features = [f for f, _, _ in APP_PATCHES if f in features]
    routing = routing or 'parallel' in features
    if not features and not routing:
        raise ValueError('nada a aplicar: escolha um patch ou os módulos de roteamento')
    data = open(src, 'rb').read()
    fw = fwtool.Firmware(data)
    kind = app_kind(fw.sections[0])
    if kind == 'own':
        raise ValueError('%s tem um app próprio, não o original' % os.path.basename(src))
    if kind == 'patched' and features and not os.path.exists(ORIGINAL_FW):
        raise ValueError('o app de %s já está patcheado e %s não foi encontrado' % (
            os.path.basename(src), os.path.basename(ORIGINAL_FW)))
    added, present = [], []
    if routing:
        lib = fw.library
        for p in ROUTING_MANIFESTS:                        # same as fwtool.py add, one manifest at a time
            m = json.load(open(p, encoding='utf-8'))
            if any(e.id == int(str(m['id']), 0) for e in lib.entries):
                present.append(m['name'])
                continue
            elf = open(os.path.join(os.path.dirname(p), m['elf']), 'rb').read()
            group, pos = lib.place(m['menu'], m)
            hflags = list(m.get('hflags', (0, 0xff, 0)))
            hflags[0] = group
            lib.add(m['menu'], fwtool.Entry.build(int(str(m['id']), 0), m['name'], m['category'], m['params'],
                                                  elf, hflags, int(str(m.get('link_id', -1)), 0)), pos)
            added.append(m['name'])
    img = fw.pack()
    patch = None
    if features:
        from matribox.app import apppatch
        img, patch = apppatch.patch(img, features)
    check = fwtool.Firmware(img)                           # parse back
    ok = (img[:4] == b'FRMW' and img[16:23] == b'QME-200' and sum(img[0x30:]) & 0xff == img[0x0e]
          and all(check.sections[i] == fw.sections[i] for i in (2, 3))
          and (features or check.sections[0] == fw.sections[0]))
    if not ok:
        raise ValueError('falha na verificação da imagem gerada')
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    open(out, 'wb').write(img)
    return dict(path=out, size=len(img), features=features, added=added, present=present, patch=patch,
                app=app_kind(check.sections[0]), effects=len(check.library.entries),
                library=len(check.sections[1]))


class Project:
    def __init__(self, base=DEFAULT_BASE, sources=()):
        """sources: [(library folder, label)], the label being the origin shown for its effects"""
        self.sources = [(d, l) for d, l in sources]
        self.lib_colors = {}                           # label -> colour declared in the folder's library.json
        for d, l in self.sources:
            c = library_meta(d)['color']
            if c:
                self.lib_colors.setdefault(l, c)
        self.load_base(base)

    # ---------------------------------------------------------------- loading
    def load_base(self, base):
        self.base_path = base
        self.data = open(base, 'rb').read()
        self.fw = fwtool.Firmware(self.data)
        self.app = app_kind(self.fw.sections[0])
        lib = self.fw.library
        self.ncat = len(lib.cat_entries)
        self.clone_base = fwtool.clone_mode(lib) if self.ncat > fwtool.CLONE_MENU else '?'
        self.app_dual = app_clonedual(self.fw.sections[0])
        self.patches = app_patches(self.fw.sections[0])
        self.clone_mode = None                         # None = the base's Clone engines
        self._clone_sizes = {}
        use = preset_usage(self.fw.sections[3])
        self.catalog = scan_catalog(self.sources)
        cat_origin = {it.id: it.origin for it in self.catalog}
        self.base_items = []
        for m, lst in enumerate(lib.cat_entries):
            for ei in lst:
                e = lib.entries[ei]
                it = Item('base', m, e.id, e.name, e.category, e.params, cat_origin.get(e.id, FACTORY))
                it.entry, it.size, it.elf_size = e, len(e.raw), len(e.elf)
                it.group = e.hflags[0]
                it.preset_ids = use.get((m, e.id), [])
                it.presets = len(it.preset_ids)
                if m == fwtool.CLONE_MENU:
                    it.locked = 'motor do Clone: escolha em "Gerar firmware"'
                elif m in FIXED_MENUS:
                    it.locked = 'menu fixo (a lista vem de outros dados)'
                elif e.id in ROUTING_IDS and 'parallel' in self.patches:
                    it.locked = 'usado pelo patch da GUI paralela'
                self.base_items.append(it)
        base_ids = {it.id for it in self.base_items}
        for it in self.catalog:
            if it.id in base_ids:
                it.problem = it.problem or 'já está no firmware base'
            elif it.menu in FIXED_MENUS or not 0 <= it.menu < self.ncat:
                it.problem = it.problem or 'menu %d não aceita efeitos da biblioteca' % it.menu
        self.items = self.base_items + [it for it in self.catalog if not it.problem or it.id not in base_ids]

    def reset(self):
        self.clone_mode = None
        for it in self.items:
            it.selected = it.kind == 'base'
            it.name, it.category = it.orig_name, it.orig_category

    # ---------------------------------------------------------------- state
    def selected(self, menu=None):
        return [it for it in self.items if it.selected and (menu is None or it.menu == menu)]

    def can_toggle(self, it):
        """'' if allowed, else the reason"""
        if it.locked:
            return it.locked
        if it.problem:
            return it.problem
        if it.selected and it.kind == 'base' and len(self.selected(it.menu)) <= 1:
            return 'cada menu precisa de ao menos um efeito (presets caem no 1º)'
        if not it.selected:
            other = next((o for o in self.selected() if o.id == it.id and o is not it), None)
            if other:
                return 'id %#010x já usado por %s' % (it.id, other.name)
        return ''

    def lib_size(self):
        """(total, index, base part, added part) in bytes"""
        counts = [0] * self.ncat
        base = added = 0
        for it in self.selected():
            counts[it.menu] += 1
            if it.kind == 'base':
                base += it.size
            else:
                added += it.size
        n, size = self._clone_size(self.clone_target())
        n0, size0 = self._clone_size(self.clone_base)
        if self.ncat > fwtool.CLONE_MENU:
            counts[fwtool.CLONE_MENU] += n - n0
        base += size - size0
        idx = 0x18 + 4 * self.ncat + sum(4 + 4 * n for n in counts)
        return idx + base + added, idx, base, added

    # ---------------------------------------------------------------- Clone engines
    def clone_target(self):
        return self.clone_mode or self.clone_base

    def clone_elfs(self):
        """{'factory', 'nam'} -> module ELF: the base's own, else the original firmware's / build_clone.py's"""
        elfs = fwtool.clone_elfs(self.fw.library) if self.clone_base != '?' else {}
        if 'factory' not in elfs and os.path.exists(ORIGINAL_FW):
            elfs['factory'] = fwtool.clone_elfs(fwtool.Firmware(open(ORIGINAL_FW, 'rb').read()).library)['factory']
        if 'nam' not in elfs and os.path.exists(NAM_CLONE_ELF):
            elfs['nam'] = open(NAM_CLONE_ELF, 'rb').read()
        return elfs

    def clone_modes(self):
        """[(mode, '' if available else why not)]"""
        elfs = self.clone_elfs()
        out = []
        for m in fwtool.CLONE_MODES:
            need = {'factory': ['factory'], 'nam': ['nam'], 'dual': ['factory', 'nam']}[m]
            why = ''
            if any(k not in elfs for k in need):
                why = 'módulo %s não encontrado' % ('NAM (rode examples/nam_clone/build_clone.py)'
                                                    if 'nam' not in elfs and 'nam' in need else 'de fábrica')
            elif m == 'dual' and not self.app_dual:
                why = 'o app da base não tem o patch "clonedual"'
            out.append((m, why))
        return out

    def _clone_size(self, mode):
        """(entries, bytes) of the Clone menu in mode"""
        if mode not in self._clone_sizes:
            lib = self.fw.library
            if mode == self.clone_base or self.ncat <= fwtool.CLONE_MENU:
                lst = lib.cat_entries[fwtool.CLONE_MENU] if self.ncat > fwtool.CLONE_MENU else []
                self._clone_sizes[mode] = (len(lst), sum(len(lib.entries[ei].raw) for ei in lst))
            else:
                elfs = self.clone_elfs()
                first = lib.entries[lib.cat_entries[fwtool.CLONE_MENU][0]]
                n = first.hdr_size + len(elfs.get('nam' if mode == 'nam' else 'factory', b''))
                if mode == 'dual':
                    n += len(fwtool.Entry.build(fwtool.CLONE_NAM_ID, fwtool.CLONE_NAM_NAME, first.category,
                                                first.params, elfs.get('nam', b''), first.hflags).raw)
                self._clone_sizes[mode] = (2 if mode == 'dual' else 1, n)
        return self._clone_sizes[mode]

    def tabs(self, menu):
        """tab labels this menu would get, in order"""
        labels = []
        for it in self.ordered(menu):
            if it.category not in labels:
                labels.append(it.category)
        return labels

    def ordered(self, menu):
        """selected items of a menu in final library order (base order, then additions after their tab)"""
        out = [it for it in self.base_items if it.menu == menu and it.selected]
        for it in self.catalog:
            if it.menu == menu and it.selected:
                k = max((i for i, o in enumerate(out) if o.category == it.category), default=None)
                if k is None:
                    out.append(it)
                else:
                    out.insert(k + 1, it)
        return out

    def issues(self):
        """[(level, text)], level 'erro' blocks the build"""
        out = []
        total, idx, _, _ = self.lib_size()
        if total > fwtool.MAX_LIBRARY:
            out.append(('erro', 'biblioteca com %s, a partição tem %s' % (fmt_size(total), fmt_size(fwtool.MAX_LIBRARY))))
        if idx > fwtool.MAX_INDEX:
            out.append(('erro', 'índice com %#x bytes, o app copia no máximo %#x' % (idx, fwtool.MAX_INDEX)))
        ids = {}
        for it in self.selected():
            if it.id in ids:
                out.append(('erro', 'id %#010x repetido: %s e %s' % (it.id, ids[it.id].name, it.name)))
            ids[it.id] = it
            if len(it.name) > fwtool.MAX_NAME or not it.name.strip():
                out.append(('erro', '%s: nome precisa ter 1 a %d caracteres' % (it.name, fwtool.MAX_NAME)))
            if len(it.category) > fwtool.MAX_NAME or not it.category.strip():
                out.append(('erro', '%s: aba precisa ter 1 a %d caracteres' % (it.name, fwtool.MAX_NAME)))
            try:
                it.name.encode('latin1'); it.category.encode('latin1')
            except UnicodeEncodeError:
                out.append(('erro', '%s: nome/aba só aceitam caracteres latin-1' % it.name))
        for m in range(self.ncat):
            if not self.selected(m):
                out.append(('erro', 'menu %s ficou vazio' % MENUS.get(m, m)))
            n = len(self.tabs(m))
            if n > FACTORY_MAX_TABS and m not in FIXED_MENUS:
                out.append(('aviso', 'menu %s com %d abas (fábrica usa no máximo %d; não testado no pedal)' % (
                    MENUS.get(m, m), n, FACTORY_MAX_TABS)))
        mode = self.clone_target()
        if mode == 'dual' and not self.app_dual and self.clone_mode in (None, self.clone_base):
            out.append(('erro', 'Clone com dois motores (fábrica + NAM) precisa do patch "clonedual" no app da base'))
        if 'clonetabs' in self.patches and mode != 'dual':
            out.append(('aviso', 'o app tem as abas do Clone, mas o motor não é "fábrica + NAM": a aba de um motor '
                        'que falta lista slots que não tocam'))
        if self.clone_mode and self.clone_mode != self.clone_base:
            why = dict(self.clone_modes()).get(self.clone_mode, 'modo desconhecido')
            if why:
                out.append(('erro', 'motor do Clone "%s": %s' % (CLONE_LABELS.get(self.clone_mode, self.clone_mode), why)))
        lost = sum(it.presets for it in self.base_items if not it.selected)
        if lost:
            out.append(('aviso', '%d usos em presets do Bank apontam para efeitos removidos (viram o 1º efeito do menu)' % lost))
        return out

    # ---------------------------------------------------------------- build
    def compose(self):
        """-> rebuilt fwtool.Firmware (DSP section replaced); raises ValueError on rule violations"""
        errs = [t for lvl, t in self.issues() if lvl == 'erro']
        if errs:
            raise ValueError('\n'.join(errs))
        fw = fwtool.Firmware(self.data)
        lib = fw.library
        keep = {(it.menu, it.id) for it in self.base_items if it.selected}
        used = set()
        cat_entries = []
        for m, lst in enumerate(lib.cat_entries):
            kept = [ei for ei in lst if (m, lib.entries[ei].id) in keep]
            # renumber subgroups densely, in the order the menu had them
            old = sorted({lib.entries[ei].hflags[0] for ei in kept})
            remap = {g: k for k, g in enumerate(old)}
            for ei in kept:
                e = lib.entries[ei]
                if remap[e.hflags[0]] != e.hflags[0]:
                    raw = bytearray(e.raw)
                    raw[17] = remap[e.hflags[0]]
                    lib.entries[ei] = fwtool.Entry(bytes(raw))
            gl = [lib.entries[ei].hflags[0] for ei in kept]
            if gl and max(gl[-2:]) != max(gl):  # tab count comes from the last two entries: keep the top group last
                top = max(gl)
                kept = [ei for ei in kept if lib.entries[ei].hflags[0] != top] + \
                       [ei for ei in kept if lib.entries[ei].hflags[0] == top]
            cat_entries.append(kept)
            used.update(kept)
        # drop the removed entries physically, keeping the base's physical order
        order = [ei for ei in range(len(lib.entries)) if ei in used]
        new_index = {ei: k for k, ei in enumerate(order)}
        lib.entries = [lib.entries[ei] for ei in order]
        lib.cat_entries = [[new_index[ei] for ei in l] for l in cat_entries]
        report = []
        for it in self.catalog:
            if not it.selected:
                continue
            m = dict(it.manifest, name=it.name, category=it.category, id='%#010x' % it.id)
            m.pop('group', None); m.pop('position', None)
            group, pos = lib.place(it.menu, m)
            lib.add(it.menu, build_entry(it, group), pos)
            report.append((it, group, pos))
        if self.clone_mode and self.clone_mode != self.clone_base:
            elfs = self.clone_elfs()
            fwtool.set_clone(lib, self.clone_mode, elfs.get('factory'), elfs.get('nam'))
        fw.library = lib
        return fw, report

    def build(self, out_path, editor_json=None, editor_out=None):
        fw, report = self.compose()
        img = fw.pack()
        check = fwtool.Firmware(img)                         # parse back
        ok = (img[:4] == b'FRMW' and img[16:23] == b'QME-200' and sum(img[0x30:]) & 0xff == img[0x0e]
              and all(check.sections[i] == self.fw.sections[i] for i in (0, 2, 3)))
        if not ok:
            raise ValueError('falha na verificação da imagem gerada')
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        open(out_path, 'wb').write(img)
        res = dict(path=out_path, size=len(img), library=len(check.sections[1]), effects=len(check.library.entries),
                   added=len(report), removed=sum(1 for it in self.base_items if not it.selected), editor=None)
        if editor_json and editor_out:
            res['editor'] = write_editor_json(check, editor_json, editor_out, self.catalog)
        return res

    # ---------------------------------------------------------------- profiles
    def to_profile(self):
        return dict(version=1, base=rel(self.base_path),
                    sources=[[rel(absp(d)), l] for d, l in self.sources],
                    remove=['%d:%#010x' % (it.menu, it.id) for it in self.base_items if not it.selected and not it.locked],
                    add=[dict(manifest=rel(it.path), **({'name': it.name} if it.name != it.orig_name else {}),
                              **({'category': it.category} if it.category != it.orig_category else {}))
                         for it in self.catalog if it.selected],
                    **({'clone': self.clone_mode} if self.clone_mode else {}))

    def apply_profile(self, prof):
        """returns the list of entries that could not be applied"""
        missing = []
        c = prof.get('clone')
        self.clone_mode = c if c in fwtool.CLONE_MODES else None
        rem = set(prof.get('remove', []))
        for it in self.base_items:
            it.selected = it.locked != '' or '%d:%#010x' % (it.menu, it.id) not in rem
        by_path = {rel(it.path): it for it in self.catalog}
        for it in self.catalog:
            it.selected = False
            it.name, it.category = it.orig_name, it.orig_category
        for a in prof.get('add', []):
            it = by_path.get(rel(absp(a['manifest'])))
            if it is None or it.problem:
                missing.append(a['manifest'])
                continue
            it.selected = True
            it.name = a.get('name', it.name)
            it.category = a.get('category', it.category)
        return missing


def write_editor_json(fw, src, dst, catalog=()):
    """alg_data.json for the PC editor matching fw: existing entries are reused by fxid, new ones generated,
    entries of removed effects dropped (menus 5/6/11 untouched)"""
    from matribox.editor import editor_json
    raw = open(src, 'rb').read()
    data = json.loads(raw.decode('utf-8-sig'))
    lists = editor_json.list_codes()
    origins = {it.id: it.origin for it in catalog}
    renamed = {it.id for it in catalog if it.selected and it.edited}
    lib = fw.library
    added = dropped = 0
    for mod in data['Modules']:
        menu = mod['index']
        if menu in editor_json.SPECIAL or menu >= len(lib.cat_entries):
            continue
        have = {a['fxid']: a for a in mod['alg']}
        new = []
        for k, ei in enumerate(lib.cat_entries[menu]):
            e = lib.entries[ei]
            a = have.pop(e.id, None)
            if a is None:
                a = editor_json.entry(e, k, lists, origins.get(e.id, 'Matribox mod'))
                added += 1
            elif e.id in renamed:                       # name / tab changed in the studio
                a.update(fxtitle=e.name, name=e.name, type=e.category)
            a['index'] = k
            new.append(a)
        dropped += len(have)
        mod['alg'] = new
    out = json.dumps(data, ensure_ascii=False, indent=4).replace('\n', '\r\n')
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    open(dst, 'wb').write((b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'') + out.encode('utf8'))
    return dict(path=dst, added=added, dropped=dropped)


def fmt_size(n):
    if n >= 1 << 20:
        return ('%.2f MB' % (n / (1 << 20))).replace('.', ',')
    if n >= 1024:
        return ('%.1f KB' % (n / 1024)).replace('.', ',')
    return '%d B' % n


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    prof = json.load(open(sys.argv[1], encoding='utf-8'))
    p = Project(absp(prof['base']), [(d, l) for d, l in prof.get('sources', [])])
    miss = p.apply_profile(prof)
    for m in miss:
        print('ignorado:', m)
    for lvl, t in p.issues():
        print('%s: %s' % (lvl, t))
    r = p.build(sys.argv[2])
    print('%s: %d bytes, biblioteca %s, %d efeitos (+%d, -%d)' % (
        r['path'], r['size'], fmt_size(r['library']), r['effects'], r['added'], r['removed']))


if __name__ == '__main__':
    main()
