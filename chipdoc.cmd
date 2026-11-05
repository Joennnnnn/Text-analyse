@echo off
setlocal
set "DIR=%~dp0"
set "APP=%DIR%portable\chipdoc-offline"

:: 1) 开发机：有 .venv 就直接用
if exist "%DIR%.venv\Scripts\python.exe" goto dev

:: 2) 离线机：用仓库里自带的成品，首次自动解压
if exist "%APP%\runtime\python\python.exe" goto payload
echo [chipdoc] 首次运行，解压离线包...
tar -xzf "%DIR%portable\chipdoc-offline.tar.gz" -C "%DIR%portable"
if errorlevel 1 goto tarfail

:payload
call "%APP%\run.cmd" %*
exit /b %ERRORLEVEL%

:dev
"%DIR%.venv\Scripts\python.exe" -m chipdoc %*
exit /b %ERRORLEVEL%

:tarfail
echo [chipdoc] 解压失败：请用 7-Zip 手动解压 portable\chipdoc-offline.tar.gz
echo            然后运行 portable\chipdoc-offline\run.cmd
exit /b 1
