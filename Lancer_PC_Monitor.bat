@echo off
REM Lance l'agent en mode local (interface servie par l'agent, sans site web)
title PC Monitor
cd /d "%~dp0agent"
python -c "import psutil" 2>nul || python -m pip install --quiet psutil
python server.py --local-ui
pause
