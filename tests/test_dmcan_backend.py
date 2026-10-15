# -*- coding: utf-8 -*-
"""dmcan 后端（会话 + pydronecan 驱动）的行为测试，跑在仿真 SDK 上。"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dronecan_gui_tool.driver import dmcan_iface, dmcan_session, dmsdk  # noqa: E402
from dronecan_gui_tool.driver.dmcan_driver import DmCanDriver  # noqa: E402
from dronecan_gui_tool.driver.dmcan_session import DmCanSession, SessionError  # noqa: E402
from tests import fake_dmcan  # noqa: E402


class BackendTestBase(unittest.TestCase):
    """注入仿真 SDK 的公共夹具。"""

    def setUp(self):
        self.fake = fake_dmcan.install([
            fake_dmcan.make_dual(serial='DEV0', index=0),
            fake_dmcan.make_dual(serial='DEV1', index=1),
        ])
        dmsdk.reset()
        dmcan_iface.set_session_active(False)
        dmcan_iface.invalidate_cache()
        # 真机等硬件重载是 1.2 s；测试里缩短，逻辑不变
        self._reload_settle, self._mode_settle = dmcan_session.RELOAD_SETTLE, dmcan_session.MODE_SETTLE
        dmcan_session.RELOAD_SETTLE = 0.01
        dmcan_session.MODE_SETTLE = 0.01
        self._drivers = []

    def tearDown(self):
        for driver in self._drivers:
            try:
                driver.close()
            except Exception:
                pass
        dmcan_session.RELOAD_SETTLE = self._reload_settle
        dmcan_session.MODE_SETTLE = self._mode_settle
        fake_dmcan.uninstall()
        dmsdk.reset()
        dmcan_iface.invalidate_cache()

    def make_driver(self, iface, **kwargs):
        """@brief 建一个驱动并登记，tearDown 时统一关闭。"""
        driver = DmCanDriver(iface, **kwargs)
        self._drivers.append(driver)
        return driver


class SessionTest(BackendTestBase):
    """会话：打开、位定时、使能、统计。"""

    def test_open_keeps_matching_bitrate(self):
        """设备位定时已经匹配时不应改写设备（只有使能写下去）。"""
        session = DmCanSession('dmcan:s:DEV0.0@1000000')
        session.open()
        self._drivers.append(session)
        self.assertTrue(session.opened)
        self.assertFalse(session.bitrate_written)
        self.assertTrue(session.device._backend.channels[0].enabled)

    def test_open_writes_new_bitrate(self):
        """要求 500 kbps 而设备是 1 Mbps 时，要改位定时并复核生效。"""
        session = DmCanSession('dmcan:s:DEV0.0@500000')
        session.open()
        self._drivers.append(session)
        self.assertTrue(session.bitrate_written)
        self.assertEqual(session.config.nominal, 500000)
        self.assertTrue(session.device._backend.channels[0].enabled)

    def test_open_canfd_writes_fd_config(self):
        """+数据段速率 = CAN FD，位定时要真的写成 FD。"""
        session = DmCanSession('dmcan:s:DEV0.0@1000000+2000000')
        session.open()
        self._drivers.append(session)
        self.assertTrue(session.config.canfd)
        self.assertEqual(session.config.nominal, 1000000)
        self.assertEqual(session.config.data, 2000000)

    def test_channel_selection(self):
        """通道号来自接口串。"""
        session = DmCanSession('dmcan:s:DEV0.1')
        session.open()
        self._drivers.append(session)
        self.assertEqual(session.channel, 1)
        self.assertTrue(session.device._backend.channels[1].enabled)
        self.assertFalse(session.device._backend.channels[0].enabled)

    def test_missing_device(self):
        """序列号对不上时给出可操作报错，而不是连到别的设备。"""
        with self.assertRaises(SessionError) as ctx:
            DmCanSession('dmcan:s:NOPE.0').open()
        self.assertIn('NOPE', str(ctx.exception))

    def test_no_devices(self):
        """一台设备都没有时报错清晰。"""
        self.fake.DmCanContext.reset([])
        with self.assertRaises(SessionError) as ctx:
            DmCanSession('dmcan:0').open()
        self.assertIn('没有找到', str(ctx.exception))

    def test_open_failure_hint(self):
        """设备被占用/拔掉时把原因与建议一起抛出来。"""
        self.fake.DmCanContext._backends[0].fail_open = True
        with self.assertRaises(SessionError) as ctx:
            DmCanSession('dmcan:s:DEV0.0').open()
        self.assertIn('占用', str(ctx.exception))

    def test_unplugged_device(self):
        """已拔出的设备不尝试打开。"""
        self.fake.DmCanContext._backends[0].present = False
        with self.assertRaises(SessionError) as ctx:
            DmCanSession('dmcan:s:DEV0.0').open()
        self.assertIn('拔出', str(ctx.exception))

    def test_enable_failure_hint(self):
        """使能失败时提示固件版本要求。"""
        self.fake.DmCanContext._backends[0].fail_enable = True
        with self.assertRaises(SessionError) as ctx:
            DmCanSession('dmcan:s:DEV0.0').open()
        self.assertIn('固件', str(ctx.exception))

    def test_stats_and_diff(self):
        """统计是累计值，增量才是速率/丢帧。"""
        session = DmCanSession('dmcan:s:DEV0.0')
        session.open()
        self._drivers.append(session)
        backend = self.fake.DmCanContext._backends[0]
        first = session.statistics()
        backend.channels[0].stats.tx_completed += 100
        backend.channels[0].stats.rx_received += 50
        backend.channels[0].stats.rx_dropped += 3
        backend.channels[0].stats.rx_dropped_queue += 3
        time.sleep(0.01)
        second = session.statistics()
        diff = second.diff(first)
        self.assertGreater(diff.seconds, 0)
        self.assertEqual(diff.tx_completed, 100)
        self.assertEqual(diff.rx_received, 50)
        self.assertEqual(diff.rx_dropped, 3)
        self.assertEqual(diff.rx_dropped_queue, 3)
        self.assertIn('丢帧', diff.summary())

    def test_selftest_loopback(self):
        """外部回环自检：不接线也能自发自收，且自检帧不污染正常收帧。"""
        received = []
        session = DmCanSession('dmcan:s:DEV0.0@1000000',
                               callback=lambda s, e: received.append(e))
        session.open()
        self._drivers.append(session)
        result = session.self_test(count=64, timeout=2.0)
        self.assertEqual(result.sent, 64)
        self.assertEqual(result.received, 64)
        self.assertEqual(result.missing, 0)
        self.assertEqual(result.duplicated, 0)
        self.assertIn('回环自检', result.summary())
        # 自检帧被摘掉了，回调里一帧都不该有
        self.assertEqual(received, [])
        # 模式已恢复成正常
        self.assertEqual(session.config.mode, 0)

    def test_session_active_counting_is_balanced(self):
        """活跃会话计数要配对：打开失败、未打开就 close 都不能误减别人的计数。"""
        good = DmCanSession('dmcan:s:DEV0.0')
        good.open()
        self._drivers.append(good)
        self.assertEqual(dmcan_iface.active_sessions(), 1)

        with self.assertRaises(SessionError):
            DmCanSession('dmcan:s:NOPE.0').open()
        self.assertEqual(dmcan_iface.active_sessions(), 1, '打开失败的会话不该减计数')

        never_opened = DmCanSession('dmcan:s:DEV1.0')
        never_opened.close()
        self.assertEqual(dmcan_iface.active_sessions(), 1, '没打开过就 close 不该减计数')

        good.close()
        self.assertEqual(dmcan_iface.active_sessions(), 0)
        good.close()
        self.assertEqual(dmcan_iface.active_sessions(), 0, 'close 要幂等')

    def test_close_disables_channel(self):
        """关闭会话顺手关通道，设备回到"没在跑"的状态。"""
        session = DmCanSession('dmcan:s:DEV0.0')
        session.open()
        backend = self.fake.DmCanContext._backends[0]
        self.assertTrue(backend.channels[0].enabled)
        session.close()
        self.assertFalse(backend.channels[0].enabled)
        self.assertFalse(backend.opened)
        session.close()          # 幂等


class DriverTest(BackendTestBase):
    """pydronecan 驱动层：帧的进出与错误处理。"""

    def test_send_receive_between_two_devices(self):
        """A 发的帧 B 能收到，字段一一对应。"""
        sender = self.make_driver('dmcan:s:DEV0.0', bitrate=1000000)
        receiver = self.make_driver('dmcan:s:DEV1.0', bitrate=1000000)
        sender.send(0x12345678, b'\x01\x02\x03', extended=True)
        deadline = time.monotonic() + 2.0
        frame = None
        while frame is None and time.monotonic() < deadline:
            frame = receiver.receive(0.05)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.id, 0x12345678)
        self.assertEqual(bytes(frame.data), b'\x01\x02\x03')
        self.assertTrue(frame.extended)
        self.assertFalse(frame.canfd)
        self.assertGreater(frame.ts_monotonic, 0)

    def test_receive_timeout_and_zero(self):
        """空队列：timeout=0 立刻返回 None，带超时也只等这么久。"""
        driver = self.make_driver('dmcan:s:DEV0.0')
        started = time.monotonic()
        self.assertIsNone(driver.receive(0))
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertIsNone(driver.receive(0.05))

    def test_tx_hook_fires(self):
        """发送要经过 IO hook（总线监视器靠它显示 TX）。"""
        driver = self.make_driver('dmcan:s:DEV0.0')
        seen = []
        driver.add_io_hook(lambda direction, frame: seen.append((direction, frame.id)))
        driver.send(0x1ABCDE, b'\x00', extended=True)
        self.assertEqual(seen, [('tx', 0x1ABCDE)])

    def test_classic_length_guard(self):
        """经典 CAN 上超过 8 字节要明确报错，而不是让 SDK 拒。"""
        driver = self.make_driver('dmcan:s:DEV0.0')
        from dronecan.driver.common import CANFrame, DriverError
        with self.assertRaises(DriverError):
            driver.send_frame(CANFrame(0x123, bytes(range(9)), True))

    def test_canfd_on_classic_channel(self):
        """通道是经典 CAN 时发 CAN FD 要报错并给出改法。"""
        driver = self.make_driver('dmcan:s:DEV0.0')
        from dronecan.driver.common import CANFrame, DriverError
        with self.assertRaises(DriverError) as ctx:
            driver.send_frame(CANFrame(0x123, b'\x01\x02', True, canfd=True))
        self.assertIn('数据段', str(ctx.exception))

    def test_canfd_roundtrip(self):
        """CAN FD 通道上 12 字节的帧能过去。"""
        sender = self.make_driver('dmcan:s:DEV0.0@1000000+2000000')
        receiver = self.make_driver('dmcan:s:DEV1.0@1000000+2000000')
        from dronecan.driver.common import CANFrame
        sender.send_frame(CANFrame(0x1ABCDEF, bytes(range(12)), True, canfd=True))
        frame = receiver.receive(1.0)
        self.assertIsNotNone(frame)
        self.assertTrue(frame.canfd)
        self.assertEqual(len(frame.data), 12)

    def test_unplugged_reported(self):
        """掉线后收帧侧要报"已拔出"，而不是静默没数据。"""
        driver = self.make_driver('dmcan:s:DEV0.0')
        from dronecan.driver.common import DriverError
        self.fake.DmCanContext._backends[0].present = False
        driver._last_presence_check = 0.0
        with self.assertRaises(DriverError) as ctx:
            driver.receive(0)
        self.assertIn('拔出', str(ctx.exception))

    def test_filter_list_ignored(self):
        """低带宽过滤请求被忽略（USB 后端不需要，也不该擅自丢帧）。"""
        driver = self.make_driver('dmcan:s:DEV0.0')
        driver.set_filter_list([0, 1, 2])
        self.assertIsNone(driver.get_filter_list())
        self.assertIsNone(driver.get_bus())

    def test_describe(self):
        """面板/CLI 要的描述字段齐全。"""
        driver = self.make_driver('dmcan:s:DEV0.0@500000')
        info = driver.describe()
        self.assertEqual(info['serial'], 'DEV0')
        self.assertEqual(info['channel'], 0)
        self.assertIn('500 kbps', info['config'])
        self.assertIn('queue_dropped', info['counters'])


if __name__ == '__main__':
    unittest.main()
