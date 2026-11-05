@echo off
rem Arranca el panel de laboratorio y abre el navegador en http://127.0.0.1:8765
cd /d "%~dp0"
python servidor.py
pause
