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

from typing import Dict, Optional, Tuple  # noqa: F401  (Tuple 供 readable_pair 用)

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

__all__ = ['background_color', 'text_color', 'icon_color', 'is_dark_theme',
           'relative_luminance', 'contrast_ratio', 'ensure_contrast', 'foreground_for',
           'readable_pair', 'blend', 'semantic', 'section_background', 'section_foreground',
           'DARK_TEXT', 'LIGHT_TEXT', 'CHIP_MIN_CONTRAST', 'adapt_upstream_chip',
           # 外观模式
           'MODE_SYSTEM', 'MODE_DARK', 'MODE_LIGHT', 'MODES', 'MODE_LABELS',
           'build_dark_palette', 'build_light_palette', 'load_mode', 'save_mode',
           'apply_mode', 'apply_saved_mode', 'mode', 'reset_cache']

#: 外观模式（`View -> Appearance`）。跟随系统 = 启动时平台给的调色板原样用。
MODE_SYSTEM = 'system'
MODE_DARK = 'dark'
MODE_LIGHT = 'light'
MODES = (MODE_SYSTEM, MODE_DARK, MODE_LIGHT)

#: 菜单里的显示名与顺序
MODE_LABELS = ((MODE_SYSTEM, 'Follow &System'),
               (MODE_DARK, '&Dark'),
               (MODE_LIGHT, '&Light'))

#: 偏好存这里（QSettings；Windows 上落在注册表 HKCU\\Software\\DroneCAN\\gui_tool）
SETTINGS_ORGANIZATION = 'DroneCAN'
SETTINGS_APPLICATION = 'gui_tool'
SETTINGS_KEY = 'appearance/color_mode'

#: 拿不到调色板时的兜底（浅色）。
FALLBACK_LIGHT_BACKGROUND = QColor(255, 255, 255)
FALLBACK_LIGHT_TEXT = QColor(0, 0, 0)

#: WCAG AA 正文对比度要求。
MIN_CONTRAST_RATIO = 4.5

#: **色块**（单元格底色）与表格底之间只需要"分得开"即可，不适用正文那套 4.5:1。
#: 拿 4.5 去要求色块，会把浅色主题里原本近白的浅色块全部压成深色（实测色块平均亮度
#: 从 0.8+ 掉到 0.15），配白字——这就是"浅色 UI 没有原版清晰"的原因。文字能不能看清
#: 由 `readable_pair()` 负责（它会按色块挑文字色），色块自己只要不跟表格底融在一起。
CHIP_MIN_CONTRAST = 1.2

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

#: 当前生效的模式；`_system_palette` 是首次应用前记下的"系统原本的样子"
_mode: str = MODE_SYSTEM
_system_palette: Optional[QPalette] = None


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


def build_dark_palette() -> QPalette:
    """@brief 深色调色板（取值与 Qt 跟随系统深色时一致：底色 #2d2d2d、窗口 #353535）。"""
    window = QColor(53, 53, 53)
    base = QColor(45, 45, 45)
    text = QColor(230, 230, 230)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(58, 58, 58))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(45, 45, 45))
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, window)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 110, 110))
    palette.setColor(QPalette.ColorRole.Link, QColor(90, 169, 255))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(158, 158, 158))
    palette.setColor(QPalette.ColorRole.Light, QColor(78, 78, 78))
    palette.setColor(QPalette.ColorRole.Midlight, QColor(64, 64, 64))
    palette.setColor(QPalette.ColorRole.Mid, QColor(35, 35, 35))
    palette.setColor(QPalette.ColorRole.Dark, QColor(25, 25, 25))
    palette.setColor(QPalette.ColorRole.Shadow, QColor(14, 14, 14))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                 QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(127, 127, 127))
    return palette


def build_light_palette() -> QPalette:
    """@brief 浅色调色板（系统是深色时也能强制用浅色）。"""
    window = QColor(239, 239, 239)
    base = QColor(255, 255, 255)
    text = QColor(0, 0, 0)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(247, 247, 247))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 220))
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, window)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.ColorRole.Link, QColor(0, 0, 255))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(48, 140, 198))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(128, 128, 128))
    palette.setColor(QPalette.ColorRole.Light, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Midlight, QColor(227, 227, 227))
    palette.setColor(QPalette.ColorRole.Mid, QColor(160, 160, 160))
    palette.setColor(QPalette.ColorRole.Dark, QColor(118, 118, 118))
    palette.setColor(QPalette.ColorRole.Shadow, QColor(105, 105, 105))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                 QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(160, 160, 160))
    return palette


def reset_cache() -> None:
    """@brief 清掉按调色板缓存的颜色（换主题后必须调用）。"""
    _cache.clear()


def load_mode() -> str:
    """@brief 读取保存的外观模式；没存过或值非法时按"跟随系统"。"""
    try:
        from PyQt6.QtCore import QSettings
        value = str(QSettings(SETTINGS_ORGANIZATION, SETTINGS_APPLICATION).value(
            SETTINGS_KEY, MODE_SYSTEM))
    except Exception:                      # pragma: no cover - 依赖环境
        return MODE_SYSTEM
    return value if value in MODES else MODE_SYSTEM


def save_mode(mode_name: str) -> None:
    """@brief 保存外观模式（非法值直接忽略）。"""
    if mode_name not in MODES:
        return
    try:
        from PyQt6.QtCore import QSettings
        settings = QSettings(SETTINGS_ORGANIZATION, SETTINGS_APPLICATION)
        settings.setValue(SETTINGS_KEY, mode_name)
        settings.sync()
    except Exception:                      # pragma: no cover - 依赖环境
        pass


def apply_mode(app=None, mode_name: str = MODE_SYSTEM) -> str:
    """
    @brief 把外观模式应用到 `app`：换调色板 + 清颜色缓存。

    @param app `QApplication`；None 时取当前实例
    @param mode_name `MODES` 之一
    @return 实际生效的模式（参数非法时返回当前模式）

    @note 本想用 Qt 6.5+ 的 `QStyleHints.setColorScheme()`，但实测在本环境下**不生效**
          （`colorScheme()` 依旧返回 Unknown、调色板纹丝不动），所以这里直接设调色板，
          并把样式固定为 Fusion——这样三种模式的观感才是可预期的。
    """
    global _mode, _system_palette
    if app is None:
        app = QApplication.instance()
    if app is None or mode_name not in MODES:
        return _mode
    if _system_palette is None:
        _system_palette = QPalette(app.palette())    # 记下"系统原本的样子"
    try:
        app.setStyle('Fusion')
    except Exception:                      # pragma: no cover - 依赖环境
        pass
    if mode_name == MODE_DARK:
        app.setPalette(build_dark_palette())
    elif mode_name == MODE_LIGHT:
        app.setPalette(build_light_palette())
    else:
        app.setPalette(QPalette(_system_palette))
    _mode = mode_name
    reset_cache()
    return _mode


def apply_saved_mode(app=None) -> str:
    """@brief 启动时按保存的偏好应用外观，返回生效的模式。"""
    return apply_mode(app, load_mode())


def mode() -> str:
    """@brief 当前生效的外观模式。"""
    return _mode


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


#: 两个"极值"文字色：给彩色单元格当文字用
DARK_TEXT = QColor(0x18, 0x18, 0x18)
LIGHT_TEXT = QColor(0xF2, 0xF2, 0xF2)


def foreground_for(background: QColor, min_ratio: float = MIN_CONTRAST_RATIO) -> QColor:
    """
    @brief 给一个"**当背景用**的色块"挑一个能看清的文字颜色。

    为什么需要：总线监视器里 Data Hex / Data ASCII / Src / Dst / Data Type 这些列返回的
    颜色是被 `BasicTable` 当 **单元格背景**用的（彩色色块），文字颜色则沿用主题默认值。
    浅色主题下默认是黑字、色块是浅色，正好能看；**深色主题下默认是浅字，浅色色块 + 浅字
    就等于看不见**（现场那张图的 Data Hex / Src / Data Type 就是这样）。

    @param background 单元格背景色
    @param min_ratio 最低对比度
    @return 文字色：优先沿用当前主题文字色；不够对比时在黑/白两档里挑更清楚的那个
    """
    current = text_color()
    if contrast_ratio(current, background) >= min_ratio:
        return current
    dark_ratio = contrast_ratio(DARK_TEXT, background)
    light_ratio = contrast_ratio(LIGHT_TEXT, background)
    if max(dark_ratio, light_ratio) < min_ratio:
        # 极端情况（中灰底）：挑更好的那个，总比看不见强
        return DARK_TEXT if dark_ratio >= light_ratio else LIGHT_TEXT
    return DARK_TEXT if dark_ratio >= light_ratio else LIGHT_TEXT


def adapt_upstream_chip(color: QColor,
                        min_ratio: float = CHIP_MIN_CONTRAST) -> QColor:
    """
    @brief 给**上游按"白底深字"设计的色块色**做主题适配。

    @param color 上游算法算出来的颜色（RGB 取反、白往黑插值那一类）
    @param min_ratio 色块与表格底的最低对比度（默认 `CHIP_MIN_CONTRAST`）
    @return 浅色主题下**原样返回**；深色主题下做亮度适配

    @note **浅色主题下一律原样返回，这是硬约束**：原版（浅色块 + 黑字）本来就清楚，
          任何"优化"都会把它改坏。现场教训——把正文的 4.5:1 套到"单元格底色"上，浅色
          主题里原本近白的浅色块被压成深色（实测色块平均亮度 0.80 -> 0.15），再压上白字。

    @note 这条规则只作用于**上游的色块**；本 fork 自己的界面（dmcan 面板等）用
          `semantic()` / `ensure_contrast()`，它们任何时候都会真的保证对比度。
    """
    if not is_dark_theme():
        return QColor(color)
    return ensure_contrast(color, min_ratio=min_ratio)


def readable_pair(background: QColor, min_ratio: float = MIN_CONTRAST_RATIO,
                  margin: float = 0.5) -> Tuple[QColor, QColor]:
    """
    @brief 把"当背景用的色块"配成 (背景, 文字) 一对，保证文字看得清。

    @param background 原本的色块颜色（语义色，保留色相）
    @param min_ratio 文字与本色块之间的最低对比度
    @param margin 调整背景时的余量（免得刚好卡在阈值上）
    @return `(背景, 文字)`；背景**只在必要时**动，且只动亮度、不动色相

    @note **浅色主题直接返回原样**——原版（浅色块 + 黑字）本来就是对的，不做任何自适应；
          只有深色主题才因为"原版假设白底深字"而需要调整。

    @note 深色主题下，中灰色块是唯一绕不过去的情况：黑字白字都不够 4.5:1，只能把色块
          亮度推一档。
    """
    if not is_dark_theme():
        return QColor(background), text_color()

    desired = min_ratio + margin
    foreground = foreground_for(background, min_ratio)
    if contrast_ratio(foreground, background) >= min_ratio:
        return QColor(background), foreground

    hue, saturation, lightness, alpha = background.getHslF()
    if hue < 0:                      # 无彩色：只调亮度
        hue, saturation = 0.0, 0.0
    for step in range(1, 201):
        for candidate_lightness in (max(0.0, lightness - step / 400.0),
                                    min(1.0, lightness + step / 400.0)):
            candidate = QColor.fromHslF(hue, saturation, candidate_lightness,
                                        alpha if alpha else 1.0)
            candidate_text = foreground_for(candidate, desired)
            if contrast_ratio(candidate_text, candidate) >= desired:
                return candidate, candidate_text
    return QColor(background), foreground          # 理论上到不了这里


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
