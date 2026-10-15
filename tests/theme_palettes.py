# -*- coding: utf-8 -*-
"""测试/渲染用的浅色与深色调色板（深色取 Qt Fusion 深色主题的实际取值）。"""

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

#: Qt Fusion 深色调色板的关键取值（实测值与系统深色主题一致）
DARK = dict(window=QColor(53, 53, 53), base=QColor(45, 45, 45), alternate=QColor(58, 58, 58),
            text=QColor(230, 230, 230))
LIGHT = dict(window=QColor(240, 240, 240), base=QColor(255, 255, 255),
             alternate=QColor(245, 245, 245), text=QColor(0, 0, 0))


def make_palette(dark: bool) -> QPalette:
    """@brief 造一个调色板（浅色或深色）。"""
    values = DARK if dark else LIGHT
    palette = QPalette()
    for role, key in ((QPalette.ColorRole.Window, 'window'),
                      (QPalette.ColorRole.Base, 'base'),
                      (QPalette.ColorRole.AlternateBase, 'alternate'),
                      (QPalette.ColorRole.Text, 'text'),
                      (QPalette.ColorRole.WindowText, 'text'),
                      (QPalette.ColorRole.ButtonText, 'text'),
                      (QPalette.ColorRole.Button, 'window'),
                      (QPalette.ColorRole.ToolTipBase, 'base'),
                      (QPalette.ColorRole.ToolTipText, 'text')):
        palette.setColor(role, QColor(values[key]))
    return palette


def apply_palette(dark: bool) -> None:
    """@brief 把调色板应用到当前 QApplication。"""
    app = QApplication.instance()
    if app is not None:
        app.setPalette(make_palette(dark))
