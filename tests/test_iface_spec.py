# -*- coding: utf-8 -*-
"""接口串解析与设备枚举的单元测试（不需要 dmsdk，也不需要硬件）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dronecan_gui_tool.driver import dmcan_iface, dmsdk  # noqa: E402
from tests import fake_dmcan  # noqa: E402


class ParseTest(unittest.TestCase):
    """"""

    def test_minimal(self):
        """最简写法：通道与位速率取默认值。"""
        spec = dmcan_iface.parse('dmcan:0')
        self.assertEqual(spec.index, 0)
        self.assertEqual(spec.channel, 0)
        self.assertIsNone(spec.nominal)
        self.assertFalse(spec.canfd)
        self.assertEqual(spec.nominal_or(1000000), 1000000)

    def test_channel(self):
        """显式通道。"""
        self.assertEqual(dmcan_iface.parse('dmcan:1.1').channel, 1)

    def test_serial(self):
        """按序列号选设备。"""
        spec = dmcan_iface.parse('dmcan:s:E4B3230B4E4B.1@500000')
        self.assertTrue(spec.by_serial)
        self.assertEqual(spec.serial, 'E4B3230B4E4B')
        self.assertEqual(spec.channel, 1)
        self.assertEqual(spec.nominal, 500000)
        self.assertIsNone(spec.index)

    def test_canfd(self):
        """给了数据段位速率就是 CAN FD。"""
        spec = dmcan_iface.parse('dmcan:0.0@1000000+5000000')
        self.assertTrue(spec.canfd)
        self.assertEqual(spec.data, 5000000)
        self.assertEqual(spec.to_string(), 'dmcan:0.0@1000000+5000000')

    def test_defaults_from_gui(self):
        """界面上的 bitrate / bus number 会作为缺省值。"""
        spec = dmcan_iface.parse('dmcan:0', default_bitrate=500000, default_channel=1)
        self.assertEqual(spec.nominal, 500000)
        self.assertEqual(spec.channel, 1)

    def test_trailing_description(self):
        """下拉框里带描述文字时按空白截断。"""
        spec = dmcan_iface.parse('  dmcan:0.1@1000000   USB2CANFD_Dual CH1 很长的描述  ')
        self.assertEqual(spec.channel, 1)
        self.assertEqual(spec.nominal, 1000000)

    def test_roundtrip(self):
        """规范化输出能被再次解析。"""
        text = dmcan_iface.format_spec(dmcan_iface.parse('dmcan:s:ABC_1.3@250000+2000000'))
        rest = dmcan_iface.parse(text)
        self.assertEqual(text, 'dmcan:s:ABC_1.3@250000+2000000')
        self.assertEqual(rest.channel, 3)

    def test_errors(self):
        """非法写法一律报错，不猜设备。"""
        for bad in ('', 'can0', 'dmcan:', 'dmcan:abc', 'dmcan:0.0@123',
                    'dmcan:0.0@2000000', 'dmcan:0.0@1000000+50000'):
            with self.subTest(bad=bad):
                with self.assertRaises(dmcan_iface.IfaceSpecError):
                    dmcan_iface.parse(bad)

    def test_prefix(self):
        """前缀判定。"""
        self.assertTrue(dmcan_iface.is_dmcan_iface('dmcan:0'))
        self.assertTrue(dmcan_iface.is_dmcan_iface('DMCAN:0.1'))
        self.assertFalse(dmcan_iface.is_dmcan_iface('COM3'))
        self.assertFalse(dmcan_iface.is_dmcan_iface('slcan:COM3'))


class EnumerationTest(unittest.TestCase):
    """枚举走仿真 SDK，验证'下拉框里出现什么'。"""

    def setUp(self):
        self.fake = fake_dmcan.install([
            fake_dmcan.make_dual(serial='DEV0', index=0),
            fake_dmcan.make_dual(serial='DEV1', index=1),
        ])
        dmsdk.reset()
        dmcan_iface.invalidate_cache()

    def tearDown(self):
        fake_dmcan.uninstall()
        dmsdk.reset()
        dmcan_iface.invalidate_cache()

    def test_devices(self):
        """枚举到两台双路设备。"""
        entries = dmcan_iface.list_devices(refresh=True)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].device_type, 'USB2CANFD_D')
        self.assertEqual(entries[0].channels, 2)
        self.assertEqual(entries[0].reference, 's:DEV0')

    def test_iface_list(self):
        """每台设备每个通道各一条，且 value 不含 @（位速率由界面上给）。"""
        ifaces = dmcan_iface.list_ifaces()
        self.assertEqual(len(ifaces), 4)
        values = list(ifaces.values())
        self.assertIn('dmcan:s:DEV0.0', values)
        self.assertIn('dmcan:s:DEV1.1', values)
        for description in ifaces:
            self.assertIn('CH', description)
            # 描述里不带位速率：真正生效的值来自 setup 窗口的 bitrate 输入框
            self.assertNotIn('@', description)

    def test_unusable_serial_falls_back_to_index(self):
        """序列号里含奇怪字符时改用下标引用（避免解析歧义）。"""
        self.fake.DmCanContext.reset([fake_dmcan.make_dual(serial='has.dot', index=0)])
        dmcan_iface.invalidate_cache()
        entries = dmcan_iface.list_devices(refresh=True)
        self.assertEqual(entries[0].reference, '0')

    def test_unplugged_hidden(self):
        """拔掉的设备不出现在列表里。"""
        device = self.fake.make_dual(serial='DEV9', index=0)
        device.present = False
        self.fake.DmCanContext.reset([device])
        dmcan_iface.invalidate_cache()
        self.assertEqual(dmcan_iface.list_devices(refresh=True), [])


if __name__ == '__main__':
    unittest.main()
