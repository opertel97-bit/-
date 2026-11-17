@echo off
rem Removes the tasks created by setup_comment_task.bat.
schtasks /Delete /F /TN "YT shorts comment 1"
schtasks /Delete /F /TN "YT shorts comment 2"
schtasks /Delete /F /TN "YT shorts comment 3"
schtasks /Delete /F /TN "YT shorts comment 4" 2>nul
echo Done. Automatic comments are turned off.
