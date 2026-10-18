"""Cross toolchain: Zig's bundled clang + lld are the ARM compiler and linker (no arm-none-eabi-gcc needed)."""
import glob, os, shutil
from matribox import paths


def find_zig():
    """$ZIG, else a zig/ folder next to the Studio executable, else zig on PATH, else D:\\DevTools\\zig"""
    bundled = os.path.join(paths.USER_ROOT, 'zig', 'zig.exe')
    return (os.environ.get('ZIG') or (bundled if os.path.exists(bundled) else None) or shutil.which('zig')
            or (glob.glob(r'D:\DevTools\zig\*\zig.exe') + ['zig'])[0])


ZIG = find_zig()

# C code linked into the original app (matribox.app.apppatch): soft-float Thumb-2, like the app's own calls
APP_CFLAGS = ['-target', 'thumb-freestanding-eabi', '-mcpu=cortex_m7', '-mfloat-abi=soft', '-O2',
              '-ffreestanding', '-fno-builtin', '-fno-pic', '-ffunction-sections', '-fdata-sections',
              '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-fno-stack-protector',
              '-fno-sanitize=all', '-Wall', '-Wextra', '-Wno-unused-parameter']
FLASH_BASE = 0x20000                    # the MCU section is stored in flash from here
