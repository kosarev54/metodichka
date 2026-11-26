@echo off
chcp 65001 >nul
title Web Service
color 0A

echo.
echo ============================================================
echo   STARTING WEB SERVICE
echo ============================================================
echo.

REM Go to portable Python folder
cd /d C:\Users\Dispi\Desktop\zadanieGradusov\WebService\Python-3.12.14

REM Check python.exe exists
if not exist "python.exe" (
    echo [ERROR] python.exe not found in folder:
    echo   C:\Users\Dispi\Desktop\zadanieGradusov\WebService\Python-3.12.14
    echo.
    echo Please check the path to portable Python.
    pause
    exit /b 1
)

REM Check app.py exists
if not exist "C:\Users\Dispi\Desktop\zadanieGradusov\WebService\app.py" (
    echo [ERROR] app.py not found in folder:
    echo   C:\inetpub\WebService
    echo.
    echo Please check the project path.
    pause
    exit /b 1
)

echo [OK] Python found: python.exe
echo [OK] Project found: app.py
echo.
echo ============================================================
echo   STARTING SERVICE ON PORT 8080
echo ============================================================
echo.
echo   Web interface:   http://localhost:8080
echo   API endpoints:   http://localhost:8080/api/...
echo   To stop:         press Ctrl + C
echo.
echo ============================================================
echo.

REM Start the service
.\python.exe C:\Users\Dispi\Desktop\zadanieGradusov\WebService\app.py

echo.
echo ============================================================
echo   SERVICE STOPPED
echo ============================================================
pause