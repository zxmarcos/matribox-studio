"""Where the tools find the stock firmware, write their builds and look for optional local resources.

Defaults live inside the repository: firmware/ (put the official QME-200_FW_USB_V1.2.2.bin there) and build/.
Both are ignored by git. A workspace.json at the repository root (also ignored) can point them elsewhere and
name optional resources; relative paths in it are relative to the repository root:

    {
      "firmware_dir": "../research/firmware",       stock firmware images
      "build_dir": "../builds",                     everything the tools generate
      "editor_dir": "../editor_patched/Matribox II PRO",   a copy of the PC editor (its alg_data.json)
      "nam_dir": "...", "ir_dir": "...",            start folders of the Studio's NAM and IR tabs
      "base_dirs": ["..."],                         more folders where the Studio looks for base images
      "catalog_dirs": ["..."],                      module catalogs outside examples/ (editor list origins)
      "htusbtools": ".../HTUSBTools.dll",           the PC editor's DLL (tests/test_qmeupdate.py)
      "nam_ref_render": ".../ref_render.exe"        NeuralAmpModelerCore reference renderer (nam/tests)
    }

Environment variables win over the file: MATRIBOX_FW (the stock image itself), MATRIBOX_FIRMWARE_DIR,
MATRIBOX_BUILD_DIR.

In the Windows build of the Studio (PyInstaller, packaging/build_exe.py) the SDK files are inside the bundle,
and firmware/, build/ and workspace.json are next to the executable instead.
"""
import json, os, sys

PACKAGE = os.path.dirname(os.path.abspath(__file__))
SDK_ROOT = os.path.dirname(PACKAGE)
FROZEN = bool(getattr(sys, 'frozen', False))
USER_ROOT = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else SDK_ROOT   # firmware/, build/, config
DATA = os.path.join(PACKAGE, 'data')
SDK_C = os.path.join(SDK_ROOT, 'sdk')                  # module ABI header, runtime, linker script
NAM_SRC = os.path.join(SDK_ROOT, 'nam')                # A2-Lite C sources
EXAMPLES = os.path.join(SDK_ROOT, 'examples')
ORIGINAL_NAME = 'QME-200_FW_USB_V1.2.2.bin'
EDITOR_JSON_IN_EDITOR = os.path.join('data', 'flutter_assets', 'assets', 'alg', 'alg_data.json')
INSTALLED_EDITOR = r'C:\Program Files\Matribox II PRO'


def _load_conf():
    try:
        with open(os.path.join(USER_ROOT, 'workspace.json'), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_CONF = _load_conf()


def _abs(p):
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(USER_ROOT, p))


def setting(key, default=None):
    """a path from workspace.json (absolute), a list of paths, or `default`"""
    v = _CONF.get(key)
    if v is None:
        return default
    return [_abs(p) for p in v] if isinstance(v, list) else _abs(v)


FIRMWARE_DIR = _abs(os.environ.get('MATRIBOX_FIRMWARE_DIR') or _CONF.get('firmware_dir') or 'firmware')
BUILD_DIR = _abs(os.environ.get('MATRIBOX_BUILD_DIR') or _CONF.get('build_dir') or 'build')


def original_firmware():
    """the stock V1.2.2 image (not part of the repository: every user brings their own copy)"""
    return _abs(os.environ['MATRIBOX_FW']) if os.environ.get('MATRIBOX_FW') else \
        os.path.join(FIRMWARE_DIR, ORIGINAL_NAME)


def build_path(*parts):
    return os.path.join(BUILD_DIR, *parts)


def editor_json_candidates():
    """alg_data.json of the configured editor copy, then of the installed editor"""
    out = []
    d = setting('editor_dir')
    if d:
        out.append(os.path.join(d, EDITOR_JSON_IN_EDITOR))
    out.append(os.path.join(INSTALLED_EDITOR, EDITOR_JSON_IN_EDITOR))
    return out


def default_editor_json():
    return next((p for p in editor_json_candidates() if os.path.exists(p)), editor_json_candidates()[0])
