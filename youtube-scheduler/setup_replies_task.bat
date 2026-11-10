@echo off
rem Registers a daily Windows task (20:30) that runs run_replies.bat.
cd /d "%~dp0"
schtasks /Create /F /TN "YT replies daily" /SC DAILY /ST 20:30 /TR "\"%~dp0run_replies.bat\""
echo.
echo Done. Up to 20 prepared replies will be posted daily at 20:30 while this PC is on.
echo Log: replies_log.txt
