# -*- coding: utf-8 -*-
"""深浅主题下的颜色/图标可读性测试（WCAG 对比度）。

背景：Qt 6.5+ 会跟随系统深色主题，Fusion 深色调色板的表格底色是 `#2d2d2d`。
上游 gui_tool 里若干颜色是按"白底深字"硬编码的（RGB 取反、从白往黑插值），
落到深色底上就变成"深底深字"——现场那张深色截图里 **73.5% 的文字像素对比度 < 3:1**。
本测试把这些颜色的对比度卡死在 WCAG AA（正文 4.5:1，次要信息 3:1）以上。
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import QApplication
except ImportError:                                          # pragma: no cover
    QApplication = None

from dronecan_gui_tool.widgets import theme                   # noqa: E402
from tests.theme_palettes import apply_palette                # noqa: E402


@unittest.skipIf(QApplication is None, '没有 PyQt6')
class ThemeContrastTest(unittest.TestCase):
    """两种主题下，所有"生成出来的"文字颜色都要够清楚。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(['theme-test'])

    def _frame(self, can_id, ts_real, data=b'\x01\x02\x03\x04\x05\x06\x07'):
        from dronecan.driver.common import CANFrame
        return CANFrame(can_id, data, True, ts_monotonic=ts_real, ts_real=ts_real)

    def test_map_7bit_to_color_all_values(self):
        """Src / Dst / Data Type 用的 `map_7bit_to_color`：128 个取值全部达标。"""
        from dronecan_gui_tool.widgets import map_7bit_to_color
        for dark in (False, True):
            apply_palette(dark)
            background = theme.background_color()
            with self.subTest(theme='dark' if dark else 'light'):
                worst = None
                for value in range(128):
                    color = map_7bit_to_color(value)
                    ratio = theme.contrast_ratio(color, background)
                    if worst is None or ratio < worst[1]:
                        worst = (value, ratio)
                    self.assertGreaterEqual(ratio, 4.5,
                                            '取值 %d -> %s 对比度只有 %.2f' % (value, color.name(), ratio))
                print('    map_7bit_to_color 最差：取值 %d 对比度 %.2f（%s）'
                      % (worst[0], worst[1], 'dark' if dark else 'light'))

    def test_can_id_colors(self):
        """CAN ID 列：优先级 0..31 全部达标。"""
        from dronecan_gui_tool.widgets.bus_monitor.window import colorize_can_id
        for dark in (False, True):
            apply_palette(dark)
            background = theme.background_color()
            for priority in range(32):
                frame = self._frame((priority << 24) | (1 << 8) | 12, 0.0)
                color = colorize_can_id(frame)
                with self.subTest(theme='dark' if dark else 'light', priority=priority):
                    self.assertGreaterEqual(theme.contrast_ratio(color, background), 4.5)

    def test_data_hex_colors(self):
        """Data Hex / Data ASCII 列（`colorize_transfer_id`）——深色下原来的重灾区。"""
        from dronecan_gui_tool.widgets.bus_monitor.window import colorize_transfer_id
        for dark in (False, True):
            apply_palette(dark)
            background = theme.background_color()
            worst = None
            for can_hi in range(16):
                for tail in range(256):
                    frame = self._frame((can_hi << 16) | 0x1234, 0.0, bytes([tail]))
                    color = colorize_transfer_id(frame)
                    if color is None:
                        continue
                    ratio = theme.contrast_ratio(color, background)
                    if worst is None or ratio < worst[1]:
                        worst = ((can_hi, tail), ratio)
                    with self.subTest(theme='dark' if dark else 'light', can_hi=can_hi, tail=tail):
                        self.assertGreaterEqual(ratio, 4.5)
            print('    colorize_transfer_id 最差：%s 对比度 %.2f（%s）'
                  % (worst[0], worst[1], 'dark' if dark else 'light'))

    def test_timestamp_fade(self):
        """Local Time 列：`Δt` 越大越淡，但淡出后仍要能看清（原来是淡成黑）。"""
        from dronecan_gui_tool.widgets.bus_monitor.window import TimestampRenderer
        for dark in (False, True):
            apply_palette(dark)
            background = theme.background_color()
            renderer = TimestampRenderer()
            previous = 1000.0
            renderer((None, self._frame(0x1007C, previous)))
            for delta in (0.0, 0.05, 0.2, 0.5, 1.0, 5.0):
                ts_real = previous + delta
                text, color = renderer((None, self._frame(0x1007C, ts_real)))
                with self.subTest(theme='dark' if dark else 'light', delta=delta):
                    self.assertTrue(text)
                    self.assertGreaterEqual(theme.contrast_ratio(color, background), 3.0)
            # 时间倒流：给警告色，也不能糊
            text, color = renderer((None, self._frame(0x1007C, 1.0)))
            self.assertGreaterEqual(theme.contrast_ratio(color, background), 4.5)

    def test_semantic_colors(self):
        """语义色（我的 dmcan 面板在用）。"""
        for dark in (False, True):
            apply_palette(dark)
            background = theme.background_color()
            for name in ('ok', 'warn', 'bad', 'muted', 'accent'):
                color = theme.semantic(name)
                with self.subTest(theme='dark' if dark else 'light', name=name):
                    self.assertGreaterEqual(theme.contrast_ratio(color, background), 4.5)

    def test_section_header_contrast(self):
        """面板的分区标题行：文字对分区底色也要够清楚（原实现是浅灰白条）。"""
        for dark in (False, True):
            apply_palette(dark)
            background = theme.section_background()
            foreground = theme.section_foreground()
            with self.subTest(theme='dark' if dark else 'light'):
                self.assertGreaterEqual(theme.contrast_ratio(foreground, background), 4.5)
                # 分区底色本身要和表格底色有可见差异，便于分区
                self.assertNotEqual(background.name(), theme.background_color().name())

    def test_icon_color_follows_theme(self):
        """图标颜色跟随主题（qtawesome 默认黑色，深色下等于看不见）。"""
        apply_palette(True)
        dark_icon = theme.icon_color()
        apply_palette(False)
        light_icon = theme.icon_color()
        self.assertGreater(dark_icon.lightness(), 128, '深色主题下图标应当是浅色')
        self.assertLess(light_icon.lightness(), 128, '浅色主题下图标应当是深色')


@unittest.skipIf(QApplication is None, '没有 PyQt6')
class IconRenderTest(unittest.TestCase):
    """真的把图标画出来，确认像素是亮的。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(['icon-test'])

    def _average_brightness(self, icon, size=32):
        image = icon.pixmap(size, size).toImage()
        total = 0.0
        count = 0
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                if color.alpha() < 32:
                    continue
                total += 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
                count += 1
        return (total / count) if count else None

    def test_icon_is_visible_in_both_themes(self):
        """同一个图标在深色下应当是亮像素、浅色下应当是暗像素。"""
        try:
            from dronecan_gui_tool.widgets import get_icon
        except Exception as ex:                                # pragma: no cover
            self.skipTest('取不到 get_icon：%s' % ex)
        apply_palette(True)
        dark_brightness = self._average_brightness(get_icon('fa6s.bus'))
        apply_palette(False)
        light_brightness = self._average_brightness(get_icon('fa6s.bus'))
        if dark_brightness is None or light_brightness is None:
            self.skipTest('图标渲染不出像素（字体缺失？）')
        self.assertGreater(dark_brightness, 120, '深色主题下图标太暗：%.0f' % dark_brightness)
        self.assertLess(light_brightness, 120, '浅色主题下图标太亮：%.0f' % light_brightness)


if __name__ == '__main__':
    unittest.main()
