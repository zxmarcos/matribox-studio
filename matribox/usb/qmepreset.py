"""Presets of the Matribox II Pro (QME-200): read them from the pedal (editor SysEx protocol, matribox/usb/htmidi.py)
or from the Bank section of a firmware .bin, and resolve the effects of the chain to names and parameters.

Pedal side (app V1.2.2, dispatcher FUN_0003a084, group 0 = preset):
  0x10000000 flag 0x11 {u32 i}       FUN_0002e8c8: the preset being edited (live, unsaved changes included);
                                      i is only echoed back
  0x10000002 flag 0x11 {u32 i}       FUN_0002e110: stored preset i (export), the active preset is not touched
  0x10000001 flag 0x11 {u32 sub}     table 0x42cc0[(sub >> 16) & 0xf]:
      0x010a0000                      FUN_0002f1a8: list {sub, i32 pos[240], name[240][17], style[240][10]}
      0x010a0001                      current preset index {sub, u32 i} (sent through FUN_0003568c, which holds
                                      back a reply identical to the previous one: ask something else in between)
  A preset reply is {u32 i} + blocks {u32 cmd, u32 size, u32 kind << 16, size - 4 bytes}:
      0x0100 defs {fs 4, fs combos 3, slots 12, exp modes 1, ctrls 5, scenes 1, params/slot 15}
      0x0101 info {u32 version, name[17], author[20], info[50], style[10]}
      0x0102 quick knobs, 0x0103 footswitches, 0x0107 EXP, 0x0108 CTRL, 0x0109 version {f32, u32}
      0x0104 chain {i8 order[12] (slot ids in display order, -1 = end), i8 menu[12], u32 id[12]}
      0x0105 values {f32 knobs[12][15], u8 on[12], u16 volume, u16 bpm, i8 pan, u8 fx loop mode}
The pedal also pushes changes made on it: 0x10000001 with sub 0x010a0001 (preset changed), 0x01040000
(chain), 0x01050001 (knob {slot, knob position, f32}), 0x01050002 (on/off {slot, state}), 0x01050007 (all on/off),
and 0x11000001 {0x0800, modified} when the edit buffer becomes (un)modified.

Writes (flag 0x12, edit buffer only: nothing is stored in flash until the preset is saved):
  {0x010a0001, u32 i}                 FUN_00021660: load preset i (discards unsaved changes of the current one)
  {0x01050002, u32 slot, u32 on}      FUN_0002f27c: block on/off, re-inits the chain, marks the preset modified
  {0x01050001, u32 slot, u32 k, f32}  FUN_000213cc(v, slot, k, 1): k is the parameter's ORDER in the library
                                      entry (FUN_0002048c maps it to the knob position; the push reports the
                                      knob position); quantised to the step, clamped, Sync switches handled
  0x11000001 flag 0x11 {0x0800}       FUN_0002ddc8: {0x0800, modified}
  {0x01040001, i8 old, i8 new, i8 menu, u32 id} (11 bytes, unaligned; table 0x42c7c[1] = 0x2f948), old / new
                                      are chain positions (index in the display order):
      old -1, new p                   FUN_000225b0: insert the effect at p (first free slot, on, default knobs);
                                      an id the firmware does not know still takes a place in the order, with
                                      an empty slot (Preset.phantoms(): remove it at once)
      old p, new -1                   FUN_00022430: remove the block at p (its slot is freed)
      old p, new q (p != q)           FUN_00022714: move; q is the final position (menu / id ignored, send -1)
      old p, new p                    FUN_00022828: replace the effect of the block at p (default knobs)
    id is the u32 the chain stores (FUN_0001eb00 resolves it: library for most menus, the IR table for Cab,
    User IR - 0x3d, Clone - 0x51). Insert / replace check the memory first (FUN_0001b4f4 / FUN_0001b598) and
    refuse with 0x11000001 {0x0700, 2} ("System overload"). All of them re-init the chain and push 0x01040000.
  {0x01040002, u8 slot, i8 menu, u32 id}  FUN_00022230: set the algorithm of slot (slot id, not position)

Clone slots (group 8; the firmware has no command that reads a slot's data back, only the names):
  0x18000000 flag 0x11                {u32 0[10], u32 0[10], name[10][17]}
  0x18000001 flag 0x12 {u32 slot, u32 0, u32 byte sum, name[20], data}: FUN_00025d30 stores 0x2000 bytes in
                                      flash, re-inits the chain and answers 0x18000003 {slot, 1 = written, 0}
User IR slots (group 4, same layout; ImportIRTask.dart of the editor):
  0x14000000 flag 0x11                {u32 0[20], u32 0[20], name[20][17]}
  0x14000001 flag 0x12 {u32 slot, u32 0, u32 checksum, name[20], int32 ir[1024]}: FUN_00025d30 stores 0x1000
                                      bytes, re-inits the chain and answers 0x14000003 {slot, 1 = written, 0}
  0x14000002 flag 0x12 {u32 slot, name[16]}: FUN_000252d8 renames the slot, no answer

Global settings (group 1) and global EQ (group 6; HTGlobalModel.dart of the editor):
  0x11000000 flag 0x11 -> FUN_0002d120: 52 bytes, parse_globals()
  0x11000001 flag 0x12 {u32 type, i32 value}: table 0x42da0[(type >> 12) & 0xf] sets one setting (type = block
                       0xB000 | (field + 1) << 8 | array index, GLOBAL_TYPES) and pushes the same message back
  0x16000000 flag 0x11 -> FUN_0002cf90: f32 eq[31] (EQ_PARAMS)
  0x16000001 {u32 0x16000001[, u32 on]}: flag 0x11 reads {0, on}, 0x12 sets the EQ on / off
  0x16000002 {u32 index[, f32 value]}: flag 0x11 reads {index, value}, 0x12 sets it (clamped to EQ_PARAMS)
  The replies to 0x16000001 / 0x16000002 go through FUN_0003568c, which drops a reply identical to the last one.

Stored preset (Bank section, 0x48c bytes, 3 per 4 KB sector: r * 0x500 + (r / 3) * 0x100):
  +0 u16 0x5aa5, +8 u16 index, +0x0a name[17], +0x1e u8 volume, +0x1f i8 pan, +0x20 u16 bpm,
  +0x22 i8 order[12], +0x30 12 x {u8 on, i8 menu, i16 index, u32 id, f32 knobs[15]}, +0x481 style[10]

  qmepreset.py list  [--port P]            preset names (pedal)
  qmepreset.py show  [N] [--port P]        chain of preset N (1-based), default: the one being edited
  qmepreset.py bank  fw.bin [N]            factory presets of a firmware image
"""
import json, os, struct, sys, time

from matribox.usb import htmidi

from matribox import paths
ROOT = paths.SDK_ROOT

CMD_PRESET, CMD_SIGNAL, CMD_EXPORT = 0x10000000, 0x10000001, 0x10000002
CMD_IR_LIST, CMD_CLONE_LIST, CMD_CLONE_DATA, CMD_CLONE_ACK = 0x14000000, 0x18000000, 0x18000001, 0x18000003
CMD_IR_DATA, CMD_IR_RENAME, CMD_IR_ACK = 0x14000001, 0x14000002, 0x14000003
CLONE_SLOTS, CLONE_NAME_MAX, CLONE_DATA_MAX = 10, 16, 0x2000 - 8 - 40
IR_SLOTS, IR_NAME_MAX, IR_DATA_SIZE = 20, 16, 0x1000
CMD_GLOBAL_SETTING = 0x16000002                 # = the global EQ's single parameter (CMD_EQ_PARAM)
CMD_GLOBAL_SINGLE, SUB_MODIFIED, SUB_OVERLOAD = 0x11000001, 0x0800, 0x0700
CMD_GLOBAL_ALL, CMD_EQ_ALL, CMD_EQ_SWITCH, CMD_EQ_PARAM = 0x11000000, 0x16000000, 0x16000001, 0x16000002
SUB_LIST, SUB_INDEX = 0x010a0000, 0x010a0001
SUB_CHAIN, SUB_KNOB, SUB_SWITCH, SUB_ALL_ON = 0x01040000, 0x01050001, 0x01050002, 0x01050007
SUB_CHAIN_EDIT = 0x01040001
LIVE_SUBS = {SUB_INDEX, SUB_CHAIN, SUB_KNOB, SUB_SWITCH, SUB_ALL_ON}

NPRESETS, NSLOTS, NKNOBS = 240, 12, 15
STORED_SIZE = 0x48c

MENU_SHORT = {0: 'DYN', 1: 'FX', 2: 'WAH', 3: 'DRV', 4: 'AMP', 5: 'CAB', 6: 'IR', 7: 'EQ', 8: 'MOD', 9: 'DLY',
              10: 'RVB', 11: 'CLN', 12: 'LOOP', 13: 'SND', 14: 'RTN', 15: 'VOL'}
SPLIT, PATH_B, MERGE = 0x01000070, 0x01000071, 0x01000072
USER_IR_BASE, CLONE_BASE = 0x0A100000, 0x0F000000

SYNC_DIVS = ['1/1', '1/2', '1/2D', '1/2T', '1/4', '1/4D', '1/4T', '1/8', '1/8D', '1/8T', '1/16']


def preset_label(i):
    """the editor's numbering: 4 presets per bank, 01-A .. 60-D"""
    return '%02d-%s' % (i // 4 + 1, 'ABCD'[i % 4])


def cstr(b):
    return bytes(b).split(b'\0')[0].decode('latin1').strip('\xff')


# ------------------------------------------------------------------------------------------------ model
class Slot:
    def __init__(self, sid, on, menu, eid, knobs):
        self.sid, self.on, self.menu, self.id, self.knobs = sid, bool(on), menu, eid, list(knobs)

    @property
    def empty(self):
        return self.menu < 0 or self.menu > 15

    def __repr__(self):
        return 'Slot(%d, %s, menu %d, %#010x)' % (self.sid, 'on' if self.on else 'off', self.menu, self.id)


class Preset:
    def __init__(self):
        self.index = -1
        self.name = self.author = self.info = self.style = ''
        self.slots = [Slot(i, 0, -1, 0, [0.0] * NKNOBS) for i in range(NSLOTS)]
        self.order = []                 # slot ids in display order
        self.volume = self.bpm = self.pan = self.fxloop = 0
        self.live = False               # True: edit buffer of the pedal (may hold unsaved changes)
        self.source = ''

    def chain(self):
        """slots in display order (empty ones skipped)"""
        out = []
        for s in self.order:
            if 0 <= s < NSLOTS and not self.slots[s].empty and self.slots[s] not in out:
                out.append(self.slots[s])
        return out

    def edit(self, op, *args, knobs=None):
        """apply a chain edit the way the firmware does (the GUI shows it before the pedal confirms):
        ('insert', pos, menu, id), ('remove', pos), ('move', old, new), ('replace', pos, menu, id).
        knobs(menu, id) -> {knob position: default} (EffectDB.default_knobs); the other positions keep their
        values, as on the pedal. Returns the slot id it touched, None if the pedal would refuse it."""
        o = self.order

        def load(s, menu, eid):
            s.menu, s.id = menu, eid
            for i, v in (knobs(menu, eid) if knobs else {}).items():
                s.knobs[i] = v
        if op == 'insert':
            pos, menu, eid = args
            free = [s.sid for s in self.slots if s.empty]
            if len(o) >= NSLOTS or not free or not 0 <= pos <= len(o):
                return None
            s = self.slots[free[0]]
            o.insert(pos, s.sid)
            s.on = True
            load(s, menu, eid)
            return s.sid
        if op == 'remove':
            pos, = args
            if not 0 <= pos < len(o):
                return None
            s = self.slots[o.pop(pos)]
            s.menu, s.id, s.on = -1, 0, False
            return s.sid
        if op == 'move':
            old, new = args
            if not (0 <= old < len(o) and 0 <= new < len(o)):
                return None
            sid = o.pop(old)
            o.insert(new, sid)
            return sid
        if op == 'replace':
            pos, menu, eid = args
            if not 0 <= pos < len(o):
                return None
            s = self.slots[o[pos]]
            load(s, menu, eid)
            return s.sid
        raise ValueError(op)

    def pos(self, sid):
        """position of slot sid in the pedal's order array: what the chain edits (0x01040001) take"""
        return self.order.index(sid)

    def phantoms(self):
        """positions of the order array that hold no effect (an empty slot, or one already listed). An insert
        whose id the firmware does not know leaves one: FUN_000225b0 puts the slot in the order and counts it,
        then FUN_00022230 refuses the id and the slot stays empty (and is handed out again by the next insert)."""
        seen, out = set(), []
        for k, s in enumerate(self.order):
            if self.slots[s].empty or s in seen:
                out.append(k)
            seen.add(s)
        return out

    def routed(self):
        """(pre, path_a, path_b, post, split, pathb, merge) when the chain has a complete Split / Path B /
        Merge section (examples/parallel), else None"""
        ch = self.chain()
        ids = [s.id for s in ch]
        if SPLIT not in ids or MERGE not in ids:
            return None
        i_s = ids.index(SPLIT)
        i_m = ids.index(MERGE, i_s) if MERGE in ids[i_s:] else -1
        if i_m < 0:
            return None
        i_b = ids.index(PATH_B, i_s) if PATH_B in ids[i_s:i_m] else i_m
        pb = ch[i_b] if i_b < i_m else None
        return (ch[:i_s], ch[i_s + 1:i_b], ch[i_b + 1:i_m] if pb else [], ch[i_m + 1:], ch[i_s], pb, ch[i_m])


def parse_blocks(data, start=0):
    """preset reply payload: u32 index + blocks -> Preset"""
    p = Preset()
    p.index = struct.unpack_from('<i', data, start)[0]
    o = start + 4
    nk = NKNOBS
    while o + 12 <= len(data):
        _cmd, size, kind = struct.unpack_from('<III', data, o)
        body = data[o + 12:o + 8 + size]
        o += 8 + size
        kind = kind >> 16 & 0xff
        if kind == 0 and len(body) >= 7:
            nk = body[6] or NKNOBS
        elif kind == 1 and len(body) >= 101:
            p.name, p.author = cstr(body[4:21]), cstr(body[21:41])
            p.info, p.style = cstr(body[41:91]), cstr(body[91:101])
        elif kind == 4 and len(body) >= 72:
            p.order = [b if b < 0x80 else b - 0x100 for b in body[0:12]]
            menus = [b if b < 0x80 else b - 0x100 for b in body[12:24]]
            ids = struct.unpack_from('<12I', body, 24)
            for i in range(NSLOTS):
                p.slots[i].menu, p.slots[i].id = menus[i], ids[i]
        elif kind == 5 and len(body) >= NSLOTS * nk * 4 + NSLOTS:
            vals = struct.unpack_from('<%df' % (NSLOTS * nk), body, 0)
            q = NSLOTS * nk * 4
            for i in range(NSLOTS):
                p.slots[i].knobs = list(vals[i * nk:(i + 1) * nk])
                p.slots[i].on = bool(body[q + i])
            q += NSLOTS
            if len(body) >= q + 6:
                p.volume, p.bpm = struct.unpack_from('<HH', body, q)
                p.pan = struct.unpack_from('<b', body, q + 4)[0]
                p.fxloop = body[q + 5]
    p.order = [s for s in p.order if 0 <= s < NSLOTS]
    return p


def parse_stored(raw, index=-1):
    """one preset of the Bank section (or FUN_00021018's copy) -> Preset, None if the record is not valid"""
    if len(raw) < STORED_SIZE or raw[0:2] != b'\xa5\x5a':
        return None
    p = Preset()
    p.index = index
    p.name = cstr(raw[0x0a:0x1b])
    p.volume, p.pan = raw[0x1e], struct.unpack_from('<b', raw, 0x1f)[0]
    p.bpm = struct.unpack_from('<H', raw, 0x20)[0]
    p.order = [b for b in raw[0x22:0x2e] if b < NSLOTS]
    for i in range(NSLOTS):
        on, m, _k, eid = struct.unpack_from('<BbhI', raw, 0x30 + 0x44 * i)
        p.slots[i] = Slot(i, on, m, eid, struct.unpack_from('<15f', raw, 0x38 + 0x44 * i))
    p.style = cstr(raw[0x481:0x48b])
    return p


def bank_presets(bank):
    """Bank section of a firmware image -> list of Presets (factory presets; user presets live elsewhere)"""
    out = []
    r = 0
    while True:
        o = r * 0x500 + (r // 3) * 0x100
        if o + STORED_SIZE > len(bank):
            break
        p = parse_stored(bank[o:o + STORED_SIZE], r)
        if p is None:
            break
        p.source = 'bank'
        out.append(p)
        r += 1
    return out


def parse_list(data):
    """0x010a0000 reply -> [(position, stored index, name, style)]"""
    o = 4
    pos = struct.unpack_from('<%di' % NPRESETS, data, o)
    o += 4 * NPRESETS
    names = [cstr(data[o + 17 * i:o + 17 * (i + 1)]) for i in range(NPRESETS)]
    o += 17 * NPRESETS
    styles = [cstr(data[o + 10 * i:o + 10 * (i + 1)]) if o + 10 * (i + 1) <= len(data) else ''
              for i in range(NPRESETS)]
    return [(i, pos[i], names[i], styles[i]) for i in range(NPRESETS)]


def parse_names(data, n, off):
    """IR / Clone list: {n u32, n u32, n names x 17}"""
    return [cstr(data[off + 17 * i:off + 17 * (i + 1)]) for i in range(n)]


# ------------------------------------------------------------------------------------------------ global settings
# 0x11000001 types (HTBaseDefine.dart; checked against the app's handlers in emulation) and the byte of the
# 0x11000000 reply that holds each one (FUN_0002d120)
GLOBAL_TYPES = {
    'auto_cab': (0x0500, 5, 'B'), 'input_level': (0x2100, 14, 'b'), 'input_mode': (0x2200, 15, 'B'),
    'nocab_l': (0x2300, 16, 'B'), 'nocab_r': (0x2301, 17, 'B'), 'rec_level': (0x3100, 18, 'b'),
    'monitor_level': (0x3200, 19, 'b'), 'rec_aux': (0x3300, 20, 'B'), 'rec_l': (0x3400, 21, 'B'),
    'rec_r': (0x3401, 22, 'B'), 'exp_mode': (0x4101, 24, 'B'), 'fs1': (0x4200, 25, 'B'), 'fs2': (0x4202, 27, 'B'),
    'bank_mode': (0x5101, 33, 'B'),     # FUN_0002d120 sends app+0x2d twice (bytes 33 and 34); 0x5100/1 both set it
    'work_mode': (0x5200, 35, 'B'), 'midi_in_trs': (0x6100, 36, 'h'), 'midi_in_usb': (0x6101, 38, 'h'),
    'midi_out_trs': (0x6102, 40, 'h'), 'midi_out_usb': (0x6103, 42, 'h'), 'midi_source': (0x6200, 44, 'B'),
    'clock_source': (0x6300, 45, 'B'), 'clock_out': (0x6400, 46, 'B'),
}
GLOBALS_SIZE = 52


def parse_globals(data):
    if len(data) < GLOBALS_SIZE:
        raise ValueError('ajustes globais com %d bytes (esperados %d)' % (len(data), GLOBALS_SIZE))
    return {k: struct.unpack_from('<' + f, data, off)[0] for k, (_t, off, f) in GLOBAL_TYPES.items()}


# the global EQ's 31 parameters (firmware table 0x428e4: name, min, max, step; default = the factory settings the
# app boots with, as the table says 0 for every switch). 19 Hz / 20001 Hz switch the cut filters off (flags 2 / 3)
EQ_PARAMS = [
    ('Lo Cut Switch', 0, 1, 1, 1), ('Lo Cut Freq', 19, 20000, 1, 19), ('Lo Cut Q', .1, 10, .1, .71),
    ('Lo Shelf Switch', 0, 1, 1, 0), ('Lo Shelf Freq', 20, 20000, 1, 20), ('Lo Shelf Q', .1, 10, .1, .71),
    ('Lo Shelf Gain', -20, 20, 1, 0),
    ('Band 1 Switch', 0, 1, 1, 1), ('Band 1 Freq', 20, 20000, 1, 100), ('Band 1 Q', .1, 10, .1, .71),
    ('Band 1 Gain', -20, 20, 1, 0),
    ('Band 2 Switch', 0, 1, 1, 1), ('Band 2 Freq', 20, 20000, 1, 500), ('Band 2 Q', .1, 10, .1, .71),
    ('Band 2 Gain', -20, 20, 1, 0),
    ('Band 3 Switch', 0, 1, 1, 1), ('Band 3 Freq', 20, 20000, 1, 1000), ('Band 3 Q', .1, 10, .1, .71),
    ('Band 3 Gain', -20, 20, 1, 0),
    ('Band 4 Switch', 0, 1, 1, 1), ('Band 4 Freq', 20, 20000, 1, 5000), ('Band 4 Q', .1, 10, .1, .71),
    ('Band 4 Gain', -20, 20, 1, 0),
    ('Hi Shelf Switch', 0, 1, 1, 0), ('Hi Shelf Freq', 20, 20000, 1, 20000), ('Hi Shelf Q', .1, 10, .1, .71),
    ('Hi Shelf Gain', -20, 20, 1, 0),
    ('Hi Cut Switch', 0, 1, 1, 1), ('Hi Cut Freq', 20, 20001, 1, 20001), ('Hi Cut Q', .1, 10, .1, .71),
    ('Level', 0, 100, 1, 50),
]
EQ_LOCUT_OFF, EQ_HICUT_OFF = 19, 20001


def eq_clamp(idx, value):
    """what the pedal stores for EQ parameter idx (it clamps to the table; steps as the editor rounds them)"""
    _n, lo, hi, step, _d = EQ_PARAMS[idx]
    v = min(hi, max(lo, float(value)))
    return round(v / step) * step if step >= 1 else round(v, 1)


# ------------------------------------------------------------------------------------------------ effects
def _fmt_table():
    """sdk/display_formats.txt -> {code: ('list', [names]) | ('format', text)}"""
    t = {}
    try:
        for line in open(os.path.join(paths.DATA, 'display_formats.txt'), encoding='utf-8-sig', errors='replace'):
            parts = line.rstrip('\n').replace('Â°', '°').split(None, 2)
            if len(parts) < 3 or not parts[0].startswith('0x'):
                continue
            code = int(parts[0], 16)
            if parts[1].startswith('list'):
                t[code] = ('list', [s.strip() for s in parts[2].split('|')][:-1])
            elif parts[1] == 'format':
                t[code] = ('format', parts[2].strip())
    except OSError:
        pass
    return t


FORMATS = _fmt_table()


def _printf(fmt, v):
    try:
        return fmt.replace('%%', '%') % v if fmt.count('%') - 2 * fmt.count('%%') == 1 else fmt
    except (TypeError, ValueError):
        return '%g' % v


def format_value(param, v, sync_on=False):
    """display text of knob value v for a parameter dict (fwtool.Entry params), like the pedal shows it"""
    flags = param.get('flags') or [0xff, 0, 0x15]
    f2, f3 = flags[1], flags[2]
    lo, hi = param.get('min', 0), param.get('max', 100)
    # cut filters park on a sentinel just past the range (Low Cut 19 Hz, High Cut 20001 Hz): the pedal shows Off
    if (v <= lo and int(lo) % 10 == 9) or (v >= hi and int(hi) % 10 == 1 and hi > 1000):
        return 'Off'
    if sync_on and f2 & 0xf0 == 0x10:
        k = int(round(v))
        return SYNC_DIVS[k] if 0 <= k < len(SYNC_DIVS) else '%g' % v
    if param.get('options'):                                # text list (app patch "textlist"), past the end = last
        o = param['options']
        return o[min(max(int(round(v)), 0), len(o) - 1)]
    kind = FORMATS.get(f3)
    if kind and kind[0] == 'list':
        k = int(round(v))
        return kind[1][k] if 0 <= k < len(kind[1]) else '%g' % v
    if kind and kind[0] == 'format' and '%' in kind[1]:
        alts = [a.strip() for a in kind[1].split('  /  ')]
        if len(alts) == 1:
            return _printf(alts[0], v)
        a0, a1 = alts[0], alts[1]
        if '%' not in a0:                                   # Off / Mute / Limit / Center
            if a0 == 'Limit':
                return a0 if v >= param.get('max', v) else _printf(a1, v)
            if a0 == 'Center' and len(alts) == 3:
                return a0 if v == 0 else _printf(alts[1] if v > 0 else alts[2], abs(v))
            return a0 if v <= param.get('min', 0) else _printf(a1, v)
        if a1.endswith('kHz'):
            return _printf(a1, v / 1000.0) if v >= 1000 else _printf(a0, v)
        if a1.endswith('s') and a0.endswith('ms'):
            return _printf(a1, v / 1000.0) if v >= 1000 else _printf(a0, v)
        return _printf(a0 if v > 0 else a1, v)
    step = param.get('step', 1) or 1
    return ('%.0f' if step >= 1 and float(v).is_integer() else '%.2f') % v


class EffectDB:
    """(menu, id) -> effect description {name, category, menu, params, origin}. Sources, first match wins:
    the effect library of a firmware image, catalog manifests (fwstudio_core), the PC editor's alg_data.json
    (factory cabinets and anything missing), then the IR / Clone names read from the pedal."""
    def __init__(self):
        self.by_id = {}
        self.ir_names, self.clone_names = [], []
        self.menus, self.menus_from = None, ''     # {menu: [ids]} of the first firmware added (palette())

    def add(self, menu, eid, name, category, params, origin, override=False):
        if override or eid not in self.by_id:
            self.by_id[eid] = dict(menu=menu, id=eid, name=name, category=category, params=params, origin=origin)

    def add_firmware(self, fw, origin='firmware'):
        lib = fw.library
        menus = {}
        for m, lst in enumerate(lib.cat_entries):
            for ei in lst:
                e = lib.entries[ei]
                self.add(m, e.id, e.name, e.category, e.params, origin)
                menus.setdefault(m, []).append(e.id)
        if self.menus is None:
            self.menus, self.menus_from = menus, origin

    def palette(self):
        """{menu: [effect dict]} that can be put in a chain: the menus of the first firmware added (in its menu
        order), the factory cabinets of the editor's list (their ids index the pedal's IR table), the 20 User IR
        and 10 Clone slots. The routing blocks (Split / Path B / Merge) are not in any menu."""
        out = {}
        for m, ids in sorted((self.menus or {}).items()):
            if m in (5, 6, 11):
                continue
            out[m] = [self.by_id[i] for i in ids if i in self.by_id]
        out[5] = [e for e in self.by_id.values() if e['menu'] == 5 and e['origin'] == 'editor'] or \
            [self.by_id[i] for i in (self.menus or {}).get(5, []) if i in self.by_id]
        out[6] = [self.lookup(Slot(-1, False, 6, USER_IR_BASE + k, [0.0] * NKNOBS)) for k in range(20)]
        out[11] = [self.lookup(Slot(-1, False, 11, CLONE_BASE + k, [0.0] * NKNOBS)) for k in range(10)]
        for e in out[6] + out[11]:
            e['menu'] = 6 if e['id'] < CLONE_BASE else 11
        return {m: v for m, v in sorted(out.items()) if v}

    def add_items(self, items):
        for it in items:
            self.add(it.menu, it.id, it.name, it.category, it.params, it.origin)

    def add_editor_json(self, path):
        try:
            d = json.load(open(path, encoding='utf-8'))
        except Exception:
            return
        for mod in d.get('Modules', []):
            for a in mod.get('alg', []):
                params = []
                for w in a.get('widget', []):
                    try:
                        code = w.get('codeing', '')
                        f3 = int(code.rsplit('_', 1)[1]) if code.startswith('ValToStr_') else 0x15
                        params.append(dict(name=w['name'], min=float(w['min']), max=float(w['max']),
                                           step=float(w['step']), default=float(w['defaultValue']),
                                           idx=int(w['ID']), flags=[0xff, 0, f3]))
                    except (KeyError, ValueError, IndexError):
                        pass
                self.add(mod.get('index', -1), a['fxid'], a.get('name', '?'), a.get('type', ''), params, 'editor')

    def lookup(self, slot):
        e = self.by_id.get(slot.id)
        name = e['name'] if e else None
        if slot.menu == 6 or USER_IR_BASE <= slot.id < USER_IR_BASE + 20:
            k = slot.id - USER_IR_BASE
            if 0 <= k < len(self.ir_names) and self.ir_names[k]:
                name = self.ir_names[k]
            name = name or 'User IR %d' % (k + 1)
        elif slot.menu == 11 or CLONE_BASE <= slot.id < CLONE_BASE + 10:
            k = slot.id - CLONE_BASE
            if 0 <= k < len(self.clone_names) and self.clone_names[k]:
                name = self.clone_names[k]
            name = name or 'Clone %d' % (k + 1)
        if e is None:
            e = dict(menu=slot.menu, id=slot.id, name=name or 'id %08x' % slot.id, category='', params=[],
                     origin='desconhecido')
            # a cab / IR / clone slot runs its menu's engine: borrow the engine's knobs when the id is not listed
            for o in self.by_id.values():
                if o['menu'] == slot.menu and slot.menu in (5, 6, 11) and o['params']:
                    e['params'] = o['params']
                    break
        else:
            e = dict(e, name=name or e['name'])
        return e

    def default_knobs(self, menu, eid):
        """{knob position: value} a slot gets when this effect is put in it: FUN_00022230 writes the defaults of
        the effect's parameters and leaves the other positions as they were"""
        k = {}
        for p in self.lookup(Slot(-1, False, menu, eid, [0.0] * NKNOBS))['params']:
            i = p.get('idx', -1)
            if 0 <= i < NKNOBS and p.get('default') is not None:
                k[i] = float(p['default'])
        return k

    def params(self, slot):
        """[(param, value, text)] in the order of the library entry"""
        e = self.lookup(slot)
        ps = e['params']
        out = []
        for p in ps:
            k = p.get('idx', 0)
            v = slot.knobs[k] if 0 <= k < len(slot.knobs) else 0.0
            f2 = (p.get('flags') or [0, 0, 0])[1]
            sync_on = False
            if f2 & 0xf0 == 0x10:                           # value with a Sync switch: param number f2 & 0xf
                n = f2 & 0xf
                if n < len(ps):
                    kk = ps[n].get('idx', n)
                    sync_on = 0 <= kk < len(slot.knobs) and slot.knobs[kk] >= 0.5
            out.append((p, v, format_value(p, v, sync_on)))
        return out


def controls(db, slot):
    """editable view of a slot's parameters: [dict(order, param, pos, value, text, lo, hi, step, options)].
    order is what Pedal.set_knob takes; pos is the knob position (slot.knobs index). A value with its Sync
    switch on is a note division (0..10); list formats come with their option names."""
    out = []
    for order, (p, v, txt) in enumerate(db.params(slot)):
        f3 = (p.get('flags') or [0xff, 0, 0x15])[2]
        lo, hi, step = p.get('min', 0.0), p.get('max', 100.0), p.get('step', 1.0) or 1.0
        options = None
        if txt in SYNC_DIVS and (p.get('flags') or [0, 0, 0])[1] & 0xf0 == 0x10:
            lo, hi, step, options = 0.0, float(len(SYNC_DIVS) - 1), 1.0, list(SYNC_DIVS)
        elif p.get('options'):                          # text list from the library entry
            options = list(p['options'])
        elif FORMATS.get(f3, ('', ''))[0] == 'list':
            names = FORMATS[f3][1]
            options = [names[k] if 0 <= k < len(names) else str(k) for k in range(int(lo), int(hi) + 1)]
            if int(lo):
                options = [''] * int(lo) + options      # index = value
        out.append(dict(order=order, param=p, pos=p.get('idx', order), value=v, text=txt, lo=lo, hi=hi,
                        step=step, options=options))
    return out


def quantise(c, v):
    """what FUN_000213cc stores: rounded to the step, clamped to the range"""
    st = c['step'] or 1.0
    v = round(v / st) * st
    return float(min(c['hi'], max(c['lo'], v)))


def default_db(fw_paths=(), items=()):
    db = EffectDB()
    from matribox.firmware import fwtool
    for path in fw_paths:
        try:
            db.add_firmware(fwtool.Firmware(open(path, 'rb').read()), os.path.basename(path))
        except Exception:
            pass
    db.add_items(items)
    for p in paths.editor_json_candidates():
        if os.path.exists(p):
            db.add_editor_json(p)
    return db


def chain_edit_payload(old, new, menu=-1, eid=-1):
    """0x01040001 message body (see the module docstring)"""
    return struct.pack('<IbbbI', SUB_CHAIN_EDIT, old, new, menu, eid & 0xffffffff)


# ------------------------------------------------------------------------------------------------ pedal
class Pedal:
    """editor protocol client. Not thread-safe: use it from one thread."""
    def __init__(self, port_name=None, log=None):
        self.port = htmidi.Port(port_name)
        self.name = self.port.name
        self.rx = htmidi.Reassembler()
        self.log = log or (lambda *a: None)
        self.events = []                # (cmd, payload) pushed by the pedal while waiting for something else

    def close(self):
        self.port.close()

    def _send(self, cmd, payload=b'', flag=htmidi.FLAG_REQ, gap=0.02):
        for p in htmidi.packets(htmidi.message(cmd, payload), flag):
            self.port.send(p)
            time.sleep(gap)

    def _drain(self):
        out = []
        for p in self.port.receive():
            r = self.rx.feed(p)
            if not r:
                continue
            if r[0] == htmidi.FLAG_NAK:
                raise RuntimeError('o pedal recusou o pacote no offset %d (NAK)' % r[2])
            out.append((r[1], r[2]))
        return out

    def request(self, cmd, payload=b'', want=None, sub=None, timeout=4.0):
        """send a request; return the payload of the first reply with cmd == want (and leading u32 == sub)"""
        want = cmd if want is None else want
        self.events += self._drain()
        self._send(cmd, payload)
        return self._wait(cmd, want, sub, timeout)

    def _wait(self, cmd, want, sub, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            for c, data in self._drain():
                if c == want and (sub is None or len(data) >= 4 and struct.unpack_from('<I', data)[0] == sub):
                    return data
                self.events.append((c, data))
            time.sleep(0.005)
        raise TimeoutError('o pedal não respondeu ao comando %#010x' % cmd)

    def poll(self):
        """messages the pedal pushed since the last call"""
        ev = self.events + self._drain()
        self.events = []
        return ev

    # -------------------------------------------------------------- reads
    def preset_list(self):
        return parse_list(self.request(CMD_SIGNAL, struct.pack('<I', SUB_LIST), sub=SUB_LIST, timeout=8))

    def _unblock(self):
        """FUN_0003568c drops a reply identical to the last one it sent: make it send something else first"""
        try:
            self.request(CMD_GLOBAL_SETTING, struct.pack('<I', 30), timeout=1)
        except TimeoutError:
            pass

    def current_index(self):
        self._unblock()
        d = self.request(CMD_SIGNAL, struct.pack('<I', SUB_INDEX), sub=SUB_INDEX, timeout=2)
        return struct.unpack_from('<i', d, 4)[0]

    def modified(self):
        """True when the edit buffer has unsaved changes"""
        self._unblock()
        d = self.request(CMD_GLOBAL_SINGLE, struct.pack('<I', SUB_MODIFIED), sub=SUB_MODIFIED, timeout=2)
        return bool(struct.unpack_from('<I', d, 4)[0])

    # -------------------------------------------------------------- writes (edit buffer)
    def activate(self, index):
        if not 0 <= index < NPRESETS:
            raise ValueError('preset %d fora de 0..%d' % (index, NPRESETS - 1))
        self._send(CMD_SIGNAL, struct.pack('<II', SUB_INDEX, index), htmidi.FLAG_DATA)

    def set_on(self, slot, on):
        if not 0 <= slot < NSLOTS:
            raise ValueError('slot %d' % slot)
        self._send(CMD_SIGNAL, struct.pack('<III', SUB_SWITCH, slot, 1 if on else 0), htmidi.FLAG_DATA)

    def set_knob(self, slot, order, value):
        """order = position of the parameter in the library entry (not the knob position)"""
        if not 0 <= slot < NSLOTS or not 0 <= order < NKNOBS:
            raise ValueError('slot %d / parâmetro %d' % (slot, order))
        self._send(CMD_SIGNAL, struct.pack('<IIIf', SUB_KNOB, slot, order, value), htmidi.FLAG_DATA)

    # chain edits (positions = index in the display order, Preset.chain())
    def _chain_edit(self, old, new, menu=-1, eid=-1):
        self._send(CMD_SIGNAL, chain_edit_payload(old, new, menu, eid), htmidi.FLAG_DATA)

    def insert_block(self, pos, menu, eid):
        self._chain_edit(-1, pos, menu, eid)

    def remove_block(self, pos):
        self._chain_edit(pos, -1)

    def move_block(self, old, new):
        if old != new:
            self._chain_edit(old, new)

    def replace_block(self, pos, menu, eid):
        self._chain_edit(pos, pos, menu, eid)

    def edit_buffer(self, index=0):
        p = parse_blocks(self.request(CMD_PRESET, struct.pack('<I', max(0, index))))
        p.index, p.live, p.source = index, True, 'pedal'
        return p

    def stored(self, index):
        p = parse_blocks(self.request(CMD_EXPORT, struct.pack('<I', index)))
        p.index, p.source = index, 'pedal'
        return p

    def ir_names(self):
        return parse_names(self.request(CMD_IR_LIST), 20, 160)

    def clone_names(self):
        return parse_names(self.request(CMD_CLONE_LIST), 10, 80)

    # -------------------------------------------------------------- Clone slot (flash)
    def write_clone(self, slot, name, data, progress=None):
        """store data (a nam/a2blob.h blob) in Clone slot 0..9 under name; overwrites the slot's capture.
        progress(sent, total) is called per packet. Same message as the editor's Clone import."""
        if not 0 <= slot < CLONE_SLOTS:
            raise ValueError('slot %d fora de 0..%d' % (slot, CLONE_SLOTS - 1))
        if len(data) > CLONE_DATA_MAX:
            raise ValueError('%d bytes não cabem no slot (máximo %d)' % (len(data), CLONE_DATA_MAX))
        nb = name.encode('latin1', 'replace')[:CLONE_NAME_MAX]
        payload = struct.pack('<III', slot, 0, sum(data) & 0xffffffff) + nb.ljust(20, b'\0') + bytes(data)
        self.events += self._drain()
        pk = htmidi.packets(htmidi.message(CMD_CLONE_DATA, payload), htmidi.FLAG_DATA)
        for k, p in enumerate(pk):
            self.port.send(p)
            time.sleep(0.04)                # the pace nam_clone.py uses (HW-confirmed)
            if progress:
                progress(k + 1, len(pk))
        r = self._wait(CMD_CLONE_DATA, CMD_CLONE_ACK, None, 15)
        s, ok = struct.unpack_from('<II', r, 0)
        if s != slot or ok != 1:
            raise RuntimeError('o pedal respondeu slot %d, estado %d' % (s + 1, ok))

    # -------------------------------------------------------------- global settings / global EQ
    def global_settings(self):
        return parse_globals(self.request(CMD_GLOBAL_ALL, timeout=4))

    def set_global(self, key, value):
        """one setting of GLOBAL_TYPES (the pedal applies and keeps it, like the editor's settings page)"""
        t = GLOBAL_TYPES[key][0]
        self._send(CMD_GLOBAL_SINGLE, struct.pack('<Ii', t, int(value)), htmidi.FLAG_DATA)

    def eq(self):
        d = self.request(CMD_EQ_ALL, timeout=4)
        return list(struct.unpack_from('<%df' % len(EQ_PARAMS), d))

    def eq_switch(self):
        self._unblock()
        d = self.request(CMD_EQ_SWITCH, struct.pack('<I', CMD_EQ_SWITCH), timeout=2)
        return bool(struct.unpack_from('<II', d)[1])

    def set_eq(self, idx, value):
        if not 0 <= idx < len(EQ_PARAMS):
            raise ValueError('parâmetro de EQ %d' % idx)
        self._send(CMD_EQ_PARAM, struct.pack('<If', idx, float(value)), htmidi.FLAG_DATA)

    def set_eq_switch(self, on):
        self._send(CMD_EQ_SWITCH, struct.pack('<II', CMD_EQ_SWITCH, 1 if on else 0), htmidi.FLAG_DATA)

    # -------------------------------------------------------------- User IR slot (flash)
    def write_ir(self, slot, name, data, progress=None):
        """store data (1024 int32 samples, matribox/usb/irwav.py) in User IR slot 0..19 under name; overwrites the
        slot's IR. Same message as the editor's IR import."""
        if not 0 <= slot < IR_SLOTS:
            raise ValueError('slot %d fora de 0..%d' % (slot, IR_SLOTS - 1))
        if len(data) != IR_DATA_SIZE:
            raise ValueError('o IR tem %d bytes (precisa ter %d)' % (len(data), IR_DATA_SIZE))
        nb = name.encode('latin1', 'replace')[:IR_NAME_MAX]
        payload = struct.pack('<III', slot, 0, sum(data) & 0xffffffff) + nb.ljust(20, b'\0') + bytes(data)
        self.events += self._drain()
        pk = htmidi.packets(htmidi.message(CMD_IR_DATA, payload), htmidi.FLAG_DATA)
        for k, p in enumerate(pk):
            self.port.send(p)
            time.sleep(0.04)                # the Clone write's pace
            if progress:
                progress(k + 1, len(pk))
        r = self._wait(CMD_IR_DATA, CMD_IR_ACK, None, 15)
        s, ok = struct.unpack_from('<II', r, 0)
        if s != slot or ok != 1:
            raise RuntimeError('o pedal respondeu slot %d, estado %d' % (s + 1, ok))

    def rename_ir(self, slot, name):
        if not 0 <= slot < IR_SLOTS:
            raise ValueError('slot %d fora de 0..%d' % (slot, IR_SLOTS - 1))
        nb = name.encode('latin1', 'replace')[:IR_NAME_MAX]
        self._send(CMD_IR_RENAME, struct.pack('<I', slot) + nb.ljust(20, b'\0'), htmidi.FLAG_DATA)


# ------------------------------------------------------------------------------------------------ CLI
def describe(p, db, out=print):
    out('%s  %s%s   estilo %s  volume %d  bpm %d' % (
        preset_label(p.index) if p.index >= 0 else '--', p.name or '(sem nome)', '  [editando]' if p.live else '',
        p.style or '-', p.volume, p.bpm))
    for s in p.chain():
        e = db.lookup(s)
        vals = ', '.join('%s %s' % (pp['name'], t) for pp, v, t in db.params(s))
        out('  %-3s %-4s %-20s %s' % ('on' if s.on else 'off', MENU_SHORT.get(e['menu'], '?'), e['name'], vals))


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('list'); p.add_argument('--port')
    p = sub.add_parser('show'); p.add_argument('n', nargs='?', type=int); p.add_argument('--port')
    p = sub.add_parser('bank'); p.add_argument('fw'); p.add_argument('n', nargs='?', type=int)
    p = sub.add_parser('activate', help='load preset N (1-based) on the pedal')
    p.add_argument('n', type=int); p.add_argument('--port'); p.add_argument('--force', action='store_true')
    p = sub.add_parser('on', help='block K (1-based, chain order) of the edit buffer on (1) or off (0)')
    p.add_argument('k', type=int); p.add_argument('state', type=int); p.add_argument('--port')
    p = sub.add_parser('knob', help='parameter P (1-based, as listed by show) of block K of the edit buffer')
    p.add_argument('k', type=int); p.add_argument('p', type=int); p.add_argument('value', type=float)
    p.add_argument('--port')
    a = ap.parse_args()
    if a.cmd == 'bank':
        from matribox.firmware import fwtool
        fw = fwtool.Firmware(open(a.fw, 'rb').read())
        db = default_db([a.fw])
        ps = bank_presets(fw.sections[3])
        for p in ps if a.n is None else [ps[a.n - 1]]:
            describe(p, db)
        return
    pedal = Pedal(a.port)
    try:
        if a.cmd == 'list':
            cur = pedal.current_index()
            for i, _pos, name, style in pedal.preset_list():
                print('%s %s %-17s %s' % ('>' if i == cur else ' ', preset_label(i), name, style))
            return
        if a.cmd == 'activate':
            if pedal.modified() and not a.force:
                sys.exit('o preset atual tem mudanças não salvas (use --force para descartá-las)')
            pedal.activate(a.n - 1)
            time.sleep(0.2)
            print('ativo:', preset_label(pedal.current_index()))
            return
        db = default_db([paths.build_path('QME-200_FW_USB_V1.2.2_namclone.bin')])
        db.ir_names, db.clone_names = pedal.ir_names(), pedal.clone_names()
        if a.cmd in ('on', 'knob'):
            live = pedal.edit_buffer(pedal.current_index())
            s = live.chain()[a.k - 1]
            if a.cmd == 'on':
                pedal.set_on(s.sid, a.state)
            else:
                pedal.set_knob(s.sid, a.p - 1, a.value)
            time.sleep(0.2)
            describe(pedal.edit_buffer(live.index), db)
            return
        if a.n is None:
            describe(pedal.edit_buffer(pedal.current_index()), db)
        else:
            describe(pedal.stored(a.n - 1), db)
    finally:
        pedal.close()


if __name__ == '__main__':
    main()
