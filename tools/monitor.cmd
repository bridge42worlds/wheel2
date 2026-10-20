@echo off
rem Запуск сервера мониторинга. Страница: http://localhost:8090/
cd /d %~dp0..
set PYTHONIOENCODING=utf-8
python server\monitor.py
pause
