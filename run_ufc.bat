@echo off
cd /d C:\styoungargbot

echo Current folder:
cd

echo.
echo Using Python:
C:\styoungargbot\venv\Scripts\python.exe --version

echo.
echo Running UFC script...
C:\styoungargbot\venv\Scripts\python.exe main_ufc.py

echo.
echo UFC script finished.
pause