@echo off

python -m venv .venv

call .venv\Scripts\activate

pip install -e .

playwright install

pause