#
# DroneCAN GUI Tool — dmcan 后端（达妙 USB2CANFD / USB2FDCANDUAL 原生 USB）
#
# 本文件不属于上游 DroneCAN/gui_tool，为本 fork 新增；与上游同样以 MIT 发布（见 LICENSE）。
#
"""
CAN 后端扩展层。

上游（DroneCAN/gui_tool）直接调 pydronecan 的 `dronecan.make_node(iface, **kwargs)`，
由接口名推断后端（slcan / socketcan / pcan / mcast / mavcan…）。本 fork 在它前面加了
薄薄一层，只做一件事：**把 `dmcan:` 前缀交给达妙 dmsdk 后端**，其余接口名原样透传。

这样"dmsdk 可选"是天然的：

* 没装 `dmcan_sdk` / 没有 `libdmcan` -> `list_optional_ifaces()` 返回空表，
  接口列表里不会多出任何条目，上游所有后端行为不变；
* 装了 -> 接口列表里多出 `dmcan: …` 项，选中即走原生 USB。

@note 本模块**不**在导入期 import `dmcan`（见 `dmsdk.load()`），也没有副作用。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Dict, Optional

from . import dmsdk
from .dmcan_iface import (PREFIX, IfaceSpec, IfaceSpecError, is_dmcan_iface,  # noqa: F401
                          list_ifaces as _list_dmcan_ifaces, parse as parse_iface)

logger = logging.getLogger(__name__)

__all__ = ['PREFIX', 'IfaceSpec', 'IfaceSpecError', 'is_dmcan_iface', 'parse_iface',
           'list_optional_ifaces', 'make_node', 'is_dmcan_driver', 'status_text', 'dmsdk']


def list_optional_ifaces(refresh: bool = False) -> "OrderedDict[str, str]":
    """
    @brief 枚举 dmcan 接口（给 setup 窗口下拉框用）。

    @param refresh 强制重新枚举 USB
    @return 描述 -> 接口串；dmsdk 不可用或没有设备时返回**空表**（绝不抛异常，
            以免拖垮上游的接口列表刷新线程）
    """
    try:
        return _list_dmcan_ifaces(refresh=refresh)
    except Exception as ex:
        logger.warning('枚举 dmcan 接口失败：%s', ex, exc_info=True)
        return OrderedDict()


def make_node(iface_name: str, **kwargs):
    """
    @brief 建 DroneCAN 节点：`dmcan:` 走本 fork 的后端，其余接口名原样交给 pydronecan。

    @param iface_name 接口名（`dmcan:…` / COM3 / slcan:… / can0 / mcast:0 …）
    @param kwargs 原样透传给驱动与 `dronecan.node.Node`（baudrate / bitrate / bus_number …）
    @return `dronecan.node.Node`
    """
    import dronecan

    if not is_dmcan_iface(iface_name):
        return dronecan.make_node(iface_name, **kwargs)

    status = dmsdk.probe()
    if not status.available:
        raise RuntimeError('选择了 dmcan 接口（%s），但 dmsdk 不可用：%s\n建议：%s'
                           % (iface_name, status.reason, status.hint))

    from .dmcan_driver import DmCanDriver

    driver = DmCanDriver(iface_name, **kwargs)
    try:
        return dronecan.node.Node(driver, **kwargs)
    except BaseException:
        driver.close()
        raise


def is_dmcan_driver(node_or_driver: Any) -> bool:
    """
    @brief 判断是不是 dmcan 后端（用于 GUI 里选面板/显示统计）。

    @param node_or_driver `dronecan.node.Node` 或驱动对象
    @return True/False（不 import 驱动模块，避免无谓的依赖检查）
    """
    driver = getattr(node_or_driver, 'can_driver', node_or_driver)
    return type(driver).__name__ == 'DmCanDriver'


def status_text() -> str:
    """@brief 一行式 dmsdk 可用性描述（GUI 的"关于/诊断"里显示）。"""
    return dmsdk.probe().describe()
