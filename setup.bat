@echo off

cd /d %~dp0

echo Starting Auto Save Tool Setup...

python -m venv .venv

call .venv\Scripts\activate.bat

python -m pip install --upgrade pip

pip install playwright

python -m playwright install

echo Setup Complete

pause