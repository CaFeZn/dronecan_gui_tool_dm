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

    def _assert_cell_colors(self, chip, label, dark):
        """
        @brief 一个"色块单元格"的完整契约。

        1. **色块**与表格底分得开——用 `CHIP_MIN_CONTRAST`，不是正文的 4.5:1：
           色块是**底色**，拿正文标准去要求它只会把浅色主题的颜色全压暗（现场翻车原因）；
        2. **色块上的文字**要看得清（`readable_pair()` 挑出来的文字色 ≥ 4.5:1）；
        3. 浅色主题不走这里：原版就是对的，见 `test_light_theme_is_exactly_upstream`。
        """
        background = theme.background_color()
        cell_background, cell_text = theme.readable_pair(chip)

        self.assertGreaterEqual(theme.contrast_ratio(cell_background, background),
                                theme.CHIP_MIN_CONTRAST,
                                '%s：色块 %s 跟表格底 %s 分不开'
                                % (label, cell_background.name(), background.name()))
        self.assertGreaterEqual(theme.contrast_ratio(cell_text, cell_background), 4.5,
                                '%s：色块 %s 上的文字 %s 看不清'
                                % (label, cell_background.name(), cell_text.name()))

    def test_light_theme_is_exactly_upstream(self):
        """
        **浅色主题 = 原版**：所有颜色必须与上游公式逐位相同，一个像素都不许改。

        现场问题：之前把正文的 4.5:1 套到"单元格底色"上，浅色主题里原本近白的浅色块
        全被压成深色（实测色块平均亮度 0.80 -> 0.15），再压上白字，反而没有原版清晰。
        原版（浅色块 + 黑字）本来就是清楚的，所以浅色路径就该原样跑原版公式。
        """
        from dronecan_gui_tool.widgets import map_7bit_to_color
        from dronecan_gui_tool.widgets.bus_monitor.window import (colorize_can_id,
                                                                  colorize_transfer_id,
                                                                  TimestampRenderer)
        apply_palette(False)
        self.assertFalse(theme.is_dark_theme())

        # map_7bit_to_color：原版 = RGB 取反
        for value in range(128):
            masked = value & 0x7f
            expected = QColor(0xFF - ((masked >> 5) & 0b11) * 48,
                              0xFF - ((masked >> 2) & 0b111) * 12,
                              0xFF - (masked & 0b11) * 48)
            self.assertEqual(map_7bit_to_color(value).name(), expected.name(),
                             'map_7bit_to_color(%d) 不是原版颜色' % value)

        # colorize_can_id：原版 = 0xFF, 0xFF-(31-prio)*6, 0xFF
        for priority in range(32):
            expected = QColor(0xFF, 0xFF - (31 - priority) * 6, 0xFF)
            frame = self._frame((priority << 24) | (1 << 8) | 12, 0.0)
            self.assertEqual(colorize_can_id(frame).name(), expected.name(),
                             'colorize_can_id(prio=%d) 不是原版颜色' % priority)

        # colorize_transfer_id：原版 = transfer id 哈希后 RGB 取反
        for can_hi in range(16):
            for tail in range(256):
                x = (tail & 0b11111) | (can_hi << 5)
                expected = QColor(0xFF - ((x >> 6) & 0b111) * 25,
                                  0xFF - ((x >> 3) & 0b111) * 25,
                                  0xFF - (x & 0b111) * 25)
                frame = self._frame((can_hi << 16) | 0x1234, 0.0, bytes([tail]))
                self.assertEqual(colorize_transfer_id(frame).name(), expected.name(),
                                 'colorize_transfer_id(%d, %d) 不是原版颜色' % (can_hi, tail))

        # Local Time：原版 = 255 - int(192*delta)；时间倒流 = (255,230,230)
        renderer = TimestampRenderer()
        previous = 1000.0
        renderer((None, self._frame(0x1007C, previous)))
        last_ts = previous
        for delta in (0.0, 0.05, 0.2, 0.5, 1.0, 5.0):
            ts_real = previous + delta
            _text, chip = renderer((None, self._frame(0x1007C, ts_real)))
            # 用渲染器**实际拿到**的间隔算期望（浮点累加会出现 0.1499999 这种差值）
            level = 255 - int(192 * min(1, ts_real - last_ts))
            last_ts = ts_real
            self.assertEqual(chip.name(), QColor(level, level, level).name(),
                             '时间戳淡出色不是原版公式（delta=%.2f）' % delta)
        _text, reversed_chip = renderer((None, self._frame(0x1007C, 1.0)))
        self.assertEqual(reversed_chip.name(), QColor(255, 230, 230).name(),
                         '时间倒流的颜色不是原版')

        # 文字色：浅色下就是主题文字色（黑字），不做自适应
        for name in ('#ffffff', '#cdcdcd', '#505050'):
            cell_background, cell_text = theme.readable_pair(QColor(name))
            with self.subTest(color=name):
                self.assertEqual(cell_background.name(), name)
                self.assertEqual(cell_text.name(), theme.text_color().name())

    def test_map_7bit_to_color_all_values(self):
        """Src / Dst / Data Type 用的 `map_7bit_to_color`：128 个取值全部达标。"""
        from dronecan_gui_tool.widgets import map_7bit_to_color
        for dark in (True,):
            apply_palette(dark)
            with self.subTest(theme='dark'):
                for value in range(128):
                    color = map_7bit_to_color(value)
                    self._assert_cell_colors(color, 'map_7bit(%d)' % value, dark)

    def test_can_id_colors(self):
        """CAN ID 列：优先级 0..31 全部达标。"""
        from dronecan_gui_tool.widgets.bus_monitor.window import colorize_can_id
        for dark in (True,):
            apply_palette(dark)
            for priority in range(32):
                frame = self._frame((priority << 24) | (1 << 8) | 12, 0.0)
                with self.subTest(theme='dark' if dark else 'light', priority=priority):
                    self._assert_cell_colors(colorize_can_id(frame),
                                             'can_id(prio=%d)' % priority, dark)

    def test_data_hex_colors(self):
        """Data Hex / Data ASCII 列（`colorize_transfer_id`）——深色下原来的重灾区。"""
        from dronecan_gui_tool.widgets.bus_monitor.window import colorize_transfer_id
        for dark in (True,):
            apply_palette(dark)
            for can_hi in range(16):
                for tail in range(256):
                    frame = self._frame((can_hi << 16) | 0x1234, 0.0, bytes([tail]))
                    color = colorize_transfer_id(frame)
                    if color is None:
                        continue
                    with self.subTest(theme='dark' if dark else 'light',
                                      can_hi=can_hi, tail=tail):
                        self._assert_cell_colors(color, 'transfer(%d,%d)' % (can_hi, tail), dark)

    def test_timestamp_fade(self):
        """
        Local Time 列：**保留上游的淡出语义**（刚到的帧最亮、越旧越暗），
        并且淡出后文字仍然看得清（上游原来淡成深色块 + 黑字 = 看不见）。
        """
        from dronecan_gui_tool.widgets.bus_monitor.window import TimestampRenderer
        for dark in (True,):
            apply_palette(dark)
            renderer = TimestampRenderer()
            previous = 1000.0
            renderer((None, self._frame(0x1007C, previous)))
            luminances = []
            for delta in (0.0, 0.05, 0.2, 0.5, 1.0, 5.0):
                ts_real = previous + delta
                text, chip = renderer((None, self._frame(0x1007C, ts_real)))
                with self.subTest(theme='dark' if dark else 'light', delta=delta):
                    self.assertTrue(text)
                    self._assert_cell_colors(chip, 'timestamp(%.2f)' % delta, dark)
                    luminances.append(theme.relative_luminance(chip))
            # 淡出方向：越旧越暗（与上游一致）
            self.assertGreaterEqual(luminances[0], luminances[-1] - 1e-9,
                                    '刚到的帧应当比很久以前的帧更亮')
            # 时间倒流：给警告色，也不能糊
            text, chip = renderer((None, self._frame(0x1007C, 1.0)))
            self._assert_cell_colors(chip, 'timestamp(倒流)', dark)

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
class ColorModeTest(unittest.TestCase):
    """外观模式：跟随系统 / 强制深色 / 强制浅色（View -> Appearance）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(['mode-test'])

    def tearDown(self):
        theme.apply_mode(self.app, theme.MODE_SYSTEM)

    def test_forced_modes(self):
        """强制深色/浅色都要真的换调色板，并且文字对底色依然达标。"""
        theme.apply_mode(self.app, theme.MODE_DARK)
        self.assertTrue(theme.is_dark_theme(), '强制深色后应当判定为深色主题')
        self.assertEqual(theme.background_color().name(), '#2d2d2d')
        self.assertGreaterEqual(
            theme.contrast_ratio(theme.text_color(), theme.background_color()), 4.5)

        theme.apply_mode(self.app, theme.MODE_LIGHT)
        self.assertFalse(theme.is_dark_theme(), '强制浅色后应当判定为浅色主题')
        self.assertEqual(theme.background_color().name(), '#ffffff')
        self.assertGreaterEqual(
            theme.contrast_ratio(theme.text_color(), theme.background_color()), 4.5)

    def test_invalid_mode_is_ignored(self):
        """非法值不许把界面搞坏。"""
        theme.apply_mode(self.app, theme.MODE_DARK)
        self.assertEqual(theme.apply_mode(self.app, 'purple'), theme.MODE_DARK)
        self.assertTrue(theme.is_dark_theme())

    def test_mode_persisted(self):
        """
        选择要记下来，下次启动（含子进程）还是它。

        @note QSettings 没有可靠的"换到临时目录"的办法（`setPath` 在 Windows 原生格式下
              不生效），所以这里读旧值 -> 测 -> **原样还原**，不污染用户配置。
        """
        from PyQt6.QtCore import QSettings

        settings = QSettings(theme.SETTINGS_ORGANIZATION, theme.SETTINGS_APPLICATION)
        original = settings.value(theme.SETTINGS_KEY, None)
        try:
            settings.remove(theme.SETTINGS_KEY)
            settings.sync()
            self.assertEqual(theme.load_mode(), theme.MODE_SYSTEM,
                             '没存过时应当回到"跟随系统"')

            theme.save_mode(theme.MODE_DARK)
            self.assertEqual(theme.load_mode(), theme.MODE_DARK)
            theme.save_mode(theme.MODE_LIGHT)
            self.assertEqual(theme.load_mode(), theme.MODE_LIGHT)

            theme.save_mode('purple')              # 非法值不写
            self.assertEqual(theme.load_mode(), theme.MODE_LIGHT)
        finally:
            if original is None:
                settings.remove(theme.SETTINGS_KEY)
            else:
                settings.setValue(theme.SETTINGS_KEY, original)
            settings.sync()


@unittest.skipIf(QApplication is None, '没有 PyQt6')
class IconRefreshTest(unittest.TestCase):
    """换主题后已经建好的图标必须重新染色（不然切到浅色就是一堆白图标）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(['icon-test'])

    def tearDown(self):
        theme.apply_mode(self.app, theme.MODE_SYSTEM)

    def _opaque_icon_color(self, action):
        image = action.icon().pixmap(32, 32).toImage()
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                if color.alpha() > 200:
                    return color
        return None

    def test_icons_follow_the_mode(self):
        from PyQt6.QtWidgets import QMainWindow
        from PyQt6.QtGui import QAction
        from dronecan_gui_tool.widgets import get_icon, refresh_icons

        window = QMainWindow()
        action = QAction(get_icon('fa6s.bus'), 'bus', window)
        window.menuBar().addMenu('&T').addAction(action)

        theme.apply_mode(self.app, theme.MODE_DARK)
        self.assertEqual(refresh_icons(self.app), 1, '应当认出这个图标')
        dark_icon = self._opaque_icon_color(action)
        self.assertIsNotNone(dark_icon)
        self.assertTrue(theme.is_dark_theme())

        theme.apply_mode(self.app, theme.MODE_LIGHT)
        self.assertEqual(refresh_icons(self.app), 1, '第二次换主题也要认得出来')
        light_icon = self._opaque_icon_color(action)
        self.assertIsNotNone(light_icon)

        self.assertNotEqual(dark_icon.name(), light_icon.name(), '图标颜色应当跟着主题变')
        self.assertGreater(theme.relative_luminance(dark_icon), theme.relative_luminance(light_icon),
                           '深色主题下图标应当更亮，浅色主题下更暗')


@unittest.skipIf(QApplication is None, '没有 PyQt6')
class CellBackgroundTextTest(unittest.TestCase):
    """
    彩色**单元格背景**上的文字必须看得清。

    现场问题：总线监视器的 Data Hex / Data ASCII / Src / Dst / Data Type 列返回的颜色
    是被 `BasicTable` 当"单元格背景"用的（彩色色块），文字沿用主题默认色。
    浅色主题下默认黑字没问题；深色主题下默认浅字 + 浅色块 = 完全看不见
    （那张深色截图里 46.7% 的文字像素对本单元格底色对比度 < 3:1）。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(['cell-test'])

    def _frame(self, can_id, data=b'\x01\x02\x03\x04\x05\x06\x07', ts_real=0.0):
        from dronecan.driver.common import CANFrame
        return CANFrame(can_id, data, True, ts_monotonic=ts_real, ts_real=ts_real)

    def _backgrounds(self):
        """@brief 收集所有会被当单元格背景用的颜色。"""
        from dronecan_gui_tool.widgets import map_7bit_to_color
        from dronecan_gui_tool.widgets.bus_monitor.window import (colorize_can_id,
                                                                  colorize_transfer_id,
                                                                  TimestampRenderer)
        for value in range(128):
            yield 'map_7bit(%d)' % value, map_7bit_to_color(value)
        for priority in range(32):
            yield 'can_id(prio=%d)' % priority, colorize_can_id(
                self._frame((priority << 24) | (1 << 8) | 12))
        for can_hi in range(16):
            for tail in (0, 1, 7, 31, 85, 170, 255):
                color = colorize_transfer_id(self._frame((can_hi << 16) | 0x1234,
                                                         bytes([tail])))
                if color is not None:
                    yield 'transfer(%d,%d)' % (can_hi, tail), color
        renderer = TimestampRenderer()
        previous = 1000.0
        renderer((None, self._frame(0x1007C, previous)))
        for delta in (0.0, 0.2, 1.0, 5.0):
            _text, color = renderer((None, self._frame(0x1007C, previous + delta)))
            yield 'timestamp(%.1f)' % delta, color

    def test_light_theme_returns_upstream_colors(self):
        """浅色主题下 `readable_pair` 必须原样返回：色块不动、文字用主题色（原版黑字）。"""
        apply_palette(False)
        for name, background in self._backgrounds():
            cell_background, foreground = theme.readable_pair(background)
            with self.subTest(cell=name):
                self.assertEqual(cell_background.name(), background.name(),
                                 '%s 在浅色主题下不许被改动' % name)
                self.assertEqual(foreground.name(), theme.text_color().name(),
                                 '%s 在浅色主题下应当用主题文字色' % name)

    def test_text_readable_on_every_cell_background(self):
        """
        **深色主题**下：每一个"色块背景"配上文字后都要 >=4.5:1。

        （浅色主题不走这条路径——原版本来就清楚，见上面那个用例。）
        """
        for dark in (True,):
            apply_palette(dark)
            worst = None
            for name, background in self._backgrounds():
                cell_background, foreground = theme.readable_pair(background)
                # 色相不能跑掉（只允许为了可读性动亮度）
                hue_in, _, _, _ = background.getHslF()
                hue_out, _, _, _ = cell_background.getHslF()
                if hue_in >= 0 and hue_out >= 0:
                    delta = min(abs(hue_in - hue_out), 1 - abs(hue_in - hue_out))
                    self.assertLess(delta, 0.02, '%s 的色相被改掉了' % name)
                background = cell_background
                ratio = theme.contrast_ratio(foreground, background)
                if worst is None or ratio < worst[1]:
                    worst = (name, ratio, background.name(), foreground.name())
                with self.subTest(theme='dark', cell=name):
                    self.assertGreaterEqual(
                        ratio, 4.5,
                        '%s：背景 %s 上的文字色 %s 只有 %.2f:1'
                        % (name, background.name(), foreground.name(), ratio))
            print('    dark 主题色块最差：%s 背景 %s 文字 %s -> %.2f:1'
                  % (worst[0], worst[2], worst[3], worst[1]))


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
