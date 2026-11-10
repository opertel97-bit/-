@echo off
rem Registers 3 daily Windows tasks (08:10, 18:10, 21:10) that run run_comment.bat.
cd /d "%~dp0"
schtasks /Create /F /TN "YT shorts comment 1" /SC DAILY /ST 08:10 /TR "\"%~dp0run_comment.bat\""
schtasks /Create /F /TN "YT shorts comment 2" /SC DAILY /ST 18:10 /TR "\"%~dp0run_comment.bat\""
schtasks /Create /F /TN "YT shorts comment 3" /SC DAILY /ST 21:10 /TR "\"%~dp0run_comment.bat\""
echo.
echo Done. Comments will be posted daily at 08:10, 18:10, 21:10 while this PC is on.
echo Log: comment_log.txt
