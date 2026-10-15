@echo off
rem Fork launcher: run the GUI with this project's venv and its own libdmcan.
rem NOTE: ASCII only - cmd.exe reads .bat in the console codepage.
rem
rem Usage (in PowerShell, prefix with .\ ):
rem   .\run_gui.bat                    open the setup dialog
rem   .\run_gui.bat dmcan:0.0          connect straight to the DM dual adapter CH0
rem   .\run_gui.bat --version          print version only
rem   .\run_gui.bat dmcan:0.0 --debug  with debug logging
setlocal
set HERE=%~dp0
if exist "%HERE%dlls\libdmcan.dll" set DMCAN_LIBRARY_PATH=%HERE%dlls\libdmcan.dll
set PY=%HERE%.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

"%PY%" -c "import dronecan, PyQt6" 2>nul
if errorlevel 1 (
  echo [run_gui] missing deps, run: "%PY%" -m pip install -r requirements.txt
  exit /b 1
)

if "%~1"=="" (
  echo [run_gui] no interface given, opening the setup dialog...
  "%PY%" -m dronecan_gui_tool.main
  goto :done
)

set FIRST=%~1
if "%FIRST:~0,1%"=="-" (
  echo [run_gui] args: %*
  "%PY%" -m dronecan_gui_tool.main %*
) else (
  echo [run_gui] interface: %~1
  "%PY%" -m dronecan_gui_tool.main --interface %*
)

:done
endlocal
