# 修改记录：把 DM Device SDK（dmsdk）接入 DroneCAN GUI Tool

> **English abstract** — How this fork adds a native-USB backend (`dmcan:`) for DM (达妙)
> USB2CANFD / USB2FDCANDUAL adapters on top of upstream [DroneCAN/gui_tool](https://github.com/DroneCAN/gui_tool):
> the layering, the optional-dependency strategy, the exact upstream touch points, the timing rules
> imposed by the SDK, and the pitfalls we hit. Useful if you want to integrate a different CAN adapter.

本文记录"怎么接的、为什么这么接"，供二次开发参考。代码在 [`dronecan_gui_tool/driver/`](../dronecan_gui_tool/driver)。

---

## 1. 目标与硬约束

| 目标 | 落地方式 |
| --- | --- |
| 原生 USB 直连达妙适配器 | 新增后端 `dmcan:`（`driver/dmcan_*.py`） |
| **不装 SDK 时与上游一字不差** | dmsdk 是可选依赖，只在函数内 import（见第 3 节） |
| 不动上游协议栈 | pydronecan 的 Node / transfer / DSDL 原样使用，只实现 `AbstractDriver` |
| 对上游文件改动尽量少 | 最终只动两处：`main.py`（2 个分发点）、`setup_window.py`（1 处接口列表） |
| GUI 不卡、不因上位机慢而丢帧 | 收帧走 SDK 通知线程 → 大容量队列 → Qt 主线程 drain（见第 4 节） |
| 丢帧能定位到"哪一段" | 设备侧 / 主机侧分开统计，再细分到原因（见第 6 节） |

## 2. 分层

```
        ┌────────────────────────── GUI ──────────────────────────┐
        │ 主窗口 / Bus Monitor / 适配器控制面板 / 交互式控制台      │
        └──────────────┬────────────────────────┬─────────────────┘
                       │                        │
              pydronecan Node             直接调用（面板 / CLI）
                       │                        │
                DmCanDriver               DmCanSession
             （AbstractDriver）      （设备会话：配置 / 统计 / 自检，
                       │              完全不依赖 pydronecan）
                       └───────────┬────────────┘
                                   │ dmsdk.py —— 全工程唯一 import dmcan 的地方
                                   ▼
                            dmcan_sdk（纯 ctypes 封装）
                                   ▼
                      libdmcan.dll / libdmcan.so + libusb
```

* **`DmCanDriver`** 只做三件事：厂商帧 ↔ `dronecan.driver.common.CANFrame` 转换、收帧入队、发送；
* **`DmCanSession`** 是"怎么配设备、怎么读统计"的唯一实现，GUI 面板与 CLI 共用；
  它不依赖 pydronecan，所以 CLI 在没装协议栈的机器上也能独立跑；
* **`dmsdk.py`** 负责环境探测（有没有 Python 包、有没有原生库）、加载、SDK 日志级别、
  Windows 依赖预加载。

## 3. 让 dmsdk 成为"可选依赖"

三条规则：

1. **只有 `driver/dmsdk.py` 出现 `import dmcan`，而且写在函数里** —— 打包器的静态分析看不到它，
   运行时没装也只是抛 `SdkUnavailable`，不会污染 import 链；
2. 上游接口列表的追加（`setup_window.py`）用 try/except 包住，失败只记一条 warning；
3. `make_node()` 只改写 `dmcan:` 前缀的接口，**其余接口原样透传**给上游。

回归测试：[`tests/test_optional_dependency.py`](../tests/test_optional_dependency.py)
（模拟"没装 dmcan_sdk"，断言接口列表、`make_node`、GUI 启动一切照常）。

## 4. 收帧路径（最关键的一段）

SDK 的收帧回调跑在**它自己的通知线程**上，规则与后果：

| 规则 | 违反的后果 |
| --- | --- |
| 回调里不许阻塞（sleep / 等锁 / 发请求） | 设备接收队列积压 → 设备侧 `rx_dropped · 队列满` 开始涨 |
| 回调里不许 `close()`、不许改位定时 | 与 SDK 内部线程互锁 → 直接死锁 |
| 回调要尽量短 | 它是唯一的"取数据"窗口，慢一点就丢 |

所以回调**只做**：拷出帧 → `queue.Queue.put_nowait()`（队列 131072 帧；满了记一次
`host_rx_dropped` 并丢弃）→ 立刻返回。DSDL 解析、画表、写日志全部放到 Qt 主线程的 10 ms spin 里。

发送侧：SDK 的 `send_can()` 是"**已受理**"语义，不代表已经发上总线。经典 CAN / CAN FD 的长度
与格式校验放在驱动层先做——留到设备侧才报错的话，错误信息既难懂又难定位。

## 5. 设备会话里必须遵守的时序

| 动作 | 时序要求 | 原因 |
| --- | --- | --- |
| 设位速率 | **先 disable_channel**，设完再 enable；并且只在"与当前值不同"时才写 | 改位定时会触发设备重载（实测 0.086~1.0 s），无脑写会造成周期性抖动 |
| 切模式（正常 / 回环） | 写完等 ~1.2 s（`MODE_SETTLE`）再恢复收帧 | 重载期间的帧本来就会丢 |
| 提交配置之后 | **读回校验**，不能只看"写成功" | 设备可能因参数非法而拒绝，静默用旧配置最难查 |
| `open()` | 一台设备只允许一个会话 | USB 会话是**整台设备**的，双路两条通道共用它 |
| `close()` | 只在主线程或明确的位置调用 | 在回调线程里 close 会死锁 |

## 6. 统计与丢帧判读

设备侧统计是 **1 Hz 的累计快照**，直接用会把"累计值"当成"当前值"，判读必须：

* 用**相邻两次快照的差值**算速率（`ChannelStats.diff()`）；
* 检测**计数清零**（设备重启、通道重载）→ 不能算成负增长；
* 看细分计数器，别只看总数：

  | 计数器 | 含义 | 归属 |
  | --- | --- | --- |
  | `rx_dropped · 队列满` | 主机没及时取走 | **上位机**的问题 |
  | `rx_dropped · 设备内部资源` | 设备处理不过来 | 设备 / 总线 |
  | `tx_dropped · 无发送额度 / 发送队列满 / 提交 MCAN 失败` | 发不出去 | 总线 / 设备 |
  | `host_rx_dropped` | 本进程队列溢出 | 上位机（GUI 卡死时才会出现） |

## 7. 与上游的接口点（code map）

| 文件 | 改动 | 为什么 |
| --- | --- | --- |
| `main.py` | `make_extended_node()` 分发；`_try_spawn_can_adapter_control_panel()` 选面板 | 上游这两处是硬编码的，只能在此分流 |
| `setup_window.py` | `_add_dmcan_ifaces(out)` 追加接口项 | 让对话框里出现 `dmcan:` 选项 |
| `widgets/__init__.py` | `get_icon()` 跟随主题前景色；`map_7bit_to_color()` 保色相做适配 | 深色主题下 qtawesome 默认黑图标看不见 |
| `widgets/bus_monitor/window.py` | 色块与文字色成对生成 | 上游把颜色当**单元格背景**用，深色下变成"浅底浅字" |
| 本分支新增 | `driver/dmsdk.py`、`driver/dmcan_iface.py`、`driver/dmcan_session.py`、`driver/dmcan_driver.py`、`dmcan_cli.py`、`widgets/dmcan_control_panel/`、`widgets/theme.py`、`tools/fetch_dmcan_lib.py`、`tools/build_exe.py` | 功能与工具链 |

## 8. 踩过的坑

1. **`OSError [WinError 127] 找不到指定的程序`**：`CDLL(path, winmode=0)` 的依赖搜索顺序是
   "exe 目录 / System32 / 当前目录 / PATH"，**不看库自己的目录**。装了 ST-Link / QEMU / MSYS2 /
   OpenOCD 的机器上 PATH 里常有别的 `libstdc++-6.dll`、`libusb-1.0.dll`，于是"文件找到了但缺导出符号"。
   处理：加载前按**绝对路径**预载 `dlls/` 里的 4 个依赖（[`dmsdk.preload_dependencies()`](../dronecan_gui_tool/driver/dmsdk.py)）。
2. **回调里 close() 会死锁**：SDK 通知线程上的任何阻塞都会拖住整条收帧链。
3. **位速率没生效**：必须在 enable 之前设，而且要读回校验。
4. **把累计值当瞬时值**：统计是 1 Hz 累计快照，必须做差值 + 清零检测。
5. **窗口模式没有控制台**：打包成 exe 后"双击没反应"，所以启动异常要自己写文件
   （[`tools/entry_gui.py`](../tools/entry_gui.py) 写 `dronecan-gui-tool-crash.log`）。
6. **`--filtered` 刻意不实现**：USB 通路的带宽不是瓶颈，而 DroneCAN 需要收全帧
   （动态节点 ID 分配、param/file 服务、匿名帧都依赖它）。

## 9. 怎么验证

| 层次 | 手段 |
| --- | --- |
| 单元 | [`tests/fake_dmcan.py`](../tests/fake_dmcan.py)：按公开 API 实现的仿真 SDK，可注入"打开失败 / 设备拔出 / 序列号读空"等故障 |
| 集成 | [`tests/test_pydronecan_end2end.py`](../tests/test_pydronecan_end2end.py)：真 pydronecan 跑单帧 / 多帧 / 服务 / 通道隔离 |
| 可选依赖 | [`tests/test_optional_dependency.py`](../tests/test_optional_dependency.py) |
| 真机 | `dmcan-dronecan-tool monitor`（实时总线 + 丢帧）、[`tests/gui_stress.py`](../tests/gui_stress.py)（GUI 压测） |
| GUI | [`tests/gui_smoke.py`](../tests/gui_smoke.py)：离屏冒烟 + 截图 + 面板内容 dump |
| Linux / WSL | [`tools/linux_smoke.sh`](../tools/linux_smoke.sh)：doctor / list / 真机 / GUI / 单元测试一条龙 |

## 10. 想接别的 CAN 适配器？照着这 8 步走

1. **接口串**：定一个前缀（`dmcan:`），把它需要的信息（设备、通道、位速率）表达完整、可校验；
2. **枚举**：写 `list_ifaces()`，返回"可直接粘进 GUI 的接口串 → 人类可读描述"；
3. **会话层**：把"打开 / 配置 / 读统计"写成**不依赖 pydronecan** 的一层（CLI 与 GUI 都要用）；
4. **驱动**：实现 `AbstractDriver` 的 `receive() / send_frame()`，把厂商帧转成 `CANFrame`
   （注意 `extended` 标志、mono/real 两套时间戳）；
5. **可选依赖**：单独一个模块放 import，写在函数里，别把上游拖下水；
6. **分发**：在 `make_node()` 与 `setup_window` 各加一处分支，不改别的；
7. **CLI + 面板**：`doctor / list / info / monitor` 是排查效率的来源，尽早做；
8. **测试**：先有仿真 SDK，再写真机脚本——真机问题复现成本高，测试要能离线跑。
