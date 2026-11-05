@echo off
setlocal
set "DIR=%~dp0"
pushd "%DIR%"

:: 选解释器：优先 .venv，其次 py -3，最后 python
if exist "%DIR%.venv\Scripts\python.exe" goto use_venv
where py >nul 2>nul
if errorlevel 1 goto use_python
set "PY=py -3"
goto check
:use_python
set "PY=python"
goto check
:use_venv
set "PY=%DIR%.venv\Scripts\python.exe"

:check
:: 缺 pymupdf 就直接装（需要联网，只装一次）
%PY% -c "import pymupdf" >nul 2>&1
if errorlevel 1 (
  echo [chipdoc] 未检测到 pymupdf，正在安装...
  %PY% -m pip install pymupdf
  if errorlevel 1 (
    echo [chipdoc] 安装失败，请手动执行：%PY% -m pip install pymupdf
    exit /b 1
  )
)

%PY% -m chipdoc %*
exit /b %ERRORLEVEL%
