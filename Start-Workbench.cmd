@echo off
if not exist "%~dp0runtime\python311\pythonw.exe" (
  echo Please download the Windows portable ZIP from GitHub Releases.
  echo Extract the whole folder before starting the workbench.
  pause
  exit /b 1
)
start "" "%~dp0runtime\python311\pythonw.exe" "%~dp0scripts\portable_launcher.py"
