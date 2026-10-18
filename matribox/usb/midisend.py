"""Send MIDI to the pedal over its USB MIDI port (Windows, WinMM via ctypes; no extra packages).

    python -m matribox midi ports
    python -m matribox midi preset 02B          program change (with bank select CC 0) to preset 02B
    python -m matribox midi pc 5 [CH]           program change 5 (channel 1-16, default 1)
    python -m matribox midi cc 7 64 [CH]        control change 7 = 64
    python -m matribox midi tempo 135           CC 68 / 69: tempo 135 BPM
    python -m matribox midi clock 100 10        MIDI clock at 100 BPM for 10 s
    --port NAME picks another port (default: the one named Matribox / QME / Sonicake)

Close the PC editor and the Matribox Studio first: Windows lets one program at a time use the port.
"""
import argparse
import ctypes
import os
import sys
import time

from matribox.usb import htmidi


class Out:
    def __init__(self, want=None):
        self.mm = htmidi._winmm()
        _, outs = htmidi.list_ports()
        self.id = htmidi._pick(outs, want)
        self.name = outs[self.id]
        self.h = ctypes.c_void_p()
        r = self.mm.midiOutOpen(ctypes.byref(self.h), self.id, None, None, 0)
        if r:
            raise RuntimeError('midiOutOpen failed (%d): close the PC editor / Matribox Studio' % r)

    def short(self, *b):
        b = list(b) + [0] * (3 - len(b))
        self.mm.midiOutShortMsg(self.h, b[0] | b[1] << 8 | b[2] << 16)

    def close(self):
        self.mm.midiOutClose(self.h)


def preset_index(label):
    """'02B' -> 5 (banks of four, A-D)"""
    label = label.strip().upper()
    bank, letter = int(label[:-1]), label[-1]
    return (bank - 1) * 4 + 'ABCD'.index(letter)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd')
    ap.add_argument('args', nargs='*')
    ap.add_argument('--port')
    a = ap.parse_args()
    if a.cmd == 'ports':
        ins, outs = htmidi.list_ports()
        print('in: ', ins)
        print('out:', outs)
        return
    o = Out(a.port)
    print('port:', o.name)
    try:
        v = [int(x) if x.lstrip('-').isdigit() else x for x in a.args]
        if a.cmd == 'preset':
            i = preset_index(a.args[0])
            o.short(0xB0, 0, 1 if i >= 120 else 0)
            o.short(0xC0, i % 120)
            print('preset %s = index %d' % (a.args[0].upper(), i))
        elif a.cmd == 'pc':
            ch = (v[1] if len(v) > 1 else 1) - 1
            o.short(0xC0 | ch, v[0] & 0x7f)
        elif a.cmd == 'cc':
            ch = (v[2] if len(v) > 2 else 1) - 1
            o.short(0xB0 | ch, v[0] & 0x7f, v[1] & 0x7f)
        elif a.cmd == 'tempo':
            o.short(0xB0, 68, v[0] >> 7 & 0x7f)
            o.short(0xB0, 69, v[0] & 0x7f)
        elif a.cmd == 'clock':
            bpm, secs = v[0], v[1] if len(v) > 1 else 10
            step = 60.0 / bpm / 24
            t = time.perf_counter()
            end = t + secs
            n = 0
            while t < end:
                o.short(0xF8)
                n += 1
                t += step
                while time.perf_counter() < t:
                    pass
            print('%d clocks at %d BPM' % (n, bpm))
        else:
            sys.exit('unknown command ' + a.cmd)
        time.sleep(0.05)
    finally:
        o.close()


if __name__ == '__main__':
    main()
