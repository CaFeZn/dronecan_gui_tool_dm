# -*- coding: utf-8 -*-
"""无界面 CLI 的离线测试：参数解析、结论判定、依赖缺失时的行为。"""

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dronecan_gui_tool import dmcan_cli  # noqa: E402
from dronecan_gui_tool.driver import dmcan_iface, dmsdk  # noqa: E402


class ParserTest(unittest.TestCase):
    """命令行接口形状。"""

    def setUp(self):
        self.parser = dmcan_cli.build_parser()

    def test_commands(self):
        """五个子命令都要在。"""
        for name in ('doctor', 'list', 'info', 'monitor', 'selftest'):
            with self.subTest(command=name):
                argv = [name] + (['dmcan:0'] if name in ('info', 'monitor', 'selftest') else [])
                args = self.parser.parse_args(argv)
                self.assertEqual(args.command, name)
                self.assertTrue(callable(args.func))

    def test_defaults(self):
        """常用默认值：1 Mbps、1 s 打印间隔、0 = 不限时。"""
        args = self.parser.parse_args(['monitor', 'dmcan:0.1'])
        self.assertEqual(args.bitrate, 1000000)
        self.assertEqual(args.interval, 1.0)
        self.assertEqual(args.seconds, 0.0)

    def test_selftest_options(self):
        """自检的帧数/速率/保留模式开关。"""
        args = self.parser.parse_args(['selftest', 'dmcan:0', '--count', '500',
                                       '--rate', '250', '--keep-mode'])
        self.assertEqual(args.count, 500)
        self.assertEqual(args.rate, 250)
        self.assertTrue(args.keep_mode)

    def test_no_command_prints_help(self):
        """不带子命令只打帮助，不算失败。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = dmcan_cli.main([])
        self.assertEqual(code, 0)
        self.assertIn('dmcan-dronecan-tool', buffer.getvalue())


class VerdictTest(unittest.TestCase):
    """丢帧结论要把"哪一段"指出来。"""

    def _totals(self, **kwargs):
        base = {'rx': 0, 'rx_dropped': 0, 'rx_dropped_queue': 0, 'rx_dropped_msgc': 0,
                'tx_completed': 0, 'tx_dropped': 0, 'host_dropped': 0}
        base.update(kwargs)
        return base

    def test_clean(self):
        """零丢帧。"""
        self.assertIn('零丢帧', dmcan_cli._verdict(self._totals(rx=1234)))

    def test_host_too_slow(self):
        """设备接收队列满 = 主机读得不够快。"""
        text = dmcan_cli._verdict(self._totals(rx=100, rx_dropped=7, rx_dropped_queue=7))
        self.assertIn('主机读得不够快', text)

    def test_device_side(self):
        """设备内部资源不足单独说。"""
        text = dmcan_cli._verdict(self._totals(rx=100, rx_dropped=3, rx_dropped_msgc=3))
        self.assertIn('设备侧瓶颈', text)

    def test_host_queue(self):
        """主机侧队列满要说清是上位机卡住。"""
        text = dmcan_cli._verdict(self._totals(rx=100, host_dropped=5))
        self.assertIn('上位机进程卡住', text)

    def test_tx_side(self):
        """发送侧丢帧归因到额度/队列。"""
        text = dmcan_cli._verdict(self._totals(tx_completed=10, tx_dropped=2))
        self.assertIn('发帧被丢', text)


class MonitorTableTest(unittest.TestCase):
    """monitor 的表：列宽必须对齐（中文表头也按显示宽度算）。"""

    def _fake(self, **kwargs):
        from dronecan_gui_tool.driver.dmcan_session import ChannelStats, StatsDiff
        diff = StatsDiff(seconds=1.0, **kwargs)
        stats = ChannelStats(ok=True, enabled=True)
        return diff, stats

    def test_header_row_width_matches(self):
        """每一行的显示宽度都与表头一致，终端里才不会错列。"""
        header = dmcan_cli._monitor_header()
        expected = dmcan_cli._display_width(header)
        for killer in ({}, {'rx_dropped': 3, 'rx_dropped_queue': 3}, {'host_rx_dropped': 1},
                       {'tx_dropped': 2, 'rx_dropped': 5, 'host_rx_dropped': 9}):
            diff, stats = self._fake(**killer)
            diff.rx_received = 12345
            diff.tx_completed = 678
            row = dmcan_cli._monitor_row(12.3, diff, stats)
            with self.subTest(drops=killer):
                self.assertEqual(dmcan_cli._display_width(row), expected)

    def test_drop_marker(self):
        """丢帧的那一秒要显眼地标出来。"""
        diff, stats = self._fake(rx_dropped=2, rx_dropped_queue=2)
        self.assertIn('⚠', dmcan_cli._monitor_row(1.0, diff, stats, '   ⚠ 本秒丢帧'))
        diff_ok, stats_ok = self._fake()
        self.assertNotIn('⚠', dmcan_cli._monitor_row(1.0, diff_ok, stats_ok))

    def test_display_width_counts_cjk_as_two(self):
        """中日韩字符按 2 列计（否则中文表头会和数字行错开）。"""
        self.assertEqual(dmcan_cli._display_width('时间'), 4)
        self.assertEqual(dmcan_cli._display_width('ab'), 2)
        # 丢(2)+帧(2)+((1)+收(2)+)(1) = 8
        self.assertEqual(dmcan_cli._display_width('丢帧(收)'), 8)

    def test_pad(self):
        """补齐后的显示宽度就是目标宽度。"""
        self.assertEqual(dmcan_cli._display_width(dmcan_cli._pad('收帧/s', 9)), 9)
        self.assertEqual(dmcan_cli._pad('7', 3), '  7')
        self.assertEqual(dmcan_cli._pad('7', 3, 'left'), '7  ')


class MissingSdkCliTest(unittest.TestCase):
    """没装 dmsdk 时 CLI 要给可照做的提示，而不是崩掉。"""

    def setUp(self):
        dmsdk.reset()
        dmcan_iface.invalidate_cache()
        self._saved = sys.modules.get('dmcan')
        sys.modules['dmcan'] = None

    def tearDown(self):
        sys.modules.pop('dmcan', None)
        if self._saved is not None:
            sys.modules['dmcan'] = self._saved
        dmsdk.reset()
        dmcan_iface.invalidate_cache()

    def _run(self, argv):
        """@brief 同时收 stdout 与 stderr（部分提示按惯例走 stderr）。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dmcan_cli.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_doctor(self):
        """doctor 报不可用并给安装建议，退出码非 0。"""
        code, output = self._run(['doctor'])
        self.assertEqual(code, 1)
        self.assertIn('dmsdk 不可用', output)
        self.assertIn('dmcan_sdk', output)

    def test_list(self):
        """list 同样给提示。"""
        code, output = self._run(['list'])
        self.assertEqual(code, 1)
        self.assertIn('建议', output)

    def test_selftest_reports_open_failure(self):
        """没依赖时 selftest 报"打开失败"，退出码 2。"""
        code, output = self._run(['selftest', 'dmcan:0'])
        self.assertEqual(code, 2)
        self.assertIn('打开失败', output)


if __name__ == '__main__':
    unittest.main()
