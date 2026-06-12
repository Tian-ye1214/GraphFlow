@echo off
cd /d "%~dp0frontend"
call npm install || exit /b 1
call npm run build || exit /b 1
cd /d "%~dp0backend"
uv sync || exit /b 1
echo.
echo GraphFlow: http://127.0.0.1:8000
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
