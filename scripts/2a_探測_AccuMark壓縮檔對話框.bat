@echo off
setlocal
set "PY=py -3"
where /q py || set "PY=python"
%PY% -B "%~dp0probe_ui.py" --mode dialog --label zip %*
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
