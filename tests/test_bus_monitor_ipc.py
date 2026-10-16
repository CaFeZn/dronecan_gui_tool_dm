# -*- coding: utf-8 -*-
"""Bus Monitor 的 IPC 通道：必须封顶，而且必须关得掉。"""

import os
import subprocess
import sys
import textwrap
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dronecan_gui_tool.widgets.bus_monitor import IPCChannel, IPC_COMMAND_STOP   # noqa: E402


def _read_one(channel, timeout=3.0):
    """
    @brief 从通道里读一条，最多等 `timeout` 秒。

    @note `multiprocessing.Queue` 的投递是**异步**的（有自己的 feeder 线程），put 完立刻
          get 可能读空——这条在 Windows 上曾经"侥幸"通过，一上 Linux 就暴露成偶发失败。
          所以这里统一用带期限的重试读，而不是一次性的 get_nowait()。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        received, obj = channel.receive_nonblocking()
        if received:
            return obj
        time.sleep(0.02)
    return None


def _drain(channel, quiet=0.3, timeout=5.0):
    """@brief 把通道读空（连续 `quiet` 秒读不到东西才算空），返回最后一条。"""
    last = None
    deadline = time.monotonic() + timeout
    quiet_since = None
    while time.monotonic() < deadline:
        received, obj = channel.receive_nonblocking()
        if received:
            last = obj
            quiet_since = None
        else:
            if quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since > quiet:
                break
            time.sleep(0.02)
    return last


class IpcChannelTest(unittest.TestCase):
    """监视器每 100 ms 只取 1 帧，投递端必须自己封顶（上游原来是无上限的）。"""

    def test_bounded_queue(self):
        """灌 3 倍上限的帧，队列不许超过上限，也不许阻塞。"""
        channel = IPCChannel()
        try:
            for index in range(IPCChannel.MAX_PENDING * 3):
                channel.send_nonblocking(('rx', index))
            size = channel._q.qsize()
            self.assertLessEqual(size, IPCChannel.MAX_PENDING + 1,
                                 '待处理量必须封顶（实测会涨到几万条）')
            self.assertGreater(channel.dropped, 0, '满了应当记账')
        finally:
            channel.close()

    def test_keeps_latest_frames(self):
        """满了以后保留的应当是**最近的**帧，而不是最早的（监视器要跟住总线）。"""
        channel = IPCChannel()
        try:
            for index in range(IPCChannel.MAX_PENDING + 500):
                channel.send_nonblocking(('rx', index))
            last = _drain(channel)
            self.assertIsNotNone(last, '应当能读到至少一条')
            self.assertEqual(last[1], IPCChannel.MAX_PENDING + 499)
        finally:
            channel.close()

    def test_control_message_still_get_through(self):
        """STOP 指令要能塞进去（关窗口时用）。"""
        channel = IPCChannel()
        try:
            channel.send_nonblocking(IPC_COMMAND_STOP)
            self.assertEqual(_read_one(channel), IPC_COMMAND_STOP)
        finally:
            channel.close()

    def test_close_does_not_hang_the_process(self):
        """
        塞满队列后 close() 必须能让进程正常退出。

        现场意义：`multiprocessing.Queue` 的 feeder 线程在"队列里还有积压、管道写不进去"
        时会让**整个 GUI 关不掉**（子进程已被 terminate，没人再取数据）。这里开子进程验证：
        真挂住的话 subprocess.run 会超时。
        """
        script = textwrap.dedent("""
            import sys
            sys.path.insert(0, %r)
            from dronecan_gui_tool.widgets.bus_monitor import IPCChannel
            channel = IPCChannel()
            for index in range(IPCChannel.MAX_PENDING * 2):
                channel.send_nonblocking(('rx', index))
            channel.close()
            print('closed')
        """) % PROJECT_ROOT
        result = subprocess.run([sys.executable, '-c', script],
                                capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        self.assertIn(b'closed', result.stdout)


if __name__ == '__main__':
    unittest.main()
