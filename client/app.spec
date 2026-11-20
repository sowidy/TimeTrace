# -*- mode: python ; coding: utf-8 -*-
#
# 客户端打包配置（server/ 不参与打包）。
#
# 路径一律用 SPECPATH 拼绝对路径。SPECPATH 是 PyInstaller 在加载 spec 时注入
# 的全局变量，值是本 spec 所在目录（也就是 client/）。这样写的目的是让 spec
# 不依赖「当前工作目录」——从仓库根还是从 client/ 调用 PyInstaller 结果都一样。
# 之前用相对路径写法时，换个目录调用就会去找 client/client/main.py。
import os
import shutil
import sys
from pathlib import Path

_SPEC_DIR = SPECPATH

block_cipher = None

a = Analysis(
    # 入口脚本
    [os.path.join(_SPEC_DIR, 'main.py')],
    # pathex 告诉 PyInstaller「除了入口脚本所在目录，还要来 client/ 找模块」。
    # main.py 用的是 `from core.storage import ...` 这种以 client/ 为根的绝对
    # 导入（core / system / ui / net 都是 client/ 下的包），所以 client/ 必须
    # 在搜索路径上，否则打包时会报找不到模块。
    pathex=[_SPEC_DIR],
    binaries=[],
    datas=[
        # (源路径, 打包后放到的目录)。资源统一解到解包目录下的 assets/，
        # 这必须和 core/paths.py 里 asset_path() 的算法保持一致：
        # 那边是「__file__ 退两级 + assets/」，打包后 __file__ 指向
        # <解包根>/core/paths.pyc，退两级就是解包根，所以这里目标是 'assets'。
        (os.path.join(_SPEC_DIR, 'assets', '图标.png'), 'assets'),
        # 配置模板跟着 exe 一起发出去。目标 'assets' 之外的这一条是放到
        # 解包根（onedir 模式下就是 exe 所在的那一层），正好是
        # core/paths.py 里 env_file_candidates() 第一个去找的位置 ——
        # 用户把它改名成 .env 就能用，不用去猜有哪些配置项。
        # 注意发出去的是 .env.example 而不是 .env：默认值不该被一次打包定死。
        (os.path.join(_SPEC_DIR, '.env.example'), '.'),
    ],
    hiddenimports=[
        'win32gui',
        'win32ui',
        'win32process',
        'win32api',
        'win32con',
        'win32com.client',
        'pycaw',
        'pycaw.pycaw',
        'comtypes',
        'comtypes.client',
        'psutil',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageFont',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'PySide6.QtWebSockets',
        'PySide6.QtNetwork',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'pytest',
        'unittest',
        'tkinter',
        'xmlrpc',
        'pydoc',
        # ⚠️ 这里**不能**排除 'email' 和 'http'。
        # net/uploader.py 用标准库的 urllib.request 上传数据，而它会链式
        # 导入 http.client 和 email.message（urllib 内部用 email 来解析
        # 头部）。之前为了减体积把它们写在排除列表里，上传功能一加就会
        # 打包后报 ModuleNotFoundError: No module named 'http' —— 而且只在
        # 打包版复现，源码跑得好好的，很难查。体积上多这两个包大约 1MB，
        # 换取功能正确，值。
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PcTime',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    # 绝对路径：PyInstaller 对相对路径是拿 workpath 去拼的，容易找错地方。
    # app.ico 由 tools/generate_icon.py 生成到 client/ 下。
    icon=os.path.join(_SPEC_DIR, 'app.ico'),)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PcTime',
)

# Keep only extension runtime files next to the executable, where users can load it.
_extension_output = os.path.join(DISTPATH, 'PcTime', 'browser-extension')
os.makedirs(_extension_output, exist_ok=True)
for _filename in ('manifest.json', 'options.html', 'options.js', 'protocol.js',
                  'service-worker.js', 'README.md'):
    shutil.copy2(os.path.join(_SPEC_DIR, 'browser_extension', 'chromium', _filename),
                 os.path.join(_extension_output, _filename))
shutil.copy2(os.path.join(_SPEC_DIR, 'browser_extension', 'chromium', 'PRIVACY.md'),
             os.path.join(_extension_output, 'PRIVACY.md'))

# Preserve reference-project notices and the licenses of the actual build dependencies.
sys.path.insert(0, os.path.join(_SPEC_DIR, 'tools'))
from collect_licenses import stage
stage(Path(DISTPATH) / 'PcTime', ('PySide6', 'Pillow', 'psutil', 'pywin32', 'PyInstaller'))
for _legal_name in ('LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md'):
    shutil.copy2(os.path.join(_SPEC_DIR, 'browser_extension', 'chromium', _legal_name),
                 os.path.join(_extension_output, _legal_name))
shutil.copytree(os.path.join(_SPEC_DIR, 'browser_extension', 'chromium', 'licenses'),
                os.path.join(_extension_output, 'licenses'), dirs_exist_ok=True)
