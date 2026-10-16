# -*- coding: utf-8 -*-
"""
生成 README 用的界面截图（输出到 docs/images/）。

**必须用真实平台渲染（Windows / X11）**：`QT_QPA_PLATFORM=offscreen` 时 Qt 加载不到字体库
（`QFontDatabase.families()` 返回 0），中文会全部渲染成**方框**——截图直接是乱码。
本脚本会在 offscreen 下直接拒绝运行。

用法::

    python tools/make_readme_images.py            # 两张都出
    python tools/make_readme_images.py --panel    # 只出适配器面板
    python tools/make_readme_images.py --monitor  # 只出总线监视器（深色）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_DIR = os.path.join(PROJECT_ROOT, 'docs', 'images')


def check_platform() -> None:
    """@brief 拒绝在无字体库的平台上渲染（否则中文是方框）。"""
    if os.environ.get('QT_QPA_PLATFORM', '').lower() == 'offscreen':
        raise SystemExit('QT_QPA_PLATFORM=offscreen 时没有字体库，中文会变方框；'
                         '请改用真实平台（Windows 下删掉该环境变量即可）。')


def ink_ratio(image) -> float:
    """@brief 深色像素占比 —— 用来确认"图里确实画了东西"，不是一张白纸。"""
    width, height = image.width(), image.height()
    dark = total = 0
    for y in range(0, height, 2):
        for x in range(0, width, 2):
            total += 1
            if image.pixelColor(x, y).lightness() < 170:
                dark += 1
    return 100.0 * dark / max(1, total)


def render_panel() -> str:
    """@brief 渲染达妙适配器控制面板（仿真 SDK，不需要硬件）。"""
    sys.path.insert(0, PROJECT_ROOT)
    from PyQt6.QtWidgets import QApplication, QMainWindow
    from tests import fake_dmcan

    fake_dmcan.install([fake_dmcan.make_dual(serial='MOCKDEV', index=0)])

    import dronecan
    import dronecan_gui_tool.main as gui
    from dronecan_gui_tool.widgets.dmcan_control_panel import spawn_window

    app = QApplication(['make-readme-images'])
    app.setStyle('Fusion')

    iface = 'dmcan:s:MOCKDEV.0'
    iface_kwargs = dict(baudrate=115200, bitrate=1000000, bus_number=1, filtered=False,
                        mavlink_source_system=250, mavlink_target_system=0,
                        mavlink_signing_key='')
    node_info = dronecan.uavcan.protocol.GetNodeInfo.Response()
    node_info.name = gui.NODE_NAME
    node_info.software_version.major = gui.__version__[0]
    node_info.software_version.minor = gui.__version__[1]

    node = gui.make_extended_node(iface, node_info=node_info, mode=0, **iface_kwargs)
    node.spin(0.1)

    parent = QMainWindow()
    parent.resize(1200, 900)
    parent.show()
    panel = spawn_window(parent, node, iface)
    panel.resize(1020, 900)
    panel.move(30, 30)

    for _ in range(60):
        app.processEvents()
        time.sleep(0.02)

    image = panel.grab().toImage()
    # 裁掉底部大片留白：以"这一行有 >25 个深色像素"作为还有内容的判据
    last = 0
    for y in range(image.height()):
        row_dark = sum(1 for x in range(0, image.width(), 3)
                       if image.pixelColor(x, y).lightness() < 170)
        if row_dark > 25:
            last = y
    bottom = min(image.height(), last + 26)
    image = image.copy(0, 0, image.width(), bottom)

    os.makedirs(IMAGES_DIR, exist_ok=True)
    path = os.path.join(IMAGES_DIR, 'adapter-panel.png')
    image.save(path)
    print('[images] %s  %dx%d  深色像素 %.2f%%' % (path, image.width(), image.height(),
                                                   ink_ratio(image)))
    node.close()
    return path


def render_bus_monitor() -> str:
    """@brief 渲染总线监视器（深色主题，复用 tests/bus_monitor_render.py）。"""
    path = os.path.join(IMAGES_DIR, 'bus-monitor-dark.png')
    env = dict(os.environ)
    env.pop('QT_QPA_PLATFORM', None)          # 关键：用真实平台，否则中文是方框
    subprocess.check_call([sys.executable, os.path.join(PROJECT_ROOT, 'tests',
                                                        'bus_monitor_render.py'),
                           '--theme', 'dark', '--out', path],
                          cwd=PROJECT_ROOT, env=env)
    print('[images] %s' % path)
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='生成 README 截图')
    parser.add_argument('--panel', action='store_true', help='只出适配器面板')
    parser.add_argument('--monitor', action='store_true', help='只出总线监视器')
    args = parser.parse_args(argv)

    check_platform()
    if not args.panel and not args.monitor:
        args.panel = args.monitor = True
    if args.panel:
        render_panel()
    if args.monitor:
        render_bus_monitor()
    return 0


if __name__ == '__main__':
    sys.exit(main())
