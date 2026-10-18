"""Build the Windows version of Matribox Studio with PyInstaller.

    pip install pyinstaller capstone unicorn
    python examples/build_all.py              # the example modules go into the bundle as catalogs
    python packaging/build_exe.py [--zig DIR] [--zip OUT.zip]

Output: dist/MatriboxStudio/ (MatriboxStudio.exe + _internal/). Next to the executable, as in a repository
checkout: firmware/ (put the official QME-200_FW_USB_V1.2.2.bin there), build/ (outputs), an optional
workspace.json, and zig/ when --zig is given (a Zig install: "Gerar base do original" compiles the app patches
with it; without it, Zig must be on the PATH). unicorn goes into the bundle: the Ferramentas tab's ports run
every converted effect in the emulator.
"""
import argparse, glob, os, shutil, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, 'dist')
APP = 'MatriboxStudio'


def data(src, dst):
    return '--add-data=%s%s%s' % (os.path.join(ROOT, src), os.pathsep, dst)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--zig', help='Zig install folder to copy next to the executable')
    ap.add_argument('--zip', help='also write the folder as this zip file')
    a = ap.parse_args()
    if not glob.glob(os.path.join(ROOT, 'examples', 'parallel', '*.axf')):
        sys.exit('build the examples first: python examples/build_all.py')
    import PyInstaller.__main__
    ignore = shutil.ignore_patterns('build', '__pycache__', '*.o', '*.py', '*.nam')
    stage = os.path.join(ROOT, 'build', 'pyinstaller')
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, 'examples'), os.path.join(stage, 'examples'), ignore=ignore)
    PyInstaller.__main__.run([
        os.path.join(ROOT, 'packaging', 'studio_main.py'),
        '--name', APP, '--windowed', '--noconfirm', '--clean',
        '--distpath', DIST, '--workpath', os.path.join(stage, 'work'), '--specpath', stage,
        '--paths', ROOT,                    # the package from the checkout (an editable install is not followed)
        '--collect-submodules', 'matribox', '--collect-all', 'capstone', '--collect-all', 'unicorn',
        data('matribox/data', 'matribox/data'),
        data('matribox/app/patches', 'matribox/app/patches'),
        data('matribox/studio/fonts', 'matribox/studio/fonts'),
        data('sdk', 'sdk'),
        '--add-data=%s%sexamples' % (os.path.join(stage, 'examples'), os.pathsep),
        data('LICENSE', '.'),
    ])
    out = os.path.join(DIST, APP)
    uc = os.path.join(out, '_internal', 'unicorn')          # link-time files: 50 MB the program never loads
    shutil.rmtree(os.path.join(uc, 'include'), ignore_errors=True)
    for f in glob.glob(os.path.join(uc, 'lib', '*.lib')):
        os.remove(f)
    for d in ('firmware', 'build'):
        os.makedirs(os.path.join(out, d), exist_ok=True)
    open(os.path.join(out, 'firmware', 'PUT_THE_OFFICIAL_FIRMWARE_HERE.txt'), 'w').write(
        'Copy the official QME-200_FW_USB_V1.2.2.bin from Sonicake into this folder.\n')
    shutil.copy(os.path.join(ROOT, 'LICENSE'), out)
    if a.zig:
        shutil.copytree(a.zig, os.path.join(out, 'zig'), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('doc', 'docs'))
    if a.zip:
        base = os.path.splitext(os.path.abspath(a.zip))[0]
        shutil.make_archive(base, 'zip', DIST, APP)
        print('wrote', base + '.zip')
    print('built', out)


if __name__ == '__main__':
    main()
