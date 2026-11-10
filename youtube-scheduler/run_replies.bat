@echo off
rem Posts up to 20 prepared replies (60s apart) from replies.txt. Run by Task Scheduler.
cd /d "%~dp0"
start "" /min cmd /c "python yt.py replies --import --limit 20 --delay 60 --auto >> replies_log.txt 2>&1"
