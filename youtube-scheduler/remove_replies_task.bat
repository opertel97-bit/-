@echo off
rem Removes the task created by setup_replies_task.bat.
schtasks /Delete /F /TN "YT replies daily"
echo Done. Automatic replies are turned off.
