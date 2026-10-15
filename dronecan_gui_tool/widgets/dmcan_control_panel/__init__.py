#
# DroneCAN GUI Tool — dmcan 后端（达妙 USB2CANFD / USB2FDCANDUAL 原生 USB）
#
# 本文件不属于上游 DroneCAN/gui_tool，为本 fork 新增；与上游同样以 MIT 发布（见 LICENSE）。
#
"""
dmcan 适配器控制面板：位定时、实时统计（含丢帧细分）、回环自检。

上游的 `CAN Adapter Control Panel` 是 SLCAN CLI 专用的，对 USB 后端没有意义；
本面板提供 dmcan 后端真正需要的东西：

* **设备与固件身份**（型号 / 序列号 / 固件版本 + 构建标识）——判"固件够不够新"看构建标识；
* **位定时**：仲裁段 / 数据段、是否 CAN FD；应用只改运行期，落盘要点"存入闪存"；
* **1 Hz 实时统计**：设备侧累计与每秒增量，并把 `rx_dropped` / `tx_dropped` 拆到具体原因
  （队列满 = 主机读得不够快；内部资源 = 设备侧；无额度/提交失败 = 总线或 MCAN 侧）；
* **主机侧收帧丢弃**：本进程收帧队列溢出次数，这是"GUI 卡住丢帧"的直接证据；
* **外部回环自检**：不接线、单模块自发自收，直接给出丢帧数与往返延迟。

界面取舍（都是为了让数字**一眼能读**）：正文用系统 UI 字体、只有数值列用等宽字体；
身份信息按"标签 / 值"两列对齐；统计按 接收 / 发送 / 主机侧 / 总线 分区，数字右对齐、
丢帧非零标红；顶部一行大字给"每秒收/发/丢"。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, List, Optional, Tuple

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QFrame,
                             QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                             QMessageBox, QPushButton, QSizePolicy, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from .. import get_monospace_font, show_error, theme

logger = logging.getLogger(__name__)

__all__ = ['spawn_window', 'DmCanControlPanel', 'STAT_SECTIONS']

#: 常见仲裁段位速率（bps）。
COMMON_BITRATES = (100000, 125000, 250000, 500000, 800000, 1000000)
#: 常见数据段位速率（bps），仅 CAN FD 用。
COMMON_DATA_BITRATES = (500000, 1000000, 2000000, 4000000, 5000000, 8000000)

#: 统计表：(分区, 标签, ChannelStats 字段名, 是否显示每秒速率, 说明)
STAT_SECTIONS: Tuple[Tuple[str, Tuple[Tuple[str, str, bool, str], ...]], ...] = (
    ('接收（设备侧）', (
        ('总线收帧', 'rx_received', True, '适配器从总线上收到的帧（累计值）'),
        ('收帧丢弃', 'rx_dropped', True, '合计；下面两行是细分原因'),
        ('　└ 接收队列满', 'rx_dropped_queue', True, '主机读得不够快（USB 侧积压）'),
        ('　└ 设备内部资源', 'rx_dropped_msgc', True, '帧写不进设备内部缓冲'),
    )),
    ('发送（设备侧）', (
        ('受理', 'tx_admitted', True, '上位机提交给设备的帧'),
        ('完成', 'tx_completed', True, '真正发到总线上的帧'),
        ('丢弃', 'tx_dropped', True, '合计；下面四行是细分原因'),
        ('　└ 无发送额度', 'tx_dropped_no_credit', True, '到达时额度耗尽'),
        ('　└ 发送队列满', 'tx_dropped_queue_full', True, '设备软件队列满'),
        ('　└ 提交 MCAN 失败', 'tx_dropped_submit_fail', True, '写 MCAN 发送 FIFO 失败'),
        ('　└ 通道已关闭', 'tx_dropped_ch_disabled', True, '被关通道/看门狗咬了'),
        ('失败', 'tx_failed', True, '总线错误导致的发送失败'),
        ('软件队列水位', 'sw_tx_fifo_level', False, '设备侧排队深度'),
        ('硬件 FIFO 水位', 'hw_tx_fifo_level', False, 'MCAN 侧排队深度'),
    )),
    ('主机侧（本进程）', (
        ('收帧', 'host_rx', True, 'SDK 回调交给本进程的帧'),
        ('发帧', 'host_tx', True, '本进程发出的帧'),
        ('收帧丢弃', 'host_rx_dropped', True, '本进程队列满——GUI 卡住时才会出现'),
    )),
)

#: 需要"非零就标红"的字段（丢帧类）。
DROP_FIELDS = ('rx_dropped', 'rx_dropped_queue', 'rx_dropped_msgc', 'tx_dropped',
               'tx_dropped_no_credit', 'tx_dropped_queue_full', 'tx_dropped_submit_fail',
               'tx_dropped_ch_disabled', 'tx_failed', 'host_rx_dropped')

def _semantic(name: str) -> str:
    """
    @brief 取语义色的十六进制串（富文本用），已按当前主题做过对比度适配。

    @note 不要在这里硬编码颜色：Qt6 跟随系统深色主题时，"深绿/深红/灰"会糊在深色底上。
    """
    return theme.semantic(name).name()


def _format_rate(value: Optional[float]) -> str:
    """@brief 速率格式化（None 表示设备侧还没上报）。"""
    if value is None:
        return '—'
    if abs(value) >= 1000:
        return '%.1fk' % (value / 1000.0)
    return '%.0f' % value


def _format_bps(value: int) -> str:
    """@brief 位速率格式化。"""
    if not value:
        return '?'
    if value % 1000000 == 0:
        return '%d Mbps' % (value // 1000000)
    if value % 1000 == 0:
        return '%d kbps' % (value // 1000)
    return '%d bps' % value


def _mono_font(point_delta: int = 0, bold: bool = False) -> QFont:
    """@brief 等宽字体（只给数字列/身份值用，正文用系统 UI 字体更好读）。"""
    font = get_monospace_font()
    if point_delta:
        font.setPointSize(max(7, font.pointSize() + point_delta))
    font.setBold(bold)
    return font


class _Worker(QObject):
    """把会阻塞的 SDK 调用（改位定时 ~1.2 s、回环自检数秒）挪到后台线程。"""

    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def run(self, function: Callable[[], Any]) -> None:
        """@brief 在后台线程跑 `function`，结果通过信号送回 UI 线程。"""
        def target():
            try:
                result = function()
            except Exception as ex:                      # noqa: BLE001 - 要原样报给用户
                logger.warning('后台任务失败：%s', ex, exc_info=True)
                self.failed.emit(str(ex))
            else:
                self.done.emit(result)

        threading.Thread(target=target, name='dmcan-panel', daemon=True).start()


class DmCanControlPanel(QWidget):
    """dmcan 适配器控制面板。"""

    REFRESH_MS = 1000

    def __init__(self, parent, node, iface_name: str):
        """
        @param parent 父窗口
        @param node `dronecan.node.Node`（后端是 `DmCanDriver`）
        @param iface_name 接口串
        """
        super(DmCanControlPanel, self).__init__(parent)
        self.setWindowTitle('dmcan Adapter Control Panel — 达妙 USB2CANFD / USB2FDCANDUAL')
        self._node = node
        self._driver = node.can_driver
        self._iface_name = iface_name
        self._last_stats = None
        self._busy = False
        self._rows: List[Tuple[int, str]] = []          # (表格行号, ChannelStats 字段名)

        self._worker = _Worker()
        self._worker.done.connect(self._on_worker_done)
        self._worker.failed.connect(self._on_worker_failed)

        self._build_ui()
        self._load_identity()
        self._refresh()

        self._timer = QTimer(self)
        self._timer.setSingleShot(False)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(self.REFRESH_MS)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        """@brief 搭界面：身份 -> 控制 -> 大字汇总 + 分区统计表。"""
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(self._build_identity_box())
        layout.addWidget(self._build_control_box())
        layout.addWidget(self._build_stats_box(), 1)

        note = QLabel('判链路是否丢帧看"外部回环自检"；'
                      '"接收队列满"说明主机读得不够快，"主机侧收帧丢弃"说明本进程卡住了。', self)
        note.setStyleSheet('color: %s;' % theme.semantic('muted').name())
        note.setToolTip('设备侧统计来自固件 1 Hz 周期上报，是**累计值**，表里"每秒"列才是增量。\n'
                        '丢帧细分只在固件对应记录到达后才有效（drop_flags），刚打开的一两秒内可能显示 0。')
        layout.addWidget(note)

        self.resize(1000, 760)

    def _build_identity_box(self) -> QGroupBox:
        """@brief 设备身份区：标签 / 值两列对齐，值用等宽字体。"""
        box = QGroupBox('设备', self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(4)

        self._labels = {}
        for key, caption in (('type', '型号'), ('serial', '序列号'), ('firmware', '固件'),
                             ('vidpid', 'VID:PID'), ('iface', '接口串'),
                             ('channel', '通道'), ('config', '位定时')):
            label = QLabel('—', self)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setFont(_mono_font())
            label.setWordWrap(True)
            form.addRow(QLabel('%s：' % caption, self), label)
            self._labels[key] = label

        box.setLayout(form)
        return box

    def _build_control_box(self) -> QGroupBox:
        """@brief 位定时与控制按钮。"""
        box = QGroupBox('通道位定时与控制', self)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self._bitrate = QComboBox(self)
        self._bitrate.setEditable(True)
        self._bitrate.insertItems(0, [str(x) for x in COMMON_BITRATES])
        self._bitrate.setToolTip('仲裁段位速率（bps）。只改运行期，掉电不保留。')

        self._fd = QCheckBox('CAN FD', self)
        self._fd.stateChanged.connect(self._on_fd_changed)
        self._fd.setToolTip('勾上才下发 CAN FD；数据段 > 1 Mbps 时 SDK 会自动开 TDC。')

        self._data_bitrate = QComboBox(self)
        self._data_bitrate.setEditable(True)
        self._data_bitrate.insertItems(0, [str(x) for x in COMMON_DATA_BITRATES])
        self._data_bitrate.setEnabled(False)

        buttons = (
            ('应用位定时', '把上面的位定时下发给通道（运行期，约 1.2 s 生效）', self._apply_bitrate),
            ('存入设备闪存', '把当前运行期参数写进设备 Flash（掉电保留）', self._save_params),
            ('使能通道', '打开通道（关机/复位后通道默认是关的）', lambda: self._toggle_channel(True)),
            ('关闭通道', '关闭通道，停止收发', lambda: self._toggle_channel(False)),
            ('清 bus-off', '清掉通道的 bus-off 状态', self._clear_bus_off),
            ('外部回环自检…', '不接线自发自收，验证整条收发链路（会临时切模式）', self._self_test),
        )
        for index, (text, tip, handler) in enumerate(buttons):
            button = QPushButton(text, self)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            grid.addWidget(button, 1 + index // 3, 2 + index % 3)

        grid.addWidget(QLabel('仲裁段 (bps)：', self), 0, 0)
        grid.addWidget(self._bitrate, 0, 1)
        grid.addWidget(self._fd, 0, 2)
        grid.addWidget(QLabel('数据段 (bps)：', self), 0, 3)
        grid.addWidget(self._data_bitrate, 0, 4)
        grid.setColumnStretch(5, 1)

        box.setLayout(grid)
        return box

    def _build_stats_box(self) -> QGroupBox:
        """@brief 大字汇总 + 分区统计表。"""
        box = QGroupBox('实时统计（设备侧 1 Hz 上报：累计值 + 每秒增量）', self)
        layout = QVBoxLayout()
        layout.setSpacing(6)

        self._headline = QLabel('等待数据…', self)
        self._headline.setTextFormat(Qt.TextFormat.RichText)
        self._headline.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._headline)

        separator = QFrame(self)
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)

        rows = sum(len(section[1]) for section in STAT_SECTIONS)
        self._table = QTableWidget(self)
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(['指标', '累计', '每秒'])
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.setWordWrap(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionsClickable(False)

        self._table.setRowCount(rows + len(STAT_SECTIONS))
        row = 0
        for section_name, entries in STAT_SECTIONS:
            self._set_section_row(row, section_name)
            row += 1
            for caption, field, show_rate, tip in entries:
                self._set_stat_row(row, caption, field, show_rate, tip)
                self._rows.append((row, field))
                row += 1

        self._table.verticalHeader().setDefaultSectionSize(22)
        for row_index in range(self._table.rowCount()):
            if self._table.item(row_index, 1) is None:
                self._table.setRowHeight(row_index, 26)

        layout.addWidget(self._table, 1)
        box.setLayout(layout)
        return box

    def _set_section_row(self, row: int, name: str) -> None:
        """@brief 分区标题行（整行合并、浅底、加粗）。"""
        item = QTableWidgetItem(name)
        font = self.font()
        font.setBold(True)
        item.setFont(font)
        item.setBackground(QBrush(theme.section_background()))
        item.setForeground(QBrush(theme.section_foreground()))
        self._table.setItem(row, 0, item)
        self._table.setSpan(row, 0, 1, 3)

    def _set_stat_row(self, row: int, caption: str, field: str, show_rate: bool, tip: str) -> None:
        """@brief 一行统计：标签左对齐、数值右对齐并用等宽字体。"""
        label = QTableWidgetItem(caption)
        label.setToolTip(tip)
        self._table.setItem(row, 0, label)

        for column, initial in ((1, '—'), (2, '—' if show_rate else '')):
            cell = QTableWidgetItem(initial)
            cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            cell.setFont(_mono_font())
            cell.setToolTip(tip)
            self._table.setItem(row, column, cell)

    # ------------------------------------------------------------- 身份/配置
    def _load_identity(self) -> None:
        """@brief 读一次设备/固件/通道信息并显示。"""
        session = self._driver.session
        try:
            info = self._driver.describe()
        except Exception as ex:
            self._labels['type'].setText('读设备信息失败：%s' % ex)
            return

        self._labels['type'].setText(str(info.get('type_name', '?')))
        self._labels['serial'].setText(str(info.get('serial') or '?'))
        self._labels['firmware'].setText(str(info.get('firmware', '?')))
        self._labels['vidpid'].setText('%04X:%04X' % (int(info.get('vid', 0)),
                                                      int(info.get('pid', 0))))
        self._labels['iface'].setText(str(info.get('iface', self._iface_name)))
        self._labels['channel'].setText('CH%d' % session.channel)
        self._labels['config'].setText(str(info.get('config', '?')))

        config = getattr(session, 'config', None)
        if config is not None:
            self._bitrate.setCurrentText(str(config.nominal))
            self._fd.setChecked(bool(config.canfd))
            if config.canfd:
                self._data_bitrate.setCurrentText(str(config.data))
            self._data_bitrate.setEnabled(bool(config.canfd))

    def _on_fd_changed(self) -> None:
        """@brief 勾 CAN FD 时才允许填数据段速率。"""
        self._data_bitrate.setEnabled(self._fd.isChecked())

    # ------------------------------------------------------------------ 操作
    def _run_async(self, description: str, function: Callable[[], Any]) -> None:
        """@brief 后台执行一个会阻塞的动作，期间禁掉按钮。"""
        if self._busy:
            return
        self._busy = True
        self._headline.setText('<span style="color:#666666;">正在执行：%s …</span>' % description)
        logger.info('dmcan 面板执行：%s', description)
        self._worker.run(function)

    def _on_worker_done(self, result) -> None:
        """@brief 后台任务成功：刷新显示，自检结果单独弹窗。"""
        self._busy = False
        if result is not None and hasattr(result, 'summary'):
            box = QMessageBox(self)
            box.setWindowTitle('回环自检结果')
            box.setText(result.summary())
            box.setInformativeText(result.detail)
            box.setIcon(QMessageBox.Icon.Information)
            box.show()
        self._load_identity()
        self._last_stats = None
        self._refresh()

    def _on_worker_failed(self, message: str) -> None:
        """@brief 后台任务失败：把原因原样显示出来。"""
        self._busy = False
        show_error('dmcan 操作失败', message, None, self)
        self._refresh()

    def _apply_bitrate(self) -> None:
        """@brief 应用位定时（运行期；掉电不保留）。"""
        try:
            nominal = int(self._bitrate.currentText())
            data = int(self._data_bitrate.currentText()) if self._fd.isChecked() else None
        except ValueError:
            show_error('参数错误', '位速率必须是整数', '例如 1000000', self)
            return
        session = self._driver.session
        self._run_async('应用位定时 %s' % _format_bps(nominal),
                        lambda: session.apply_bitrate(nominal, data))

    def _save_params(self) -> None:
        """@brief 把当前通道参数写进设备闪存。"""
        session = self._driver.session
        self._run_async('写入设备闪存', session.save_params)

    def _toggle_channel(self, enable: bool) -> None:
        """@brief 使能 / 关闭通道（同步很快，不占后台线程）。"""
        try:
            self._driver.session.device.enable_channel(self._driver.session.channel, enable)
        except Exception as ex:
            show_error('通道操作失败', str(ex), None, self)
        self._load_identity()

    def _clear_bus_off(self) -> None:
        """@brief 清通道 bus-off。"""
        try:
            self._driver.clear_bus_off()
        except Exception as ex:
            show_error('清 bus-off 失败', str(ex), None, self)

    def _self_test(self) -> None:
        """@brief 外部回环自检（会临时切模式，需确认）。"""
        answer = QMessageBox.question(
            self, '外部回环自检',
            '将把通道临时切到"外部回环"模式并自发自收 1000 帧，期间：\n'
            '• 帧照常驱动总线（总线上其它节点会看到这批测试帧）；\n'
            '• 本机在这几秒内不接收正常总线流量；\n'
            '• 测完自动恢复原来的模式与位定时。\n\n'
            '建议在没有其它节点、或确认可以短暂打断时执行。是否继续？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        session = self._driver.session
        driver = self._driver

        def job():
            driver.pause_rx(True)
            try:
                return session.self_test(count=1000, timeout=5.0)
            finally:
                driver.pause_rx(False)

        self._run_async('外部回环自检', job)

    # ------------------------------------------------------------------ 刷新
    def _refresh(self) -> None:
        """@brief 1 Hz 刷新统计（设备侧累计 + 每秒增量）。"""
        if self._busy:
            return
        try:
            stats = self._driver.statistics()
        except Exception as ex:
            self._headline.setText('<span style="color:%s;">读统计失败：%s</span>'
                                   % (_semantic('bad'), ex))
            return

        diff = stats.diff(self._last_stats) if self._last_stats is not None else None
        self._last_stats = stats

        for row, field in self._rows:
            value = int(getattr(stats, field, 0))
            rate = ''
            if diff is not None and self._table.item(row, 2).text() != '':
                rate = _format_rate(getattr(diff, field, 0) / diff.seconds if diff.seconds else 0)
            elif self._table.item(row, 2).text() != '':
                rate = '—'

            cumulative_item = self._table.item(row, 1)
            cumulative_item.setText(str(value))
            if field in DROP_FIELDS and value:
                cumulative_item.setForeground(QBrush(theme.semantic('bad')))
            elif field in DROP_FIELDS:
                cumulative_item.setForeground(QBrush(theme.semantic('muted')))

            rate_item = self._table.item(row, 2)
            rate_item.setText(rate)
            seconds_value = (getattr(diff, field, 0) if diff is not None else 0)
            if field in DROP_FIELDS and seconds_value:
                rate_item.setForeground(QBrush(theme.semantic('bad')))
            else:
                rate_item.setForeground(QBrush(theme.semantic('muted')))

        self._headline.setText(self._format_headline(stats, diff))

    def _format_headline(self, stats, diff) -> str:
        """@brief 顶部大字：每秒收/发、丢帧与总线状态。"""
        if stats.pending:
            return ('<span style="color:%s; font-size:12pt;">'
                    '等待设备的第一条周期上报（1 Hz）…</span>' % _semantic('muted'))

        normal_color = theme.text_color().name()
        muted_color = _semantic('muted')

        def big(text, color=None, size=20):
            return ('<span style="font-size:%dpt; font-weight:600; color:%s;">%s</span>'
                    % (size, color or normal_color, text))

        def unit(text):
            return '<span style="color:%s;">%s</span>' % (muted_color, text)

        dropped = 0
        detail = ''
        if diff is not None:
            dropped = (diff.rx_dropped + diff.tx_dropped + diff.host_rx_dropped)
            if diff.rx_dropped or diff.tx_dropped or diff.host_rx_dropped:
                detail = ('　设备收 %d（队列 %d / 内部 %d）· 设备发 %d · 主机 %d'
                          % (diff.rx_dropped, diff.rx_dropped_queue, diff.rx_dropped_msgc,
                             diff.tx_dropped, diff.host_rx_dropped))

        rx = _format_rate(diff.rx_fps) if diff is not None else '—'
        tx = _format_rate(diff.tx_fps) if diff is not None else '—'
        drop_color = _semantic('bad') if dropped else _semantic('ok')

        return ('%s %s　　%s %s　　%s %s%s<br>'
                '<span style="color:%s;">总线 %s · TEC %d · REC %d · '
                '软件队列 %d · 硬件 FIFO %d · 通道%s</span>'
                % (big(rx), unit('收帧/s'), big(tx), unit('发帧/s'),
                   big(str(dropped), drop_color), unit('丢帧/s'), detail,
                   muted_color, stats.bus_state_name, stats.tec, stats.rec,
                   stats.sw_tx_fifo_level, stats.hw_tx_fifo_level,
                   '已使能' if stats.enabled else '未使能'))

    def closeEvent(self, event) -> None:
        """@brief 关窗停掉刷新定时器。"""
        self._timer.stop()
        super(DmCanControlPanel, self).closeEvent(event)


def spawn_window(parent, node, iface_name: str) -> DmCanControlPanel:
    """
    @brief 打开 dmcan 适配器控制面板（上游 `spawn_window` 的同名接口）。

    @param parent 父窗口
    @param node `dronecan.node.Node`
    @param iface_name 接口串
    @return 面板窗口对象
    """
    window = DmCanControlPanel(parent, node, iface_name)
    window.show()
    return window
