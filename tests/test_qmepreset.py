"""Check matribox/usb/qmepreset.py against the app's own preset handlers, run in emulation (matribox/app/uiemu.py boot with
the firmware's factory Bank).

The handlers are called the way FUN_0003a084 calls them (decompressed message {cmd, n, payload}, flag 0x11).
Their replies are caught at FUN_00030a0c (already LZO-compressed by FUN_00030b38, so matribox/usb/htmidi.py's
decoder is exercised too) or at FUN_0003568c (notification queue), then parsed with qmepreset and compared
with the presets read straight from the Bank section.

  test_qmepreset.py [firmware.bin]          (needs unicorn on PYTHONPATH)
"""
import os, struct, sys
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_LR, UC_ARM_REG_PC
from unicorn import UC_HOOK_CODE
from matribox.firmware import fwtool
from matribox.usb import htmidi
from matribox.app import uiemu
from matribox.usb import qmepreset as Q

SEND_PACKETS = 0x30a0c          # FUN_00030a0c(msg, len, flag): splits a compressed message into SysEx packets
NOTIFY = 0x3568c                # FUN_0003568c(msg, len): queued, uncompressed-on-entry notification
H_PRESET, H_EXPORT, H_LIST_INDEX = 0x2e8c8, 0x2e110, 0x2f1a8
H_SLOTS = 0x2f27c               # table 0x42cc0[5]: 0x0105xxxx (knob values, on/off, volume, ...)
H_MODIFIED = 0x2ddc8            # 0x11000001 sub 0x08xx (table 0x42cec[8])
H_CHAIN = 0x2fef0               # table 0x42cc0[4]: 0x0104xxxx (chain: list, edit, algorithm, paste)
MSG = 0x2001f000
MALLOC, FREE = 0x674, 0x6d2
HEAP = 0x81c00000               # below the LCD framebuffers at 0x81f00000


class App:
    def __init__(self, fw):
        self.ui = ui = uiemu.Ui(fw)
        ui.start()
        self.out = []
        ui.uc.hook_add(UC_HOOK_CODE, self.on_packets, begin=SEND_PACKETS, end=SEND_PACKETS)
        ui.uc.hook_add(UC_HOOK_CODE, self.on_notify, begin=NOTIFY, end=NOTIFY)
        # uiemu enters main without the C library init, so the heap's free list is garbage: serve malloc from
        # a bump allocator in unused SDRAM and make free a no-op
        self.brk = HEAP
        ui.uc.hook_add(UC_HOOK_CODE, self.on_malloc, begin=MALLOC, end=MALLOC)
        ui.uc.hook_add(UC_HOOK_CODE, self._ret_hook, begin=FREE, end=FREE)

    def on_malloc(self, uc, addr, size, ud):
        n = uc.reg_read(UC_ARM_REG_R0)
        p, self.brk = self.brk, (self.brk + n + 15) & ~7
        uc.reg_write(UC_ARM_REG_R0, p)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR) | 1)

    def _ret_hook(self, uc, addr, size, ud):
        self._ret(uc)

    def _ret(self, uc):
        uc.reg_write(UC_ARM_REG_R0, 0)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR) | 1)

    def on_packets(self, uc, addr, size, ud):
        p, n, flag = (uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2))
        m = bytes(uc.mem_read(p, n))
        cmd, z = struct.unpack_from('<II', m)
        self.out.append((cmd, htmidi.lzo1x_decompress(m[8:8 + z]) if z else b'', flag))
        self._ret(uc)

    def on_notify(self, uc, addr, size, ud):
        p, n = uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R1)
        m = bytes(uc.mem_read(p, n))
        cmd, k = struct.unpack_from('<II', m)
        self.out.append((cmd, m[8:8 + k], 'notify'))
        self._ret(uc)

    def call(self, handler, cmd, *words, flag=0x11, payload=None):
        """run a handler like FUN_0003a084 does; flag 0x11 returns the single reply, 0x12 all the messages"""
        if payload is None:
            payload = b''.join(struct.pack('<I', w) for w in words)
        msg = struct.pack('<II', cmd, len(payload)) + payload
        self.ui.uc.mem_write(MSG, msg)
        self.out = []
        self.brk = HEAP                 # free is a no-op: what a handler mallocs is only used during the call
        # the list handler alone takes ~9 KB of stack: start at the top of DTCM (initial MSP), like the main loop
        self.ui.fn(handler, MSG, len(msg), flag, sp=0x20008000 - 0x40)
        if flag != 0x11:
            return self.out
        assert len(self.out) == 1, 'handler %#x: %d replies' % (handler, len(self.out))
        return self.out[0]

    def edit_buffer(self):
        return Q.parse_blocks(self.call(H_PRESET, Q.CMD_PRESET, 0)[1])


def same(a, b, menu=True):
    """compare what the chain view uses (menu=False: the app re-resolves the menu from the id when it loads a
    preset, so the edit buffer says 15 where the factory Bank stores Volume as 14)"""
    diffs = []
    for k in ('name', 'style', 'volume', 'bpm', 'pan'):
        if getattr(a, k) != getattr(b, k):
            diffs.append('%s %r != %r' % (k, getattr(a, k), getattr(b, k)))
    if [s.sid for s in a.chain()] != [s.sid for s in b.chain()]:
        diffs.append('order %s != %s' % (a.order, b.order))
    for x, y in zip(a.slots, b.slots):
        if (x.on, x.id) != (y.on, y.id) or (menu and x.menu != y.menu) or (not x.empty and x.knobs != y.knobs):
            diffs.append('slot %d: %r != %r' % (x.sid, x, y))
    return diffs


def pushes(out, sub):
    return [struct.unpack_from('<I%dI' % ((len(d) - 4) // 4), d) for c, d, f in out
            if len(d) >= 4 and struct.unpack_from('<I', d)[0] == sub]


def check_writes(app, bank, db):
    """the messages the GUI sends (Pedal.set_on / set_knob / activate), through the app's handlers"""
    fails = 0
    live = app.edit_buffer()
    # on/off: the first block that is off
    s = next(x for x in live.chain() if not x.on)
    out = app.call(H_SLOTS, Q.CMD_SIGNAL, payload=struct.pack('<III', Q.SUB_SWITCH, s.sid, 1), flag=0x12)
    after = app.edit_buffer()
    ok = after.slots[s.sid].on and all(a.on == b.on for a, b in zip(after.slots, live.slots) if a.sid != s.sid)
    # "modified" read (0x11000001 {0x0800}): FUN_0002ddc8 is called as (sub, word, sub & 0xff, flag)
    app.out = []
    app.ui.fn(H_MODIFIED, Q.SUB_MODIFIED, 0, 0, 0x11, sp=0x20008000 - 0x40)
    mod = pushes(app.out, Q.SUB_MODIFIED)
    print('liga/desliga 0x01050002: slot %d (%s) ligado: %s; leitura "modificado": %s' % (
        s.sid, db.lookup(s)['name'], ok, mod))
    fails += not ok or not mod or mod[0][1] != 1
    # knob: the Cab, whose parameter order (0, 1, 2) differs from its knob positions (1, 5, 6)
    cab = next(x for x in live.chain() if x.menu == 5)
    ps = db.lookup(cab)['params']
    k = next(i for i, prm in enumerate(ps) if prm['name'] == 'Low Cut')
    pos = ps[k]['idx']
    out = app.call(H_SLOTS, Q.CMD_SIGNAL, payload=struct.pack('<IIIf', Q.SUB_KNOB, cab.sid, k, 100.4), flag=0x12)
    after = app.edit_buffer()
    v = after.slots[cab.sid].knobs
    ok = v[pos] == 100.0 and all(v[i] == live.slots[cab.sid].knobs[i] for i in range(15) if i != pos)
    push = [struct.unpack_from('<IIIf', d) for c, d, f in out
            if len(d) >= 16 and struct.unpack_from('<I', d)[0] == Q.SUB_KNOB]
    ok = ok and push and push[0][1:] == (cab.sid, pos, 100.0)
    print('knob 0x01050001: %s %s (ordem %d) = 100,4 -> knobs[%d] = %g; aviso %s: %s' % (
        db.lookup(cab)['name'], ps[k]['name'], k, pos, v[pos], push, bool(ok)))
    fails += not ok
    # clamp + quantise: below the minimum
    app.call(H_SLOTS, Q.CMD_SIGNAL, payload=struct.pack('<IIIf', Q.SUB_KNOB, cab.sid, k, -5.0), flag=0x12)
    v = app.edit_buffer().slots[cab.sid].knobs[pos]
    print('knob abaixo do mínimo -> %g (mínimo %g): %s' % (v, ps[k]['min'], v == ps[k]['min']))
    fails += v != ps[k]['min']
    # activate preset 5: the edit buffer becomes Bank[5] and the index is announced
    out = app.call(H_LIST_INDEX, Q.CMD_SIGNAL, payload=struct.pack('<II', Q.SUB_INDEX, 5), flag=0x12)
    d = same(app.edit_buffer(), bank[5], menu=False)
    idx = pushes(out, Q.SUB_INDEX)
    cmd, data, _f = app.call(H_LIST_INDEX, Q.CMD_SIGNAL, Q.SUB_INDEX)
    cur = struct.unpack_from('<i', data, 4)[0]
    ok = not d and cur == 5 and idx and idx[0][1] == 5
    print('ativar 0x010a0001 -> preset 5: buffer = Bank[5]: %s, atual = %d, aviso %s: %s' % (not d, cur, idx, bool(ok)))
    fails += not ok
    return fails


def check_chain(app, db):
    """chain edits 0x01040001 (Pedal.insert_block / remove_block / move_block / replace_block)"""
    import copy
    fails = 0
    pal = db.palette()
    state = {'p': app.edit_buffer(), 'mirror_bad': 0}

    def edit(old, new, menu=-1, eid=-1):
        out = app.call(H_CHAIN, Q.CMD_SIGNAL, payload=Q.chain_edit_payload(old, new, menu, eid), flag=0x12)
        p = app.edit_buffer()
        # Preset.edit (what the GUI shows before the pedal answers) must end where the pedal did
        m = copy.deepcopy(state['p'])
        op = (('insert', new, menu, eid) if old == -1 else ('remove', old) if new == -1 else
              ('replace', old, menu, eid) if old == new else ('move', old, new))
        known = eid in db.by_id or menu in (6, 11) or old != -1 and new != -1 and old != new or new == -1
        if known and not (op[0] == 'insert' and len(m.order) >= Q.NSLOTS):
            m.edit(*op, knobs=db.default_knobs)
            diff = [k for k in range(Q.NSLOTS) if (m.slots[k].menu, m.slots[k].id, m.slots[k].on) !=
                    (p.slots[k].menu, p.slots[k].id, p.slots[k].on) or
                    (not p.slots[k].empty and p.slots[k].menu not in (5, 6, 11) and
                     any(abs(a - b) > 1e-4 for a, b in zip(m.slots[k].knobs, p.slots[k].knobs)))]
            if m.order != p.order or diff:
                state['mirror_bad'] += 1
                print('  Preset.edit%s difere do pedal: ordem %s / %s, slots %s' % (op, m.order, p.order, diff))
                for k in diff:
                    print('    slot %d: %r on %s %s / pedal %r on %s %s' % (
                        k, m.slots[k], m.slots[k].on, m.slots[k].knobs, p.slots[k], p.slots[k].on, p.slots[k].knobs))
        state['p'] = p
        return p, pushes(out, Q.SUB_CHAIN), pushes(out, Q.SUB_OVERLOAD)

    def defaults_ok(p, s):
        return all(abs(s.knobs[prm['idx']] - prm.get('default', s.knobs[prm['idx']])) < 1e-4
                   for prm in db.lookup(s)['params'] if 0 <= prm.get('idx', -1) < Q.NKNOBS)

    def report(what, ok, extra=''):
        nonlocal fails
        print('%s: %s%s' % (what, 'ok' if ok else 'FALHOU', extra))
        fails += not ok

    p0 = app.edit_buffer()
    ch = [s.sid for s in p0.chain()]
    print('cadeia inicial:', ', '.join(db.lookup(s)['name'] for s in p0.chain()))
    # move: position 0 -> 3 (3 is the final position)
    p1, push, _ = edit(0, 3)
    want = ch[1:4] + ch[:1] + ch[4:]
    report('mover 0 -> 3', [s.sid for s in p1.chain()] == want and push,
           ' (aviso 0x01040000: %d)' % len(push))
    # move back: 3 -> 0
    p1, push, _ = edit(3, 0)
    report('mover 3 -> 0', [s.sid for s in p1.chain()] == ch)
    # remove position 1: the slot is freed
    sid = ch[1]
    p2, push, _ = edit(1, -1)
    report('remover posição 1 (%s)' % db.lookup(p0.slots[sid])['name'],
           [s.sid for s in p2.chain()] == ch[:1] + ch[2:] and p2.slots[sid].empty and push)
    # insert a drive at position 2: first free slot, on, default knobs
    e = pal[3][4]
    p3, push, _ = edit(-1, 2, 3, e['id'])
    s = p3.chain()[2]
    report('inserir %s na posição 2' % e['name'],
           s.id == e['id'] and s.menu == 3 and s.on and len(p3.chain()) == len(p2.chain()) + 1 and defaults_ok(p3, s)
           and [x.sid for x in p3.chain()[:2]] == [x.sid for x in p2.chain()[:2]],
           ' (slot %d, knobs padrão: %s)' % (s.sid, defaults_ok(p3, s)))
    # replace position 0 with a modulation: same slot, new id, default knobs
    e = pal[8][3]
    sid0 = p3.chain()[0].sid
    p4, push, _ = edit(0, 0, 8, e['id'])
    s = p4.chain()[0]
    report('trocar a posição 0 por %s' % e['name'], s.sid == sid0 and s.id == e['id'] and s.menu == 8
           and defaults_ok(p4, s) and len(p4.chain()) == len(p3.chain()))
    # replace with another effect of the same menu
    e = pal[8][5]
    p4, push, _ = edit(0, 0, 8, e['id'])
    report('trocar a posição 0 por %s (mesmo menu)' % e['name'], p4.chain()[0].id == e['id'] and
           defaults_ok(p4, p4.chain()[0]))
    # a factory cab (editor list id, resolved through the IR table) and a Clone slot
    n = len(p4.chain())
    for menu, e in ((5, pal[5][7]), (11, pal[11][2])):
        # take the old block of that menu out first: one cab / one clone per chain is what presets use
        old = [k for k, x in enumerate(p4.chain()) if x.menu == menu]
        if old:
            p4, _, _ = edit(old[0], -1)
        pos = len(p4.chain()) - 1
        p5, push, ov = edit(-1, pos, menu, e['id'])
        s = p5.chain()[pos]
        report('inserir %s (menu %d, id %08x) na posição %d' % (e['name'], menu, e['id'], pos),
               s.id == e['id'] and s.menu == menu, ' overload %s' % ov if ov else '')
        p4 = p5
    # an id that is not in the library: the firmware leaves an empty slot in the order (a phantom)
    n, order = len(p4.chain()), list(p4.order)
    p6, push, ov = edit(-1, 1, 3, 0x03007777)
    ph = p6.phantoms()
    report('inserir id inexistente 03007777: deixa um fantasma na posição 1', ph == [1] and len(p6.chain()) == n,
           ' (ordem %s -> %s)' % (order, p6.order))
    for k in reversed(ph):                       # what the GUI does: remove it at once
        p6, _, _ = edit(k, -1)
    report('  fantasma removido', not p6.phantoms() and p6.order == order)
    # fill the chain to 12 and try a 13th
    e = pal[7][0]
    p7 = p6
    while len(p7.chain()) < Q.NSLOTS:
        p7, _, ov = edit(-1, len(p7.chain()), 7, e['id'])
        if ov:
            print('  overload ao encher a cadeia: %s' % ov)
            break
    full = [x.sid for x in p7.chain()]
    p8, push, ov = edit(-1, 0, 7, e['id'])
    report('13º bloco com a cadeia cheia (%d) é recusado' % len(full), [x.sid for x in p8.chain()] == full)
    report('Preset.edit reproduz o pedal em todas as edições', not state['mirror_bad'])
    return fails


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else Q.paths.build_path('QME-200_FW_USB_V1.2.2_namclone.bin')
    fw = fwtool.Firmware(open(path, 'rb').read())
    bank = Q.bank_presets(fw.sections[3])
    app = App(fw)
    fails = 0

    cmd, data, flag = app.call(H_LIST_INDEX, Q.CMD_SIGNAL, Q.SUB_LIST)
    lst = Q.parse_list(data)
    names = [n for _i, _p, n, _s in lst]
    ok = cmd == Q.CMD_SIGNAL and names[:len(bank)] == [p.name for p in bank]
    print('lista 0x010a0000: %d bytes, %d nomes; os %d do Bank batem: %s  (ex.: %s)' % (
        len(data), len(lst), len(bank), ok, ', '.join('%s %s/%s' % (Q.preset_label(i), n, s)
                                                     for i, _p, n, s in lst[:3])))
    fails += not ok

    cmd, data, flag = app.call(H_LIST_INDEX, Q.CMD_SIGNAL, Q.SUB_INDEX)
    sub, cur = struct.unpack_from('<Ii', data)
    ok = cmd == Q.CMD_SIGNAL and sub == Q.SUB_INDEX and flag == 'notify'
    print('preset atual 0x010a0001: %s (via %s): %s' % (cur, flag, ok))
    fails += not ok

    bad = 0
    for i, ref in enumerate(bank):
        cmd, data, flag = app.call(H_EXPORT, Q.CMD_EXPORT, i)
        p = Q.parse_blocks(data)
        d = same(p, ref) + ([] if cmd == Q.CMD_EXPORT and p.index == i and len(data) == 0x4bb else ['header'])
        if d:
            bad += 1
            if bad <= 3:
                print('  export %d: %s' % (i, '; '.join(d[:4])))
    print('export 0x10000002: %d presets, %d diferentes do Bank' % (len(bank), bad))
    fails += bad != 0

    cmd, data, flag = app.call(H_PRESET, Q.CMD_PRESET, 7)
    p = Q.parse_blocks(data)
    d = same(p, bank[cur], menu=False)
    print('preset em edição 0x10000000: "%s", %d blocos, igual ao Bank[%d]: %s %s' % (
        p.name, len(p.chain()), cur, not d, '; '.join(d[:4])))
    fails += bool(d)

    db = Q.default_db([path])
    Q.describe(p, db)
    fails += check_writes(app, bank, db)
    fails += check_chain(app, db)
    print('FALHOU' if fails else 'OK')
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
