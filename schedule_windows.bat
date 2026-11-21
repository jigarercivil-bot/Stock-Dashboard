@echo off
REM Builds the dashboard every weekday at 6:30pm (after the ASX close and Yahoo's end-of-day update).
schtasks /Create /F /TN "Stock Dashboard" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 18:30 /TR "\"%~dp0run_dashboard.bat\" --scheduled"
echo Scheduled. Output: %~dp0output\asx_dashboard_latest.html
pause
