"""Artifacts of the stock firmware V1.2.2, derived on demand from the user's own copy of the official image.

Nothing from the vendor's firmware is part of this repository. Tests and emulators that need the original app or
a factory effect module get it from here: the app is rebuilt as an ELF (matribox.firmware.mkappelf) and the
modules come straight out of the effect library, both identical to what the device runs.
"""
import os
from matribox import paths
from matribox.firmware import fwtool, mkappelf

_fw = {}


def path():
    p = paths.original_firmware()
    if not os.path.exists(p):
        raise FileNotFoundError('stock firmware not found: %s (put the official %s in %s, or set MATRIBOX_FW)'
                                % (p, paths.ORIGINAL_NAME, paths.FIRMWARE_DIR))
    return p


def data():
    return open(path(), 'rb').read()


def firmware():
    p = path()
    if p not in _fw:
        _fw[p] = fwtool.Firmware(open(p, 'rb').read())
    return _fw[p]


def app_elf():
    """the original app as an ELF (ITCM + SDRAM images, scatter-loaded), cached in the build folder"""
    src = path()
    out = paths.build_path('stock', 'app.elf')
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(src):
        os.makedirs(os.path.dirname(out), exist_ok=True)
        mkappelf.make(open(src, 'rb').read(), out, quiet=True)
    return out


def factory_entry(menu, slot):
    """library entry `slot` of menu `menu` (the order of the pedal's lists)"""
    lib = firmware().library
    return lib.entries[lib.cat_entries[menu][slot]]


def factory_module(menu, slot):
    """the module ELF of a factory effect"""
    return factory_entry(menu, slot).elf
