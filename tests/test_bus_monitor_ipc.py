# -*- coding: utf-8 -*-
"""Bus Monitor 的 IPC 通道：必须封顶，不能在快总线上无限增长。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dronecan_gui_tool.widgets.bus_monitor import IPCChannel, IPC_COMMAND_STOP   # noqa: E402


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
            channel._q.close()

    def test_keeps_latest_frames(self):
        """满了以后保留的应当是**最近的**帧，而不是最早的（监视器要跟住总线）。"""
        channel = IPCChannel()
        try:
            for index in range(IPCChannel.MAX_PENDING + 500):
                channel.send_nonblocking(('rx', index))
            last = None
            while True:
                try:
                    last = channel._q.get_nowait()
                except Exception:
                    break
            self.assertIsNotNone(last)
            self.assertEqual(last[1], IPCChannel.MAX_PENDING + 499)
        finally:
            channel._q.close()

    def test_control_message_still_get_through(self):
        """STOP 指令要能塞进去（关窗口时用）。"""
        channel = IPCChannel()
        try:
            channel.send_nonblocking(IPC_COMMAND_STOP)
            self.assertEqual(channel._q.get_nowait(), IPC_COMMAND_STOP)
        finally:
            channel._q.close()


if __name__ == '__main__':
    unittest.main()
