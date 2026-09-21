@echo off
REM remote-coder interactive CLI. Usage:
REM   rc                  - session in the current directory
REM   rc path\to\repo     - session in another repo
REM   rc repo "task"      - one-shot, no prompt
set ROOT=C:\Projects\remote-coder
if "%~1"=="" (
    "%ROOT%\.venv\Scripts\python.exe" "%ROOT%\cli.py" "%CD%"
) else (
    "%ROOT%\.venv\Scripts\python.exe" "%ROOT%\cli.py" %*
)
