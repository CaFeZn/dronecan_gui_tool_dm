#
# Copyright (C) 2016  UAVCAN Development Team  <uavcan.org>
#
# This software is distributed under the terms of the MIT License.
#
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

import os
import sys
import queue
import logging
import multiprocessing
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
from .window import BusMonitorWindow
from .. import theme

logger = logging.getLogger(__name__)

try:
    # noinspection PyUnresolvedReferences
    sys.getwindowsversion()
    RUNNING_ON_WINDOWS = True
except AttributeError:
    RUNNING_ON_WINDOWS = False
    PARENT_PID = os.getppid()


class IPCChannel:
    """
    This class is built as an abstraction over the underlying IPC communication channel.
    """

    #: 待处理上限（帧）。监视器进程每 100 ms 只取 1 帧（见 `get_frame`），所以只要总线快于
    #: ~10 帧/s，这个队列就会一直涨：实测 6 秒灌 4 万帧时涨到 6.5 万条待处理（几十 MB，
    #: 长时间开着只增不减）。这里封顶，满了**丢最旧的一条**——监视器要看的是"最近的总线"，
    #: 卡在几分钟前的帧没有意义；丢帧数与原因在 dmcan 面板/CLI 里另有精确统计。
    MAX_PENDING = 20000

    def __init__(self):
        # Queue is slower than pipe, but it allows to implement non-blocking sending easier.
        self._q = multiprocessing.Queue(self.MAX_PENDING)
        self._dropped = 0

    @property
    def dropped(self):
        """@brief 因队列满被丢弃的帧数（监视器显示的是最近的总线，不是全量）。"""
        return self._dropped

    def send_nonblocking(self, obj):
        try:
            self._q.put_nowait(obj)
            return
        except queue.Full:
            pass
        # 满了：丢掉最旧的一条再塞新的，保证显示跟着总线走而不是越落越远
        try:
            self._q.get_nowait()
            self._q.put_nowait(obj)
        except (queue.Empty, queue.Full):
            pass
        self._dropped += 1
        if self._dropped % 5000 == 1:
            logger.warning('Bus Monitor 处理不过来，已丢弃 %d 帧（每 100 ms 只刷新一次，'
                           '属上游设计；精确丢帧统计请看 dmcan 适配器面板）', self._dropped)

    def receive_nonblocking(self):
        """Returns: (True, object) if successful, (False, None) if no data to read """
        try:
            return True, self._q.get_nowait()
        except queue.Empty:
            return False, None

    def close(self):
        """
        @brief 关闭通道：先把积压倒干净，再关队列。

        @note **不清空就直接关会挂住整个进程**：`multiprocessing.Queue` 的 feeder 线程在
              "队列里还有数据、管道又写不进去"的时候会一直阻塞，解释器退出阶段
              （`Queue._finalize_join`）要等它，结果就是 GUI 关不掉。监视器子进程此时
              已经被 terminate，没人再来取数据，越积越死。上游原本根本没调过 close()，
              队列里的积压（快总线上动辄几万条）就是这样把退出拖住的。
        """
        while True:
            try:
                self._q.get_nowait()
            except (queue.Empty, OSError, ValueError):
                break
        try:
            self._q.close()
        except Exception:
            pass
        try:
            self._q.cancel_join_thread()      # 兜底：解释器别再等 feeder 线程
        except Exception:
            pass


IPC_COMMAND_STOP = 'stop'


def _process_entry_point(channel, iface_name):
    logger.info('Bus monitor process started with PID %r', os.getpid())
    app = QApplication(sys.argv)    # Inheriting args from the parent process

    # Set the style for Qt6 to be same across systems
    app.setStyle("Fusion")
    # 跟主窗口用同一套深浅外观（这是独立进程，读的是同一份 QSettings）
    theme.apply_saved_mode(app)

    def exit_if_should():
        if RUNNING_ON_WINDOWS:
            return False
        else:
            return os.getppid() != PARENT_PID       # Parent is dead

    exit_check_timer = QTimer()
    exit_check_timer.setSingleShot(False)
    exit_check_timer.timeout.connect(exit_if_should)
    exit_check_timer.start(2000)

    def get_frame():
        received, obj = channel.receive_nonblocking()
        if received:
            if obj == IPC_COMMAND_STOP:
                logger.info('Bus monitor process has received a stop request, goodbye')
                app.exit(0)
            else:
                return obj

    win = BusMonitorWindow(get_frame, iface_name)
    win.show()

    logger.info('Bus monitor process %r initialized successfully, now starting the event loop', os.getpid())
    sys.exit(app.exec_())


# TODO: Duplicates PlotterManager; refactor into an abstract process factory
class BusMonitorManager:
    def __init__(self, node, can_iface_name):
        self._node = node
        self._can_iface_name = can_iface_name
        self._inferiors = []    # process object, channel
        self._hook_handle = None

    def _frame_hook(self, direction, frame):
        for proc, channel in self._inferiors[:]:
            if proc.is_alive():
                try:
                    channel.send_nonblocking((direction, frame))
                except Exception:
                    logger.error('Failed to send data to process %r', proc, exc_info=True)
            else:
                logger.info('Bus monitor process %r appears to be dead, removing', proc)
                self._inferiors.remove((proc, channel))

    def spawn_monitor(self):
        channel = IPCChannel()

        if self._hook_handle is None:
            self._hook_handle = self._node.can_driver.add_io_hook(self._frame_hook)

        proc = multiprocessing.Process(target=_process_entry_point, name='bus_monitor',
                                       args=(channel, self._can_iface_name))
        proc.daemon = True
        proc.start()

        self._inferiors.append((proc, channel))

        logger.info('Spawned new bus monitor process %r', proc)

    def close(self):
        try:
            self._hook_handle.remove()
        except Exception:
            pass

        for _, channel in self._inferiors:
            try:
                channel.send_nonblocking(IPC_COMMAND_STOP)
            except Exception:
                pass

        for proc, _ in self._inferiors:
            try:
                proc.join(1)
            except Exception:
                pass

        for proc, _ in self._inferiors:
            try:
                proc.terminate()
            except Exception:
                pass

        # 子进程已经没了，剩下的积压必须由我们自己清掉，否则进程退不出去（见 IPCChannel.close）
        for _, channel in self._inferiors:
            try:
                channel.close()
            except Exception:
                pass
        self._inferiors = []
