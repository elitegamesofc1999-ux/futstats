@echo off
title FutStats
cd /d "%~dp0"
echo Instalando/verificando dependencias (so demora na primeira vez)...
py -m pip install -q fastapi uvicorn requests
echo.
echo ============================================
echo   FutStats rodando! Abrindo no navegador...
echo   NAO FECHE ESTA JANELA enquanto usa o site
echo ============================================
start "" http://localhost:8000
py -m uvicorn main:app --host 0.0.0.0 --port 8000
pause
