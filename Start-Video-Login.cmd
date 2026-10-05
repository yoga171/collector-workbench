@echo off
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0runtime\browsers"
pushd "%~dp0modules\video-parser"
start "" "%~dp0runtime\video313\pythonw.exe" "%~dp0modules\video-parser\qt_app.py"
popd
