@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "VENV_DIR=%CD%\.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

if exist "%PYTHON_EXE%" goto check_deps

echo [edge-workbench] Creating Python 3.12 virtual environment...
where uv >nul 2>nul
if not errorlevel 1 (
  uv venv --python 3.12 --seed "%VENV_DIR%"
  if exist "%PYTHON_EXE%" goto check_deps
)

py -3.12 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  py -3.12 -m venv "%VENV_DIR%"
  if exist "%PYTHON_EXE%" goto check_deps
)

echo [edge-workbench] Python 3.12 was not found.
echo Install Python 3.12 or uv, then run this file again.
pause
exit /b 1

:check_deps
"%PYTHON_EXE%" -c "import playwright, customtkinter" >nul 2>nul
if not errorlevel 1 goto run

echo [edge-workbench] Installing dependencies...
where uv >nul 2>nul
if not errorlevel 1 (
  if defined EDGE_WORKBENCH_INDEX (
    uv pip install --python "%PYTHON_EXE%" --default-index "%EDGE_WORKBENCH_INDEX%" -r requirements.txt
  ) else (
    uv pip install --python "%PYTHON_EXE%" -r requirements.txt
  )
) else (
  if defined EDGE_WORKBENCH_INDEX (
    "%PYTHON_EXE%" -m pip install --index-url "%EDGE_WORKBENCH_INDEX%" -r requirements.txt
  ) else (
    "%PYTHON_EXE%" -m pip install -r requirements.txt
  )
)
if errorlevel 1 (
  echo [edge-workbench] Dependency installation failed.
  pause
  exit /b 1
)

:run
"%PYTHON_EXE%" -B edge_workbench.py --autostart
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
