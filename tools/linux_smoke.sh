#!/bin/bash
# Linux/WSL 侧的验证脚本：真机收发 + GUI 冒烟 + 单元测试。
# 由 tools/wsl_sync.ps1 把源码同步到 $TARGET 之后跑。
set -u
PY=${PY:-/home/dm/.venvs/dgt/bin/python}
cd "$(dirname "$0")/.." 2>/dev/null || cd "$TARGET"
export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-offscreen}
FAIL=0

banner() { echo; echo "########## $* ##########"; }

banner "环境"
echo "内核 $(uname -r) / $(. /etc/os-release && echo $PRETTY_NAME)"
$PY --version
$PY -c "import PyQt6, dronecan, can, serial, dmcan; from PyQt6.QtCore import QT_VERSION_STR; print('依赖 OK，Qt', QT_VERSION_STR)"

banner "1) 自检（原生库 / 依赖 / 设备枚举）"
$PY -m dronecan_gui_tool.dmcan_cli doctor || FAIL=1

banner "2) 接口串列表"
$PY -m dronecan_gui_tool.dmcan_cli list || FAIL=1

banner "3) 真机：外部回环 CH1，500 帧"
timeout 120 $PY -m dronecan_gui_tool.dmcan_cli selftest dmcan:0.1@1000000 --count 500 || FAIL=1

banner "4) 真机：监听 CH1 五秒"
timeout 60 $PY -m dronecan_gui_tool.dmcan_cli monitor dmcan:0.1@1000000 --seconds 5 || FAIL=1

banner "5) GUI 冒烟（仿真后端，离屏）"
timeout 180 $PY -u tests/gui_smoke.py --mock --seconds 2 > /tmp/gui_smoke.log 2>&1 || FAIL=1
grep -E "^\[smoke\] (使用|连接|节点已建立|主窗口截图|Appearance|菜单|完成)" /tmp/gui_smoke.log | head -8
grep -q "菜单切换正常" /tmp/gui_smoke.log || { echo "!! GUI 冒烟没跑完"; FAIL=1; }

banner "6) 单元测试"
timeout 300 $PY -m unittest discover -s tests -t . 2>&1 | tail -4 || FAIL=1

banner "结果"
[ $FAIL -eq 0 ] && echo "全部通过" || echo "有失败项（见上面）"
exit $FAIL
