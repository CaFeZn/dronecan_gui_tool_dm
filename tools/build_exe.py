#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键打 Windows 免安装 exe（PyInstaller）。

做三件事：

1. 确认 `dlls/` 里有 `libdmcan` 及其依赖（没有就自动跑 `fetch_dmcan_lib.py`）；
2. 用 `tools/dmcan-gui-tool.spec` 打包出 `dist/dronecan-gui-tool-dm/`；
3. 把 `dlls/` 拷到 exe 旁边——`libdmcan.dll` **必须**和它的 4 个依赖同目录，
   而 dmsdk 在冻结后会优先找 `<exe目录>/dlls/`。

用法::

    python tools/build_exe.py            # 完整打包
    python tools/build_exe.py --no-clean # 复用上次的缓存（快很多）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(PROJECT_ROOT, 'tools', 'dmcan-gui-tool.spec')
DLL_DIR = os.path.join(PROJECT_ROOT, 'dlls')
DIST_DIR = os.path.join(PROJECT_ROOT, 'dist')
OUT_DIR = os.path.join(DIST_DIR, 'dronecan-gui-tool-dm')
BUILD_DIR = os.path.join(PROJECT_ROOT, 'build')

#: 运行期必须与 libdmcan 同目录的依赖
REQUIRED_DLLS = ('libdmcan.dll', 'libusb-1.0.dll', 'libstdc++-6.dll',
                 'libgcc_s_seh-1.dll', 'libwinpthread-1.dll')


def ensure_native_libs() -> None:
    """@brief 确认原生库齐全，缺了就自动从 SDK 取。"""
    missing = [name for name in REQUIRED_DLLS
               if not os.path.isfile(os.path.join(DLL_DIR, name))]
    if not missing:
        print('[build] 原生库就绪：%s' % DLL_DIR)
        return
    print('[build] 缺 %s，先跑 tools/fetch_dmcan_lib.py' % ', '.join(missing))
    subprocess.check_call([sys.executable, os.path.join(PROJECT_ROOT, 'tools',
                                                        'fetch_dmcan_lib.py')])
    still = [name for name in REQUIRED_DLLS
             if not os.path.isfile(os.path.join(DLL_DIR, name))]
    if still:
        raise SystemExit('原生库仍缺失：%s' % ', '.join(still))


def main(argv=None) -> int:
    """@brief 打包并把原生库摆到 exe 旁边。"""
    parser = argparse.ArgumentParser(description='打 Windows 免安装 exe')
    parser.add_argument('--no-clean', action='store_true', help='保留上次的构建缓存（更快）')
    parser.add_argument('--skip-lib-check', action='store_true', help='不检查/不获取原生库')
    args = parser.parse_args(argv)

    if not args.skip_lib_check:
        ensure_native_libs()

    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm',
               '--distpath', DIST_DIR, '--workpath', BUILD_DIR]
    if not args.no_clean:
        command.append('--clean')
    command.append(SPEC)

    print('[build] %s' % ' '.join(command))
    result = subprocess.call(command, cwd=PROJECT_ROOT)
    if result != 0:
        return result

    target = os.path.join(OUT_DIR, 'dlls')
    shutil.copytree(DLL_DIR, target, dirs_exist_ok=True)
    print('[build] 原生库已放到 exe 旁边：%s' % target)

    gui_exe = os.path.join(OUT_DIR, 'dronecan-gui-tool-dm.exe')
    cli_exe = os.path.join(OUT_DIR, 'dmcan-dronecan-tool.exe')
    for path in (gui_exe, cli_exe):
        if not os.path.isfile(path):
            raise SystemExit('打包成功但没找到 %s' % path)

    size = sum(os.path.getsize(os.path.join(root, name))
               for root, _dirs, files in os.walk(OUT_DIR) for name in files)
    print('')
    print('[build] 完成：%s（%.0f MB）' % (OUT_DIR, size / 1e6))
    print('[build]   GUI：%s' % gui_exe)
    print('[build]   CLI：%s' % cli_exe)
    print('[build] 自检：%s doctor' % cli_exe)
    return 0


if __name__ == '__main__':
    sys.exit(main())
