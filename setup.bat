@echo off

cd /d %~dp0

echo ==================================
echo Starting Sakurazaka Message Auto Save Tool Setup...
echo ==================================

python -m venv .venv

call .venv\Scripts\activate.bat

python -m pip install --upgrade pip

pip install -e .

python -m playwright install

echo.
echo ==================================
echo Setup Complete
echo start.batを実行してください
echo ==================================

pause