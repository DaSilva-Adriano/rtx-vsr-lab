@echo off
setlocal
set "ROOT=%~dp0"
set "PY=%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe"
if not exist "%PY%" set "PY=python"
cd /d "%ROOT%"
"%PY%" -m vsr_lab %*
