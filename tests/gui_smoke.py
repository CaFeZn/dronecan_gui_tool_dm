# -*- coding: utf-8 -*-
"""
离屏 GUI 冒烟测试：不需要显示器，跑完会截图。

用法::

    python tests/gui_smoke.py                       # 用 dmcan:0.0
    python tests/gui_smoke.py "dmcan:0.1@1000000"
    python tests/gui_smoke.py --mock                # 用仿真 SDK，不需要硬件

做三件事，任何一件失败都以非 0 退出：

1. 按 gui_tool 的方式建一个 dmcan 后端的 DroneCAN 节点；
2. 起主窗口并跑几百毫秒事件循环（验证 `main.py`/`setup_window.py` 的接入没坏）；
3. 打开 dmcan 适配器控制面板，截图主窗口与面板到 `artifacts/`。
"""

import argparse
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')      # 必须在建 QApplication 之前

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

ARTIFACTS = os.path.join(PROJECT_ROOT, 'artifacts')


def dump_panel(panel) -> None:
    """@brief 把面板里真正渲染出来的文本打出来（比看截图更可靠，也能进 CI 日志）。"""
    print('[smoke] ---- 面板内容 ----')
    print('[smoke] 设备信息：')
    for key, label in getattr(panel, '_labels', {}).items():
        print('[smoke]   %-9s %s' % (key, label.text()))
    headline = getattr(panel, '_headline', None)
    if headline is not None:
        plain = headline.text().replace('<br>', ' / ')
        while '<' in plain and '>' in plain:      # 粗暴去掉富文本标签，只为日志可读
            start, end = plain.find('<'), plain.find('>')
            if start < 0 or end < start:
                break
            plain = plain[:start] + plain[end + 1:]
        print('[smoke] 大字汇总：%s' % plain)
    print('[smoke] 统计表：')
    for row in range(panel._table.rowCount()):
        cells = []
        for column in range(panel._table.columnCount()):
            item = panel._table.item(row, column)
            cells.append(item.text() if item is not None else '')
        if not any(cells):
            continue
        print('[smoke]   %-24s %12s %10s' % tuple(cells))
    print('[smoke] ---- 面板内容结束 ----')


def main() -> int:
    """@brief 跑一遍冒烟流程并截图。"""
    parser = argparse.ArgumentParser(description='离屏 GUI 冒烟测试（带截图）')
    parser.add_argument('iface', nargs='?', default='dmcan:0.0', help='接口串')
    parser.add_argument('--mock', action='store_true', help='用仿真 dmcan SDK（不需要硬件）')
    parser.add_argument('--seconds', type=float, default=3.0, help='事件循环时长')
    parser.add_argument('--theme', choices=('light', 'dark', 'system'), default='system',
                        help='强制浅色/深色调色板（默认跟随系统）')
    args = parser.parse_args()

    if args.mock:
        from tests import fake_dmcan
        fake_dmcan.install([fake_dmcan.make_dual(serial='MOCKDEV', index=0)])
        args.iface = 'dmcan:s:MOCKDEV.0'
        print('[smoke] 使用仿真 dmcan SDK')
    else:
        # 真机：先装原生库（没装会由 dmsdk 直接报出原因）
        from dronecan_gui_tool.driver import dmsdk
        status = dmsdk.probe()
        print('[smoke] %s' % status.describe())
        if not status.available:
            print('[smoke] 建议：%s' % status.hint)
            return 1

    # 先让 main.py 的 argparse 看到干净的 argv（它的参数在 import 时就解析了）
    sys.argv = ['dronecan_gui_tool', '--interface', args.iface]

    import dronecan
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    import dronecan_gui_tool.main as gui

    app = QApplication(['dronecan_gui_tool'])
    app.setStyle('Fusion')
    if args.theme != 'system':
        from tests.theme_palettes import apply_palette
        apply_palette(args.theme == 'dark')
        from dronecan_gui_tool.widgets import theme
        print('[smoke] 调色板：%s（表底 %s）' % (args.theme, theme.background_color().name()))

    iface_kwargs = dict(baudrate=gui.args.baudrate, bitrate=gui.args.bitrate,
                        bus_number=gui.args.bus, filtered=bool(gui.args.filtered),
                        mavlink_source_system=gui.args.source_system,
                        mavlink_target_system=gui.args.target_system,
                        mavlink_signing_key='')

    node_info = dronecan.uavcan.protocol.GetNodeInfo.Response()
    node_info.name = gui.NODE_NAME
    node_info.software_version.major = gui.__version__[0]
    node_info.software_version.minor = gui.__version__[1]

    print('[smoke] 连接 %s …' % args.iface)
    node = gui.make_extended_node(
        args.iface, node_info=node_info,
        mode=dronecan.uavcan.protocol.NodeStatus().MODE_OPERATIONAL, **iface_kwargs)
    node.spin(0.1)
    print('[smoke] 节点已建立，驱动 %s' % type(node.can_driver).__name__)

    window = gui.MainWindow(node, args.iface, iface_kwargs)
    window.resize(1400, 900)
    window.show()

    os.makedirs(ARTIFACTS, exist_ok=True)
    results = []

    def shoot_main():
        path = os.path.join(ARTIFACTS, 'main_window.png')
        window.grab().save(path)
        results.append(path)
        print('[smoke] 主窗口截图：%s' % path)
        panel = gui.spawn_can_adapter_control_panel  # noqa: F841  (只是提醒下面用的是 dmcan 面板)
        from dronecan_gui_tool.widgets.dmcan_control_panel import spawn_window
        global _panel
        _panel = spawn_window(window, node, args.iface)
        QTimer.singleShot(int(args.seconds * 500), shoot_panel)

    def shoot_panel():
        window.grab().save(os.path.join(ARTIFACTS, 'main_window_with_panel.png'))
        path = os.path.join(ARTIFACTS, 'dmcan_panel.png')
        _panel.grab().save(path)
        results.append(path)
        print('[smoke] 面板截图：%s' % path)
        dump_panel(_panel)
        exercise_appearance_menu()
        app.quit()

    def exercise_appearance_menu() -> None:
        """
        @brief 把 View -> Appearance 菜单真点一遍：既验证菜单接上了，也产出两种主题的截图。

        @note 用仿真后端也能跑，不需要硬件。
        """
        from PyQt6.QtCore import QSettings
        from dronecan_gui_tool.widgets import theme

        # 精确还原：原先没有这个键就删掉，别给用户留一个"测试选的主题"
        settings = QSettings(theme.SETTINGS_ORGANIZATION, theme.SETTINGS_APPLICATION)
        previous_value = settings.value(theme.SETTINGS_KEY, None)
        window_menu = None
        for action in window.menuBar().actions():
            if action.text().replace('&', '') == 'View':
                window_menu = action.menu()
        assert window_menu is not None, '主窗口没有 View 菜单'

        appearance_menu = None
        for action in window_menu.actions():
            if action.menu() is not None and action.text().replace('&', '') == 'Appearance':
                appearance_menu = action.menu()
        assert appearance_menu is not None, 'View 下面没有 Appearance 子菜单'

        entries = {a.text().replace('&', ''): a for a in appearance_menu.actions()}
        print('[smoke] Appearance 菜单项：%s' % list(entries))
        for label, expected in (('Dark', theme.MODE_DARK), ('Light', theme.MODE_LIGHT)):
            assert label in entries, '菜单里没有 %r' % label
            entries[label].trigger()
            app.processEvents()
            print('[smoke] 点 %s -> 生效 %s，表格底色 %s'
                  % (label, theme.mode(), theme.background_color().name()))
            assert theme.mode() == expected, '%s 没有生效' % label
            path = os.path.join(ARTIFACTS, 'appearance_%s.png' % expected)
            window.grab().save(path)
            results.append(path)

        entries['Follow System'].trigger()
        app.processEvents()
        assert theme.mode() == theme.MODE_SYSTEM, '切不回跟随系统'

        if previous_value is None:
            settings.remove(theme.SETTINGS_KEY)
        else:
            settings.setValue(theme.SETTINGS_KEY, previous_value)
        settings.sync()
        print('[smoke] 菜单切换正常，偏好已还原为 %r' % previous_value)

    QTimer.singleShot(int(args.seconds * 1000), shoot_main)
    app.exec()

    node.close()
    print('[smoke] 完成，产出：%s' % ', '.join(results))
    return 0 if len(results) >= 2 else 1


if __name__ == '__main__':
    sys.exit(main())
