@echo off

cd /d %~dp0

echo ============================
echo karinfuji 起動中...
echo ============================

.venv\Scripts\python.exe app\launcher.py

pause