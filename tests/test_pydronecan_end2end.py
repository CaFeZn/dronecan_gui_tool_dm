# -*- coding: utf-8 -*-
"""端到端：真正的 pydronecan 节点跑在 dmcan 后端上（仿真 USB，无硬件）。

这里验证的是"DroneCAN 协议栈 + 我们的驱动"这条完整链路：
传输层编码 -> 驱动发帧 -> 设备 -> 驱动收帧 -> 传输层解码 -> 回调。
单帧/多帧/服务请求响应都覆盖。
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dronecan  # noqa: E402

from dronecan_gui_tool.driver import dmcan_iface, dmsdk, make_node  # noqa: E402
from tests import fake_dmcan  # noqa: E402

#: GUI 传下来的参数（setup 窗口那一套），用来确认接口签名真的兼容
IFACE_KWARGS = dict(baudrate=115200, bitrate=1000000, bus_number=1, filtered=False,
                    mavlink_source_system=250, mavlink_target_system=0,
                    mavlink_signing_key='')


def pump(nodes, deadline_seconds, stop_when=None):
    """@brief 把若干节点一起 spin 一段时间（直到条件满足或超时）。"""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        for node in nodes:
            node.spin(0.002)
        if stop_when is not None and stop_when():
            return True
    return stop_when() if stop_when is not None else False


class EndToEndTest(unittest.TestCase):
    """"""

    def setUp(self):
        self.fake = fake_dmcan.install([
            fake_dmcan.make_dual(serial='DEV0', index=0),
            fake_dmcan.make_dual(serial='DEV1', index=1),
        ])
        dmsdk.reset()
        dmcan_iface.invalidate_cache()
        self.nodes = []

    def tearDown(self):
        for node in self.nodes:
            try:
                node.close()
            except Exception:
                pass
        fake_dmcan.uninstall()
        dmsdk.reset()
        dmcan_iface.invalidate_cache()

    def make_node(self, iface, node_id=None):
        """@brief 按 gui_tool 的方式建节点（node_info + mode + 那一堆 kwargs）。"""
        node_info = dronecan.uavcan.protocol.GetNodeInfo.Response()
        node_info.name = 'org.dronecan.gui_tool'
        node_info.software_version.major = 4
        node_info.software_version.minor = 0
        node = make_node(iface, node_info=node_info,
                         mode=dronecan.uavcan.protocol.NodeStatus().MODE_OPERATIONAL,
                         node_id=node_id, **IFACE_KWARGS)
        self.nodes.append(node)
        return node

    def test_broadcast_single_frame(self):
        """单帧广播：A 发 NodeStatus，B 收到且源节点号正确。"""
        node_a = self.make_node('dmcan:s:DEV0.0', node_id=10)
        node_b = self.make_node('dmcan:s:DEV1.0', node_id=20)

        received = []
        node_b.add_handler(dronecan.uavcan.protocol.NodeStatus,
                           lambda e: received.append(e.transfer.source_node_id))

        node_a.broadcast(dronecan.uavcan.protocol.NodeStatus())
        self.assertTrue(pump([node_a, node_b], 2.0, stop_when=lambda: bool(received)))
        self.assertEqual(received[0], 10)

    def test_multiframe_broadcast_no_loss(self):
        """多帧报文：200 条 KeyValue 全到，一条不丢、顺序不乱。"""
        node_a = self.make_node('dmcan:s:DEV0.0', node_id=10)
        node_b = self.make_node('dmcan:s:DEV1.0', node_id=20)

        received = []
        node_b.add_handler(dronecan.uavcan.protocol.debug.KeyValue,
                           lambda e: received.append(e.message.key))

        total = 200
        for index in range(total):
            node_a.broadcast(dronecan.uavcan.protocol.debug.KeyValue(
                key='n=%04d' % index, value=float(index)))

        pump([node_a, node_b], 5.0, stop_when=lambda: len(received) >= total)
        self.assertEqual(len(received), total, '多帧传输有丢失')
        self.assertEqual(received, ['n=%04d' % i for i in range(total)], '多帧传输顺序被破坏')

    def test_service_request_response(self):
        """服务请求/响应：A 向 B 要 GetNodeInfo。"""
        node_a = self.make_node('dmcan:s:DEV0.0', node_id=10)
        node_b = self.make_node('dmcan:s:DEV1.0', node_id=20)

        responses = []

        def on_response(event):
            responses.append(event)

        node_a.request(dronecan.uavcan.protocol.GetNodeInfo.Request(), 20, on_response,
                       timeout=3.0)
        pump([node_a, node_b], 3.0, stop_when=lambda: bool(responses))
        self.assertTrue(responses, '没有收到服务响应')
        self.assertIsNotNone(responses[0])
        self.assertEqual(responses[0].response.name, 'org.dronecan.gui_tool')

    def test_node_status_periodic(self):
        """自带周期 NodeStatus：A 的 NodeStatus 周期性出现，B 一直能收到。"""
        node_a = self.make_node('dmcan:s:DEV0.0', node_id=10)
        node_b = self.make_node('dmcan:s:DEV1.0', node_id=20)
        seen = []
        node_b.add_handler(dronecan.uavcan.protocol.NodeStatus,
                           lambda e: seen.append(e.transfer.source_node_id))
        pump([node_a, node_b], 2.5, stop_when=lambda: len(seen) >= 2)
        self.assertGreaterEqual(len(seen), 2, '周期 NodeStatus 没上来')

    def test_anonymous_start_like_gui(self):
        """GUI 是匿名启动、之后再设节点 ID：这条路也要通。"""
        node_a = self.make_node('dmcan:s:DEV0.0')
        node_b = self.make_node('dmcan:s:DEV1.0', node_id=20)
        self.assertTrue(node_a.is_anonymous)
        node_a.node_id = 11
        self.assertEqual(node_a.node_id, 11)
        seen = []
        node_b.add_handler(dronecan.uavcan.protocol.NodeStatus,
                           lambda e: seen.append(e.transfer.source_node_id))
        node_a.broadcast(dronecan.uavcan.protocol.NodeStatus())
        pump([node_a, node_b], 2.0, stop_when=lambda: bool(seen))
        self.assertEqual(seen[:1], [11])

    def test_channels_are_isolated(self):
        """双路模块的两条通道互不串帧：CH0 上发的帧 CH1 收不到。"""
        node_a = self.make_node('dmcan:s:DEV0.0', node_id=10)
        node_c = self.make_node('dmcan:s:DEV1.1', node_id=30)
        seen = []
        node_c.add_handler(dronecan.uavcan.protocol.NodeStatus,
                           lambda e: seen.append(e.transfer.source_node_id))
        node_a.broadcast(dronecan.uavcan.protocol.NodeStatus())
        pump([node_a, node_c], 1.0)
        self.assertEqual(seen, [])

    def test_same_device_needs_one_session(self):
        """同一台设备开两个会话要被挡住并说清楚（open 是整台设备的 USB 会话）。"""
        node_a = self.make_node('dmcan:s:DEV0.0', node_id=10)
        self.assertIsNotNone(node_a)
        from dronecan.driver.common import DriverError
        with self.assertRaises(DriverError) as ctx:
            self.make_node('dmcan:s:DEV0.1', node_id=30)
        self.assertIn('只能有一个', str(ctx.exception))

    def test_session_reusable_after_close(self):
        """关掉之后同一台设备可以再开（CLI/GUI 重连路径）。"""
        node_a = self.make_node('dmcan:s:DEV0.0', node_id=10)
        node_a.close()
        self.nodes.remove(node_a)
        node_b = self.make_node('dmcan:s:DEV0.0', node_id=10)
        self.assertIsNotNone(node_b)


if __name__ == '__main__':
    unittest.main()
