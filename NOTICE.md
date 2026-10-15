# NOTICE / 来源说明

本仓库是 [DroneCAN/gui_tool](https://github.com/DroneCAN/gui_tool) 的 fork，
在上游基础上**新增**了达妙 dmcan（USB2CANFD / USB2FDCANDUAL 原生 USB）CAN 后端。

* 上游代码：MIT，见 `LICENSE`（原样保留，版权归 DroneCAN / UAVCAN Development Team）。
* 本 fork **新增**的文件（同样以 MIT 发布）：
  * `dronecan_gui_tool/driver/` —— dmcan 后端（可选依赖探测、接口串、会话、pydronecan 驱动）
  * `dronecan_gui_tool/dmcan_cli.py` —— 无界面 CLI
  * `dronecan_gui_tool/widgets/dmcan_control_panel/` —— 适配器控制面板
  * `tools/fetch_dmcan_lib.py`、`tests/`、`README.md`、`run_gui.bat`
* 本 fork **修改**的上游文件（改动点附近都有注释）：
  `dronecan_gui_tool/main.py`、`dronecan_gui_tool/setup_window.py`、`setup.py`。
* 上游 README 原文保留在 `docs/upstream-README.md`。

`libdmcan` 原生库来自达妙 **DM-DeviceSDK**（dm-device-sdk 仓库），**不在本仓库内**：
用 `tools/fetch_dmcan_lib.py` 从 SDK 拷贝，或设置环境变量 `DMCAN_LIBRARY_PATH`。
Python 封装 `dmcan_sdk` 是独立发行包（`pip install dmcan_sdk`），同样不作为本仓库的依赖强绑定。
