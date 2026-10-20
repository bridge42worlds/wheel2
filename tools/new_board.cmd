@echo off
rem Первая прошивка новых плат по USB: подключите плату кабелем с передачей данных и запустите.
rem Новые платы запишутся в реестр и прошьются; записанные раньше не трогаются (их обновляет Wi-Fi).
cd /d %~dp0..
set PYTHONIOENCODING=utf-8
python tools\new_board.py %*
pause
