#
# DroneCAN GUI Tool — dmcan 后端（达妙 USB2CANFD / USB2FDCANDUAL 原生 USB）
#
# 本文件不属于上游 DroneCAN/gui_tool，为本 fork 新增；与上游同样以 MIT 发布（见 LICENSE）。
#
"""
一个 dmcan 会话：打开设备、配位定时、使能通道、订阅收帧、收发与统计。

这一层**不依赖 pydronecan**，GUI（`DmCanDriver`）、无界面 CLI 与自检都复用它，
所以"设备到底怎么配、丢帧统计从哪来"只有一份实现。

按 SDK 的硬约束办事（见 dm-device-sdk/skills/dmcan-sdk/SKILL.md）：

* 顺序：先 `set_channel_baudrate*` 再 `enable_channel`；掉线重连后通道默认是关的，要重新使能；
* 改位定时 / 改过滤表**先关通道**，改完再使能；下发到硬件生效有 0.086 ~ 1.0 s 的延迟，要等；
* 收帧回调跑在 SDK 的通知线程上，里面只能做"拷一份塞队列"这种轻活；
* 统计是**1 Hz 快照且为累计值**，判丢帧必须连着读两次看增量，并先看 `drop_flags` 再信细分字段。
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import dmcan_iface, dmsdk
from .dmcan_iface import IfaceSpec

logger = logging.getLogger(__name__)

__all__ = ['SessionError', 'ChannelConfig', 'ChannelStats', 'StatsDiff', 'SelfTestResult',
           'DmCanSession', 'BUS_STATES', 'DROP_FLAG_RX_ECHO', 'DROP_FLAG_TX']

#: `dmcan_channel_stats::drop_flags` 的位定义（对应 SDK 头的 DMCAN_DROP_FLAG_*）。
DROP_FLAG_RX_ECHO = 0x01
DROP_FLAG_TX = 0x02

#: 总线状态码 -> 人读名（0 = active … 3 = bus-off）。
BUS_STATES = {0: 'active', 1: 'warning', 2: 'passive', 3: 'bus-off'}

#: 位定时下发后等硬件重载的时间（秒）。SDK 实测 0.086 ~ 1.0 s，取 1.2 s 留余量。
RELOAD_SETTLE = 1.2

#: 自定义模式（回环等）同样要等重载。
MODE_SETTLE = 1.2

#: 打开设备的重试次数与间隔。USB 句柄刚被别的进程（上一个工具实例）释放时，
#: 紧跟着的 open() 经常会拿到一个短暂的忙错误；重试几次就能过，不必打扰用户。
OPEN_RETRIES = 3
OPEN_RETRY_DELAY = 0.4

#: 自检帧的 ID 基址（扩展帧，落在 29 位内且远离常规 DroneCAN 流量）。
SELFTEST_ID_BASE = 0x1E000000
SELFTEST_ID_MASK = 0x000FFFFF
SELFTEST_MAGIC = 0xA5


class SessionError(RuntimeError):
    """会话级失败（打开/配置/收发），消息里带可操作的排查建议。"""


#: 本进程已打开的 dmcan 设备（key -> 会话描述）。dmcan 的 open() 是整台设备的 USB 会话，
#: 双路模块的两个通道共用它，所以同一台设备不能开两个会话。
_open_devices: Dict[str, str] = {}
_registry_lock = threading.Lock()


@dataclass
class ChannelConfig:
    """通道位定时快照（`dmcan_channel_can_info` + 细节接口补的字段）。"""

    channel: int = 0
    canfd: bool = False
    non_iso: bool = False
    retrans: bool = False
    nominal: int = 0
    data: int = 0
    nominal_sp: float = 0.0
    data_sp: float = 0.0
    #: 来自 `get_channel_baudrate_details()`；读不到时为 None
    mode: Optional[int] = None
    enable_tdc: Optional[bool] = None
    busoff_autoreset_ms: Optional[int] = None

    def describe(self) -> str:
        """@brief 一行式描述，用于 CLI/面板标题。"""
        text = 'CH%d %s 仲裁 %s' % (self.channel, 'CAN FD' if self.canfd else '经典 CAN',
                                    _rate(self.nominal))
        if self.canfd:
            text += '/数据 %s%s' % (_rate(self.data), ' BRS' if self.data > self.nominal else '')
        if self.nominal_sp:
            text += ' 采样点 %.1f%%' % (self.nominal_sp * 100.0)
        if self.mode is not None:
            text += ' 模式 %s' % MODE_NAMES.get(self.mode, str(self.mode))
        if self.enable_tdc:
            text += ' TDC开'
        return text


#: `dmcan_channel_mode` -> 人读名。
MODE_NAMES = {0: '正常', 1: '内部回环', 2: '外部回环', 3: '只听'}


def _rate(value: int) -> str:
    """@brief 位速率美化（1000000 -> 1 Mbps）。"""
    if not value:
        return '?'
    if value % 1000000 == 0:
        return '%d Mbps' % (value // 1000000)
    if value % 1000 == 0:
        return '%d kbps' % (value // 1000)
    return '%d bps' % value


@dataclass
class ChannelStats:
    """一次统计快照：设备侧（1 Hz 累计上报）+ 主机侧（本进程计数）。"""

    ok: bool = False
    #: 设备侧：这一拍还没收到周期上报时为 True（字段全 0，不代表"没丢过帧"）
    pending: bool = False
    tx_admitted: int = 0
    tx_completed: int = 0
    tx_dropped: int = 0
    tx_failed: int = 0
    rx_received: int = 0
    rx_dropped: int = 0
    sw_tx_fifo_level: int = 0
    hw_tx_fifo_level: int = 0
    enabled: bool = False
    drop_flags: int = 0
    rx_dropped_queue: int = 0
    rx_dropped_msgc: int = 0
    echo_dropped_queue: int = 0
    echo_dropped_msgc: int = 0
    echo_sent: int = 0
    tx_dropped_no_credit: int = 0
    tx_dropped_queue_full: int = 0
    tx_dropped_submit_fail: int = 0
    tx_dropped_ch_disabled: int = 0
    bus_state: int = 0
    tec: int = 0
    rec: int = 0
    credits: int = 0
    in_flight: int = 0
    #: 主机侧（本会话从打开到现在）
    host_rx: int = 0
    host_tx: int = 0
    host_rx_dropped: int = 0
    timestamp: float = 0.0

    @property
    def bus_state_name(self) -> str:
        """@brief 总线状态人读名。"""
        return BUS_STATES.get(self.bus_state, str(self.bus_state))

    @property
    def detail_valid(self) -> bool:
        """@brief 丢帧细分字段是否已有值（`drop_flags` 位）。"""
        return bool(self.drop_flags)

    def diff(self, previous: 'ChannelStats') -> 'StatsDiff':
        """@brief 与上一次快照的增量（uint32 回绕安全）。"""
        dt = max(1e-6, self.timestamp - previous.timestamp)
        return StatsDiff(
            seconds=dt,
            tx_admitted=_delta(self.tx_admitted, previous.tx_admitted),
            tx_completed=_delta(self.tx_completed, previous.tx_completed),
            tx_dropped=_delta(self.tx_dropped, previous.tx_dropped),
            tx_failed=_delta(self.tx_failed, previous.tx_failed),
            rx_received=_delta(self.rx_received, previous.rx_received),
            rx_dropped=_delta(self.rx_dropped, previous.rx_dropped),
            rx_dropped_queue=_delta(self.rx_dropped_queue, previous.rx_dropped_queue),
            rx_dropped_msgc=_delta(self.rx_dropped_msgc, previous.rx_dropped_msgc),
            host_rx=_delta(self.host_rx, previous.host_rx),
            host_tx=_delta(self.host_tx, previous.host_tx),
            host_rx_dropped=_delta(self.host_rx_dropped, previous.host_rx_dropped),
        )


def _delta(current: int, previous: int) -> int:
    """@brief 计数增量（按 32 位无符号回绕）。"""
    return (int(current) - int(previous)) & 0xFFFFFFFF


@dataclass
class StatsDiff:
    """两次统计之间的增量与速率。"""

    seconds: float = 1.0
    tx_admitted: int = 0
    tx_completed: int = 0
    tx_dropped: int = 0
    tx_failed: int = 0
    rx_received: int = 0
    rx_dropped: int = 0
    rx_dropped_queue: int = 0
    rx_dropped_msgc: int = 0
    host_rx: int = 0
    host_tx: int = 0
    host_rx_dropped: int = 0

    @property
    def rx_fps(self) -> float:
        """@brief 设备侧收帧速率（帧/秒）。"""
        return self.rx_received / max(1e-6, self.seconds)

    @property
    def tx_fps(self) -> float:
        """@brief 设备侧发帧速率（帧/秒）。"""
        return self.tx_completed / max(1e-6, self.seconds)

    def summary(self) -> str:
        """@brief 一行式汇总（丢帧 > 0 时明确标出来）。"""
        text = ('收 %.0f 帧/s 发 %.0f 帧/s' % (self.rx_fps, self.tx_fps))
        if self.rx_dropped or self.tx_dropped or self.host_rx_dropped:
            text += '  |  丢帧: 设备收 %d (队列 %d / 内部 %d) 设备发 %d 主机 %d' % (
                self.rx_dropped, self.rx_dropped_queue, self.rx_dropped_msgc,
                self.tx_dropped, self.host_rx_dropped)
        return text


@dataclass
class SelfTestResult:
    """外部回环自检结果。"""

    sent: int = 0
    received: int = 0
    missing: int = 0
    duplicated: int = 0
    elapsed: float = 0.0
    latency_min_ms: float = 0.0
    latency_p50_ms: float = 0.0
    latency_avg_ms: float = 0.0
    latency_max_ms: float = 0.0
    channel_config: str = ''
    detail: str = ''

    @property
    def loss_ratio(self) -> float:
        """@brief 丢帧比例（0.0 ~ 1.0）。"""
        return (self.missing / self.sent) if self.sent else 0.0

    def summary(self) -> str:
        """@brief 一行式结论。"""
        text = ('回环自检：发 %d 收 %d 丢 %d（%.3f%%）'
                % (self.sent, self.received, self.missing, self.loss_ratio * 100.0))
        if self.received:
            text += ('；往返延迟 min/p50/平均/最大 = %.2f/%.2f/%.2f/%.2f ms'
                     % (self.latency_min_ms, self.latency_p50_ms,
                        self.latency_avg_ms, self.latency_max_ms))
        return text


class DmCanSession:
    """
    一台设备 + 一个通道的会话。

    @note 生命周期：`open()` -> 收发 -> `close()`；`close()` 可重复调用。
    """

    def __init__(self, spec, default_bitrate: Optional[int] = None,
                 default_channel: Optional[int] = None,
                 events: Optional[int] = None,
                 callback: Optional[Callable[[Any, Any], None]] = None,
                 allow_bitrate_change: bool = True):
        """
        @param spec 接口串或已解析好的 `IfaceSpec`
        @param default_bitrate 接口串没写 `@` 时用的位速率
        @param default_channel 接口串没写 `.ch` 时用的通道号
        @param events 订阅的事件位图（`dmcan_event_flag`），默认只订 RX_FRAME
        @param callback 事件回调 `callback(session, event)`，在 SDK 通知线程上同步执行
        @param allow_bitrate_change 允许为了匹配 `@` 而改设备位定时吗；
                                    False 时只在"当前配置不匹配"上报错而不写设备
        """
        self._spec: IfaceSpec = spec if isinstance(spec, IfaceSpec) else dmcan_iface.parse(
            spec, default_bitrate=default_bitrate, default_channel=default_channel)
        self._default_bitrate = default_bitrate
        self._events = events
        self._callback = callback
        self._allow_bitrate_change = allow_bitrate_change

        self._dmcan: Any = None
        self._device: Any = None
        self._subscription: Any = None
        self._opened = False
        self._closed = False
        self._lock = threading.RLock()

        self._host_rx = 0
        self._host_tx = 0
        self._host_rx_dropped = 0
        self._last_error: Optional[BaseException] = None
        self._selftest_sink: Optional[Callable[[Any], None]] = None
        self._mode_override: Optional[int] = None
        #: 本会话是否已经给"会话活跃"计数加过一（保证只减一次）
        self._counting_active = False

        self.device_info: Dict[str, Any] = {}
        self.firmware: str = ''
        self.config: Optional[ChannelConfig] = None
        #: `open()` 时是否真的改写过设备位定时（用于日志与面板提示）
        self.bitrate_written = False

    # ------------------------------------------------------------------ 属性
    @property
    def spec(self) -> IfaceSpec:
        """@brief 接口串解析结果。"""
        return self._spec

    @property
    def channel(self) -> int:
        """@brief 通道号。"""
        return self._spec.channel

    @property
    def nominal(self) -> int:
        """@brief 期望的仲裁段位速率。"""
        return self._spec.nominal_or(self._default_bitrate or 1000000)

    @property
    def data_rate(self) -> Optional[int]:
        """@brief 期望的数据段位速率（None = 经典 CAN）。"""
        return self._spec.data

    @property
    def canfd(self) -> bool:
        """@brief 是否按 CAN FD 配置。"""
        return self._spec.canfd

    @property
    def device(self) -> Any:
        """@brief 底层 `dmcan.DmCanDevice`（未打开时为 None）。"""
        return self._device

    @property
    def opened(self) -> bool:
        """@brief 会话是否已打开。"""
        return self._opened and not self._closed

    @property
    def description(self) -> str:
        """@brief 设备 + 通道的一行式描述。"""
        name = self.device_info.get('type_name') or 'dmcan 设备'
        serial = self.device_info.get('serial') or '?'
        return '%s SN=%s CH%d' % (name, serial, self.channel)

    def host_counters(self) -> Dict[str, int]:
        """@brief 主机侧计数（线程安全读）。"""
        return {'rx': self._host_rx, 'tx': self._host_tx, 'rx_dropped': self._host_rx_dropped}

    # ------------------------------------------------------------------ 打开
    def open(self) -> 'DmCanSession':
        """
        @brief 打开设备、必要时改位定时、使能通道、订阅事件。

        @return self
        @raise SessionError 打不开设备 / 配置失败（消息里带排查建议）
        """
        with self._lock:
            if self._opened:
                return self
            self._dmcan = dmsdk.load()
            try:
                context = dmsdk.get_context()
            except dmsdk.SdkUnavailable as ex:
                raise SessionError(str(ex)) from ex

            if not self._counting_active:
                self._counting_active = True
                dmcan_iface.set_session_active(True)
            try:
                self._device = self._resolve_device(context)
                self._read_identity()
                self._claim_device()
                self._open_device()
                # 固件信息必须 open 之后读：绑定会以 NOT_OPEN 拒绝未打开的查询
                self._read_firmware()
                self._configure_channel()
                self._subscribe()
            except BaseException:
                self._teardown()      # 里面负责递减 set_session_active 并释放句柄
                raise
            self._opened = True
            logger.info('dmcan 会话就绪：%s %s', self.description,
                        self.config.describe() if self.config else '')
            return self

    def _resolve_device(self, context):
        """@brief 按下标或序列号找设备（找不到给出可操作的提示）。"""
        count = context.find_devices()
        if count <= 0:
            raise SessionError('没有找到 dmcan 设备。检查：USB 是否插好、是否被别的程序（串口助手/'
                               '另一个 GUI 实例）占用、Linux 下 udev 权限。')
        if self._spec.by_serial:
            want = self._spec.serial
            for _attempt in range(2):
                for index in range(count):
                    try:
                        device = context.get_device(index)
                    except KeyError:
                        continue
                    try:
                        serial = device.get_info().serial or ''
                    except Exception:
                        continue
                    if serial == want:
                        if not dmcan_iface.device_present(device):
                            raise SessionError('设备 #%d（SN=%s）已拔出；请重新插好或重新打开接口列表。'
                                               % (index, want))
                        return device
                # 序列号偶尔会读空：刷一次设备表再找一遍
                count = context.find_devices()
            unread = 0
            for index in range(count):
                try:
                    if not (context.get_device(index).get_info().serial or ''):
                        unread += 1
                except Exception:
                    unread += 1
            hint = ('其中 %d 台这次没读到序列号，可以改用下标引用（例如 %s0.0）'
                    % (unread, dmcan_iface.PREFIX)) if unread else '请重新打开接口列表选择'
            raise SessionError('按序列号 %r 没找到设备（当前枚举到 %d 台；%s）。'
                               % (want, count, hint))
        index = int(self._spec.index)
        try:
            device = context.get_device(index)
        except KeyError:
            raise SessionError('设备下标 %d 不存在（当前枚举到 %d 台）；请重新打开接口列表选择。'
                               % (index, count)) from None
        if not dmcan_iface.device_present(device):
            raise SessionError('设备 #%d 已拔出；请重新插好或重新打开接口列表。' % index)
        return device

    def _open_device(self) -> None:
        """
        @brief 打开设备，带有限次重试。

        @raise SessionError 重试用尽仍打不开（消息里给排查顺序）
        """
        last: Optional[BaseException] = None
        for attempt in range(OPEN_RETRIES):
            try:
                self._device.open()
                return
            except Exception as ex:
                last = ex
                if attempt + 1 < OPEN_RETRIES:
                    logger.info('打开设备失败（第 %d/%d 次），%.1f s 后重试：%s',
                                attempt + 1, OPEN_RETRIES, OPEN_RETRY_DELAY, ex)
                    time.sleep(OPEN_RETRY_DELAY)
        raise SessionError(_open_hint(self._device, last)) from last

    def _claim_device(self) -> None:
        """
        @brief 登记"这台设备已经被本进程的某个会话占用"。

        @note dmcan 的 `open()` 是**整台设备**的 USB 会话（双路模块的两个通道共用它），
              所以同一进程里对同一台设备开第二个会话只会拿到一个含糊的 USB 错误。
              这里提前拦住并说清楚。
        """
        key = self._device_key()
        if key is None:
            return
        with _registry_lock:
            owner = _open_devices.get(key)
            if owner is not None:
                raise SessionError(
                    '设备 %s 已经被本进程里的另一个会话打开（%s）。dmcan 的 open() 是整台设备的 '
                    'USB 会话，一台设备同一时刻只能有一个；请先关闭那个会话。'
                    % (key, owner))
            _open_devices[key] = self.description

    def _device_key(self) -> Optional[str]:
        """@brief 设备的稳定标识（序列号优先，退化到下标）。"""
        info = self.device_info
        if info.get('serial'):
            return 'SN=%s' % info['serial']
        if info.get('index') is not None:
            return '#%s' % info['index']
        return None

    def _release_device(self) -> None:
        """@brief 注销设备占用登记。"""
        key = self._device_key()
        if key is None:
            return
        with _registry_lock:
            _open_devices.pop(key, None)

    def _read_identity(self) -> None:
        """@brief 读设备型号/序列号/固件信息（读不到不算失败）。"""
        try:
            info = self._device.get_info()
            self.device_info = {
                'index': getattr(self._device, 'index', None),
                'type': getattr(info.device_type, 'name', str(info.device_type)),
                'type_name': dmcan_iface.DEVICE_TYPE_NAMES.get(
                    getattr(info.device_type, 'name', ''), str(info.device_type)),
                'serial': info.serial or '',
                'vid': int(info.vid),
                'pid': int(info.pid),
            }
        except Exception as ex:
            logger.warning('读设备信息失败：%s', ex)

    def _read_firmware(self) -> None:
        """@brief 读固件版本与构建标识（**需要设备已 open**）。"""
        try:
            firmware = self._device.get_firmware_info()
            valid = bool(getattr(firmware, 'version_valid', True))
            build_valid = bool(getattr(firmware, 'build_id_valid', False))
            version = getattr(firmware, 'version', None)
            text = ''
            if valid and version is not None:
                text = 'V%d.%d.%d' % (version.major, version.minor, version.build)
            else:
                text = 'V?（设备可能在 boot，或固件不报版本）'
            if build_valid:
                text += ' [%s]' % bytes(getattr(firmware, 'build_id', b'')).decode('utf-8', 'replace')
            self.firmware = text
        except Exception as ex:
            self.firmware = '读不到（%s）' % ex
        self.device_info['firmware'] = self.firmware

    # ------------------------------------------------------------- 通道配置
    def _configure_channel(self) -> None:
        """@brief 按需改写位定时并使能通道。"""
        device, channel = self._device, self.channel
        current = None
        try:
            current = device.get_channel_baudrate(channel)
        except Exception as ex:
            logger.warning('读通道位定时失败（继续按期望值配置）：%s', ex)

        plan = self._plan_bitrate(current)
        if plan is not None:
            if not self._allow_bitrate_change:
                raise SessionError('设备通道位定时与接口串要求不一致，但当前策略不允许改写；'
                                   '请把接口串里的 @/+ 去掉，或允许改写位定时。')
            self._write_bitrate(plan)
        self._enable_channel()
        self._refresh_config()

    def _plan_bitrate(self, current) -> Optional[Any]:
        """@brief 算出需要下发的位定时；已经匹配时返回 None（不动设备）。"""
        want_nominal = self.nominal
        want_data = self.data_rate
        want_fd = self.canfd

        if current is not None:
            same_fd = bool(current.canfd) == want_fd
            same_nominal = int(current.can_baudrate) == want_nominal
            same_data = (not want_fd) or int(current.canfd_baudrate) == int(want_data or want_nominal)
            if same_fd and same_nominal and same_data:
                logger.info('通道 CH%d 位定时与期望一致（%s），不改设备', self.channel,
                            _rate(want_nominal))
                return None

        info = self._dmcan.dmcan_channel_can_info()
        if current is not None:
            # 保留用户原有的采样点/非 ISO/重传等设置，只改"要不要 FD"和位速率
            info.can_sp = current.can_sp
            info.canfd_sp = current.canfd_sp
            info.non_iso = bool(current.non_iso)
            info.retrans = bool(current.retrans)
        info.canfd = want_fd
        info.can_baudrate = int(want_nominal)
        # 经典 CAN 时数据段字段也要非 0（固件会拒"经典 + 数据段全 0"）；
        # 走 set_channel_baudrate 时 SDK 会自动填，这里显式给上更保险。
        info.canfd_baudrate = int(want_data or want_nominal)
        return info

    def _write_bitrate(self, info) -> None:
        """@brief 关通道 -> 下发位定时 -> 等重载 -> 使能 -> 复核（失败给出可操作提示）。"""
        device, channel = self._device, self.channel
        try:
            device.enable_channel(channel, False)
        except Exception as ex:
            logger.warning('改位定时前关闭通道 CH%d 失败（继续尝试下发）：%s', channel, ex)
        try:
            device.set_channel_baudrate(channel, info)
        except Exception as ex:
            # 下发失败可能让通道变成不可用：先尽量恢复，再把原因抛出去
            self._try_enable()
            raise SessionError(
                '写通道 CH%d 位定时失败：%s。常见原因：位速率不在固件支持范围、'
                '经典 CAN 却给了全 0 的数据段参数、固件过旧。设备可能已被置为关闭，'
                '已尝试重新使能。' % (channel, ex)) from ex
        time.sleep(RELOAD_SETTLE)
        self.bitrate_written = True
        self._enable_channel()
        expected_fd = bool(info.canfd)
        expected_nominal = int(info.can_baudrate)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                back = device.get_channel_baudrate(channel)
            except Exception:
                break
            if bool(back.canfd) == expected_fd and int(back.can_baudrate) == expected_nominal:
                logger.info('位定时已生效：CH%d %s', channel, _rate(int(back.can_baudrate)))
                return
            time.sleep(0.3)
        logger.warning('位定时回读与期望不一致（CH%d 期望 %s / %s），'
                       '可能固件不支持该速率或尚未重载完成', channel,
                       'FD' if expected_fd else '经典', _rate(expected_nominal))

    def _enable_channel(self) -> None:
        """@brief 使能通道（失败时给出固件版本相关的提示）。"""
        try:
            self._device.enable_channel(self.channel, True)
        except Exception as ex:
            raise SessionError(
                '使能通道 CH%d 失败：%s。常见原因：(1) 固件过旧——通道开/关要求固件回 2 字节应答，'
                '需要 V2.0.0 及以上（构建标识 ≥ 单路 d64e840 / 双路 b5c2574 / 四路 d5bc7db）；'
                '(2) 设备刚复位，需要 close/open 后重新使能。' % (self.channel, ex)) from ex

    def _try_enable(self) -> None:
        """@brief 尽力使能通道（不抛异常，供错误恢复路径使用）。"""
        try:
            self._device.enable_channel(self.channel, True)
        except Exception as ex:
            logger.warning('恢复使能通道失败：%s', ex)

    def _refresh_config(self) -> None:
        """@brief 回读通道配置（读不到就保留旧值）。"""
        device, channel = self._device, self.channel
        config = ChannelConfig(channel=channel)
        try:
            info = device.get_channel_baudrate(channel)
            config.canfd = bool(info.canfd)
            config.non_iso = bool(info.non_iso)
            config.retrans = bool(info.retrans)
            config.nominal = int(info.can_baudrate)
            config.data = int(info.canfd_baudrate)
            config.nominal_sp = float(info.can_sp or 0.0)
            config.data_sp = float(info.canfd_sp or 0.0)
        except Exception as ex:
            logger.warning('回读位定时失败：%s', ex)
        try:
            details = device.get_channel_baudrate_details(channel)
            config.mode = int(details.mode)
            config.enable_tdc = bool(details.enable_tdc)
            config.busoff_autoreset_ms = int(details.busoff_autoreset_ms)
        except Exception as ex:
            logger.debug('回读位定时细节失败：%s', ex)
        self.config = config

    def _subscribe(self) -> None:
        """@brief 订阅设备事件（默认只订收帧）。"""
        events = self._events
        if events is None:
            events = int(self._dmcan.dmcan_event_flag.RX_FRAME)
        self._subscription = self._device.subscribe(events, self._on_event,
                                                    channel_mask=1 << self.channel)

    # ------------------------------------------------------------ 事件与收发
    def _on_event(self, device, event) -> None:
        """@brief SDK 通知线程上的回调：只做计数与转发，绝不阻塞。"""
        try:
            if event.frame is not None:
                self._host_rx += 1
                sink = self._selftest_sink
                if sink is not None and self._is_selftest_frame(event.frame):
                    sink(event.frame)
                    return
            if self._callback is not None:
                self._callback(self, event)
        except Exception:
            logger.error('dmcan 事件处理异常', exc_info=True)

    @staticmethod
    def _is_selftest_frame(frame) -> bool:
        """@brief 是不是本会话自检用的帧（自检期间把它们从正常收帧路径里摘出去）。"""
        return (int(frame.can_id) & ~SELFTEST_ID_MASK) == SELFTEST_ID_BASE

    def send(self, can_id: int, data: bytes = b'', canfd: bool = False,
             ext: bool = True, rtr: bool = False, brs: bool = False) -> None:
        """
        @brief 发一帧。

        @param can_id 帧 ID（扩展帧 ≤ 0x1FFFFFFF）
        @param data 数据（0~64 字节，长度由它决定）
        @param canfd 是否 CAN FD
        @param ext 是否扩展帧
        @param rtr 是否远程帧
        @param brs 是否 BRS（仅 CAN FD）
        @raise SessionError 发送被拒（设备掉了 / 通道关了 / 参数非法）
        """
        device = self._device
        if device is None:
            raise SessionError('会话未打开，不能发送')
        try:
            device.send_can(self.channel, can_id, bytes(data), canfd=bool(canfd),
                            ext=bool(ext), rtr=bool(rtr), brs=bool(brs))
        except Exception as ex:
            raise SessionError('发送帧 0x%X 失败：%s' % (can_id, ex)) from ex
        self._host_tx += 1

    def reopen_after_reset(self) -> None:
        """
        @brief 掉线重连：close -> open，并重新使能通道。

        @note 设备复位后通道默认是关的，SDK 也要求重新使能，所以这里整条重来。
        """
        logger.info('重新打开 dmcan 会话…')
        spec, events, callback = self._spec, self._events, self._callback
        self.close(disable_channel=False)
        self._spec, self._events, self._callback = spec, events, callback
        self._closed = False
        self.open()

    # ------------------------------------------------------------------ 统计
    def statistics(self) -> ChannelStats:
        """
        @brief 读一次统计（设备侧 1 Hz 累计快照 + 主机侧计数）。

        @return `ChannelStats`；设备侧那一拍还没到时 `pending=True`（**不是失败**）
        """
        stats = ChannelStats(host_rx=self._host_rx, host_tx=self._host_tx,
                             host_rx_dropped=self._host_rx_dropped, timestamp=time.monotonic())
        device = self._device
        if device is None:
            return stats
        try:
            raw = device.get_channel_stats(self.channel)
        except Exception as ex:
            logger.debug('读通道统计失败：%s', ex)
            raw = None
        if raw is None:
            stats.pending = True
        else:
            stats.ok = True
            for name in ('tx_admitted', 'tx_completed', 'tx_dropped', 'tx_failed',
                         'rx_received', 'rx_dropped', 'sw_tx_fifo_level', 'hw_tx_fifo_level',
                         'enabled', 'drop_flags', 'rx_dropped_queue', 'rx_dropped_msgc',
                         'echo_dropped_queue', 'echo_dropped_msgc', 'echo_sent',
                         'tx_dropped_no_credit', 'tx_dropped_queue_full',
                         'tx_dropped_submit_fail', 'tx_dropped_ch_disabled'):
                setattr(stats, name, int(getattr(raw, name, 0) or 0))
        try:
            bus = device.get_bus_state(self.channel)
            stats.bus_state = int(bus.bus_state)
            stats.tec = int(bus.tec)
            stats.rec = int(bus.rec)
            stats.credits = int(getattr(bus, 'credits', 0) or 0)
            stats.in_flight = int(getattr(bus, 'in_flight', 0) or 0)
        except Exception as ex:
            logger.debug('读总线状态失败：%s', ex)
        return stats

    # -------------------------------------------------------------- 控制能力
    def apply_bitrate(self, nominal: int, data: Optional[int] = None) -> ChannelConfig:
        """
        @brief 运行期改通道位定时（掉电/拔插不保留；要保留另调 `save_params()`）。

        @param nominal 仲裁段位速率（bps）
        @param data 数据段位速率（bps）；给了就是 CAN FD + BRS
        @return 新的通道配置快照
        @raise SessionError 参数非法或下发失败

        @note 会先关通道再使能（SDK 要求），整个过程约 1.2 s 以上，别在 GUI 线程里直接调。
        """
        nominal = int(nominal)
        data = int(data) if data is not None else None
        if not (dmcan_iface.NOMINAL_RANGE[0] <= nominal <= dmcan_iface.NOMINAL_RANGE[1]):
            raise SessionError('仲裁段位速率越界：%d（%d ~ %d bps）'
                               % (nominal, dmcan_iface.NOMINAL_RANGE[0],
                                  dmcan_iface.NOMINAL_RANGE[1]))
        if data is not None and not (dmcan_iface.DATA_RANGE[0] <= data <= dmcan_iface.DATA_RANGE[1]):
            raise SessionError('数据段位速率越界：%d（%d ~ %d bps）'
                               % (data, dmcan_iface.DATA_RANGE[0], dmcan_iface.DATA_RANGE[1]))

        info = self._dmcan.dmcan_channel_can_info()
        try:
            current = self._device.get_channel_baudrate(self.channel)
            info.can_sp = current.can_sp
            info.canfd_sp = current.canfd_sp
            info.non_iso = bool(current.non_iso)
            info.retrans = bool(current.retrans)
        except Exception as ex:
            logger.debug('读当前位定时失败（按默认参数下发）：%s', ex)
        info.canfd = data is not None
        info.can_baudrate = nominal
        info.canfd_baudrate = int(data or nominal)
        self._write_bitrate(info)
        self._refresh_config()
        self._spec = IfaceSpec(target=self._spec.target, channel=self._spec.channel,
                               nominal=nominal, data=data)
        return self.config

    def set_mode(self, mode) -> None:
        """
        @brief 切通道模式（正常 / 内部回环 / 外部回环 / 只听）。

        @param mode `dmcan_channel_mode` 取值
        """
        device, channel = self._device, self.channel
        try:
            device.enable_channel(channel, False)
        except Exception as ex:
            logger.warning('切模式前关闭通道失败（继续）：%s', ex)
        device.set_channel_mode(channel, mode)
        time.sleep(MODE_SETTLE)
        self._enable_channel()
        self._refresh_config()
        logger.info('通道 CH%d 模式已切到 %s', channel, MODE_NAMES.get(int(mode), mode))

    def clear_bus_off(self) -> None:
        """@brief 清通道 bus-off 状态。"""
        self._device.clear_bus_off(self.channel)

    def save_params(self) -> None:
        """@brief 把当前运行期参数写进设备闪存（受理即返回，后台写入）。"""
        self._device.save_param()

    def read_filters(self):
        """@brief 回读硬件过滤表（读不到返回 None）。"""
        try:
            return self._device.get_channel_filter(self.channel)
        except Exception as ex:
            logger.debug('回读过滤表失败：%s', ex)
            return None

    # ------------------------------------------------------------------ 自检
    def self_test(self, count: int = 1000, pacing: float = 0.0,
                  timeout: float = 5.0, keep_mode: bool = False) -> SelfTestResult:
        """
        @brief 外部回环自检（**不需要接线**）：切到 `LOOPBACK_EXTERNAL` 后自发自收。

        @param count 发送帧数
        @param pacing 每帧之间的间隔（秒），0 表示全速发
        @param timeout 等最后一帧回收的最长时间（秒）
        @param keep_mode 测完是否保留回环模式（默认恢复原模式）
        @return `SelfTestResult`
        @raise SessionError 设备不可用或切模式失败

        @note 外部回环的帧**真的会上总线**（只是不依赖对端 ACK），所以总线上若有其它节点，
              测试期间会看到这批测试帧；本会话会把自检帧从正常收帧路径里摘掉，不影响 DroneCAN 解码。
        """
        device, channel = self._device, self.channel
        if device is None:
            raise SessionError('会话未打开，不能自检')

        details = None
        previous_mode = 0
        try:
            details = device.get_channel_baudrate_details(channel)
            # 模式单独记成标量：不依赖"读回来的细节对象是一份快照"（不同绑定/不同实现下不保证）
            previous_mode = int(getattr(details, 'mode', 0) or 0)
        except Exception as ex:
            logger.warning('读位定时细节失败（自检后可能无法恢复原模式）：%s', ex)

        result = SelfTestResult(sent=count, channel_config=self.config.describe() if self.config else '')
        sent_at: Dict[int, float] = {}
        received: Dict[int, float] = {}
        duplicated = [0]

        def sink(frame) -> None:
            seq = (int(frame.can_id) & SELFTEST_ID_MASK) - 1
            now = time.monotonic()
            if seq in received:
                duplicated[0] += 1
            received.setdefault(seq, now)

        # 自检期间把自检帧从正常收帧路径摘走，避免污染 DroneCAN 解码
        if self.config is None or self.config.canfd:
            logger.info('自检使用经典 CAN 帧')

        self._selftest_sink = sink
        try:
            self.set_mode(2)  # LOOPBACK_EXTERNAL
            started = time.monotonic()
            for seq in range(count):
                can_id = SELFTEST_ID_BASE | ((seq + 1) & SELFTEST_ID_MASK)
                payload = struct.pack('<IB3x', seq, SELFTEST_MAGIC)
                sent_at[seq] = time.monotonic()
                self.send(can_id, payload, canfd=False, ext=True, brs=False)
                if pacing:
                    time.sleep(pacing)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and len(received) < count:
                time.sleep(0.02)
            elapsed = time.monotonic() - started
        finally:
            self._selftest_sink = None
            if not keep_mode and details is not None:
                try:
                    details.mode = previous_mode
                    self._restore_details(details)
                except Exception as ex:
                    logger.error('恢复原通道模式/位定时失败：%s（请手动检查通道模式）', ex)

        result.received = len(received)
        result.missing = max(0, count - len(received))
        result.duplicated = duplicated[0]
        result.elapsed = elapsed
        latencies = sorted((received[seq] - sent_at.get(seq, received[seq])) * 1000.0
                           for seq in received)
        if latencies:
            result.latency_min_ms = latencies[0]
            result.latency_p50_ms = latencies[len(latencies) // 2]
            result.latency_avg_ms = sum(latencies) / len(latencies)
            result.latency_max_ms = latencies[-1]
        result.detail = ('外部回环（LOOPBACK_EXTERNAL）：帧真的上了总线但不依赖对端 ACK，'
                         '测完已恢复原模式。\n'
                         '注意：不控速连发时，尾部延迟反映的是"总线时间"——'
                         '例如 2000 帧 8 字节经典 CAN 在 1 Mbps 上光发完就要 ~0.22 s，'
                         '所以平均/最大延迟会明显大于 p50；判链路质量看丢帧数与 p50。')
        return result
        return result

    def _restore_details(self, details) -> None:
        """@brief 把之前读到的完整位定时配置写回（用于自检后恢复模式）。"""
        device, channel = self._device, self.channel
        try:
            device.enable_channel(channel, False)
        except Exception as ex:
            logger.warning('恢复配置前关闭通道失败（继续）：%s', ex)
        device.set_channel_baudrate_details(channel, details)
        time.sleep(MODE_SETTLE)
        self._enable_channel()
        self._refresh_config()

    # ------------------------------------------------------------------ 关闭
    def close(self, disable_channel: bool = True) -> None:
        """
        @brief 关闭会话（可重复调用）。

        @param disable_channel 是否顺手关掉通道（默认关；这样设备回到"没在跑"的状态，
                               下次打开会重新使能）
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._opened = False
            if self._device is not None and disable_channel:
                try:
                    self._device.enable_channel(self.channel, False)
                except Exception as ex:
                    logger.debug('关闭会话时关通道失败（忽略）：%s', ex)
            self._teardown()

    def _teardown(self) -> None:
        """@brief 释放订阅与设备句柄（幂等）。"""
        self._release_device()
        if self._subscription is not None:
            try:
                self._subscription.cancel()
            except Exception as ex:
                logger.debug('注销订阅失败（忽略）：%s', ex)
            self._subscription = None
        if self._device is not None:
            try:
                self._device.close()
            except Exception as ex:
                logger.debug('关闭设备失败（忽略）：%s', ex)
            self._device = None
        # 计数只减一次：open() 失败后再 close() 也不能把别的活跃会话的计数减掉
        if self._counting_active:
            self._counting_active = False
            dmcan_iface.set_session_active(False)

    def __enter__(self) -> 'DmCanSession':
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()


def _open_hint(device, ex: BaseException) -> str:
    """@brief 打开设备失败时给一段能直接照做的提示。"""
    name = '#%s' % getattr(device, 'index', '?')
    return ('打开 dmcan 设备 %s 失败：%s。常见原因：'
            '(1) Windows 下设备被别的进程占用（串口助手、另一个 GUI 实例、上次没退干净的进程）；'
            '(2) Linux 下当前用户没有 USB 设备节点权限（加 udev 规则或临时用 root）；'
            '(3) 设备被拔掉或正在重启。' % (name, ex))
