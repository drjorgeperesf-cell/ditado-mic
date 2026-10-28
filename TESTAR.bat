@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PY%" goto :sem_venv
title Ditado Mic - TESTAR
echo.
echo   Rodando a suite automatizada.
echo   Nao mexe na sua tela nem no seu teclado. Leva uns 2 minutos.
echo.
"%PY%" "%~dp0_acoes\testar.py"
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" goto :ruim
echo   TESTE OK
goto :pronto
:ruim
echo   FALHOU
:pronto
echo.
pause
exit /b %RC%

goto :fim
:sem_venv
echo.
echo   ERRO: a venv desta pasta nao existe.
echo   Esperava encontrar o python em .venv\Scripts\ aqui dentro.
echo.
pause
exit /b 9
:fim
