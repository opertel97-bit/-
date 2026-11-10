@echo off
rem Posts the channel comment on newly published scheduled shorts. Run by Task Scheduler.
cd /d "%~dp0"
python yt.py comment --execute --auto >> comment_log.txt 2>&1
