# -*- coding: utf-8 -*-
"""可选依赖：没装 dmsdk 时，一切都还得照常工作。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dronecan  # noqa: E402

from dronecan_gui_tool.driver import dmcan_iface, dmsdk, is_dmcan_driver, make_node  # noqa: E402
from dronecan_gui_tool.driver import list_optional_ifaces  # noqa: E402


class MissingSdkTest(unittest.TestCase):
    """模拟"机器上根本没装 dmcan_sdk"。"""

    def setUp(self):
        dmsdk.reset()
        dmcan_iface.invalidate_cache()
        self._saved = sys.modules.get('dmcan')
        sys.modules['dmcan'] = None       # 让 import dmcan 直接失败

    def tearDown(self):
        sys.modules.pop('dmcan', None)
        if self._saved is not None:
            sys.modules['dmcan'] = self._saved
        dmsdk.reset()
        dmcan_iface.invalidate_cache()

    def test_probe_reports_reason_and_hint(self):
        """探测给出原因与可直接照做的建议，而不是抛异常。"""
        status = dmsdk.probe(refresh=True)
        self.assertFalse(status.available)
        self.assertIn('dmcan_sdk', status.reason)
        self.assertTrue(status.hint)
        self.assertFalse(status.describe().startswith('dmsdk 可用'))

    def test_iface_list_is_empty(self):
        """接口列表里不会多出任何 dmcan 项（上游行为不变）。"""
        self.assertEqual(list_optional_ifaces(), {})
        self.assertEqual(dmcan_iface.list_ifaces(), {})
        # list_devices 是"要答案"的接口：dmsdk 不可用时明确抛错，而不是假装"没有设备"
        with self.assertRaises(dmsdk.SdkUnavailable):
            dmcan_iface.list_devices(refresh=True)

    def test_make_node_gives_actionable_error(self):
        """用户真的选了 dmcan 接口时，报错要能指导下一步。"""
        with self.assertRaises(RuntimeError) as ctx:
            make_node('dmcan:0')
        message = str(ctx.exception)
        self.assertIn('dmsdk 不可用', message)
        self.assertIn('dmcan_sdk', message)

    def test_other_backends_untouched(self):
        """非 dmcan 接口原样交给 pydronecan（上游后端一个都不受影响）。"""
        calls = []

        def fake_make_node(name, **kwargs):
            calls.append((name, kwargs))
            return 'sentinel-node'

        original = dronecan.make_node
        dronecan.make_node = fake_make_node
        try:
            result = make_node('slcan:COM9', baudrate=115200, bitrate=1000000)
            result_can = make_node('can0', bitrate=500000)
        finally:
            dronecan.make_node = original

        self.assertEqual(result, 'sentinel-node')
        self.assertEqual(result_can, 'sentinel-node')
        self.assertEqual([c[0] for c in calls], ['slcan:COM9', 'can0'])
        self.assertEqual(calls[0][1]['baudrate'], 115200)

    def test_is_dmcan_driver(self):
        """驱动类型判定（GUI 用它决定开哪个面板）。"""
        class Node(object):
            can_driver = None

        self.assertFalse(is_dmcan_driver(Node()))

        class DmCanDriver(object):
            pass

        node = Node()
        node.can_driver = DmCanDriver()
        self.assertTrue(is_dmcan_driver(node))
        self.assertTrue(is_dmcan_driver(DmCanDriver()))


if __name__ == '__main__':
    unittest.main()
