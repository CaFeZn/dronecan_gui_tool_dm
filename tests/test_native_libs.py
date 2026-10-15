# -*- coding: utf-8 -*-
"""原生库定位与依赖预加载的测试（这一条对应现场踩过的 WinError 127）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dronecan_gui_tool.driver import dmsdk  # noqa: E402


class DependencyPreloadTest(unittest.TestCase):
    """`libdmcan` 的依赖必须来自"我们自己的那一份"，不能被 PATH 顶掉。"""

    def test_noop_without_path(self):
        """没有库路径时不做任何事（非 Windows 同样是空操作）。"""
        self.assertEqual(dmsdk.preload_dependencies(None), {})

    def test_report_never_raises(self):
        """诊断报告在任何状态下都能生成。"""
        self.assertTrue(dmsdk.dependency_report())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows 专用')
    def test_preload_resolves_to_local_copy(self):
        """
        预加载之后，4 个依赖的**实际落盘路径**必须就在 libdmcan 旁边。

        这是现场问题的回归测试：dmcan_sdk 用 `CDLL(..., winmode=0)` 加载原生库，
        依赖只会从内置搜索顺序里找；机器上装了 ST-Link / QEMU / MSYS2 / OpenOCD 时
        PATH 里会出现同名 DLL，结果报 WinError 127（找到文件但缺导出符号）。
        """
        library = dmsdk.resolve_library_path()
        if not library or not os.path.isfile(library):
            self.skipTest('本机没有 dlls/libdmcan.dll（先跑 tools/fetch_dmcan_lib.py）')

        directory = os.path.normcase(os.path.dirname(os.path.abspath(library)))
        loaded = dmsdk.preload_dependencies(library)
        self.assertTrue(loaded, 'dlls/ 里应当有 libdmcan 的依赖 DLL')
        for name, resolved in loaded.items():
            with self.subTest(dependency=name):
                self.assertEqual(os.path.normcase(os.path.dirname(os.path.abspath(resolved))),
                                 directory,
                                 '%s 用的是 %s，不是工程内那一份' % (name, resolved))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows 专用')
    def test_full_stack_loads_from_project_dlls(self):
        """预加载 + 建上下文：整条链路在本机环境里真的能起来。"""
        library = dmsdk.resolve_library_path()
        if not library or not os.path.isfile(library):
            self.skipTest('本机没有 dlls/libdmcan.dll')
        dmsdk.reset()
        try:
            status = dmsdk.probe(refresh=True)
        finally:
            dmsdk.close_context()
        self.assertIsInstance(status.available, bool)
        if not status.available:
            # 依赖缺失是可诊断的失败，但**不能**是"加载不了"这种含糊结果
            self.assertTrue(status.reason)
            self.assertNotIn('WinError 127', status.reason)


if __name__ == '__main__':
    unittest.main()
