#
# DroneCAN GUI Tool — dmcan 后端（达妙 USB2CANFD / USB2FDCANDUAL 原生 USB）
#
# 本文件不属于上游 DroneCAN/gui_tool，为本 fork 新增；与上游同样以 MIT 发布（见 LICENSE）。
#
"""
深浅主题感知的颜色与图标工具。

**为什么需要**：上游 gui_tool 里有若干"按语义生成颜色"的地方是按**白底深字**硬编码的
（典型做法是把 RGB 取反、或者从白色往黑色插值）。Qt 6.5+ 会跟随系统深色主题，
Fusion 深色调色板的表格底色是 `#2D2D2D`——这些颜色一落到深色底上就变成"深底深字"，
整片区域糊掉。典型受害者：

* `colorize_transfer_id()`：Data Hex / Data ASCII 列，最深到 (80,80,80)；
* `TimestampRenderer`：Local Time 列，空闲 ≥1 s 时变 (63,63,63)；
* `map_7bit_to_color()`：Src / Dst / Data Type 列，半数取值是暗色；
* `qtawesome` 图标默认是黑色，深色主题下菜单/工具栏图标不可见。

本模块把所有"生成颜色"的入口统一到这里：**保留原来的色相**，只把亮度夹到与当前背景
有足够对比（WCAG 对比度 ≥ 4.5:1）的范围里；图标颜色跟随调色板前景色。
`ensure_contrast()` 命中缓存，热路径（每行每列都算颜色）不会成为负担。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

__all__ = ['background_color', 'text_color', 'icon_color', 'is_dark_theme',
           'relative_luminance', 'contrast_ratio', 'ensure_contrast', 'blend',
           'semantic', 'section_background', 'section_foreground']

#: 拿不到调色板时的兜底（浅色）。
FALLBACK_LIGHT_BACKGROUND = QColor(255, 255, 255)
FALLBACK_LIGHT_TEXT = QColor(0, 0, 0)

#: WCAG AA 正文对比度要求。
MIN_CONTRAST_RATIO = 4.5

_SEMANTIC_LIGHT = {
    'ok': QColor('#1b5e20'),
    'warn': QColor('#b26a00'),
    'bad': QColor('#c62828'),
    'muted': QColor('#666666'),
    'accent': QColor('#0d47a1'),
}

_SEMANTIC_DARK = {
    'ok': QColor('#7ddc85'),
    'warn': QColor('#ffc266'),
    'bad': QColor('#ff8a80'),
    'muted': QColor('#9e9e9e'),
    'accent': QColor('#82b1ff'),
}

_cache: Dict[Tuple[int, int, int], QColor] = {}


def _palette_color(role: QPalette.ColorRole, fallback: QColor) -> QColor:
    """@brief 取当前应用调色板里的颜色（没有 QApplication 时用兜底值）。"""
    app = QApplication.instance()
    if app is None:
        return QColor(fallback)
    try:
        color = app.palette().color(role)
    except Exception:      # pragma: no cover - 依赖环境
        return QColor(fallback)
    return color if color.isValid() else QColor(fallback)


def background_color() -> QColor:
    """@brief 文本/表格区域的当前背景色（`Base`）。"""
    return _palette_color(QPalette.ColorRole.Base, FALLBACK_LIGHT_BACKGROUND)


def text_color() -> QColor:
    """@brief 当前调色板的前景色（`Text`）。"""
    return _palette_color(QPalette.ColorRole.Text, FALLBACK_LIGHT_TEXT)


def icon_color() -> QColor:
    """
    @brief 图标颜色：跟随调色板前景色。

    @note qtawesome 的默认颜色是**黑色**，深色主题下图标会看不见，所以 `get_icon()` 用它。
    """
    return _palette_color(QPalette.ColorRole.WindowText, FALLBACK_LIGHT_TEXT)


def is_dark_theme() -> bool:
    """@brief 当前是不是深色主题（按 `Base` 的亮度判定）。"""
    return background_color().lightness() < 128


def relative_luminance(color: QColor) -> float:
    """@brief WCAG 相对亮度（0 = 黑，1 = 白）。"""
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    return (0.2126 * channel(color.redF()) +
            0.7152 * channel(color.greenF()) +
            0.0722 * channel(color.blueF()))


def contrast_ratio(first: QColor, second: QColor) -> float:
    """@brief 两色之间的 WCAG 对比度（1.0 ~ 21.0）。"""
    a, b = relative_luminance(first), relative_luminance(second)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def ensure_contrast(color: QColor, background: Optional[QColor] = None,
                    min_ratio: float = MIN_CONTRAST_RATIO) -> QColor:
    """
    @brief 保留色相，把亮度调整到与背景有足够对比。

    @param color 原色（通常是按算法生成的"语义色"）
    @param background 背景色；None = 当前调色板的 `Base`
    @param min_ratio 最低对比度，默认 WCAG AA 的 4.5
    @return 调整后的颜色；原色已经达标时原样返回（带缓存）
    """
    background = background or background_color()
    key = (color.rgba(), background.rgba(), int(min_ratio * 100))
    cached = _cache.get(key)
    if cached is not None:
        return QColor(cached)

    result = QColor(color)
    if contrast_ratio(result, background) < min_ratio:
        hue, saturation, lightness, alpha = color.getHslF()
        if hue < 0:                      # 无彩色：直接给一个与背景相反的灰
            hue, saturation = 0.0, 0.0
        toward_light = background.lightnessF() < 0.5
        best = result
        best_ratio = contrast_ratio(result, background)
        steps = 100
        for step in range(1, steps + 1):
            candidate_lightness = (min(1.0, lightness + step / steps) if toward_light
                                   else max(0.0, lightness - step / steps))
            candidate = QColor.fromHslF(hue, saturation, candidate_lightness, alpha if alpha else 1.0)
            ratio = contrast_ratio(candidate, background)
            if ratio > best_ratio:
                best, best_ratio = candidate, ratio
            if ratio >= min_ratio:
                best, best_ratio = candidate, ratio
                break
            if candidate_lightness in (0.0, 1.0):
                break
        result = best

    if len(_cache) > 4096:               # 简单限长，避免长跑攒太多
        _cache.clear()
    _cache[key] = QColor(result)
    return QColor(result)


def blend(foreground: QColor, background: QColor, amount: float) -> QColor:
    """
    @brief 把前景色朝背景色插值（`amount`=0 原色，=1 与背景相同）。

    @note 用来做"越旧越淡"这种效果时，**必须朝背景淡出**而不是朝黑色淡出，
          否则深色主题下就变成"深底深字"了。
    """
    amount = max(0.0, min(1.0, amount))
    return QColor(
        int(foreground.red() * (1 - amount) + background.red() * amount),
        int(foreground.green() * (1 - amount) + background.green() * amount),
        int(foreground.blue() * (1 - amount) + background.blue() * amount),
        foreground.alpha())


def semantic(name: str, background: Optional[QColor] = None) -> QColor:
    """
    @brief 语义色（`ok` / `warn` / `bad` / `muted` / `accent`），已按主题做过对比度适配。

    @param name 语义名
    @param background 背景色；None = 当前调色板 `Base`
    @return 颜色；未知语义名回退到前景色
    """
    background = background or background_color()
    table = _SEMANTIC_DARK if background.lightness() < 128 else _SEMANTIC_LIGHT
    color = table.get(str(name))
    if color is None:
        return text_color()
    return ensure_contrast(color, background)


def section_background(background: Optional[QColor] = None) -> QColor:
    """@brief 分区标题行的底色：与背景**轻微**区分（深色主题下不能再是浅灰白条）。"""
    background = background or background_color()
    target = QColor(255, 255, 255) if background.lightness() < 128 else QColor(0, 0, 0)
    return blend(background, target, 0.10)


def section_foreground(background: Optional[QColor] = None) -> QColor:
    """@brief 分区标题行的文字色（与分区底色保持对比）。"""
    background = background or background_color()
    return ensure_contrast(text_color(), section_background(background))
