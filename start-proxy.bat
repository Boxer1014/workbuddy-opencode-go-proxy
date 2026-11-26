@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="
for %%I in (python.exe) do if not defined PYTHON_EXE set "PYTHON_EXE=%%~$PATH:I"
if defined PYTHON_EXE goto run_proxy

for /d %%D in ("%LocalAppData%\Programs\Python\Python*") do (
    if exist "%%~fD\python.exe" set "PYTHON_EXE=%%~fD\python.exe"
)
if defined PYTHON_EXE goto run_proxy

for /d %%D in ("%ProgramFiles%\Python*") do (
    if exist "%%~fD\python.exe" set "PYTHON_EXE=%%~fD\python.exe"
)
if not defined PYTHON_EXE (
    echo Python was not found. Install Python 3.10 or newer and try again.
    pause
    exit /b 1
)

:run_proxy
"%PYTHON_EXE%" "%~dp0opencode_go_proxy.py" %*
if errorlevel 1 pause
endlocal
