@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.12 -c "import playwright, customtkinter" 2>nul
if errorlevel 1 (
  echo Installing dependencies...
  py -3.12 -m pip install -r requirements.txt
)
py -3.12 -B edge_workbench.py --autostart
