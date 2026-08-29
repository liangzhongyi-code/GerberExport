@echo off
setlocal
set "PY=py -3"
where /q py || set "PY=python"
%PY% -B "%~dp0check_env.py" %*
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
