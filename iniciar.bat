@echo off
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
start "" http://localhost:8000
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
