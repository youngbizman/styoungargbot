@echo off
cd /d C:\styoungargbot

echo Current folder:
cd

echo.
echo Using Python:
C:\styoungargbot\venv\Scripts\python.exe --version

echo.
echo Running Soccer script...
C:\styoungargbot\venv\Scripts\python.exe main_soccer.py

echo.
echo Soccer script finished.
pause