@echo off
setlocal EnableDelayedExpansion

cd /d C:\styoungargbot

if not exist logs mkdir logs

if exist logs\run_all.lock (
    echo [%date% %time%] Previous run still active. Skipping this run. >> logs\run_all_task.log
    exit /b 0
)

echo %date% %time% > logs\run_all.lock

(
echo ==================================================
echo START RUN: %date% %time%
echo ==================================================

echo.
echo ==============================
echo Running NBA...
echo ==============================
C:\styoungargbot\venv\Scripts\python.exe main_nba.py
echo NBA exit code: !ERRORLEVEL!

echo.
echo ==============================
echo Running Soccer...
echo ==============================
C:\styoungargbot\venv\Scripts\python.exe main_soccer.py
echo Soccer exit code: !ERRORLEVEL!

echo.
echo ==============================
echo Running UFC...
echo ==============================
C:\styoungargbot\venv\Scripts\python.exe main_ufc.py
echo UFC exit code: !ERRORLEVEL!

echo.
echo ==================================================
echo END RUN: %date% %time%
echo ==================================================
echo.

) >> logs\run_all_task.log 2>&1

del logs\run_all.lock

exit /b 0