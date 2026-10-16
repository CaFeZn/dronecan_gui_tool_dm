**简体中文** · [English](README.md)

# DroneCAN GUI Tool · 达妙 DM 设备原生 USB 分支

> 用达妙 **USB2CANFD / USB2FDCANDUAL** 直连 DroneCAN 总线的 [DroneCAN GUI Tool](https://github.com/DroneCAN/gui_tool)。
> 走**原生 USB**（达妙 DM Device SDK），不再经过 SLCAN 的"USB 串口 + ASCII 行"通路——
> 满总线速率下实测**零丢帧**，而且能告诉你**丢在哪一段**。

![总线监视器](docs/images/bus-monitor-dark.png)

## 这个分支多做了什么

| | 上游 gui_tool | 本分支 |
| --- | --- | --- |
| 连接达妙适配器 | 只能当 USB 串口走 SLCAN | **原生 USB 直连**（新增 `dmcan:` 接口） |
| 高负载总线 | 串口波特率 + 文本解析是瓶颈，负载一高就丢 | USB 批量端点，实测 20 000 帧/s 灌入零丢帧 |
| 丢帧排查 | 只能看到"没收到" | 设备侧 / 主机侧分开计，细分到"接收队列满 / 设备内部资源 / 无发送额度…" |
| 适配器面板 | SLCAN 的 CLI 面板 | 达妙专用面板：身份、位定时、1 Hz 统计、通道使能 |
| 深色主题 | 表格文字与图标在深色下看不清 | 已修（浅色主题与上游**逐位一致**，不做任何改动） |

**不装达妙 SDK 也照常能用**：这时接口列表里只是少一项 `dmcan:`，
SLCAN / socketCAN / PCAN / mcast / mavcan 等后端的行为与上游**完全一致**。

## 支持的硬件与环境

* 达妙 **USB2CANFD / USB2FDCANDUAL**（VID:PID `34B7:6632`），双路/四路模块的每条通道各算一个接口；
* 经典 CAN 与 **CAN FD**（`+<数据段速率>` 语法，>1 Mbps 自动开 TDC）；
* Windows 10/11（含免安装 exe）；Linux，含 WSL2（实测 Ubuntu 22.04）。

---

## 快速开始

### 方式 A：免安装 exe（Windows，不用装 Python）

到 **[Releases](https://github.com/CaFeZn/dronecan_gui_tool_dm/releases/latest)** 下载
`dronecan-gui-tool-dm-v0.1.0-win64.zip`（约 105 MB），解压后双击即可：

```
dronecan-gui-tool-dm\
├── dronecan-gui-tool-dm.exe   ← 双击这个（图形界面）
├── dmcan-dronecan-tool.exe      命令行工具（doctor / list / monitor / selftest）
├── dlls\                        原生库，**必须和 exe 放在一起**
├── _internal\                   运行时（Python / PyQt6 / 库）
└── README-使用说明.txt
```

整个文件夹拷走就能用；第一次运行如果弹 SmartScreen，点"仍要运行"。
想自己重新打包（或打新版），见 [打包成 exe](#打包成-exe可选)。

### 方式 B：从源码跑

```bash
git clone https://github.com/CaFeZn/dronecan_gui_tool_dm
cd dronecan_gui_tool_dm

python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt    # Linux/macOS 用 .venv/bin/python
.venv/Scripts/python -m pip install dmcan_sdk              # 达妙 SDK 的 Python 封装
.venv/Scripts/python tools/fetch_dmcan_lib.py              # 把原生库取到 dlls/
.venv/Scripts/python -m pip install -e .                   # 可选：装出两个命令行入口
```

[`tools/fetch_dmcan_lib.py`](tools/fetch_dmcan_lib.py) 会从达妙 SDK 里自动找 `libdmcan`
（Windows `libdmcan.dll`、Linux `libdmcan.so`）连同它的依赖一起拷进工程 `dlls/`，并打印 SHA256。
SDK 不在默认位置就加 `--sdk /path/to/dm-device-sdk`，或用环境变量 `DMCAN_LIBRARY_PATH` 直接指库文件。

### 自检一下

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

---

## 第一次连上总线

1. 插上适配器，**总线侧接好**（CANH/CANL、两端各一个 120 Ω 终端电阻、共地——和任何 CAN 设备一样）；
2. 启动界面：

   ```bash
   .venv/Scripts/python -m dronecan_gui_tool.main     # 或装好后直接敲 dronecan_gui_tool
   ```

   Windows 上也可以直接双击 `run_gui.bat`；
3. 对话框里选 **`dmcan:` 开头**的那一项，位速率填总线实际速率（DroneCAN 默认 **1 Mbps**）；
4. 连上后主窗口会列出总线上的节点。想确认链路质量，见下面的「丢帧怎么看」。

不想每次点对话框：

```bash
dronecan_gui_tool --interface dmcan:0.0@1000000
```

---

## 接口串怎么写

```
dmcan:<设备>[.<通道>][@<仲裁段速率>][+<数据段速率>]
```

| 片段 | 含义 |
| --- | --- |
| `<设备>` | 枚举下标 `0`/`1`…，或 `s:<序列号>`（**推荐**——换 USB 口后下标会变） |
| `.<通道>` | 双路模块 0/1、四路 0~3；不写就用界面里的 Bus Number − 1 |
| `@<速率>` | 仲裁段位速率（bps）；不写就用界面里的 "CAN bus bit rate" |
| `+<速率>` | 数据段位速率；**写了就是 CAN FD + BRS**，不写就是经典 CAN |

```bash
dmcan:0                     # 设备 0 的 CH0
dmcan:0.1@500000            # CH1，500 kbps
dmcan:s:E4B3230B4E4B.0      # 按序列号选设备（推荐）
dmcan:0.0@1000000+5000000   # CAN FD：仲裁 1 Mbps / 数据 5 Mbps
```

写法是**严格校验**的，写错直接报错，不会"猜一个差不多的设备"——猜错的代价是往别的总线上发帧。
接口串后面可以跟说明文字（按空白截断），所以从下拉框里复制粘贴过来也能直接用。

---

## 命令行工具（排错最快的入口）

```bash
dmcan-dronecan-tool <命令>          # pip install -e . 之后
# 或者：python -m dronecan_gui_tool.dmcan_cli <命令>
```

| 命令 | 用途 |
| --- | --- |
| `doctor` | 环境自检：Python / dmcan_sdk / 原生库 / 依赖解析路径 / 设备枚举 |
| `list` | 列出设备与可用接口串（可直接粘进 GUI） |
| `info <接口>` | 设备身份、固件版本与**构建标识**、位定时细节（含 TDC）、统计细分 |
| `monitor <接口> [--seconds N]` | **盯丢帧**：每秒一行，设备侧增量 + 主机侧丢弃 + 总线状态 |
| `selftest <接口> [--count N] [--rate HZ]` | **不用接线**的外部回环自检：给丢帧率与往返延迟 |

```bash
$ dmcan-dronecan-tool monitor dmcan:0.0@1000000 --seconds 6
    时间    收帧/s    发帧/s  丢帧(收)  队列/内部  丢帧(发)   主机丢 总线      TEC  REC
    2.2s      61.9       0.0         0        0/0         0        0 active      0    0
    3.2s      62.0       0.0         0        0/0         0        0 active      0    0
===== 汇总（6.1 s）=====
设备侧收帧      ：377（61.9 帧/s）
设备侧收帧丢弃  ：0（其中接收队列满 0 / 设备内部 0）
主机侧收帧丢弃  ：0
结论            ：本段观测内零丢帧。

$ dmcan-dronecan-tool selftest dmcan:0.1@1000000 --count 2000
回环自检：发 2000 收 2000 丢 0（0.000%）

$ dmcan-dronecan-tool selftest dmcan:0.1@1000000 --count 1000 --rate 500
回环自检：发 1000 收 1000 丢 0（0.000%）；往返延迟 min/p50/平均/最大 = 1.22/1.61/1.62/2.18 ms
```

> `selftest` 会把通道临时切到"外部回环"：帧**真的会上总线**（只是不依赖对端 ACK），
> 所以别在总线上有其它节点时随便跑；测完会自动恢复原模式。
> 想验证"真接一根线"的收发，可以两台模块对拉：A 端 `selftest --keep-mode`，B 端 `monitor`。

---

## 界面里的三个窗口

* **主窗口** —— 节点列表、服务调用、参数读写、固件升级……上游功能原样保留；
* **Tools → Bus Monitor** —— 逐帧看总线，`Data Hex` / `Data ASCII` 按传输 ID 上色，右侧画负载曲线；
* **Tools → CAN Adapter Control Panel** —— **本分支新增的达妙适配器面板**：

  * 设备身份：型号 / 序列号 / VID:PID / 固件版本 + 构建标识 / 当前通道配置；
  * 位定时：仲裁段、CAN FD 数据段；「应用」= 立刻生效，「存入闪存」= 掉电保留；
  * **1 Hz 实时统计**：设备侧累计值 + 每秒增量，收/发丢帧细分到原因，另有主机侧丢弃与发送队列水位；
  * 使能 / 关闭通道、清 bus-off。

![适配器面板](docs/images/adapter-panel.png)

### 丢帧怎么看

只可能在四个地方出现"丢"，对应四种原因：

| 计数器 | 出现说明 |
| --- | --- |
| 设备侧 `收帧丢弃 · 队列满` | 主机没及时取走 —— 上位机侧的问题（GUI 卡顿、回调里做了重活） |
| 设备侧 `收帧丢弃 · 内部资源` | 设备内部处理不过来 —— 总线负载已经超过它了 |
| 设备侧 `发帧丢弃` | 发不出去：总线被塞满 / 没有发送额度 / MCAN 队列满 |
| 主机侧 `收帧丢弃` | 本进程队列溢出，只有 GUI 长时间卡死时才会出现 |

---

## 外观：跟随系统 / 强制深色 / 强制浅色

菜单 **View → Appearance**：`Follow System`（默认）/ `Dark` / `Light`。选择会被记住，
下次启动以及 Bus Monitor、Plotter 两个独立窗口都跟着变。切换**立即生效**（图标会重新染色）。

命令行也可以临时指定（做快捷方式时好用，只对本次运行生效、不写回偏好）：

```bash
dronecan-gui-tool-dm.exe --interface dmcan:0.0@1000000 --appearance light
```

---

## 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 接口列表里没有 `dmcan:` 项 | 先跑 `doctor`：多半是没装 `dmcan_sdk` 或没找到 `libdmcan`（提示里会写清楚） |
| `libdmcan 原生库加载失败：OSError [WinError 127] 找不到指定的程序` | 依赖 DLL 被 PATH 里的同名文件顶掉了。装了 ST-Link / QEMU / MSYS2 / OpenOCD / STM32CubeCLT 的机器上，PATH 里常有别的 `libstdc++-6.dll`、`libusb-1.0.dll`。**本工具已修**（加载前按绝对路径预载 `dlls/` 里的依赖），仍失败就用 `doctor` 看"原生库依赖"一节到底解析到了哪一份 |
| `打开设备失败 … 被别的进程占用` | 串口助手或另一个 GUI 实例占着；本工具会自动重试 3 次，仍失败就关掉占用方 |
| `使能通道失败` | 固件过旧：开/关通道要求固件回 2 字节应答，需 **V2.0.0 及以上**（`info` 里能看到固件版本与构建标识） |
| 按序列号找不到设备 | 序列号偶尔读空（上一个进程刚释放 USB 句柄时），工具会自动刷新重试；仍不行就改用下标 `dmcan:0.1` |
| 想同时开双路的两条通道 | `open()` 是**整台设备**的 USB 会话，两条通道共用它；一个进程只允许一个会话，工具会明确报错 |
| Linux 下打不开设备 | 当前用户对 `/dev/bus/usb/*/*` 没有写权限：加 udev 规则（`SUBSYSTEM=="usb", ATTR{idVendor}=="34b7", MODE="0666"`）或用 root 跑 |
| 收不到任何帧 | 先跑 `selftest`（不接线自收自发）确认链路本身没问题；再查两端波特率/采样点、终端电阻、经典 CAN 还是 FD、通道是否使能 |
| 总线负载太高怎么办 | 上游的 `--filtered`（低带宽过滤）对 dmcan 后端**刻意不实现**：USB 通路的带宽不需要过滤，而 DroneCAN 需要收全帧（动态节点 ID 分配、param/file 服务、匿名帧都靠它） |
| 想确认有没有丢帧 | **Tools → CAN Adapter Control Panel**，设备侧与主机侧的计数都在那里；命令行用 `monitor` |
| 双击 exe 没反应 | 看 exe 旁边的 `dronecan-gui-tool-crash.log`（窗口模式下没有控制台，异常会落在这里） |
| PowerShell 里 `run_gui.bat` 没反应 | 要写成 `.\.\run_gui.bat dmcan:0.0`（PowerShell 不会从当前目录直接执行），也不要 `python run_gui.bat` |

---

## 实测数据

**为什么不用 SLCAN**：SLCAN 把每一帧 CAN 编成一行 ASCII（`t1238AABBCCDD\r`）走 USB-UART，
链路上有三个结构性瓶颈：

| 环节 | SLCAN | 本分支（dmcan） |
| --- | --- | --- |
| 物理通路 | USB-UART 桥，受串口波特率限制 | USB 批量端点，主机侧无波特率上限 |
| 帧编码 | 每帧约 2 倍膨胀的 ASCII + 行分隔符 | 二进制 84 B 结构体（含时间戳/通道/标志） |
| 主机处理 | 逐字节解析文本、按 `\r` 切行 | SDK 通知线程回调，帧已切片好，直接入队 |
| 丢帧可见性 | 只有"没收到"，无从定位 | 设备侧 / 主机侧分开计，且细分到具体原因 |

达妙 SDK 自带的性能基线（`docs/perf/PERFORMANCE_BASELINE.md`）：单模块外部回环
**25.8 k 帧/s 丢 0**；两台对拉单向 **10 万帧 26.4 k 帧/s 丢 0**；主机侧 CPU **0.78 µs/帧**。
SLCAN 在同样硬件上受串口与文本解析限制，差一到两个数量级，且总线负载一高就开始丢。

**GUI 开着会不会丢帧**（真机 USB2CANFD_Dual，CH1 外部回环，经典 CAN 1 Mbps）：

| 目标灌入速率 | 实发 | 解码回 | 设备侧丢帧 | 主机侧丢帧 | 收帧队列峰值 |
| --- | --- | --- | --- | --- | --- |
| 1 000 帧/s | 4 844 | 4 849 | 0 | 0 | 20 |
| 5 000 帧/s | 24 999 | 25 004 | 0 | 0 | 110 |
| 10 000 帧/s | 50 085 | 49 711 | 0（个别轮次 17） | 0 | 155 |
| 20 000 帧/s | 76 428 | 76 440 | 0 | 0 | 181 |
| 10 000 帧/s + Bus Monitor + 适配器面板 | 39 619 | 39 625 | 0 | 0 | 149 |

即使把一条 1 Mbps 经典 CAN 灌到接近饱和（物理上限约 9 000 帧/s），主机侧收帧队列峰值也不到
200 帧——驱动给的是 131 072 帧的余量。灌到 10 000 帧/s 以上时设备侧偶发丢弃，
那是**总线塞满**，判据是 `队列满` 始终为 0（说明主机从来没有读不过来）。

---

## Linux / WSL

同一份代码在 Linux 上也能跑（Windows 找 `libdmcan.dll`、Linux 找 `libdmcan.so`）。
实测环境：WSL2 / Ubuntu 22.04，用 [usbipd-win](https://github.com/dorssel/usbipd-win)
把适配器透传进 WSL：

```powershell
usbipd list                                   # 找到 34b7:6632 那一行的 BUSID
usbipd bind --busid 4-1 --force
usbipd attach --wsl --busid 4-1
```

```bash
wsl -d Ubuntu -e bash -c "lsusb | grep 34b7"   # 确认 WSL 里能看到设备
```

WSL 里跑之前，用仓库里的脚本把源码同步到 Linux 文件系统（比直接在 `/mnt/d` 上跑快很多，
也避免跨文件系统的权限/编码坑），再跑整套冒烟：

```powershell
.\tools\wsl_sync.ps1
wsl -d Ubuntu -u root -e bash -c "cd /home/dm/dgt && bash tools/linux_smoke.sh"
```

实测结果（同一台适配器，CH1 外部回环）：

| 项目 | 结果 |
| --- | --- |
| `doctor` | `dmsdk 可用（libdmcan 2.1.0：…/dlls/libdmcan.so）`，设备枚举正常 |
| 回环自检 | 发 500 收 500 **丢 0**；往返 p50 **33.4 ms**（Windows 上 1.67 ms，差在 USB/IP + WSL 虚拟化） |
| 监听 CH1 | 设备侧 / 主机侧丢帧全 0 |
| GUI | 走完整链路：`dmcan 会话就绪` → `Init complete` → 认出总线上的节点 |
| 单元测试 | 87 个全过（2 个 Windows 专属用例自动跳过） |
| 上游 **SLCAN** 后端（USB 串口） | 同样可用，10 s 收到 30 帧 |

> 测完记得 `usbipd detach --busid 4-1` **再 `usbipd unbind --busid 4-1`**：
> 只 detach 的话设备还挂在 usbipd 的驱动上，Windows 侧的 GUI 会找不到它。

---

## 打包成 exe（可选）

```bash
python -m pip install pyinstaller
python tools/build_exe.py          # 产物在 dist/dronecan-gui-tool-dm/
```

GUI 与 CLI 两个 exe **共用同一份运行时**，整个目录约 224 MB，拷走即用。
打包脚本里已经处理好的几件事（都注了原因）：

* pydronecan 的 **DSDL 数据文件**（`dronecan/dsdl_specs/**/*.uavcan`）必须带上，否则所有消息类型都注册不出来；
* `dmcan` 是"函数内 import"的可选依赖，必须显式 `hiddenimports`，否则静态分析看不到它；
* 运行期用不到的 `*.dist-info` 元数据不打包（`python-can` 在 import 时会扫 `entry_points.txt`，
  读到异常内容会直接崩，而窗口模式下表现成"双击没反应"）；
* 窗口模式没有控制台，所以启动异常会写到 exe 旁边的 `dronecan-gui-tool-crash.log`；
* `multiprocessing.freeze_support()` 必须在最前面（Bus Monitor / Plotter 会 spawn 子进程）。

---

## 给二次开发的人

> **接入方法（怎么把 dmsdk 接进来的）**：[docs/porting-dmsdk.md](docs/porting-dmsdk.md) ——
> 分层结构、可选依赖怎么落地、收帧路径与设备时序约束、上游接口点、踩过的坑、
> 以及"想接别的 CAN 适配器要照着做哪 8 步"。

```
dronecan_gui_tool/
├── driver/                      ★ 本分支新增的 dmcan 后端
│   ├── dmsdk.py                 可选依赖的唯一入口（探测 / 加载 / 原生库查找 / SDK 日志级别）
│   ├── dmcan_iface.py           接口串语法、设备枚举
│   ├── dmcan_session.py         会话：打开 / 位定时 / 使能 / 统计 / 自检（不依赖 pydronecan）
│   ├── dmcan_driver.py          pydronecan 驱动粘合（AbstractDriver）
│   └── __init__.py              make_node 分发 + 接口列表
├── dmcan_cli.py                 ★ 无界面 CLI
├── widgets/dmcan_control_panel/ ★ 适配器控制面板
├── widgets/theme.py             ★ 深浅主题适配
├── main.py                      ✎ 只改两处：make_node 分发、面板选择
└── setup_window.py              ✎ 只加一处：接口列表追加 dmcan 项
```

`DmCanSession` 完全不依赖 pydronecan，GUI 驱动 / CLI / 自检共用同一份"设备怎么配、统计怎么读"的实现；
`DmCanDriver` 只做帧格式转换与队列。**"dmsdk 可选"** 靠的是全工程只有 `driver/dmsdk.py` 会
`import dmcan`（而且是函数内导入），接口列表的追加也被 try/except 包住。

```bash
python -m unittest discover -s tests -t .     # 87 个用例，不需要硬件
python tests/gui_smoke.py --mock              # 离屏 GUI 冒烟（仿真 SDK，截图 + 面板内容 dump）
python tests/gui_stress.py --rates 10000 --seconds 6 --monitor --panel   # 真机压力测试（需要硬件）
```

`tests/fake_dmcan.py` 按 dmcan_sdk 的**公开 API** 实现（类名/方法/返回值语义对齐），
把 USB 换成进程内虚拟总线，还能注入"设备已拔出 / 打开失败 / 序列号读空"等故障——
所以驱动代码是在"以为自己在跟真 SDK 说话"的前提下被测的。

---

## 与上游的关系

* 基于 [DroneCAN/gui_tool](https://github.com/DroneCAN/gui_tool)（MIT），保留其全部功能与后端；
* 新增代码集中在 `driver/dmsdk.py`、`driver/dmcan_*.py`、`dmcan_cli.py`、
  `widgets/dmcan_control_panel/`、`widgets/theme.py`，对上游文件的改动只有
  `main.py`（两处分发）与 `setup_window.py`（一处接口列表）；
* 原生库与商标归达妙（DM）所有，从官方 [DM-Device-SDK](https://gitee.com/kit-miao/dm-device-sdk) 获取，本仓库不附带；
* 许可与致谢见 [LICENSE](LICENSE) 与 [NOTICE.md](NOTICE.md)。
