# -*- coding: utf-8 -*-
"""
仿真 `dmcan`（达妙 dmsdk）模块：让 dmcan 后端在没有硬件时也能端到端跑起来。

它不是 mock：按 dmcan_sdk 2.1.1 的**公开 API** 逐项实现（类名、方法名、参数、
返回值语义都对齐 wheel 里的 `dmcan/` 包），只是把 USB 换成了进程内的虚拟总线。
被验证的驱动代码不知道自己在跟假的 SDK 说话，所以测出来的行为是可迁移的。

虚拟总线语义：
* 每台设备有独立的设备表项（index 0..N-1），通道位定时/使能状态/统计各自独立；
* 发帧时，同一通道上"其它已打开设备"会收到该帧；源设备只在 (内部|外部) 回环模式下收到自己；
* 统计是**累计值**，与真机一致（判丢帧要看增量）；
* 可注入故障：`dev.fail_open` / `dev.present = False` / 位速率下发失败等。

@note 真实 SDK 的 `dmcan_rx_frame.payload` 是 ctypes 数组（切片得到 int 列表），
      这里用 `bytes`——两者都能被 `bytes(frame.payload[:dlen])` 统一处理。
"""

from __future__ import annotations

import copy
import struct
import threading
import time
from enum import IntEnum, IntFlag
from typing import Any, Callable, Dict, List, Optional

__version__ = '2.1.1-fake'


# --------------------------------------------------------------------- 枚举
class dmcan_device_type(IntEnum):
    USB2CANFD_S = 0
    USB2CANFD_D = 1
    LINKX4C = 2
    UNKNOWN = 3


class dmcan_status(IntEnum):
    OK = 0
    INVALID_ARG = 1
    NOT_OPEN = 2
    DEVICE_NOT_FOUND = 3
    NO_MEMORY = 4
    IO = 5
    UNSUPPORTED = 6
    INTERNAL = 7
    REENTRANT = 8


class dmcan_channel_mode(IntEnum):
    NORMAL = 0
    LOOPBACK_INTERNAL = 1
    LOOPBACK_EXTERNAL = 2
    LISTEN_ONLY = 3


class dmcan_event_flag(IntFlag):
    RX_FRAME = 1 << 0
    TX_COMPLETED = 1 << 1
    TX_ON_BUS = 1 << 2
    CHANNEL_ERROR = 1 << 3
    UART_RX = 1 << 4


class dmcan_filter_mode(IntEnum):
    WHITELIST = 0
    BLACKLIST = 1


class DmCanError(Exception):
    """与 dmcan_sdk 一致：失败抛异常，`status` 是错误码。"""

    def __init__(self, status=dmcan_status.IO, api: str = ''):
        self.status = status
        self.api = api
        name = status.name if isinstance(status, dmcan_status) else str(status)
        super().__init__('%s 失败: %s' % (api, name) if api else name)


# --------------------------------------------------------------------- 结构
class dmcan_channel_can_info:
    """`dmcan_channel_can_info_t` 的等价物（19B，不含 TDC）。"""

    def __init__(self, canfd=False, non_iso=False, retrans=False, can_baudrate=1000000,
                 canfd_baudrate=5000000, can_sp=0.0, canfd_sp=0.0):
        self.canfd = canfd
        self.non_iso = non_iso
        self.retrans = retrans
        self.can_baudrate = can_baudrate
        self.canfd_baudrate = canfd_baudrate
        self.can_sp = can_sp
        self.canfd_sp = canfd_sp

    def __repr__(self):
        return ('<can_info %s nominal=%d data=%d>' %
                ('FD' if self.canfd else 'CL', self.can_baudrate, self.canfd_baudrate))


class dmcan_ch_baud_conf:
    """`dmcan_ch_baud_conf_t` 的等价物（22B）。"""

    def __init__(self, enable_canfd=False, enable_tdc=False, non_iso=False, enable_retrans=True,
                 mode=0, nominal_brp=1, nominal_seg1=1, nominal_seg2=1, nominal_sjw=1,
                 data_brp=1, data_seg1=1, data_seg2=1, data_sjw=1, busoff_autoreset_ms=100,
                 tdc_offset=0, tdc_filter=0, reserved=0):
        self.enable_canfd = enable_canfd
        self.enable_tdc = enable_tdc
        self.non_iso = non_iso
        self.enable_retrans = enable_retrans
        self.mode = mode
        self.nominal_brp = nominal_brp
        self.nominal_seg1 = nominal_seg1
        self.nominal_seg2 = nominal_seg2
        self.nominal_sjw = nominal_sjw
        self.data_brp = data_brp
        self.data_seg1 = data_seg1
        self.data_seg2 = data_seg2
        self.data_sjw = data_sjw
        self.busoff_autoreset_ms = busoff_autoreset_ms
        self.tdc_offset = tdc_offset
        self.tdc_filter = tdc_filter
        self.reserved = reserved


class dmcan_firmware_info:
    """`dmcan_firmware_info_t` 的等价物。"""

    def __init__(self, device=dmcan_device_type.USB2CANFD_D, version=(2, 0, 1),
                 build_id=b'b5c2574 2026-09-24 15:23:06',
                 version_valid=True, build_id_valid=True):
        self.device = device
        self.version = type('version_t', (), {'major': version[0], 'minor': version[1],
                                              'build': version[2]})()
        self.build_id = bytearray(build_id)
        self.version_valid = version_valid
        self.build_id_valid = build_id_valid


class dmcan_rx_frame:
    """`dmcan_rx_frame_t` 的等价物。"""

    def __init__(self, can_id=0, timestamp=0, channel=0, ext=1, rtr=0, canfd=0, brs=0,
                 esi=0, ack=0, payload=b''):
        self.can_id = can_id
        self.timestamp = timestamp
        self.channel = channel
        self.ext = ext
        self.rtr = rtr
        self.canfd = canfd
        self.brs = brs
        self.esi = esi
        self.ack = ack
        self.payload = bytes(payload)
        self.dlen = len(self.payload)

    def __str__(self):
        return 'ID:%08X CH:%d DLEN:%d DATA:%s' % (self.can_id, self.channel, self.dlen,
                                                  self.payload.hex())


class dmcan_channel_stats:
    """`dmcan_channel_stats_t` 的等价物（全部为累计值）。"""

    def __init__(self, **kwargs):
        self.tx_admitted = 0
        self.tx_completed = 0
        self.tx_dropped = 0
        self.tx_failed = 0
        self.rx_received = 0
        self.rx_dropped = 0
        self.sw_tx_fifo_level = 0
        self.hw_tx_fifo_level = 0
        self.enabled = 0
        self.reserved = 0
        self.drop_flags = 0x03          # 真机补齐细分记录后为 0x03；0 = "还没收到过"
        self.reserved2 = b'\x00\x00\x00'
        self.rx_dropped_queue = 0
        self.rx_dropped_msgc = 0
        self.echo_dropped_queue = 0
        self.echo_dropped_msgc = 0
        self.echo_sent = 0
        self.tx_dropped_no_credit = 0
        self.tx_dropped_queue_full = 0
        self.tx_dropped_submit_fail = 0
        self.tx_dropped_ch_disabled = 0
        self.__dict__.update(kwargs)


class dmcan_bus_state:
    """`dmcan_bus_state_t` 的等价物。"""

    def __init__(self, bus_state=0, tec=0, rec=0, credits=8, in_flight=0):
        self.bus_state = bus_state
        self.tec = tec
        self.rec = rec
        self.credits = credits
        self.in_flight = in_flight


class dmcan_device_info_t:
    """`DmCanDeviceInfo` 的等价物。"""

    def __init__(self, device_type, pid, vid, serial):
        self.device_type = device_type
        self.pid = pid
        self.vid = vid
        self.serial = serial


class dmcan_filter_table:
    def __init__(self):
        self.mode = int(dmcan_filter_mode.BLACKLIST)
        self.std_max = 16
        self.ext_max = 16
        self.count = 0
        self.rules: List[Any] = []


class DmCanEvent:
    """`dmcan.DmCanEvent` 的等价物。"""

    __slots__ = ('type', 'channel', 'frame', 'error', 'uart')

    def __init__(self, type_, channel, frame=None, error=None, uart=None):
        self.type = type_
        self.channel = channel
        self.frame = frame
        self.error = error
        self.uart = uart

    def __repr__(self):
        return '<DmCanEvent %r ch=%d %r>' % (self.type, self.channel, self.frame)


# ------------------------------------------------------------------ 虚拟总线
class _VirtualChannel:
    """一条虚拟 CAN 通道：位定时、使能状态、统计与订阅者。"""

    def __init__(self, index: int):
        self.index = index
        self.info = dmcan_channel_can_info()
        self.details = dmcan_ch_baud_conf()
        self.enabled = False
        self.stats = dmcan_channel_stats()
        self.bus = dmcan_bus_state()
        self.subscribers: List[Any] = []


class _Device:
    """一台虚拟设备。"""

    def __init__(self, index: int, device_type=dmcan_device_type.USB2CANFD_D, serial: str = '',
                 vid: int = 0x34B7, pid: int = 0x6632, channels: int = 2):
        self.index = index
        self.device_type = device_type
        self.serial = serial or ('FAKE%08X' % (0x1000 + index))
        self.vid = vid
        self.pid = pid
        self.channels: Dict[int, _VirtualChannel] = {i: _VirtualChannel(i) for i in range(channels)}
        self.opened = False
        self.present = True
        self.fail_open = False
        self.fail_set_baudrate = False
        self.fail_enable = False
        self.saved = 0
        self.firmware = dmcan_firmware_info(device=device_type)


class _Subscription:
    def __init__(self, device, events, callback, channel_mask):
        self.device = device
        self.events = events
        self.callback = callback
        self.channel_mask = channel_mask
        self._active = True

    @property
    def active(self):
        return self._active

    def cancel(self):
        if not self._active:
            return
        self._active = False
        for ch in self.device.channels.values():
            if self in ch.subscribers:
                ch.subscribers.remove(self)


class DmCanDevice:
    """`dmcan.DmCanDevice` 的等价物。"""

    def __init__(self, context, backend: _Device):
        self._context = context
        self._backend = backend
        self.index = backend.index
        self.dll = None

    # ------------------------------------------------------------ 基本信息
    def get_info(self):
        b = self._backend
        return dmcan_device_info_t(b.device_type, b.pid, b.vid, b.serial)

    def is_present(self) -> bool:
        return self._backend.present

    def get_firmware_info(self):
        return self._backend.firmware

    def get_version(self) -> str:
        return 'USB2CANFD_D V2.0.1 [b5c2574 2026-09-24 15:23:06]'

    # ------------------------------------------------------------ 生命周期
    def open(self):
        b = self._backend
        if not b.present:
            raise DmCanError(dmcan_status.IO, 'dmcan_device_open')
        if b.fail_open:
            raise DmCanError(dmcan_status.IO, 'dmcan_device_open')
        if b.opened:
            raise DmCanError(dmcan_status.IO, 'dmcan_device_open')
        b.opened = True

    def close(self):
        self._backend.opened = False

    # ------------------------------------------------------------ 通道配置
    def enable_channel(self, channel: int, enable: bool):
        if not self._backend.opened:
            raise DmCanError(dmcan_status.NOT_OPEN, 'dmcan_device_enable_channel')
        if channel not in self._backend.channels:
            raise DmCanError(dmcan_status.INVALID_ARG, 'dmcan_device_enable_channel')
        if enable and self._backend.fail_enable:
            raise DmCanError(dmcan_status.IO, 'dmcan_device_enable_channel')
        if not enable:
            return self.disable_channel(channel)
        ch = self._backend.channels[channel]
        ch.enabled = True
        ch.stats.enabled = 1

    def disable_channel(self, channel: int, verify: bool = True):
        if channel not in self._backend.channels:
            raise DmCanError(dmcan_status.INVALID_ARG, 'dmcan_device_disable_channel')
        ch = self._backend.channels[channel]
        ch.enabled = False
        ch.stats.enabled = 0

    def get_channel_baudrate(self, channel: int):
        self._require_channel(channel)
        return copy.copy(self._backend.channels[channel].info)

    def set_channel_baudrate(self, channel: int, info):
        self._require_channel(channel)
        if self._backend.fail_set_baudrate:
            raise DmCanError(dmcan_status.IO, 'dmcan_device_set_channel_baudrate')
        ch = self._backend.channels[channel]
        # 真机语义：经典 CAN + 数据段全 0 会被固件拒
        if not info.canfd and not info.canfd_baudrate:
            raise DmCanError(dmcan_status.IO, 'dmcan_device_set_channel_baudrate')
        ch.info = info
        ch.details.enable_canfd = bool(info.canfd)

    def get_channel_baudrate_details(self, channel: int):
        self._require_channel(channel)
        return copy.copy(self._backend.channels[channel].details)

    def set_channel_baudrate_details(self, channel: int, config):
        self._require_channel(channel)
        ch = self._backend.channels[channel]
        ch.details = config
        ch.info.canfd = bool(config.enable_canfd)

    def set_channel_mode(self, channel: int, mode):
        self._require_channel(channel)
        self._backend.channels[channel].details.mode = int(mode)

    def get_channel_filter(self, channel: int):
        self._require_channel(channel)
        return dmcan_filter_table()

    # ------------------------------------------------------------ 收发
    def send_can(self, channel: int, can_id: int, payload: bytes = b'', canfd: bool = False,
                 ext: bool = False, rtr: bool = False, brs: bool = False):
        if not self._backend.opened:
            raise DmCanError(dmcan_status.NOT_OPEN, 'dmcan_device_send_can')
        self._require_channel(channel)
        ch = self._backend.channels[channel]
        data = b'' if rtr else bytes(payload)
        if not canfd and len(data) > 8:
            raise DmCanError(dmcan_status.INVALID_ARG, 'dmcan_device_send_can')
        if not ch.enabled:
            ch.stats.tx_dropped += 1
            ch.stats.tx_dropped_ch_disabled += 1
            raise DmCanError(dmcan_status.IO, 'dmcan_device_send_can')
        ch.stats.tx_admitted += 1
        ch.stats.tx_completed += 1
        frame = dmcan_rx_frame(can_id=can_id, channel=channel, ext=int(bool(ext)),
                               canfd=int(bool(canfd)), brs=int(bool(brs)),
                               timestamp=int(time.monotonic() * 1e6), ack=1, payload=data)
        self._backend_deliver(channel, frame)

    def _backend_deliver(self, channel: int, frame: dmcan_rx_frame):
        """@brief 把帧投给同通道上其它已打开设备的订阅者（+ 回环模式下的自己）。"""
        backend = self._backend
        for other in self._context._backends:
            if other is backend:
                continue
            if not other.opened:
                continue
            target = other.channels.get(channel)
            if target is None or not target.enabled:
                continue
            target.stats.rx_received += 1
            if target.stats.rx_dropped_inject:
                target.stats.rx_dropped += 1
                continue
            self._emit(target, frame)
        own = backend.channels[channel]
        if own.details.mode in (int(dmcan_channel_mode.LOOPBACK_INTERNAL),
                                int(dmcan_channel_mode.LOOPBACK_EXTERNAL)):
            own.stats.rx_received += 1
            self._emit(own, frame)

    @staticmethod
    def _emit(channel: _VirtualChannel, frame: dmcan_rx_frame):
        for sub in list(channel.subscribers):
            if not sub._active:
                continue
            if not (sub.channel_mask & (1 << channel.index)):
                continue
            event = DmCanEvent(dmcan_event_flag.RX_FRAME, channel.index, frame=frame)
            try:
                sub.callback(None, event)
            except Exception:
                pass

    def subscribe(self, events, callback, channel_mask=0xFFFFFFFF):
        if not self._backend.opened:
            raise DmCanError(dmcan_status.NOT_OPEN, 'dmcan_device_subscribe')
        sub = _Subscription(self, int(events), callback, int(channel_mask))
        for index, ch in self._backend.channels.items():
            if channel_mask & (1 << index):
                ch.subscribers.append(sub)
        return sub

    def unsubscribe(self, subscription):
        subscription.cancel()

    # ------------------------------------------------------------ 统计
    def get_channel_stats(self, channel: int):
        self._require_channel(channel)
        return copy.copy(self._backend.channels[channel].stats)

    def get_bus_state(self, channel: int):
        self._require_channel(channel)
        return copy.copy(self._backend.channels[channel].bus)

    # ------------------------------------------------------------ 控制面
    def save_param(self):
        if not self._backend.opened:
            raise DmCanError(dmcan_status.NOT_OPEN, 'dmcan_device_save_param')
        self._backend.saved += 1

    def clear_bus_off(self, channel: int):
        self._require_channel(channel)
        self._backend.channels[channel].bus.bus_state = 0

    def _require_channel(self, channel: int):
        if not self._backend.opened:
            raise DmCanError(dmcan_status.NOT_OPEN, 'dmcan_device')
        if channel not in self._backend.channels:
            raise DmCanError(dmcan_status.INVALID_ARG, 'dmcan_device')


# ------------------------------------------------------------------- 上下文
class DmCanContext:
    """`dmcan.DmCanContext` 的等价物。"""

    #: 所有上下文共享的虚拟设备表（真机是同一批 USB 设备，本来就共享）
    _backends: List[_Device] = []

    def __init__(self, dll_path=None):
        self.dll_path = dll_path or '<fake libdmcan>'
        self.dll = None
        self._devices: Dict[int, DmCanDevice] = {}
        self._destroyed = False

    # -- 测试用配置接口 -------------------------------------------------
    @classmethod
    def reset(cls, devices: Optional[List[_Device]] = None):
        for backend in cls._backends:
            for channel in backend.channels.values():
                channel.subscribers.clear()
                channel.enabled = False
                channel.stats = dmcan_channel_stats()
            backend.opened = False
        cls._backends = list(devices or [])

    # -- 公开 API -------------------------------------------------------
    def sdk_version(self) -> str:
        return '2.1.0-fake'

    def find_devices(self, device_type=None) -> int:
        if device_type is None:
            candidates = list(self._backends)
        else:
            candidates = [b for b in self._backends if int(b.device_type) == int(device_type)]
        self._devices = {b.index: DmCanDevice(self, b) for b in candidates}
        return len(candidates)

    def get_device(self, index: int) -> DmCanDevice:
        return self._devices[index]

    def show_all_devices(self):
        for entry in self._devices.values():
            print(entry.get_info())

    def destroy(self):
        self._destroyed = True
        self._devices.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.destroy()


# `rx_dropped_inject` 是测试用的注入开关，挂在 stats 对象上
def _install_inject_flag():
    original = dmcan_channel_stats.__init__

    def patched(self, **kwargs):
        original(self, **kwargs)
        self.rx_dropped_inject = False

    dmcan_channel_stats.__init__ = patched


_install_inject_flag()


def make_dual(serial: str = 'FAKE0001', index: int = 0) -> _Device:
    """@brief 造一台 USB2CANFD_Dual（双路）虚拟设备。"""
    return _Device(index=index, device_type=dmcan_device_type.USB2CANFD_D, serial=serial,
                   channels=2)


def install(devices: Optional[List[_Device]] = None):
    """
    @brief 把本模块塞进 `sys.modules['dmcan']`，并重置虚拟设备表。

    @param devices 设备列表（默认一台双路设备）
    @return 本模块
    """
    import sys
    module = sys.modules[__name__]
    sys.modules['dmcan'] = module
    DmCanContext.reset(devices if devices is not None else [make_dual()])
    return module


def uninstall():
    """@brief 撤掉注入并清空虚拟设备表。"""
    import sys
    sys.modules.pop('dmcan', None)
    DmCanContext.reset([])
