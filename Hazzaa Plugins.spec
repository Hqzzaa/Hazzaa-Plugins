from pathlib import Path
import tkinter
import _tkinter
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
assets = root / 'assets'
tcl = Path(tkinter.Tcl().eval('info library'))
tk = tcl.parent / ('tk' + _tkinter.TK_VERSION)
tcl8 = tcl.parent / 'tcl8'
if not all(path.is_dir() for path in (tcl, tk, tcl8)):
    raise RuntimeError('Install Python with the Tcl/Tk component before building.')

datas = [(str(Path(tkinter.__file__).parent), 'tkinter'),
         (str(tcl), '_tcl_data'), (str(tk), '_tk_data'), (str(tcl8), 'tcl8')]
for name in ['cat_logo.png', 'bat_cursor.ani', 'bat_cursor.cur', 'app_icon.ico',
             'installed-package-index.json', 'UNRAR-LICENSE.txt']:
    datas.append((str(assets / name), 'assets'))
datas.append((str(root / 'licenses'), 'licenses'))
binaries = [(str(assets / 'UnRAR.exe'), 'assets')]
hiddenimports = ['rarfile', 'tkinter', '_tkinter']
pil_data, pil_binaries, pil_imports = collect_all('PIL')
datas += pil_data
binaries += pil_binaries
hiddenimports += pil_imports

a = Analysis([str(root / 'ae_archive_deployer.py')], pathex=[str(root)],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             hookspath=[], hooksconfig={}, runtime_hooks=[str(root / 'pyi_rth_tcl.py')],
             excludes=[], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='Hazzaa Plugins',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, uac_admin=True,
          version=str(assets / 'version_info.txt'), icon=[str(assets / 'app_icon.ico')])
