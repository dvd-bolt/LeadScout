@echo off
title LeadScout AI
cd /d "%~dp0"
echo ===================================================
echo   LeadScout: Zapusk bota i Telegram Mini App
echo ===================================================
echo.
.\.venv\Scripts\python.exe -u scripts\start_tunnel_and_server.py
pause
