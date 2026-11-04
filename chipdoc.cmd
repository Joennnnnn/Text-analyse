@echo off
setlocal
set "DIR=%~dp0"
set "PYTHONPATH=%DIR%;%PYTHONPATH%"
if exist "%DIR%.venv\Scripts\python.exe" (
  "%DIR%.venv\Scripts\python.exe" -m chipdoc %*
  exit /b %errorlevel%
)
where py >nul 2>nul && (py -3 -m chipdoc %* & exit /b %errorlevel%)
python -m chipdoc %*
