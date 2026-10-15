#
# DroneCAN GUI Tool — dmcan 后端（达妙 USB2CANFD / USB2FDCANDUAL 原生 USB）
#
# 本文件不属于上游 DroneCAN/gui_tool，为本 fork 新增；与上游同样以 MIT 发布（见 LICENSE）。
#
"""
pydronecan 的 dmcan 驱动：把 `DmCanSession`（原生 USB）接进 DroneCAN 的帧接口。

pydronecan 只要一个"能收能发"的驱动对象（`AbstractDriver` 那套约定）：
`receive(timeout)` / `send_frame(frame)` / `close()`，外加可选的 IO hook。
这里就把 USB 侧的收帧回调转成 `CANFrame` 塞进队列，把要发的帧直接交给 SDK。

**为什么不走 SLCAN**：SLCAN 把 CAN 帧编成 ASCII 行塞在 USB-UART 上，上行带宽和
主机轮询节奏都是瓶颈，总线一忙就丢；而 dmsdk 走的是 USB 批量端点，SDK 侧实测
单向 26.4 k 帧/s、零丢帧（见 dm-device-sdk/docs/perf/PERFORMANCE_BASELINE.md）。
"""

from __future__ import annotations

import logging
import queue
import time
from typing import Any, Dict, Optional

from . import dmcan_iface, dmsdk
from .dmcan_iface import IfaceSpec
from .dmcan_session import ChannelStats, DmCanSession, SelfTestResult, SessionError

try:  # pydronecan 在 1.0.x 里一直把基类放在 driver.common
    from dronecan.driver.common import AbstractDriver, CANFrame, DriverError, TxQueueFullError
except Exception:  # pragma: no cover - 兜底：自己搭一个等价基类
    from dronecan.driver import CANFrame, DriverError  # type: ignore

    class TxQueueFullError(DriverError):  # type: ignore
        """发送队列满（兜底定义）。"""

    class AbstractDriver(object):  # type: ignore
        """最小等价基类：只保留 pydronecan 用到的 IO hook 约定。"""

        FRAME_DIRECTION_INCOMING = 'rx'
        FRAME_DIRECTION_OUTGOING = 'tx'

        class HookRemover(object):
            def __init__(self, remover):
                self.remove = remover

        def __init__(self):
            self._io_hooks = []

        def add_io_hook(self, hook):
            def proxy(*args):
                hook(*args)
            self._io_hooks.append(proxy)
            return self.HookRemover(lambda: self._io_hooks.remove(proxy))

        def _call_io_hooks(self, direction, frame):
            for hook in self._io_hooks:
                try:
                    hook(direction, frame)
                except Exception as ex:
                    logging.getLogger(__name__).error('CAN IO hook 异常：%r', ex, exc_info=True)

        def _tx_hook(self, frame):
            self._call_io_hooks(self.FRAME_DIRECTION_OUTGOING, frame)

        def _rx_hook(self, frame):
            self._call_io_hooks(self.FRAME_DIRECTION_INCOMING, frame)

        def set_filter_list(self, ids):
            pass

        def get_filter_list(self):
            return None

        def set_bus(self, busnum):
            pass

        def get_bus(self):
            return None

        def set_signing_passphrase(self, passphrase):
            pass

        def send(self, message_id, message, extended=False, canfd=False):
            self.send_frame(CANFrame(message_id, message, extended, canfd=canfd))

logger = logging.getLogger(__name__)

__all__ = ['DmCanDriver']

#: 收帧队列上限（帧）。这层只做"SDK 回调 -> 队列"，DroneCAN 主循环每 10 ms 来取一次；
#: 队列满说明主机侧（GUI）卡住了，此时丢帧并计数，不阻塞 SDK 的通知线程。
RX_QUEUE_SIZE = 131072

#: 设备"还在不在"的检查间隔（秒）。掉线后尽快把错误报给上层，而不是静默收不到。
PRESENCE_CHECK_INTERVAL = 1.0


class DmCanDriver(AbstractDriver):
    """达妙 dmcan（libdmcan / dmcan_sdk）原生 USB 后端。"""

    def __init__(self, iface, bitrate: Optional[int] = None, bus_number: Optional[int] = None,
                 **_extras):
        """
        @param iface `dmcan:` 接口串（见 `dmcan_iface`）
        @param bitrate 界面上设置的仲裁段位速率（接口串里写了 `@` 时以接口串为准）
        @param bus_number 界面上的 Bus Number（1 起）：接口串没写通道时用它当"通道号 + 1"
        @param _extras pydronecan 会把一堆无关参数一起传进来（baudrate/filtered/...），忽略
        """
        super(DmCanDriver, self).__init__()

        default_channel = max(0, int(bus_number) - 1) if bus_number else 0
        self.spec: IfaceSpec = dmcan_iface.parse(iface, default_bitrate=bitrate,
                                                 default_channel=default_channel)
        self._rx_queue: "queue.Queue[Any]" = queue.Queue(RX_QUEUE_SIZE)
        self._rx_dropped = 0
        self._rx_frames = 0
        self._tx_frames = 0
        self._last_presence_check = 0.0
        self._rx_paused = False

        logger.info('打开 dmcan 后端：%s（%s）', iface, self.spec.to_string())
        try:
            self.session = DmCanSession(self.spec, callback=self._on_event)
            self.session.open()
        except (SessionError, dmsdk.SdkUnavailable) as ex:
            raise DriverError(str(ex)) from ex

        #: CAN FD 时是否带 BRS（数据段比仲裁段快才带）
        self._brs = bool(self.session.canfd and
                         (self.session.data_rate or 0) > (self.session.nominal or 0))

    # ------------------------------------------------------------- 基本信息
    @property
    def iface_name(self) -> str:
        """@brief pydronecan 里的接口名。"""
        return self.spec.to_string()

    def describe(self) -> Dict[str, Any]:
        """@brief 设备/固件/通道位定时快照（给 GUI 面板与 CLI 用）。"""
        session = self.session
        out: Dict[str, Any] = dict(session.device_info)
        out['iface'] = self.spec.to_string()
        out['channel'] = session.channel
        out['config'] = session.config.describe() if session.config else ''
        out['bitrate_written'] = session.bitrate_written
        out['counters'] = dict(session.host_counters(), queue_dropped=self._rx_dropped,
                               queued=self._rx_queue.qsize())
        return out

    def statistics(self) -> ChannelStats:
        """@brief 读一次设备侧 + 主机侧统计。"""
        stats = self.session.statistics()
        stats.host_rx_dropped += self._rx_dropped
        return stats

    def self_test(self, **kwargs) -> SelfTestResult:
        """@brief 外部回环自检（见 `DmCanSession.self_test`）。"""
        return self.session.self_test(**kwargs)

    def save_params(self) -> None:
        """@brief 把当前通道参数写进设备闪存。"""
        self.session.save_params()

    def clear_bus_off(self) -> None:
        """@brief 清通道 bus-off。"""
        self.session.clear_bus_off()

    # --------------------------------------------------------------- 收发路径
    def pause_rx(self, paused: bool) -> None:
        """
        @brief 暂停/恢复"收帧入队"（回环自检期间用）。

        @param paused True = 收到的帧直接丢弃、不进 DroneCAN 解码队列
        @note 只影响本驱动的入队路径，设备侧照常工作；自检结束后记得恢复。
        """
        self._rx_paused = bool(paused)
        logger.info('dmcan 收帧%s', '已暂停（回环自检中）' if paused else '已恢复')

    def _on_event(self, session: DmCanSession, event) -> None:
        """@brief SDK 通知线程：把设备事件转成 pydronecan 的 CANFrame 塞队列。"""
        frame = event.frame
        if frame is None:
            return
        if self._rx_paused:
            return
        try:
            length = max(0, min(int(frame.dlen), 64))
            data = bytes(bytearray(frame.payload[:length]))
            can_frame = CANFrame(int(frame.can_id), data, bool(frame.ext),
                                 ts_monotonic=time.monotonic(), ts_real=time.time(),
                                 canfd=bool(frame.canfd))
        except Exception:
            logger.error('转换收帧失败，已丢弃该帧', exc_info=True)
            return

        try:
            self._rx_queue.put_nowait(can_frame)
        except queue.Full:
            self._rx_dropped += 1
            if self._rx_dropped % 1000 == 1:
                logger.error('收帧队列已满（%d 帧），主机侧处理不过来，累计丢弃 %d 帧',
                             RX_QUEUE_SIZE, self._rx_dropped)
        self._rx_frames += 1
        self._rx_hook(can_frame)

    def receive(self, timeout: Optional[float] = None):
        """
        @brief 取一帧（pydronecan 的回调入口）。

        @param timeout 0 = 立刻返回；None = 一直阻塞；其它 = 最多等这么久
        @return `CANFrame` 或 None
        """
        try:
            if timeout == 0:
                item = self._rx_queue.get_nowait()
            else:
                item = self._rx_queue.get(True, timeout)
        except queue.Empty:
            self._check_device_present()
            return None

        if isinstance(item, Exception):
            raise item
        return item

    def send_frame(self, frame) -> None:
        """
        @brief 发一帧（pydronecan 的回调入口）。

        @param frame `CANFrame`
        @raise DriverError 设备掉了 / 参数非法 / 经典 CAN 上发了 CAN FD
        """
        canfd = bool(getattr(frame, 'canfd', False))
        if canfd and not self.session.canfd:
            raise DriverError('当前通道是经典 CAN，不能发 CAN FD 帧；'
                              '请在接口串里加 "+<数据段位速率>"（例如 dmcan:0.0@1000000+5000000）')
        data = bytes(frame.data or b'')
        if not canfd and len(data) > 8:
            raise DriverError('经典 CAN 单帧最多 8 字节，收到 %d 字节；请把通道配成 CAN FD' % len(data))
        try:
            self.session.send(int(frame.id), data, canfd=canfd,
                              ext=bool(frame.extended), brs=canfd and self._brs)
        except SessionError as ex:
            raise DriverError(str(ex)) from ex
        self._tx_frames += 1
        self._tx_hook(frame)

    # --------------------------------------------------------------- 兼容接口
    def set_filter_list(self, ids) -> None:
        """
        @brief 接受但不实现硬件过滤。

        @note 达妙模块支持硬件过滤表，但 DroneCAN 需要收到全部帧（动态节点 ID 分配、
              param/file 服务、匿名帧都靠它）；`--filtered` 这种"低带宽"模式是给
              串口适配器用的，走 USB 批量端点时没有必要，贸然过滤只会让节点"消失"。
              这里明确记一条日志，不做任何设备操作。
        """
        if ids:
            logger.info('dmcan 后端忽略硬件过滤请求（%d 条规则）：DroneCAN 需要收到全部帧；'
                        'USB 后端的带宽不需要过滤', len(ids))

    def get_filter_list(self):
        """@brief 不支持硬件过滤，恒为 None。"""
        return None

    def get_bus(self):
        """@brief dmcan 没有 mavcan 那种"远程总线号"，恒为 None（界面据此隐藏相关控件）。"""
        return None

    def set_signing_passphrase(self, passphrase) -> None:
        """@brief MAVLink 签名口令与 dmcan 后端无关，这里跟随基类的空实现。"""
        super(DmCanDriver, self).set_signing_passphrase(passphrase)

    def _check_device_present(self) -> None:
        """@brief 收帧空闲时顺手确认设备还在，掉线了就把错误抛给上层。"""
        now = time.monotonic()
        if now - self._last_presence_check < PRESENCE_CHECK_INTERVAL:
            return
        self._last_presence_check = now
        device = self.session.device
        if device is None:
            raise DriverError('dmcan 设备已关闭')
        if not dmcan_iface.device_present(device):
            raise DriverError('dmcan 设备已拔出（%s）；请重新插好并重连。' % self.session.description)

    def close(self) -> None:
        """@brief 关闭会话（可重复调用）。"""
        try:
            self.session.close()
        except Exception as ex:
            logger.warning('关闭 dmcan 会话时出错：%s', ex)
