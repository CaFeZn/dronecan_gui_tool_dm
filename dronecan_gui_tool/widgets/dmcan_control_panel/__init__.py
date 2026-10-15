#
# DroneCAN GUI Tool — dmcan 后端（达妙 USB2CANFD / USB2FDCANDUAL 原生 USB）
#
# 本文件不属于上游 DroneCAN/gui_tool，为本 fork 新增；与上游同样以 MIT 发布（见 LICENSE）。
#
"""
dmcan 适配器控制面板：位定时、实时统计（含丢帧细分）、回环自检。

上游的 `CAN Adapter Control Panel` 是 SLCAN CLI 专用的，对 USB 后端没有意义；
本面板提供的是 dmcan 后端真正需要的东西：

* **设备与固件身份**（型号 / 序列号 / 固件版本 + 构建标识）——判"固件够不够新"看构建标识；
* **位定时**：仲裁段 / 数据段、是否 CAN FD、采样点、模式；应用只改运行期，落盘要点"存入闪存"；
* **1 Hz 实时统计**：设备侧累计与每秒增量，并把 `rx_dropped` / `tx_dropped` 拆到具体原因
  （队列满 = 主机读得不够快；内部资源 = 设备侧；无额度/提交失败 = 总线或 MCAN 侧）；
* **主机侧收帧丢弃**：本进程的收帧队列溢出次数，这是"GUI 卡住丢帧"的直接证据；
* **外部回环自检**：不接线、单模块自发自收，直接给出丢帧数与往返延迟——用来证明链路本身不丢帧。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout,
                             QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from .. import get_monospace_font, show_error

logger = logging.getLogger(__name__)

__all__ = ['spawn_window', 'DmCanControlPanel']

#: 常见仲裁段位速率（bps）。
COMMON_BITRATES = (100000, 125000, 250000, 500000, 800000, 1000000)
#: 常见数据段位速率（bps），仅 CAN FD 用。
COMMON_DATA_BITRATES = (500000, 1000000, 2000000, 4000000, 5000000, 8000000)

#: 统计表：(标签, ChannelStats 字段名, 是否显示每秒速率, 提示)
STAT_ROWS = (
    ('设备侧 收帧', 'rx_received', True, '总线收到的帧（累计）'),
    ('设备侧 收帧丢弃', 'rx_dropped', True, '合计；细看下面两行'),
    ('  其中 接收队列满', 'rx_dropped_queue', True, '主机读得不够快（USB 侧积压）'),
    ('  其中 设备内部资源', 'rx_dropped_msgc', True, '帧写不进设备内部缓冲'),
    ('设备侧 发帧受理', 'tx_admitted', True, '上位机提交给设备的帧'),
    ('设备侧 发帧完成', 'tx_completed', True, '真正发到总线上的帧'),
    ('设备侧 发帧丢弃', 'tx_dropped', True, '合计；细看下面四行'),
    ('  其中 无发送额度', 'tx_dropped_no_credit', True, '到达时额度耗尽'),
    ('  其中 发送队列满', 'tx_dropped_queue_full', True, '设备软件队列满'),
    ('  其中 提交 MCAN 失败', 'tx_dropped_submit_fail', True, '写 MCAN 发送 FIFO 失败'),
    ('  其中 通道已关闭', 'tx_dropped_ch_disabled', True, '被关通道/看门狗咬了'),
    ('设备侧 发帧失败', 'tx_failed', True, '总线错误导致失败'),
    ('软件发送队列水位', 'sw_tx_fifo_level', False, '设备侧排队深度'),
    ('硬件发送 FIFO 水位', 'hw_tx_fifo_level', False, 'MCAN 侧排队深度'),
    ('主机侧 收帧', 'host_rx', True, 'SDK 回调交给本进程的帧'),
    ('主机侧 发帧', 'host_tx', True, '本进程发出的帧'),
    ('主机侧 收帧丢弃', 'host_rx_dropped', True, '本进程队列满（GUI 卡住时才会出现）'),
)


def _format_rate(value: Optional[float]) -> str:
    """@brief 速率格式化（None 表示设备侧还没上报）。"""
    if value is None:
        return '—'
    if abs(value) >= 1000:
        return '%.1fk/s' % (value / 1000.0)
    return '%.0f/s' % value


def _format_rate_bps(value: int) -> str:
    """@brief 位速率格式化。"""
    if not value:
        return '?'
    if value % 1000000 == 0:
        return '%d Mbps' % (value // 1000000)
    if value % 1000 == 0:
        return '%d kbps' % (value // 1000)
    return '%d bps' % value


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
        self.setWindowTitle('dmcan Adapter Control Panel')
        self._node = node
        self._driver = node.can_driver
        self._iface_name = iface_name
        self._last_stats = None
        self._busy = False

        self.setFont(get_monospace_font())
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
        """@brief 搭界面：身份 -> 控制 -> 统计。"""
        self._identity = QLabel(self)
        self._identity.setWordWrap(True)

        identity_box = QGroupBox('设备', self)
        identity_layout = QVBoxLayout()
        identity_layout.addWidget(self._identity)
        identity_box.setLayout(identity_layout)

        control_box = QGroupBox('通道位定时与控制', self)
        control_layout = QGridLayout()

        self._bitrate = QComboBox(self)
        self._bitrate.setEditable(True)
        self._bitrate.insertItems(0, [str(x) for x in COMMON_BITRATES])
        self._fd = QCheckBox('CAN FD', self)
        self._fd.stateChanged.connect(self._on_fd_changed)
        self._data_bitrate = QComboBox(self)
        self._data_bitrate.setEditable(True)
        self._data_bitrate.insertItems(0, [str(x) for x in COMMON_DATA_BITRATES])
        self._data_bitrate.setEnabled(False)

        apply_button = QPushButton('应用位定时（运行期）', self)
        apply_button.clicked.connect(self._apply_bitrate)
        save_button = QPushButton('存入设备闪存', self)
        save_button.clicked.connect(self._save_params)
        enable_button = QPushButton('使能通道', self)
        enable_button.clicked.connect(lambda: self._toggle_channel(True))
        disable_button = QPushButton('关闭通道', self)
        disable_button.clicked.connect(lambda: self._toggle_channel(False))
        clear_button = QPushButton('清 bus-off', self)
        clear_button.clicked.connect(self._clear_bus_off)
        selftest_button = QPushButton('外部回环自检…', self)
        selftest_button.clicked.connect(self._self_test)

        row = 0
        control_layout.addWidget(QLabel('仲裁段位速率 (bps):'), row, 0)
        control_layout.addWidget(self._bitrate, row, 1)
        control_layout.addWidget(self._fd, row, 2)
        control_layout.addWidget(self._data_bitrate, row, 3)
        row += 1
        buttons = QHBoxLayout()
        for button in (apply_button, save_button, enable_button, disable_button,
                       clear_button, selftest_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        control_layout.addLayout(buttons, row, 0, 1, 4)
        control_box.setLayout(control_layout)

        self._summary = QLabel(self)
        self._summary.setWordWrap(True)

        self._table = QTableWidget(len(STAT_ROWS), 3, self)
        self._table.setHorizontalHeaderLabels(['指标', '累计', '每秒'])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        for row_index, (label, _field, _rate, tip) in enumerate(STAT_ROWS):
            item = QTableWidgetItem(label)
            item.setToolTip(tip)
            self._table.setItem(row_index, 0, item)
            for column in (1, 2):
                self._table.setItem(row_index, column, QTableWidgetItem('—'))
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setStretchLastSection(True)

        stats_box = QGroupBox('实时统计（设备侧 1 Hz 上报，累计值 + 每秒增量）', self)
        stats_layout = QVBoxLayout()
        stats_layout.addWidget(self._summary)
        stats_layout.addWidget(self._table)
        stats_box.setLayout(stats_layout)

        note = QLabel('提示：设备侧"收帧丢弃/接收队列满"说明主机读得不够快；'
                      '"主机侧收帧丢弃"说明本 GUI 进程卡住了。'
                      '判定链路本身是否丢帧请用"外部回环自检"（不接线）。', self)
        note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(identity_box)
        layout.addWidget(control_box)
        layout.addWidget(stats_box, 1)
        layout.addWidget(note)
        self.setLayout(layout)
        self.resize(760, 620)

    # ------------------------------------------------------------- 身份/配置
    def _load_identity(self) -> None:
        """@brief 读一次设备/固件/通道信息并显示。"""
        try:
            info = self._driver.describe()
        except Exception as ex:
            self._identity.setText('读设备信息失败：%s' % ex)
            return
        text = ('接口 %s\n型号 %s   序列号 %s   VID:PID %04X:%04X\n固件 %s\n当前通道 %s\n主机计数 %s'
                % (info.get('iface', self._iface_name),
                   info.get('type_name', '?'), info.get('serial', '?'),
                   int(info.get('vid', 0)), int(info.get('pid', 0)),
                   info.get('firmware', '?'), info.get('config', '?'),
                   info.get('counters', {})))
        self._identity.setText(text)

        config = getattr(self._driver.session, 'config', None)
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
        self._summary.setText('正在执行：%s …' % description)
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
        self._run_async('应用位定时 %s' % _format_rate_bps(nominal),
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
        """@brief 1 Hz 刷新统计表（设备侧累计 + 每秒增量）。"""
        if self._busy:
            return
        try:
            stats = self._driver.statistics()
        except Exception as ex:
            self._summary.setText('读统计失败：%s' % ex)
            return
        diff = None
        if self._last_stats is not None:
            diff = stats.diff(self._last_stats)
        self._last_stats = stats

        for row_index, (_label, field, show_rate, _tip) in enumerate(STAT_ROWS):
            self._table.item(row_index, 1).setText(str(getattr(stats, field, 0)))
            if show_rate and diff is not None:
                self._table.item(row_index, 2).setText(
                    _format_rate(getattr(diff, field, 0) / diff.seconds if diff.seconds else 0))
            elif show_rate:
                self._table.item(row_index, 2).setText('—')

        if stats.pending:
            self._summary.setText('等待设备的第一条周期上报（1 Hz）…')
        else:
            text = ('%s  |  总线 %s  TEC=%d REC=%d  软件发送队列 %d  硬件发送 FIFO %d'
                    % ('收 %.1f 帧/s 发 %.1f 帧/s' % (diff.rx_fps, diff.tx_fps) if diff
                       else '收 — 发 —',
                       stats.bus_state_name, stats.tec, stats.rec,
                       stats.sw_tx_fifo_level, stats.hw_tx_fifo_level))
            if diff is not None and (diff.rx_dropped or diff.tx_dropped or diff.host_rx_dropped):
                text += ('  |  ⚠ 这一秒丢帧：设备收 %d（队列 %d/内部 %d）设备发 %d 主机 %d'
                         % (diff.rx_dropped, diff.rx_dropped_queue, diff.rx_dropped_msgc,
                            diff.tx_dropped, diff.host_rx_dropped))
            self._summary.setText(text)

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
