@echo off
cd /d %~dp0..
venv\Scripts\python.exe webui\main.py
pause
