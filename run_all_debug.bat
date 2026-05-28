@echo off
cd /d C:\styoungargbot

echo ==============================
echo Running NBA...
echo ==============================
C:\styoungargbot\venv\Scripts\python.exe main_nba.py

echo.
echo ==============================
echo Running Soccer...
echo ==============================
C:\styoungargbot\venv\Scripts\python.exe main_soccer.py

echo.
echo ==============================
echo Running UFC...
echo ==============================
C:\styoungargbot\venv\Scripts\python.exe main_ufc.py

echo.
echo ==============================
echo All scripts finished.
echo ==============================
pause