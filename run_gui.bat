@echo off
rem 本 fork 新增：用工程内的 venv 启动 GUI（免去记 python 路径与 DMCAN_LIBRARY_PATH）。
rem 用法：
rem   run_gui.bat                      打开 setup 对话框选接口
rem   run_gui.bat dmcan:0.0            直接连达妙双路模块 CH0（原生 USB）
rem   run_gui.bat dmcan:0.0 --debug    带 SDK/GUI 调试日志
setlocal
set HERE=%~dp0
if exist "%HERE%dlls\libdmcan.dll" set DMCAN_LIBRARY_PATH=%HERE%dlls\libdmcan.dll
set PY=%HERE%.venv\Scripts\python.exe
if not exist "%PY%" set PY=python
"%PY%" -c "import dronecan, PyQt6" 2>nul
if errorlevel 1 (
  echo [run_gui] 缺依赖，请先： pip install -r requirements.txt
  exit /b 1
)
if "%~1"=="" (
  "%PY%" -m dronecan_gui_tool.main
) else (
  "%PY%" -m dronecan_gui_tool.main --interface %*
)
endlocal
