@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   NPB - Add game results
echo ============================================
echo Example: F 4-2 DB E 0-7 G L 5-4 C
echo Home team first. For a rained-out game, edit the file
echo data\actual_results\DATE.txt by hand instead.
echo.
set /p D=Date (YYYY-MM-DD): 
set /p T=Results: 

python npb_predictor.py --add-results "%D%" "%T%"

echo.
pause
