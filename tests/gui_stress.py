# -*- coding: utf-8 -*-
"""
压力测试：**GUI 开着的时候**会不会丢帧。

做法（全程走真实硬件与真实协议栈，不是仿真）：

1. 按 gui_tool 的方式建 dmcan 节点并起主窗口（可选再开 Bus Monitor / DM 面板）；
2. 把通道切到"外部回环"——我们自己发的帧真的上总线、又回到自己（不依赖对端 ACK）；
3. 用**真实的 DroneCAN 广播**（`node.broadcast`）按给定速率灌帧：编码 → 驱动 → USB →
   设备 → 总线 → 设备 → USB → 驱动收帧回调 → 队列 → GUI 的 10 ms spin → 解码 → 回调；
4. 统计丢帧：设备侧 `rx_dropped`（队列满 = 主机读得不够快）、主机侧队列丢弃、
   以及驱动收帧队列的**最大积压深度**（积压说明 GUI 快跟不上了，只是还没丢）。

> 默认用 CH1（另一路）。CH0 上如果有正在跑的总线，**不要**拿它做回环测试：
> 外部回环会把测试帧真的发到那条总线上。

用法::

    python tests/gui_stress.py --iface dmcan:0.1 --rates 1000,3000,5000 --seconds 5
    python tests/gui_stress.py --rates 3000 --seconds 8 --monitor      # 同时开 Bus Monitor
    python tests/gui_stress.py --rates 3000 --seconds 8 --panel        # 同时开 DM 面板
"""

import argparse
import os
import sys
import threading
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')      # 要在建 QApplication 之前

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def main() -> int:
    """@brief 跑完所有速率档位并打印结论。"""
    parser = argparse.ArgumentParser(description='dmcan 后端 + GUI 的灌帧压力测试')
    parser.add_argument('--iface', default='dmcan:0.1@1000000',
                        help='接口串（默认 CH1；别拿接了真实总线的通道做回环）')
    parser.add_argument('--rates', default='1000,3000,5000,8000', help='待测速率（帧/秒，逗号分隔）')
    parser.add_argument('--seconds', type=float, default=5.0, help='每档持续时间')
    parser.add_argument('--node-id', type=int, default=110, help='本机节点 ID（要能被总线接受）')
    parser.add_argument('--drain', type=float, default=20.0,
                        help='每档结束后等积压排空的上限（秒）；排完就提前进下一档')
    parser.add_argument('--monitor', action='store_true', help='同时打开 Bus Monitor 窗口')
    parser.add_argument('--panel', action='store_true', help='同时打开 dmcan 适配器面板')
    args = parser.parse_args()

    import dronecan
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    # gui_tool 的 main.py 在 **import 时**就解析 sys.argv；不清理会把本脚本的参数喂给它
    sys.argv = ['dronecan_gui_tool', '--interface', args.iface]

    import dronecan_gui_tool.main as gui
    from dronecan_gui_tool.driver import make_node as make_extended_node

    app = QApplication(['gui_stress'])
    app.setStyle('Fusion')

    node_info = dronecan.uavcan.protocol.GetNodeInfo.Response()
    node_info.name = gui.NODE_NAME
    node_info.software_version.major = gui.__version__[0]
    node_info.software_version.minor = gui.__version__[1]

    iface_kwargs = dict(baudrate=115200, bitrate=1000000, bus_number=2, filtered=False,
                        mavlink_source_system=250, mavlink_target_system=0,
                        mavlink_signing_key='')

    print('[stress] 连接 %s …' % args.iface)
    node = make_extended_node(
        args.iface, node_info=node_info,
        mode=dronecan.uavcan.protocol.NodeStatus().MODE_OPERATIONAL, node_id=args.node_id,
        **iface_kwargs)
    node.spin(0.1)
    driver = node.can_driver
    print('[stress] 驱动 %s，通道 CH%d，节点 ID %d'
          % (type(driver).__name__, driver.session.channel, node.node_id))

    window = gui.MainWindow(node, args.iface, iface_kwargs)
    window.resize(1400, 900)
    window.show()

    monitor_manager = None
    stats_probe = {'hook_calls': 0}
    if args.monitor:
        print('[stress] 打开 Bus Monitor（上游窗口，独立进程）…')
        monitor_manager = window._bus_monitor_manager
        original_hook = monitor_manager._frame_hook

        def counting_hook(direction, frame):
            stats_probe['hook_calls'] += 1
            return original_hook(direction, frame)

        monitor_manager._frame_hook = counting_hook      # 必须在 spawn 之前换，注册的是这个
        monitor_manager.spawn_monitor()

    panel = None
    if args.panel:
        from dronecan_gui_tool.widgets.dmcan_control_panel import spawn_window
        panel = spawn_window(window, node, args.iface)
        print('[stress] 打开 dmcan 适配器面板')

    # 把通道切到外部回环：自己发的帧会回到自己（不用接线、不依赖对端 ACK）
    print('[stress] 切到外部回环模式…')
    driver.session.set_mode(2)
    node.spin(0.2)

    message = dronecan.uavcan.protocol.NodeStatus()
    message.health = dronecan.uavcan.protocol.NodeStatus().HEALTH_OK
    received = [0]

    def on_message(_event):
        received[0] += 1

    handle = node.add_handler(dronecan.uavcan.protocol.NodeStatus, on_message)

    rates = [float(x) for x in args.rates.split(',') if x.strip()]
    results = []
    state = {'index': -1, 'sent': 0, 'max_queue': 0, 'started': 0.0, 'before': None,
             'phase_sent': 0}

    # 灌帧**不能**用 Qt 定时器：Windows 上 QTimer 的实际精度约 15 ms，1 ms 的定时器
    # 只会以 ~65 Hz 触发，速率上限被卡在几百帧/s。这里用独立线程按目标速率精确发送，
    # GUI 线程照常跑自己的 10 ms spin —— 这才是"GUI 开着的时候扛不扛得住"的真实场景。
    flood_stop = threading.Event()
    phase_timer = QTimer()
    phase_timer.setSingleShot(False)

    from dronecan.driver.common import CANFrame
    from dronecan.transport import Transfer

    FLOOD_SOURCE_NODE_ID = 126

    #: 预生成的帧池：DRONE 的传输 ID 只有 5 位，正好一圈 32 条互不重复
    pool = []
    for index in range(32):
        payload = dronecan.uavcan.protocol.NodeStatus()
        payload.uptime_sec = index
        payload.health = dronecan.uavcan.protocol.NodeStatus().HEALTH_OK
        transfer = Transfer(payload=payload, source_node_id=FLOOD_SOURCE_NODE_ID,
                            transfer_id=index, transfer_priority=16,
                            service_not_message=False, canfd=False)
        pool.append([CANFrame(frame.message_id, frame.bytes, True)
                     for frame in transfer.to_frames()])

    def flood_worker(rate: float) -> None:
        """
        @brief 按目标速率发真实 DroneCAN 帧，直到 `flood_stop` 被置位。

        @note 帧在进循环之前就编好了：否则限制速率的是**Python 的 DSDL 编码开销**
              （实测每帧几百微秒），而不是 USB/CAN 链路，压不到 GUI。
        """
        # 令牌桶：按"到此刻应该已经发出多少条"补齐。**不能**用"睡 1/rate 秒"——
        # Windows 上 sleep 粒度是 0.5~15 ms，那样会被睡眠精度卡在几百~几千帧/s，
        # 测出来的就不是链路上限而是测试脚本的上限（实测裸链路能到 15 万次调用/秒）。
        rate = max(1.0, rate)
        started = time.monotonic()
        sent = 0
        burst_limit = 2000
        while not flood_stop.is_set():
            due = int(rate * (time.monotonic() - started)) - sent
            if due <= 0:
                time.sleep(0.0001)
                continue
            due = min(due, burst_limit)
            try:
                for _ in range(due):
                    for frame in pool[sent & 0x1F]:
                        driver.send_frame(frame)
                    sent += 1
            except Exception as ex:
                print('[stress] 发送异常：%s' % ex)
                return
            state['sent'] += due
            state['phase_sent'] += due

    def queue_depth() -> int:
        try:
            return driver._rx_queue.qsize()          # 只为观测，别在热路径里做
        except Exception:
            return 0

    def start_phase() -> None:
        state['index'] += 1
        if state['index'] >= len(rates):
            finish()
            return
        state['rate'] = rates[state['index']]
        # 每 1 ms 发一小撮，凑出目标速率（由 Qt 定时器驱动，和 GUI 抢同一个事件循环）
        state['burst'] = max(1, int(round(state['rate'] / 1000.0)))
        state['phase_sent'] = 0
        state['phase_received'] = received[0]
        state['max_queue'] = 0
        state['started'] = time.monotonic()
        state['before'] = driver.statistics()
        print('[stress] ---- %d 帧/s 档位：后台线程按 %.3f ms 间隔发送，持续 %.0f s ----'
              % (state['rate'], 1000.0 / state['rate'], args.seconds))
        flood_stop.clear()
        threading.Thread(target=flood_worker, args=(state['rate'],),
                         name='stress-flood', daemon=True).start()
        phase_timer.start(int(args.seconds * 1000))

    def queue_probe() -> None:
        """@brief 每隔一会儿记一次收帧队列积压（积压说明 GUI 开始跟不上了）。"""
        depth = queue_depth()
        if depth > state['max_queue']:
            state['max_queue'] = depth
        QTimer.singleShot(20, queue_probe)

    def on_phase_end() -> None:
        flood_stop.set()
        phase_timer.stop()
        # 排空再统计：上一档灌进去的帧可能还在设备/主机队列里按总线速率往外吐
        # （1 Mbps 经典 CAN 一帧 ~111 bit，上限约 9k 帧/s；积压几万条要好几秒才排完）。
        # 判据是"未解码的条数不再下降"，而不是固定等 2 s。
        deadline = time.monotonic() + args.drain
        previous_pending = None
        stable_since = None
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
            pending = state['phase_sent'] - (received[0] - state['phase_received'])
            if pending <= 0:
                break
            if previous_pending is not None and pending >= previous_pending:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since > 1.5:
                    break                      # 1.5 s 没再下降，认定排完了
            else:
                stable_since = None
            previous_pending = pending
        node.spin(0.2)
        after = driver.statistics()
        before = state['before']
        diff = after.diff(before)
        elapsed = max(1e-6, time.monotonic() - state['started'])
        decoded = received[0] - state['phase_received']
        results.append(dict(
            rate=state['rate'], sent=state['phase_sent'], decoded=decoded,
            device_rx=diff.rx_received, device_rx_dropped=diff.rx_dropped,
            device_queue_full=diff.rx_dropped_queue, device_msgc=diff.rx_dropped_msgc,
            device_tx_dropped=diff.tx_dropped, host_dropped=diff.host_rx_dropped,
            host_rx=diff.host_rx, max_queue=state['max_queue'], elapsed=elapsed))
        print('[stress] 实发 %d 条（%.0f 条/s）· 解码回 %d 条 · 设备收帧 %d 丢 %d（队列满 %d）'
              ' · 设备发帧丢 %d · 主机丢 %d · 队列最大积压 %d'
              % (state['phase_sent'], state['phase_sent'] / elapsed, decoded,
                 diff.rx_received, diff.rx_dropped, diff.rx_dropped_queue,
                 diff.tx_dropped, diff.host_rx_dropped, state['max_queue']))
        start_phase()

    def finish() -> None:
        print('')
        print('===== 汇总 =====')
        print('%-10s %10s %10s %10s %11s %10s %9s %10s'
              % ('目标 帧/s', '实发', '解码回', '设备收帧', '设备丢(队列满)', '主机丢', '队列峰值', '丢失率'))
        total_sent = total_decoded = 0
        for item in results:
            lost = max(0, item['sent'] - item['decoded'])
            total_sent += item['sent']
            total_decoded += item['decoded']
            print('%-10.0f %10d %10d %10d %11d %10d %9d %9.3f%%'
                  % (item['rate'], item['sent'], item['decoded'], item['device_rx'],
                     item['device_queue_full'], item['host_dropped'], item['max_queue'],
                     100.0 * lost / max(1, item['sent'])))
        share = 100.0 * (total_sent - total_decoded) / max(1, total_sent)
        print('')
        if total_sent and total_decoded >= total_sent:
            print('结论：全程 %d 条一个没丢。' % total_sent)
        else:
            print('结论：%d 条里解码回 %d 条，丢失 %.3f%%；'
                  '看"设备丢(队列满)"判断是不是主机侧跟不上。' % (total_sent, total_decoded, share))
        if monitor_manager is not None:
            try:
                process, channel = monitor_manager._inferiors[0]
                print('Bus Monitor：投递给它的帧 %d 条 · 进程存活 %s · IPC 待处理 %d 条'
                      '（上游窗口每 100 ms 只取 1 帧，投得快时这个数会一路涨）'
                      % (stats_probe['hook_calls'], process.is_alive(), channel._q.qsize()))
            except Exception as ex:
                print('Bus Monitor 读数失败：%s' % ex)
        handle.remove()
        driver.session.set_mode(0)          # 恢复普通模式
        node.close()
        app.quit()

    phase_timer.timeout.connect(on_phase_end)
    QTimer.singleShot(50, queue_probe)
    QTimer.singleShot(300, start_phase)
    app.exec()
    return 0


if __name__ == '__main__':
    sys.exit(main())
