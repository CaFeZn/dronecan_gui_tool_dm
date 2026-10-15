#
# DroneCAN GUI Tool — dmcan 后端（达妙 USB2CANFD / USB2FDCANDUAL 原生 USB）
#
# 本文件不属于上游 DroneCAN/gui_tool，为本 fork 新增；与上游同样以 MIT 发布（见 LICENSE）。
#
"""
无界面 CLI：`dmcan-dronecan-tool`（也可 `python -m dronecan_gui_tool.dmcan_cli`）。

GUI 之外最常用的四件事，跑在没有 Qt 的机器上也行：

* `doctor`   依赖与环境自检（Python / dmcan_sdk / libdmcan / 权限 / 设备）
* `list`     列出设备与可用接口串（可以直接粘到 GUI 的接口框里）
* `monitor`  盯着一条通道看丢帧：设备侧 1 Hz 累计值的**每秒增量** + 主机侧丢弃 + 总线状态
* `selftest` 外部回环自检：不接线、单模块自发自收，直接给丢帧数与往返延迟
* `info`     设备身份、固件构建标识、位定时细节（含 TDC）、过滤表与统计细分

`monitor` 与 `selftest` 是回答"到底丢不丢帧、丢在哪一段"的两个工具：
前者看真实总线上的统计，后者把 USB/固件/主机这一整条链路单独拎出来测。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import List, Optional

from .driver import dmcan_iface, dmsdk
from .driver.dmcan_iface import IfaceSpec, format_spec
from .driver.dmcan_session import ChannelStats, DmCanSession, SessionError

__all__ = ['main']

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    """@brief 日志走 stderr，别污染 stdout 的结果。"""
    logging.basicConfig(stream=sys.stderr,
                        level=logging.DEBUG if verbose else logging.WARNING,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')


def _print_status() -> bool:
    """@brief 打印 dmsdk 可用性；不可用时给出照做就行的建议。"""
    status = dmsdk.probe()
    print(status.describe())
    if not status.available:
        print('  建议：%s' % status.hint)
        return False
    if status.package_version:
        print('  dmcan_sdk 包版本：%s' % status.package_version)
    return True


def _rate(value: int) -> str:
    """@brief 位速率美化。"""
    if not value:
        return '?'
    if value % 1000000 == 0:
        return '%d Mbps' % (value // 1000000)
    if value % 1000 == 0:
        return '%d kbps' % (value // 1000)
    return '%d bps' % value


# --------------------------------------------------------------------- 命令
def cmd_doctor(args) -> int:
    """@brief 依赖与环境自检。"""
    import platform
    print('平台：%s %s / Python %s' % (platform.system(), platform.machine(),
                                      platform.python_version()))
    available = _print_status()
    print('原生库依赖：')
    for line in dmsdk.dependency_report().splitlines():
        print('  %s' % line.strip())
    if available:
        try:
            entries = dmcan_iface.list_devices(refresh=True)
        except dmsdk.SdkUnavailable as ex:
            print('枚举设备失败：%s' % ex)
            return 1
        print('设备数：%d' % len(entries))
        for entry in entries:
            print('  %s' % entry.describe())
        if not entries:
            print('  提示：插好 USB2CANFD/USB2FDCANDUAL 后重跑；'
                  'Linux 下还要确认当前用户能读写 /dev/bus/usb/*/*（udev 规则或 root）。')
            return 1
    else:
        return 1
    print('结论：环境就绪，可以用 list / monitor / selftest。')
    return 0


def cmd_list(args) -> int:
    """@brief 列出设备与接口串。"""
    if not _print_status():
        return 1
    entries = dmcan_iface.list_devices(refresh=args.refresh)
    if not entries:
        print('没有找到设备。')
        return 1
    for entry in entries:
        print('%s' % entry.describe())
        for channel in range(max(1, entry.channels)):
            spec = format_spec(IfaceSpec(target=entry.reference, channel=channel))
            print('    接口串：%s' % spec)
    return 0


def cmd_info(args) -> int:
    """@brief 打印设备身份、位定时细节、过滤表与统计细分。"""
    session = DmCanSession(args.iface, default_bitrate=args.bitrate)
    try:
        session.open()
    except (SessionError, dmsdk.SdkUnavailable) as ex:
        print('打开失败：%s' % ex, file=sys.stderr)
        return 2
    try:
        info = session.device_info
        print('设备      ：%s  SN=%s  VID:PID=%04X:%04X'
              % (info.get('type_name'), info.get('serial'), info.get('vid', 0),
                 info.get('pid', 0)))
        print('固件      ：%s' % session.firmware)
        print('接口串    ：%s' % session.spec.to_string())
        print('通道配置  ：%s' % (session.config.describe() if session.config else '?'))
        if session.config and session.config.busoff_autoreset_ms is not None:
            print('bus-off 自动恢复：%d ms' % session.config.busoff_autoreset_ms)
        try:
            tdc = session.device.get_channel_tdc(session.channel)
            if getattr(tdc, 'valid', False):
                print('TDC 生效  ：offset=%d filter=%d 收发器延迟=%d'
                      % (tdc.tdco, tdc.tdcf, tdc.tdcv))
            else:
                print('TDC 生效  ：未启用/未读到（经典 CAN 或未开启 TDC）')
        except Exception as ex:
            print('TDC 生效  ：读不到（%s）' % ex)
        table = session.read_filters()
        if table is not None:
            print('硬件过滤表：mode=%s 规则 %d 条（本工具不下发过滤；'
                  'DroneCAN 需要收全帧）' % (table.mode, table.count))
        stats = session.statistics()
        if stats.pending:
            # 统计来自固件的 1 Hz 周期上报，刚打开时还没到；等两拍再判定
            print('统计      ：等待第一条周期上报（最多 2.5 s）…')
            deadline = time.monotonic() + 2.5
            while stats.pending and time.monotonic() < deadline:
                time.sleep(0.25)
                stats = session.statistics()
        if not stats.pending:
            print('  收帧 %d（丢 %d：队列满 %d / 内部 %d）'
                  % (stats.rx_received, stats.rx_dropped,
                     stats.rx_dropped_queue, stats.rx_dropped_msgc))
            print('  发帧 受理 %d 完成 %d 丢弃 %d 失败 %d'
                  % (stats.tx_admitted, stats.tx_completed, stats.tx_dropped, stats.tx_failed))
            print('  总线 %s  TEC=%d REC=%d' % (stats.bus_state_name, stats.tec, stats.rec))
    finally:
        session.close()
    return 0


def _display_width(text: str) -> int:
    """@brief 终端显示宽度（中日韩字符算 2 列）。"""
    return sum(2 if ord(char) > 0x2E80 else 1 for char in str(text))


def _pad(text: str, width: int, align: str = 'right') -> str:
    """@brief 按**显示宽度**对齐（不然中文表头会和数字行错开）。"""
    text = str(text)
    padding = ' ' * max(0, width - _display_width(text))
    return (padding + text) if align == 'right' else (text + padding)


#: monitor 表的列：标题 -> 显示宽度 -> 对齐
MONITOR_COLUMNS = (
    ('时间', 8, 'right'),
    ('收帧/s', 9, 'right'),
    ('发帧/s', 9, 'right'),
    ('丢帧(收)', 9, 'right'),
    ('队列/内部', 10, 'right'),
    ('丢帧(发)', 9, 'right'),
    ('主机丢', 8, 'right'),
    ('总线', 8, 'left'),
    ('TEC', 4, 'right'),
    ('REC', 4, 'right'),
)


def _monitor_header() -> str:
    """@brief 表头（外加一行说明，中文宽度按显示宽度对齐）。"""
    return ' '.join(_pad(title, width, align) for title, width, align in MONITOR_COLUMNS)


def _monitor_row(elapsed: float, diff, stats, marker: str = '') -> str:
    """@brief 一行统计：1 s 的增量 + 当前总量/状态。"""
    cells = (
        ('%.1fs' % elapsed, 8, 'right'),
        ('%.1f' % diff.rx_fps, 9, 'right'),
        ('%.1f' % diff.tx_fps, 9, 'right'),
        ('%d' % diff.rx_dropped, 9, 'right'),
        ('%d/%d' % (diff.rx_dropped_queue, diff.rx_dropped_msgc), 10, 'right'),
        ('%d' % diff.tx_dropped, 9, 'right'),
        ('%d' % diff.host_rx_dropped, 8, 'right'),
        (stats.bus_state_name, 8, 'left'),
        ('%d' % stats.tec, 4, 'right'),
        ('%d' % stats.rec, 4, 'right'),
    )
    return ' '.join(_pad(text, width, align) for text, width, align in cells) + marker


def cmd_monitor(args) -> int:
    """@brief 每秒打印统计增量，结束时给结论。"""
    session = DmCanSession(args.iface, default_bitrate=args.bitrate,
                           default_channel=args.channel)
    try:
        session.open()
    except (SessionError, dmsdk.SdkUnavailable) as ex:
        print('打开失败：%s' % ex, file=sys.stderr)
        return 2

    print('监听 %s' % session.description)
    print('      %s' % (session.config.describe() if session.config else ''))
    print('列含义：丢帧(收)=设备侧接收丢弃（队列/内部 是它的细分原因）· 丢帧(发)=设备侧发送丢弃 · '
          '主机丢=本进程队列丢弃；单位都是帧/秒。Ctrl-C 结束。')
    print('')
    print(_monitor_header())
    print('-' * _display_width(_monitor_header()))
    started = time.monotonic()
    previous: Optional[ChannelStats] = None
    totals = {'rx': 0, 'rx_dropped': 0, 'rx_dropped_queue': 0, 'rx_dropped_msgc': 0,
              'tx_completed': 0, 'tx_dropped': 0, 'host_dropped': 0}
    # 先取一次"基线"快照再开始统计：设备侧计数是**从设备开机起累计**的，
    # 直接拿第一拍做差分会把"打开之前的历史"算进来，看起来像一阵突发流量。
    try:
        while previous is None:
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            snapshot = session.statistics()
            if not snapshot.pending:
                previous = snapshot
            time.sleep(0.2)
        last_print = time.monotonic()
        rows = 0
    except KeyboardInterrupt:
        session.close()
        print('已中断。')
        return 130
    try:
        while True:
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            stats = session.statistics()
            now = time.monotonic()
            if previous is not None and now - last_print >= args.interval:
                diff = stats.diff(previous)
                totals['rx'] += diff.rx_received
                totals['rx_dropped'] += diff.rx_dropped
                totals['rx_dropped_queue'] += diff.rx_dropped_queue
                totals['rx_dropped_msgc'] += diff.rx_dropped_msgc
                totals['tx_completed'] += diff.tx_completed
                totals['tx_dropped'] += diff.tx_dropped
                totals['host_dropped'] += diff.host_rx_dropped
                last_print = now
                rows += 1
                if rows % 20 == 0:      # 长跑时隔一段重打表头，翻屏后还能对上列
                    print('-' * _display_width(_monitor_header()))
                dropped_now = diff.rx_dropped + diff.tx_dropped + diff.host_rx_dropped
                marker = '   ⚠ 本秒丢帧' if dropped_now else ''
                print(_monitor_row(now - started, diff, stats, marker))
            if previous is None or now - last_print >= args.interval:
                previous = stats
            time.sleep(0.2)
    except KeyboardInterrupt:
        print('\n已中断。')
    finally:
        session.close()

    elapsed = max(1e-6, time.monotonic() - started)
    print('\n===== 汇总（%.1f s）=====' % elapsed)
    print('设备侧收帧      ：%d（%.1f 帧/s）' % (totals['rx'], totals['rx'] / elapsed))
    print('设备侧发帧完成  ：%d（%.1f 帧/s）' % (totals['tx_completed'], totals['tx_completed'] / elapsed))
    print('设备侧收帧丢弃  ：%d（其中接收队列满 %d / 设备内部 %d）'
          % (totals['rx_dropped'], totals['rx_dropped_queue'], totals['rx_dropped_msgc']))
    print('设备侧发帧丢弃  ：%d' % totals['tx_dropped'])
    print('主机侧收帧丢弃  ：%d' % totals['host_dropped'])
    print('结论            ：%s' % _verdict(totals))
    return 0


def _verdict(totals: dict) -> str:
    """@brief 按丢帧细分给出人读结论（哪一段该背锅）。"""
    if not any(totals[key] for key in ('rx_dropped', 'tx_dropped', 'host_dropped')):
        return '本段观测内零丢帧。'
    reasons: List[str] = []
    if totals['rx_dropped_queue']:
        reasons.append('设备接收队列满 = 主机读得不够快（别在收帧回调里做重活，或降总线负载）')
    if totals['rx_dropped_msgc']:
        reasons.append('设备内部资源不足 = 设备侧瓶颈（降总线负载或换低延迟档位）')
    if totals['rx_dropped'] and not (totals['rx_dropped_queue'] or totals['rx_dropped_msgc']):
        reasons.append('有收帧丢弃但细分还没上报（等一秒再看，drop_flags 未置位时细分字段恒 0）')
    if totals['tx_dropped']:
        reasons.append('设备侧发帧被丢 = 发送额度/队列不足（降发送速率，或确认通道没被关）')
    if totals['host_dropped']:
        reasons.append('主机侧收帧队列满 = 上位机进程卡住（GUI 卡顿时会出现）')
    return '；'.join(reasons) if reasons else '有丢帧，但累计值没细分到具体原因。'


def cmd_selftest(args) -> int:
    """@brief 外部回环自检：不接线验证整条链路。"""
    session = DmCanSession(args.iface, default_bitrate=args.bitrate)
    try:
        session.open()
    except (SessionError, dmsdk.SdkUnavailable) as ex:
        print('打开失败：%s' % ex, file=sys.stderr)
        return 2
    try:
        print('自检 %s（%s）' % (session.description,
                                 session.config.describe() if session.config else ''))
        print('发送 %d 帧，间隔 %.3f ms…' % (args.count, args.rate and (1000.0 / args.rate) or 0.0))
        result = session.self_test(count=args.count,
                                   pacing=(1.0 / args.rate) if args.rate else 0.0,
                                   timeout=args.timeout, keep_mode=args.keep_mode)
        print(result.summary())
        print(result.detail)
    finally:
        session.close()
    return 1 if result.missing else 0


# ---------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    """@brief 组装命令行。"""
    parser = argparse.ArgumentParser(
        prog='dmcan-dronecan-tool',
        description='达妙 USB2CANFD / USB2FDCANDUAL 的 DroneCAN 辅助工具（原生 USB 后端）')
    parser.add_argument('-v', '--verbose', action='store_true', help='打印 SDK 调试日志')
    parser.add_argument('--lib', help='libdmcan 动态库完整路径（等价于环境变量 DMCAN_LIBRARY_PATH）')
    sub = parser.add_subparsers(dest='command')

    p_doctor = sub.add_parser('doctor', help='依赖与环境自检')
    p_doctor.set_defaults(func=cmd_doctor)

    p_list = sub.add_parser('list', help='列出设备与接口串')
    p_list.add_argument('--refresh', action='store_true', help='忽略缓存重新枚举')
    p_list.set_defaults(func=cmd_list)

    p_info = sub.add_parser('info', help='设备身份 / 位定时细节 / 统计细分')
    p_info.add_argument('iface', help='接口串，例如 dmcan:0 或 dmcan:0.1@1000000')
    p_info.add_argument('--bitrate', type=int, default=1000000, help='接口串没写 @ 时用的位速率')
    p_info.set_defaults(func=cmd_info)

    p_monitor = sub.add_parser('monitor', help='盯通道看丢帧（每秒增量）')
    p_monitor.add_argument('iface', help='接口串，例如 dmcan:0.0@1000000')
    p_monitor.add_argument('--bitrate', type=int, default=1000000, help='接口串没写 @ 时用的位速率')
    p_monitor.add_argument('--channel', type=int, default=None, help='接口串没写 .ch 时用的通道号')
    p_monitor.add_argument('--seconds', type=float, default=0.0, help='跑多少秒后自动结束（0 = 一直跑）')
    p_monitor.add_argument('--interval', type=float, default=1.0, help='打印间隔（秒）')
    p_monitor.set_defaults(func=cmd_monitor)

    p_selftest = sub.add_parser('selftest', help='外部回环自检（不用接线）')
    p_selftest.add_argument('iface', help='接口串，例如 dmcan:0.0@1000000')
    p_selftest.add_argument('--bitrate', type=int, default=1000000, help='接口串没写 @ 时用的位速率')
    p_selftest.add_argument('--count', type=int, default=1000, help='发送帧数')
    p_selftest.add_argument('--rate', type=float, default=0.0, help='发送速率（帧/秒，0 = 全速）')
    p_selftest.add_argument('--timeout', type=float, default=5.0, help='等最后一帧回来的超时（秒）')
    p_selftest.add_argument('--keep-mode', action='store_true',
                            help='测完保留回环模式（默认恢复原模式）')
    p_selftest.set_defaults(func=cmd_selftest)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """
    @brief CLI 入口。

    @param argv 参数列表（None = 取 sys.argv）
    @return 进程退出码：0 成功；1 有丢帧/环境不满足；2 打开设备失败
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    if args.lib:
        import os
        os.environ[dmsdk.ENV_LIBRARY_PATH] = args.lib
        dmsdk.reset()
    if not getattr(args, 'command', None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print('已中断。', file=sys.stderr)
        return 130
    except (SessionError, dmsdk.SdkUnavailable) as ex:
        print('错误：%s' % ex, file=sys.stderr)
        return 2
    finally:
        dmsdk.close_context()


if __name__ == '__main__':
    sys.exit(main())
