#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 `libdmcan` 原生库（及其运行期依赖）从 dm-device-sdk 拷到本工程的 `dlls/`。

为什么要这一步：`dmcan_sdk`（Python 包）是纯 ctypes 封装，**不带原生库**；
Windows 上 `libdmcan.dll` 还依赖 `libusb-1.0.dll` / `libstdc++-6.dll` /
`libgcc_s_seh-1.dll` / `libwinpthread-1.dll`，这几个必须与它同目录。

用法::

    python tools/fetch_dmcan_lib.py                      # 自动找 dm-device-sdk
    python tools/fetch_dmcan_lib.py --sdk D:\\path\\dm-device-sdk
    python tools/fetch_dmcan_lib.py --source D:\\path\\dm-device-sdk\\Python\\demo\\dlls
    python tools/fetch_dmcan_lib.py --dry-run            # 只看会拷什么

@note 按 SDK 仓库的约定打印 SHA256：换了原生库后要确认四种语言目录里的
      `libdmcan` 是同一版（基准是 `C&C++/lib/<版本>/` 下那份）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DEST = os.path.join(PROJECT_ROOT, 'dlls')

#: 运行期需要的文件（Windows/MinGW 一套；Linux/macOS 见 find_sources）。
EXTRA_FILES = ('libusb-1.0.dll', 'libstdc++-6.dll', 'libgcc_s_seh-1.dll', 'libwinpthread-1.dll')


def sha256(path: str) -> str:
    """@brief 算文件 SHA256（用于跨目录比对版本）。"""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _library_name() -> str:
    """@brief 当前平台的原生库文件名。"""
    if sys.platform == 'win32':
        return 'libdmcan.dll'
    if sys.platform == 'darwin':
        return 'libdmcan.dylib'
    return 'libdmcan.so'


def _candidate_sources(sdk_root: Optional[str], explicit: Optional[str]) -> List[str]:
    """@brief 按优先级列出可能的原生库来源目录。"""
    out: List[str] = []
    if explicit:
        out.append(explicit)

    roots: List[str] = []
    if sdk_root:
        roots.append(sdk_root)
    env_root = os.environ.get('DMCAN_SDK_DIR')
    if env_root:
        roots.append(env_root)
    # 常见相对位置：与本工程同级的 dm-device-sdk / 上一级的 dm-device-sdk
    for base in (os.path.dirname(PROJECT_ROOT), PROJECT_ROOT,
                 os.path.dirname(os.path.dirname(PROJECT_ROOT))):
        for name in ('dm-device-sdk', 'dm-device-sdk.worktrees'):
            roots.append(os.path.join(base, name))

    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        # 1) C&C++ 发布包（版本基准）
        libs = os.path.join(root, 'C&C++', 'lib')
        if os.path.isdir(libs):
            for version in sorted(os.listdir(libs), reverse=True):
                for sub in (os.path.join('windows', 'mingw'), os.path.join('windows', 'msvc'),
                            'macos', 'linux'):
                    candidate = os.path.join(libs, version, sub)
                    if os.path.isdir(candidate):
                        out.append(candidate)
        # 2) Python demo 自带的 dlls/
        out.append(os.path.join(root, 'Python', 'demo', 'dlls'))
        # 3) 直接把 SDK 根当来源
        out.append(root)

    seen = set()
    unique: List[str] = []
    for item in out:
        key = os.path.normcase(os.path.abspath(item))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def find_sources(sdk_root: Optional[str], explicit: Optional[str]) -> List[Tuple[str, List[str]]]:
    """@brief 找到"有原生库的目录 -> 该目录里要拷的文件"列表。"""
    library = _library_name()
    found: List[Tuple[str, List[str]]] = []
    for directory in _candidate_sources(sdk_root, explicit):
        if not os.path.isdir(directory):
            continue
        main = os.path.join(directory, library)
        if not os.path.isfile(main):
            continue
        files = [library]
        for extra in EXTRA_FILES:
            if os.path.isfile(os.path.join(directory, extra)):
                files.append(extra)
        found.append((directory, files))
    return found


def main(argv: Optional[List[str]] = None) -> int:
    """@brief 拷贝原生库到 `dlls/`，并打印 SHA256 供跨目录比对。"""
    parser = argparse.ArgumentParser(description='把 libdmcan 及其依赖拷到本工程 dlls/')
    parser.add_argument('--sdk', help='dm-device-sdk 仓库根目录')
    parser.add_argument('--source', help='原生库所在目录（直接指定，优先级最高）')
    parser.add_argument('--dest', default=DEFAULT_DEST, help='目标目录（默认 <工程>/dlls）')
    parser.add_argument('--dry-run', action='store_true', help='只打印，不拷贝')
    args = parser.parse_args(argv)

    sources = find_sources(args.sdk, args.source)
    if not sources:
        print('没找到 %s。' % _library_name())
        print('请用 --source 指向 SDK 里带原生库的目录，例如：')
        print('  python tools/fetch_dmcan_lib.py --source <dm-device-sdk>/Python/demo/dlls')
        print('  python tools/fetch_dmcan_lib.py --source <dm-device-sdk>/C&C++/lib/v2.1.0/windows/mingw')
        return 1

    primary = sources[0]
    print('来源：%s' % primary[0])
    for other in sources[1:]:
        print('  （另外还看到：%s）' % other[0])

    if not args.dry_run:
        os.makedirs(args.dest, exist_ok=True)
    for name in primary[1]:
        source = os.path.join(primary[0], name)
        target = os.path.join(args.dest, name)
        digest = sha256(source)
        if args.dry_run:
            print('  [dry-run] %s -> %s  sha256=%s' % (name, target, digest[:16]))
        else:
            shutil.copy2(source, target)
            print('  %s -> %s  sha256=%s' % (name, target, digest[:16]))

    # 跨语言目录一致性检查（SDK 仓库的约定）
    if len(sources) > 1:
        library = _library_name()
        digests = {}
        for directory, files in sources:
            if library in files:
                digests[directory] = sha256(os.path.join(directory, library))
        distinct = set(digests.values())
        if len(distinct) > 1:
            print('\n⚠ 注意：这些目录里的 %s 不是同一版：' % library)
            for directory, digest in digests.items():
                print('    %s  %s' % (digest[:16], directory))
            print('  取的是第一个（%s）；要换版本请用 --source 明确指定。' % primary[0])
        else:
            print('\n四种语言目录里的 %s 是同一版（sha256 %s）。'
                  % (library, list(distinct)[0][:16]))

    if args.dry_run:
        return 0
    print('\n完成。设置 DMCAN_LIBRARY_PATH 可显式指定；本工程会自动找 dlls/。')
    print('自检：python -m dronecan_gui_tool.dmcan_cli doctor')
    return 0


if __name__ == '__main__':
    sys.exit(main())
