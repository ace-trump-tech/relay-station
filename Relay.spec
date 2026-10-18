# Windows x64 one-file bundle. Persistent data is never collected from data/.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH)
a = Analysis(
    [str(root / 'desktop.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / 'static'), 'static'), (str(root / 'assets'), 'assets'),
           (str(root / 'LICENSE'), '.'), (str(root / 'NOTICE.txt'), '.'),
           (str(root / 'packaging' / 'THIRD_PARTY_LICENSES.txt'), '.')]
           + collect_data_files('tzdata') + collect_data_files('webview'),
    hiddenimports=['uvicorn.logging', 'uvicorn.loops.asyncio',
                   'uvicorn.protocols.http.h11_impl', 'uvicorn.lifespan.on',
                   'webview.platforms.winforms', 'webview.platforms.edgechromium',
                   'pystray._win32'] + collect_submodules('clr_loader'),
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'IPython', 'matplotlib',
              'numpy', 'pandas', 'pytest', 'cefpython3', 'webview.platforms.qt',
              'webview.platforms.gtk', 'webview.platforms.cocoa'],
    noarchive=False, optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='Relay',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=True,
    icon=str(root / 'assets' / 'relay.ico'),
    version=str(root / 'packaging' / 'windows-version.txt'),
)
