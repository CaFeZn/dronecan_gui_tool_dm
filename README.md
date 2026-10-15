# DroneCAN GUI Tool — dmcan（达妙 USB2CANFD / USB2FDCANDUAL）分支

在 [DroneCAN/gui_tool](https://github.com/DroneCAN/gui_tool) 上做了一个 **dmcan 后端**：
用达妙 DM-DeviceSDK（`libdmcan`）走**原生 USB 批量端点**跑 DroneCAN，
替代原来的 SLCAN（USB-UART + ASCII 行）通路。

**dmsdk 是可选依赖**：没装 `dmcan_sdk` / 没有 `libdmcan` 时，这个 fork 与上游行为完全一致
（slcan / socketcan / pcan / mcast / mavcan 一个都不少），只是接口列表里不会出现 `dmcan:` 项。

---

## 1. 为什么不用 SLCAN

SLCAN 把每一帧 CAN 编成一行 ASCII（`t1238AABBCCDD\r`）经 USB-UART 收发，链路上有三个结构性瓶颈：

| 环节 | SLCAN | dmcan（本分支） |
| --- | --- | --- |
| 物理通路 | USB-UART 桥，速率受串口波特率限制 | USB 批量端点，主机侧无波特率上限 |
| 帧编码 | 每帧约 2 倍膨胀的 ASCII + 行分隔符 | 二进制，84B 结构体（含时间戳/通道/标志） |
| 主机处理 | 逐字节解析文本、按 `\r` 切行、带 CLI 混合态 | SDK 通知线程回调，帧已切片好，直接入队 |
| 丢帧可见性 | 只有"没收到"，无从定位 | 设备侧 `rx_dropped`/`tx_dropped` **细分到队列满/内部资源/无额度**…，主机侧另有队列计数 |

SDK 自带的性能基线（`dm-device-sdk/docs/perf/PERFORMANCE_BASELINE.md`）：
单模块外部回环 **25.8 k 帧/s 丢 0**；两台对拉单向 10 万帧 **26.4 k 帧/s 丢 0**；
主机侧 CPU **0.78 µs/帧**。SLCAN 在同样硬件上受串口与文本解析限制，量级差一到两个数量级，
而且总线上负载一高就开始丢——这正是"slcan 版本丢包率太高"的根因。

**本分支实测**（本机 + USB2CANFD_Dual，固件 `V2.0.0 [b5c2574]`）：

```
$ dmcan-dronecan-tool monitor dmcan:0.0@1000000 --seconds 6      # 真实总线，CH0
设备侧收帧      ：377（61.9 帧/s）
设备侧收帧丢弃  ：0（其中接收队列满 0 / 设备内部 0）
主机侧收帧丢弃  ：0
结论            ：本段观测内零丢帧。

$ dmcan-dronecan-tool selftest dmcan:0.1@1000000 --count 2000    # 外部回环，不用接线
回环自检：发 2000 收 2000 丢 0（0.000%）

$ dmcan-dronecan-tool selftest dmcan:0.1@1000000 --count 1000 --rate 500
回环自检：发 1000 收 1000 丢 0（0.000%）；往返延迟 min/p50/平均/最大 = 1.22/1.61/1.62/2.18 ms
```

---

## 2. 快速开始

### 2.1 装 Python 依赖

```bash
cd dronecan-gui-tool-dm
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt      # Linux/macOS: .venv/bin/python
```

### 2.2 装 dmcan_sdk（Python 封装）

```bash
.venv/Scripts/python -m pip install dmcan_sdk
# 离线（推荐，用 SDK 仓库里的 wheel）：
.venv/Scripts/python -m pip install <dm-device-sdk>/Python/pack/dmcan_sdk-2.1.1-py3-none-any.whl
```

> `dmcan_sdk` 是**纯 ctypes** 包，不带原生库。原生库要单独准备，见下一步。

### 2.3 放原生库 `libdmcan`

```bash
.venv/Scripts/python tools/fetch_dmcan_lib.py
# 或手动指定 SDK 位置：
.venv/Scripts/python tools/fetch_dmcan_lib.py --sdk D:/path/to/dm-device-sdk
```

脚本把 `libdmcan.dll` 连同 `libusb-1.0.dll` / `libstdc++-6.dll` / `libgcc_s_seh-1.dll` /
`libwinpthread-1.dll` 一起拷到本工程 `dlls/`，并打印 SHA256（SDK 约定：四种语言目录里的
`libdmcan` 必须同版）。也可以用环境变量 `DMCAN_LIBRARY_PATH` 直接指到库文件。

### 2.4 自检

```bash
.venv/Scripts/python -m dronecan_gui_tool.dmcan_cli doctor
```

```
平台：Windows AMD64 / Python 3.14.3
dmsdk 可用（libdmcan 2.1.0：...\dlls\libdmcan.dll）
  dmcan_sdk 包版本：2.1.1
设备数：1
  #0  USB2CANFD_Dual (双路)  SN=08BD7F93F24654BCCAB457ACF4C13C29  VID:PID=34B7:6632
结论：环境就绪，可以用 list / monitor / selftest。
```

### 2.5 起 GUI / CLI

```bash
pip install -e .              # 可选：装成开发模式，得到两个命令行入口
dmcan-dronecan-tool doctor    # 本 fork 新增的 CLI
dronecan_gui_tool             # 上游的 GUI

# 不安装也能跑（在工程根目录下）：
.venv/Scripts/python -m dronecan_gui_tool.dmcan_cli doctor
.venv/Scripts/python -m dronecan_gui_tool.main --interface dmcan:0.0

# Windows 快捷方式：
run_gui.bat                   # 打开 setup 对话框选接口
run_gui.bat dmcan:0.0         # 直接连达妙双路模块 CH0
```

setup 对话框的接口列表里会多出两项（其余是上游的 COM 口 / mcast / mavcan）：

```
dmcan:USB2CANFD_Dual (双路) CH0  [SN 08BD7F93F246…]   -> dmcan:s:08BD7F93F24654BCCAB457ACF4C13C29.0
dmcan:USB2CANFD_Dual (双路) CH1  [SN 08BD7F93F246…]   -> dmcan:s:08BD7F93F24654BCCAB457ACF4C13C29.1
```

---

## 3. 接口串语法

```
dmcan:<target>[.<channel>][@<nominal>][+<data>]
```

| 片段 | 含义 | 缺省 |
| --- | --- | --- |
| `<target>` | 设备表下标 `0`/`1`…，或 `s:<序列号>` | 必填。枚举出来的条目一律优先用序列号（换 USB 口后下标会变） |
| `.<channel>` | 通道号（双路 0/1、四路 0~3） | setup 窗口的 Bus Number − 1，再退到 0 |
| `@<nominal>` | 仲裁段位速率 (bps) | setup 窗口的 "CAN bus bit rate"（默认 1 Mbps） |
| `+<data>` | 数据段位速率 (bps)，**给了就是 CAN FD + BRS** | 不给 = 经典 CAN |

例子：

```bash
dmcan:0                     # 设备 0 的 CH0，位速率取界面设置
dmcan:0.1@500000            # CH1，500 kbps
dmcan:s:E4B3230B4E4B.0      # 按序列号选设备（推荐）
dmcan:0.0@1000000+5000000   # CAN FD：仲裁 1 Mbps / 数据 5 Mbps（>1 Mbps 自动开 TDC）
```

解析是**严格**的：写错直接报错，绝不"猜一个差不多的设备"——猜错的代价是往别的总线上发帧。
允许在接口串后面跟描述文字（按空白截断），所以从下拉框里复制粘贴也能用。

---

## 4. 无界面 CLI

```bash
python -m dronecan_gui_tool.dmcan_cli <命令>     # 或安装后直接用 dmcan-dronecan-tool
```

| 命令 | 干什么 |
| --- | --- |
| `doctor` | 依赖与环境自检（Python / dmcan_sdk / libdmcan / 设备枚举） |
| `list` | 列出设备与可用接口串（可直接粘到 GUI） |
| `info <iface>` | 设备身份、固件版本+**构建标识**、位定时细节（含 TDC）、过滤表、统计细分 |
| `monitor <iface> [--seconds N]` | 盯通道看丢帧：设备侧 1 Hz **增量** + 主机侧丢弃 + 总线状态 |
| `selftest <iface> [--count N] [--rate HZ]` | **外部回环自检**：不接线、单模块自发自收，给丢帧率与往返延迟 |

`monitor` 输出（每秒一行）：

```
[   2.2s] 收     28.1 帧/s  发     0.5 帧/s | 设备丢帧 收 0（队列 0 / 内部 0） 发 0 | 主机丢帧 0 | 总线 active TEC=0 REC=0 | 队列 sw=0 hw=0
```

结束时给结论，并指出**是哪一段丢的**：

* `接收队列满` → 主机读得不够快（回调里做了重活 / 上位机卡顿）
* `设备内部资源` → 设备侧瓶颈
* `主机侧收帧丢弃` → 本进程队列溢出（GUI 卡死时才会出现）
* `发帧丢弃/无额度/提交失败` → 发送侧额度或 MCAN 队列问题

> `selftest` 会把通道临时切到"外部回环"：帧**真的会驱动总线**（只是不依赖对端 ACK），
> 所以别在总线上有其它节点时随便跑；测完会自动恢复原模式。想验证"再接一根线"的收发，
> 也可以两台模块对拉：A 端发（`selftest --keep-mode`），B 端跑 `monitor`。

---

## 5. dmcan 适配器控制面板

GUI 里菜单 **Tools → CAN Adapter Control Panel**（后端是 dmcan 时自动开这一个，而不是上游的
SLCAN CLI 面板）：

* **设备身份**：型号 / 序列号 / VID:PID / 固件版本 + 构建标识 / 当前通道配置；
* **位定时**：仲裁段、CAN FD 数据段 → "应用位定时（运行期）"；"存入设备闪存"才落盘；
* **实时统计（1 Hz）**：设备侧累计 + 每秒增量，`rx_dropped`/`tx_dropped` 拆到具体原因，
  另有主机侧收帧丢弃与发送队列水位；
* **使能/关闭通道、清 bus-off**；
* **外部回环自检**：不接线自发自收 1000 帧，直接弹结果（丢帧数 + 往返延迟）；
  测试期间驱动会暂停收帧，自检帧不会污染 DroneCAN 解码。

---

## 6. 代码结构

```
dronecan-gui-tool-dm/
├── dronecan_gui_tool/
│   ├── driver/                        ★ 本 fork 新增：dmcan 后端
│   │   ├── dmsdk.py                   可选依赖的唯一入口（探测/加载/原生库查找/SDK 日志级别）
│   │   ├── dmcan_iface.py             接口串语法、设备枚举
│   │   ├── dmcan_session.py           会话：打开/位定时/使能/统计/自检（不依赖 pydronecan）
│   │   ├── dmcan_driver.py            pydronecan 驱动粘合（AbstractDriver）
│   │   └── __init__.py                make_node 分发 + 接口列表
│   ├── dmcan_cli.py                   ★ 无界面 CLI
│   ├── widgets/dmcan_control_panel/   ★ 适配器控制面板
│   ├── main.py                        ✎ 只改了两处：make_node 分发、面板选择
│   └── setup_window.py                ✎ 只加了一处：接口列表追加 dmcan 项
├── tools/fetch_dmcan_lib.py           ★ 从 SDK 取 libdmcan 及其依赖
├── tests/                             ★ 单元/集成/仿真测试（47 个，无需硬件）
│   ├── fake_dmcan.py                  仿真 SDK（按公开 API 实现，可注入故障）
│   ├── test_iface_spec.py             语法、枚举
│   ├── test_dmcan_backend.py          会话/驱动行为、错误处理、自检、统计增量
│   ├── test_pydronecan_end2end.py     真 pydronecan 端到端（单帧/多帧/服务/通道隔离）
│   ├── test_optional_dependency.py    没装 dmsdk 时一切照常
│   └── gui_smoke.py                   离屏 GUI 冒烟（截图 + 面板内容 dump）
└── dlls/                              运行期放 libdmcan（不入库）
```

**分层要点**：`DmCanSession` 完全不依赖 pydronecan，GUI 驱动、CLI、自检共用同一份
"设备怎么配、统计怎么读"的实现；`DmCanDriver` 只做帧格式转换与队列。

**"dmsdk 可选"是怎么做到的**：全工程只有 `driver/dmsdk.py` 会 `import dmcan`，而且是**函数内**
导入；`setup_window` 里追加接口列表的调用被 try/except 包住，失败只记一条 warning。
所以没装依赖时：接口列表不变、`make_node` 对非 dmcan 接口原样透传（`tests/test_optional_dependency.py` 覆盖）。

---

## 7. 跑测试

```bash
.venv/Scripts/python -m unittest discover -s tests -t .      # 47 个用例，不需要硬件
.venv/Scripts/python tests/gui_smoke.py --mock               # 离屏 GUI 冒烟（仿真 SDK）
.venv/Scripts/python tests/gui_smoke.py "dmcan:0.0"          # 离屏 GUI 冒烟（真机）
```

`tests/fake_dmcan.py` 按 dmcan_sdk 的**公开 API** 实现（类名/方法/返回值语义对齐），
把 USB 换成进程内虚拟总线，还能注入"设备已拔出/打开失败/序列号读空"等故障——
所以驱动代码是在"以为自己在跟真 SDK 说话"的前提下被测的。

---

## 8. 排错

| 现象 | 原因与处理 |
| --- | --- |
| 接口列表里没有 `dmcan:` 项 | 跑 `doctor`：多半是没装 `dmcan_sdk` 或没找到 `libdmcan`（提示里会写清楚） |
| `打开设备失败 ... 被别的进程占用` | Windows 下串口助手 / 另一个 GUI 实例占着；本工具会**自动重试 3 次**，仍失败就关掉占用方 |
| `使能通道失败` | 固件过旧：通道开/关要求固件回 2 字节应答，需 V2.0.0 及以上、构建标识 ≥ 双路 `b5c2574`（`info` 里能直接看到） |
| `按序列号没找到设备` | 序列号偶尔会读空（上一个进程刚释放 USB 句柄时）。工具会自动刷新一次重试；仍不行就改用下标（`dmcan:0.1`） |
| 同一台设备想开第二条通道 | `open()` 是**整台设备**的 USB 会话，双路的两条通道共用它；一个进程只允许一个会话，工具会明确报错 |
| Linux 下打不开 | 当前用户对 `/dev/bus/usb/*/*` 没写权限：加 udev 规则（`SUBSYSTEM=="usb", ATTR{idVendor}=="34b7", MODE="0666"`）或用 root |
| 收不到帧 | 先跑 `selftest`（外部回环）确认"自己能收自己"；再查两端波特率/采样点、终端电阻、经典 vs FD、通道是否使能 |
| 想降总线负载 | 上游的 `--filtered`（低带宽过滤）对 dmcan 后端**刻意不实现**：USB 通路的带宽不需要过滤，而 DroneCAN 需要收到全部帧（动态节点 ID 分配、param/file 服务、匿名帧都靠它） |
| GUI 里看不到丢帧 | **Tools → CAN Adapter Control Panel**：设备侧与主机侧的丢帧计数都在那里 |

---

## 9. 与上游的关系

* 上游改动只落在两处（都在文件里标了注释）：`main.py` 的 `make_node` 分发 + 面板选择、
  `setup_window.py` 的接口列表追加。其余上游文件原封不动，方便 `git merge upstream/master`。
* 上游 README 原文保留在 [`docs/upstream-README.md`](docs/upstream-README.md)。
* 许可：上游 MIT（`LICENSE` 原样保留）；本 fork 新增部分同样 MIT，来源说明见 [`NOTICE.md`](NOTICE.md)。
