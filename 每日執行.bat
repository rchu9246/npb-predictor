@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   NPB Prediction - Running...
echo ============================================
echo.

python npb_predictor.py

if errorlevel 1 (
    echo.
    echo [ERROR] Something went wrong. See messages above.
    pause
) else (
    echo.
    echo Done. The report should have opened in your browser.
    timeout /t 5
)
