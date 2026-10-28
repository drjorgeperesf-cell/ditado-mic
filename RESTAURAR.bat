@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PY%" goto :sem_venv
title Ditado Mic - restaurar
"%PY%" "%~dp0_acoes\restaurar.py"
echo.
pause

goto :fim
:sem_venv
echo.
echo   ERRO: a venv desta pasta nao existe.
echo   Esperava encontrar o python em .venv\Scripts\ aqui dentro.
echo.
pause
exit /b 9
:fim
