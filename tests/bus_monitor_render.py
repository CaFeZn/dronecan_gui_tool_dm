# -*- coding: utf-8 -*-
"""
把 Bus Monitor 离屏渲染出来（用于排查/对比界面可读性，不需要硬件）。

用法::

    python tests/bus_monitor_render.py [--width 1204] [--height 1100] [--out artifacts/bus_monitor.png]

会造一段"像真总线"的 DroneCAN 流量（3 个节点的 NodeStatus + 若干其它类型），
按用户窗口的尺寸渲染并截图。
"""

import argparse
import os
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')      # 必须在建 QApplication 之前

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import dronecan  # noqa: E402
from dronecan.driver.common import CANFrame  # noqa: E402


def make_stream(count: int = 260):
    """@brief 造一段 DroneCAN 帧流：与现场（3 个节点的 NodeStatus）接近。"""
    rng = __import__('random').Random(7)
    frames = []
    node_ids = (124, 125, 127)
    uptime = [100, 200, 300]
    types = []
    for _ in range(count):
        roll = rng.random()
        src = rng.choice(node_ids)
        if roll < 0.82:
            dtid, priority = 1, 16                       # NodeStatus
            payload = bytes([uptime[src % 3] & 0xFF, (uptime[src % 3] >> 8) & 0xFF,
                             (uptime[src % 3] >> 16) & 0xFF, (uptime[src % 3] >> 24) & 0xFF,
                             0, 0, rng.randrange(3)])
            uptime[src % 3] += 1
        elif roll < 0.92:
            dtid, priority = 341, 30                     # LogMessage 的 dtid
            payload = bytes([0x20 | rng.randrange(8)]) + b'param read failed\x00'[:20]
        else:
            dtid, priority = 2, 16                       # Allocator / 其它
            payload = bytes(rng.randrange(256) for _ in range(8))
        can_id = (priority << 24) | (dtid << 8) | src
        frame = CANFrame(can_id, payload, True,
                         ts_monotonic=time.monotonic(), ts_real=time.time())
        direction = 'tx' if rng.random() < 0.03 else 'rx'
        types.append((dtid, src))
        frames.append((direction, frame))
    return frames


def _contrast_report(image, min_ratio: float = 3.0) -> str:
    """
    @brief 量"**色块单元格上的文字**"看得清不清。

    做法：对每个采样像素，取周围一小片区域的**局部众数色**当作这一格的底色：

    * 色块内部 -> 众数就是自己，跳过；
    * 表格网格线（浅灰线压在白底）-> 众数是表格底色、不是色块，跳过
      （那是装饰不是文字，算进来只会把数字搅乱）；
    * **色块上的文字** -> 众数是色块色，量"文字 vs 色块"的对比度——正是要看的东西。

    只跟整表底色比是不行的：色块本身与表格底对比度很高，但**色块上的字**可能糊成一团
    （现场那版浅色 UI 就是这样：近白的浅色块被压成深色、再压白字）。

    @param image `QImage`
    @param min_ratio 判定"看不清"的对比度阈值
    @return 人读结论
    """
    from collections import Counter
    from PyQt6.QtGui import QColor
    from dronecan_gui_tool.widgets.theme import contrast_ratio

    width, height = image.width(), image.height()
    step = max(1, min(width, height) // 300)

    histogram = Counter()
    grid = []
    for y in range(0, height, step):
        row = []
        for x in range(0, width, step):
            color = image.pixelColor(x, y)
            rgb = (color.red(), color.green(), color.blue())
            histogram[rgb] += 1
            row.append(rgb)
        grid.append(row)
    global_background = QColor(*histogram.most_common(1)[0][0])

    def is_chip(rgb) -> bool:
        """@brief 这个颜色能不能算"色块"（而不是表格底色）。"""
        return contrast_ratio(QColor(*rgb), global_background) >= 1.4

    rows, cols = len(grid), len(grid[0]) if grid else 0
    #: 取局部众数的窗口半径（像素）
    radius = 4
    text_pixels = bad_pixels = 0
    ratios = []
    for row in range(rows):
        for col in range(cols):
            window = Counter()
            for dy in range(-radius, radius + 1):
                neighbor_row = row + dy
                if not (0 <= neighbor_row < rows):
                    continue
                line = grid[neighbor_row]
                for dx in range(-radius, radius + 1):
                    neighbor_col = col + dx
                    if 0 <= neighbor_col < cols:
                        color = line[neighbor_col]
                        window[(color.red(), color.green(), color.blue())] += 1
            local = window.most_common(1)[0][0]
            # 这一格底色必须是**色块**：网格线（浅灰线压在白底）会被自动排除，
            # 那是装饰不是文字，算进来只会把数字搅乱
            if not is_chip(local):
                continue
            rgb = (grid[row][col].red(), grid[row][col].green(), grid[row][col].blue())
            if rgb == local:
                continue                        # 色块内部
            ratio = contrast_ratio(QColor(*rgb), QColor(*local))
            if ratio < 1.4:
                continue                        # 抗锯齿过渡色
            text_pixels += 1
            ratios.append(ratio)
            if ratio < min_ratio:
                bad_pixels += 1

    ratios.sort()
    share = (100.0 * bad_pixels / text_pixels) if text_pixels else 0.0
    median = ratios[len(ratios) // 2] if ratios else 0.0
    good = 100.0 * sum(1 for r in ratios if r >= 7.0) / len(ratios) if ratios else 0.0
    return ('表格底色 %s；色块上的文字像素 %d 个，对比度 <%.1f:1 的占 %.1f%%'
            '（中位数 %.1f:1，>=7:1 的占 %.1f%%）'
            % (global_background.name(), text_pixels, min_ratio, share, median, good))


def main() -> int:
    """@brief 渲染并截图，返回进程退出码。"""
    parser = argparse.ArgumentParser(description='离屏渲染 Bus Monitor')
    parser.add_argument('--width', type=int, default=1204)
    parser.add_argument('--height', type=int, default=1100)
    parser.add_argument('--out', default=os.path.join(PROJECT_ROOT, 'artifacts', 'bus_monitor.png'))
    parser.add_argument('--seconds', type=float, default=2.5)
    parser.add_argument('--theme', choices=('light', 'dark', 'system'), default='system',
                        help='强制浅色/深色调色板（默认跟随系统）')
    parser.add_argument('--measure', metavar='PNG',
                        help='只统计这张图的"低对比度像素比例"，不渲染')
    parser.add_argument('--crop', metavar='X,Y,W,H',
                        help='统计前先裁剪（只看表格区域，别把示波器图算进来）')
    args = parser.parse_args()

    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QApplication

    from dronecan_gui_tool.widgets import map_7bit_to_color, theme
    from dronecan_gui_tool.widgets.bus_monitor.window import BusMonitorWindow
    from dronecan_gui_tool.widgets.theme import contrast_ratio

    if args.measure:
        app = QApplication(['measure'])
        image = QImage(args.measure)
        if args.crop:
            x, y, w, h = (int(v) for v in args.crop.split(','))
            image = image.copy(x, y, w, h)
        print('[render] %s -> %s' % (args.measure, _contrast_report(image)))
        return 0

    stream = make_stream()
    cursor = [0]
    next_at = [0.0]

    def get_frame():
        now = time.monotonic()
        if now < next_at[0]:
            return None
        next_at[0] = now + 1.0 / 120.0            # 约 120 帧/s 灌进去
        if cursor[0] >= len(stream):
            return None
        item = stream[cursor[0]]
        cursor[0] += 1
        return item

    app = QApplication(['bus_monitor_render'])
    app.setStyle('Fusion')
    if args.theme != 'system':
        from tests.theme_palettes import apply_palette
        apply_palette(args.theme == 'dark')
        print('[render] 调色板：%s（表底 %s）'
              % (args.theme, theme.background_color().name()))

    window = BusMonitorWindow(get_frame, 'dmcan:0.0')
    window.resize(args.width, args.height)
    window.show()
    window._log_widget._start_button.setChecked(True)     # 等价于点一下"开始捕获"

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    def shoot():
        window.grab().save(args.out)
        table_path = os.path.splitext(args.out)[0] + '_table.png'
        table_image = window._log_widget.table.grab().toImage()
        table_image.save(table_path)
        print('[render] %d 帧已灌入，截图：%s' % (cursor[0], args.out))
        print('[render] 表格区域：%s -> %s' % (table_path, _contrast_report(table_image)))
        for probe in (map_7bit_to_color(3), map_7bit_to_color(99), theme.text_color(),
                      theme.semantic('bad'), theme.semantic('muted')):
            print('[render] 颜色探针 %s（对表底 %s 对比度 %.2f）'
                  % (probe.name(), theme.background_color().name(),
                     contrast_ratio(probe, theme.background_color())))
        table = window._log_widget.table
        print('[render] 行数 %d，列数 %d，窗口 %dx%d'
              % (table.rowCount(), table.columnCount(), window.width(), window.height()))
        widths = [table.columnWidth(i) for i in range(table.columnCount())]
        print('[render] 列宽：%s（合计 %d）' % (widths, sum(widths)))
        headers = [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
        print('[render] 表头：%s' % headers)
        app.quit()

    QTimer.singleShot(int(args.seconds * 1000), shoot)
    app.exec()
    return 0


if __name__ == '__main__':
    sys.exit(main())
