@echo off
setlocal
set "PY=py -3"
where /q py || set "PY=python"
%PY% -B "%~dp0batch_export.py" --force %*
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
