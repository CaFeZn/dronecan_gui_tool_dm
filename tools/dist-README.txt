DroneCAN GUI Tool — dmcan 分支（免安装版）
==========================================

双击 dronecan-gui-tool-dm.exe 即可运行，不需要装 Python。

目录里有什么
------------
  dronecan-gui-tool-dm.exe   图形界面（双击运行）
  dmcan-dronecan-tool.exe    命令行工具（doctor / list / monitor / selftest / info）
  dlls\                       libdmcan 及其依赖（Native library folder）
  _internal\                 运行时（Python / PyQt6 / 各种库，别动）

重要
----
* dlls\ 里的 5 个 DLL 必须与两个 exe 同目录（它们是一个整体）：
    libdmcan.dll  libusb-1.0.dll  libstdc++-6.dll  libgcc_s_seh-1.dll  libwinpthread-1.dll
  移动 / 拷贝时请整目录一起拷（可以把整个文件夹压成 zip 发给别人）。
* 想升级原生库：用 SDK 里的 tools/fetch_dmcan_lib.py 重新取，或者设置环境变量
  DMCAN_LIBRARY_PATH 指向新的 libdmcan.dll。
* 卸载 = 删掉整个文件夹。

怎么用
------
1. 插上达妙 USB2CANFD / USB2FDCANDUAL（双路模块两条通道会各出现一项）；
2. 双击 dronecan-gui-tool-dm.exe，在对话框里选 "dmcan: USB2CANFD_Dual CH0 ..."；
   位速率用下面的 "CAN bus bit rate" 填（DroneCAN 标准是 1000000）；
3. 想看丢帧：菜单 Tools -> CAN Adapter Control Panel（dmcan 适配器面板）；
   想看总线报文：Tools -> Bus Monitor。

命令行（排错用）
----------------
  dmcan-dronecan-tool.exe doctor    环境自检（原生库、依赖解析、设备枚举）
  dmcan-dronecan-tool.exe list      列出可用接口串
  dmcan-dronecan-tool.exe monitor dmcan:0.0@1000000 --seconds 10
  dmcan-dronecan-tool.exe selftest dmcan:0.1@1000000 --count 2000

外观（深色 / 浅色）
-------------------
菜单 View -> Appearance：Follow System（跟随系统，默认）/ Dark（强制深色）/ Light（强制浅色），
选择会被记住，下次启动以及 Bus Monitor、Plotter 两个窗口都跟着变，切换立即生效。
深色模式下表格文字与图标按当前背景做过对比度适配。

来源
----
基于 DroneCAN/gui_tool（MIT）+ 达妙 DM-DeviceSDK。详见工程里的 README.md 与 NOTICE.md。
