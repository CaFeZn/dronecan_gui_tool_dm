#
# DroneCAN GUI Tool — dmcan 后端（达妙 USB2CANFD / USB2FDCANDUAL 原生 USB）
#
# 本文件不属于上游 DroneCAN/gui_tool，为本 fork 新增；与上游同样以 MIT 发布（见 LICENSE）。
#
"""
dmcan 接口串：语法、解析、设备枚举。

接口串（下拉框里显示的是描述，真正传下去的是下面这个 value）::

    dmcan:<target>[.<channel>][@<nominal>][+<data>]

* `<target>`：`0` / `1` … 设备表下标；或 `s:<序列号>` 按序列号选设备（换 USB 口后下标会变，
  序列号不会，所以枚举出来的一律优先用序列号）；
* `<channel>`：通道号，双路模块是 `0` / `1`；省略时取界面上的 Bus Number - 1，再退到 0；
* `@<nominal>`：仲裁段位速率（bps）；省略时用界面上的 bitrate（默认 1 Mbps）；
* `+<data>`：数据段位速率（bps），**给了就是 CAN FD + BRS**；不给就是经典 CAN。

例子::

    dmcan:0                 # 设备 0 的 CH0，位速率取界面设置（默认 1 Mbps，经典 CAN）
    dmcan:s:E4B3230B4E4B.1  # 按序列号挑设备，用 CH1
    dmcan:0.0@1000000       # 显式 1 Mbps
    dmcan:0.0@1000000+5000000   # CAN FD：仲裁 1 Mbps / 数据 5 Mbps（自动开 TDC）

@note 解析是**严格**的：只认上面的语法（允许后面跟一段描述性文字，按空白截断）。
      手输错时直接报错，绝不"猜一个差不多的设备"——猜错的代价是往别的总线上发帧。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import dmsdk

logger = logging.getLogger(__name__)

__all__ = ['PREFIX', 'IfaceSpec', 'IfaceSpecError', 'DeviceEntry', 'is_dmcan_iface',
           'parse', 'format_spec', 'list_devices', 'list_ifaces', 'device_type_name',
           'channel_count', 'device_present', 'set_session_active', 'invalidate_cache']

#: 接口串前缀（大小写不敏感）。
PREFIX = 'dmcan:'

#: 设备表缓存的保鲜期（秒）。setup 窗口每 0.5 s 刷新一次接口列表，
#: 这里做一层缓存，避免 2 Hz 去枚举 USB。
CACHE_TTL = 1.0

#: 序列号里允许出现的字符；含其它字符（例如带 `.`）时该设备只按下标引用。
_SERIAL_RE = re.compile(r'^[0-9A-Za-z_-]{1,32}$')

_SPEC_RE = re.compile(r'''^dmcan:
    (?P<target>[0-9]{1,3}|s:[0-9A-Za-z_-]{1,40})
    (?:\.(?P<channel>[0-9]{1,2}))?
    (?:@(?P<nominal>[0-9]{3,9}))?
    (?:\+(?P<data>[0-9]{3,9}))?
$''', re.VERBOSE | re.IGNORECASE)

#: 位速率合法区间（bps）：仲裁段按 CAN FD 规范上限 1 Mbps；数据段给到 8 Mbps。
NOMINAL_RANGE = (10000, 1000000)
DATA_RANGE = (100000, 8000000)

DEVICE_TYPE_NAMES = {
    'USB2CANFD_S': 'USB2CANFD_S (单路)',
    'USB2CANFD_D': 'USB2CANFD_Dual (双路)',
    'LINKX4C': 'LinkX4C (四路)',
    'UNKNOWN': '未知型号',
}

DEVICE_TYPE_CHANNELS = {
    'USB2CANFD_S': 1,
    'USB2CANFD_D': 2,
    'LINKX4C': 4,
    'UNKNOWN': 1,
}


class IfaceSpecError(ValueError):
    """接口串不合法（含语法错误与取值越界）。"""


@dataclass(frozen=True)
class IfaceSpec:
    """一条 `dmcan:` 接口的解析结果。"""

    #: 设备下标（字符串形式，便于与序列号统一处理）
    target: str = '0'
    #: 通道号
    channel: int = 0
    #: 仲裁段位速率；None = 用界面上的 bitrate
    nominal: Optional[int] = None
    #: 数据段位速率；非 None 即 CAN FD
    data: Optional[int] = None

    @property
    def by_serial(self) -> bool:
        """@brief 是否按序列号选设备。"""
        return self.target.startswith('s:')

    @property
    def serial(self) -> str:
        """@brief 序列号（不是按序列号选设备时为空串）。"""
        return self.target[2:] if self.by_serial else ''

    @property
    def index(self) -> Optional[int]:
        """@brief 设备表下标（按序列号选设备时为 None）。"""
        return None if self.by_serial else int(self.target)

    @property
    def canfd(self) -> bool:
        """@brief 是否 CAN FD（给了数据段位速率就是）。"""
        return self.data is not None

    def nominal_or(self, fallback: int) -> int:
        """@brief 仲裁段位速率，缺省取 `fallback`。"""
        return int(self.nominal or fallback or 1000000)

    def to_string(self) -> str:
        """@brief 规范化回字符串（`format_spec` 的简写）。"""
        return format_spec(self)


@dataclass
class DeviceEntry:
    """枚举到的一台设备（只含枚举信息，不打开设备）。"""

    index: int
    serial: str = ''
    device_type: str = 'UNKNOWN'
    vid: int = 0
    pid: int = 0
    present: bool = True

    @property
    def type_name(self) -> str:
        """@brief 型号人读名。"""
        return DEVICE_TYPE_NAMES.get(self.device_type, self.device_type)

    @property
    def channels(self) -> int:
        """@brief 该型号的通道数。"""
        return DEVICE_TYPE_CHANNELS.get(self.device_type, 1)

    @property
    def reference(self) -> str:
        """@brief 用于接口串的引用：能用的序列号优先，否则下标。"""
        if self.serial and _SERIAL_RE.match(self.serial):
            return 's:' + self.serial
        return str(self.index)

    def describe(self) -> str:
        """@brief 一行式描述，用于 CLI 与日志。"""
        parts = ['#%d' % self.index, self.type_name]
        parts.append('SN=%s' % self.serial if self.serial else 'SN=?')
        parts.append('VID:PID=%04X:%04X' % (self.vid, self.pid))
        if not self.present:
            parts.append('(已拔出)')
        return '  '.join(parts)


def is_dmcan_iface(name: str) -> bool:
    """@brief 名字是不是 dmcan 接口（只判前缀，不校验语法）。"""
    return str(name).strip().lower().startswith(PREFIX)


def _first_token(text: str) -> str:
    """@brief 取首个空白分隔的片段（允许接口串后面跟描述文字）。"""
    text = str(text).strip()
    parts = text.split()
    return parts[0] if parts else ''


def parse(text: str, default_bitrate: Optional[int] = None,
          default_channel: Optional[int] = None) -> IfaceSpec:
    """
    @brief 解析 `dmcan:` 接口串。

    @param text 接口串（允许后面跟描述文字，按空白截断）
    @param default_bitrate 没写 `@...` 时的位速率（通常来自界面上的 bitrate 输入框）
    @param default_channel 没写 `.ch` 时的通道号（通常来自界面上的 Bus Number - 1）
    @return `IfaceSpec`
    @raise IfaceSpecError 语法或取值不合法（报错信息里带正确写法）
    """
    token = _first_token(text)
    if not token:
        raise IfaceSpecError('接口串为空；正确写法例如 %s0 或 %s0.1@1000000' % (PREFIX, PREFIX))
    if not token.lower().startswith(PREFIX):
        raise IfaceSpecError('不是 dmcan 接口（应以 %r 开头）：%r' % (PREFIX, token))

    match = _SPEC_RE.match(token)
    if not match:
        raise IfaceSpecError(
            '无法解析 dmcan 接口 %r；正确写法：%s<下标|s:序列号>[.<通道>][@<位速率>][+<数据段位速率>]，'
            '例如 %s0、%s0.1@1000000、%s0.0@1000000+5000000' % (token, PREFIX, PREFIX, PREFIX, PREFIX))

    target = match.group('target')
    channel = match.group('channel')
    nominal = match.group('nominal')
    data = match.group('data')

    channel_value = int(channel) if channel is not None else int(default_channel or 0)
    if channel_value < 0 or channel_value > 7:
        raise IfaceSpecError('通道号越界：%d（合理范围 0~7）' % channel_value)

    nominal_value: Optional[int] = None
    if nominal is not None:
        nominal_value = int(nominal)
        if not (NOMINAL_RANGE[0] <= nominal_value <= NOMINAL_RANGE[1]):
            raise IfaceSpecError('仲裁段位速率越界：%d（%d ~ %d bps）'
                                 % (nominal_value, NOMINAL_RANGE[0], NOMINAL_RANGE[1]))
    elif default_bitrate:
        nominal_value = int(default_bitrate)
        if not (NOMINAL_RANGE[0] <= nominal_value <= NOMINAL_RANGE[1]):
            raise IfaceSpecError('界面上的位速率越界：%d（%d ~ %d bps）'
                                 % (nominal_value, NOMINAL_RANGE[0], NOMINAL_RANGE[1]))

    data_value: Optional[int] = None
    if data is not None:
        data_value = int(data)
        if not (DATA_RANGE[0] <= data_value <= DATA_RANGE[1]):
            raise IfaceSpecError('数据段位速率越界：%d（%d ~ %d bps）'
                                 % (data_value, DATA_RANGE[0], DATA_RANGE[1]))
        if nominal_value is None:
            nominal_value = 1000000

    return IfaceSpec(target=target, channel=channel_value, nominal=nominal_value, data=data_value)


def format_spec(spec: IfaceSpec) -> str:
    """@brief 把 `IfaceSpec` 规范化回接口串。"""
    out = '%s%s.%d' % (PREFIX, spec.target, spec.channel)
    if spec.nominal:
        out += '@%d' % spec.nominal
    if spec.data:
        out += '+%d' % spec.data
    return out


# --------------------------------------------------------------------------- 枚举
_session_active = 0
_session_lock = threading.Lock()
_cache_lock = threading.Lock()
_cache: Optional[Tuple[float, List[DeviceEntry]]] = None


def set_session_active(active: bool) -> None:
    """
    @brief 标记"有会话正在使用设备"，期间暂停重新枚举。

    枚举会重建 SDK 的设备封装表；正在收发时去重建是没必要的风险，所以会话
    打开期间 `list_devices()` 直接回上一次的结果。

    @param active 会话是否活跃
    """
    global _session_active
    with _session_lock:
        _session_active = max(0, _session_active + (1 if active else -1))


def active_sessions() -> int:
    """@brief 当前有多少个活跃会话在占用设备（枚举据此暂停重建设备表）。"""
    with _session_lock:
        return _session_active


def invalidate_cache() -> None:
    """@brief 丢弃设备表缓存（下次调用重新枚举）。"""
    global _cache
    with _cache_lock:
        _cache = None


def _read_entry(context, index: int) -> Optional[DeviceEntry]:
    """@brief 读一台设备的枚举信息（读不到返回 None）。"""
    try:
        device = context.get_device(index)
    except KeyError:
        return None
    serial = ''
    device_type = 'UNKNOWN'
    vid = pid = 0
    try:
        info = device.get_info()
        serial = info.serial or ''
        device_type = getattr(info.device_type, 'name', str(info.device_type))
        vid, pid = int(info.vid), int(info.pid)
    except Exception as ex:  # pragma: no cover - 依赖环境
        logger.warning('读取设备 #%d 枚举信息失败：%s', index, ex)
    return DeviceEntry(index=index, serial=serial, device_type=device_type,
                       vid=vid, pid=pid, present=device_present(device))


def _enumerate() -> List[DeviceEntry]:
    """
    @brief 真正去枚举一次（需要 dmsdk 可用）。

    @note 序列号偶尔会读空（典型场景：上一个进程刚释放 USB 句柄，这一次枚举只读到了
          VID/PID）：这时刷新一次设备表重读，仍为空则退化成用下标引用。
    """
    context = dmsdk.get_context()
    count = context.find_devices()
    entries = [entry for entry in (_read_entry(context, index) for index in range(count))
               if entry is not None]
    missing = [entry.index for entry in entries if not entry.serial and entry.present]
    if missing:
        logger.debug('设备 %s 没读到序列号，刷新设备表后重读', missing)
        try:
            context.find_devices()
            for position, entry in enumerate(entries):
                if entry.index in missing and entry.present:
                    refreshed = _read_entry(context, entry.index)
                    if refreshed is not None:
                        entries[position] = refreshed
        except Exception as ex:  # pragma: no cover - 依赖环境
            logger.warning('重读设备序列号失败：%s', ex)
    # 拔掉的设备不列出来（句柄还在，插回来会重新出现）
    return [entry for entry in entries if entry.present]


def device_present(device) -> bool:
    """@brief 设备是否还插着（老绑定没有该接口时保守地当作"在"）。"""
    checker = getattr(device, 'is_present', None)
    if checker is None:
        return True
    try:
        return bool(checker())
    except Exception:
        return True


def list_devices(refresh: bool = False) -> List[DeviceEntry]:
    """
    @brief 枚举设备（带 1 s 缓存；会话活跃时直接回缓存）。

    @param refresh 忽略缓存强制重新枚举
    @return `DeviceEntry` 列表（空列表 = 没设备或 dmsdk 不可用）
    """
    global _cache
    with _session_lock:
        session_active = _session_active > 0
    with _cache_lock:
        cached = _cache
    now = time.monotonic()
    if cached is not None and (not refresh or session_active) and (session_active or now - cached[0] < CACHE_TTL):
        return list(cached[1])
    try:
        entries = _enumerate()
    except dmsdk.SdkUnavailable:
        raise
    except Exception as ex:
        logger.warning('枚举 dmcan 设备失败：%s', ex, exc_info=True)
        entries = []
    with _cache_lock:
        _cache = (now, list(entries))
    return entries


def list_ifaces(refresh: bool = False) -> "OrderedDict[str, str]":
    """
    @brief 生成给 setup 窗口下拉框用的 描述 -> 接口串 映射。

    @param refresh 强制重新枚举
    @return `OrderedDict`；dmsdk 不可用或没有设备时返回空表（**不抛异常**）

    @note 接口串里**不带** `@` 位速率：真正生效的值始终来自 setup 窗口的
          "CAN bus bit rate" 输入框，界面上改一下立刻生效，不用再去改接口串。
    """
    out: "OrderedDict[str, str]" = OrderedDict()
    try:
        devices = list_devices(refresh=refresh)
    except dmsdk.SdkUnavailable as ex:
        logger.debug('dmcan 设备不可枚举：%s', ex)
        return out
    for entry in devices:
        for channel in range(max(1, entry.channels)):
            # 接口串里**不带** @ 位速率：真正生效的值始终来自 setup 窗口的 bitrate 输入框，
            # 这样界面上改位速率立刻生效，不用再去改接口串。
            value = format_spec(IfaceSpec(target=entry.reference, channel=channel))
            # 描述里不写位速率：真正生效的那个值始终来自 setup 窗口的 "CAN bus bit rate" 输入框
            tag = ('SN %s…' % entry.serial[:12]) if entry.serial else ('#%d' % entry.index)
            description = '%s%s CH%d  [%s]' % (PREFIX, entry.type_name, channel, tag)
            out[description] = value
    return out
