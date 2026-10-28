@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
title Ditado Mic - INSTALAR
echo.
echo   DITADO MIC - instalacao (leva uns 5 minutos; baixa 200 MB)
echo   ---------------------------------------------------------
set "PYBASE="
where py >nul 2>nul && set "PYBASE=py -3"
if not defined PYBASE where python >nul 2>nul && set "PYBASE=python"
if not defined PYBASE goto :sem_python
if exist ".venv\Scripts\python.exe" goto :tem_venv
echo   Criando o ambiente Python...
%PYBASE% -m venv .venv
if errorlevel 1 goto :erro
:tem_venv
echo   Instalando os pacotes...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :erro
".venv\Scripts\python.exe" "%~dp0_acoes\instalar.py"
echo.
pause
exit /b 0
:sem_python
echo.
echo   Nao achei o Python 3. Instale em https://www.python.org/downloads/
echo   (marque "Add python.exe to PATH") e rode este arquivo de novo.
echo.
pause
exit /b 9
:erro
echo.
echo   ERRO na instalacao. Leia a mensagem acima.
echo.
pause
exit /b 1
