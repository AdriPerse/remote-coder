@echo off
REM ---------------------------------------------------------------------
REM Starts the three processes that make up remote-coder.
REM
REM NEVER EDIT THIS FILE WHILE IT IS RUNNING. cmd re-reads a batch file by
REM byte offset, so a live window resumes mid-word at the old position and
REM executes whatever fragment it lands on. MusicLore learned this by
REM watching a running launcher execute "omeone".
REM
REM Only ONE process may poll the Telegram token -- a second gets HTTP 409
REM and the bot silently stops answering. This script therefore kills any
REM previous instances before starting new ones.
REM
REM Everything logs to logs\. That matters more here than anywhere else:
REM this runs unattended, and a failure with no log is indistinguishable
REM from an idle bot.
REM ---------------------------------------------------------------------

set ROOT=%~dp0
set PY=%ROOT%.venv\Scripts\python.exe

if not exist "%PY%" (
    echo Virtualenv missing at %PY%
    echo Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'bot\.py|artifact_server\.py|watch\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

cd /d "%ROOT%"
if not exist "%ROOT%logs" mkdir "%ROOT%logs"

REM The artifact preview server. Its own port = its own origin, which is
REM what keeps a generated page away from everything else. LOOPBACK ONLY:
REM its prompt box feeds watch.py, which runs the agent with approvals off,
REM so it must never answer the LAN. To use it from the phone, bind the
REM Tailscale address instead (README, "Artifacts").
start "remote-coder artifacts" /min cmd /c ""%PY%" artifact_server.py --root "%ROOT%artifacts" --port 8001 --host 127.0.0.1 >> "%ROOT%logs\artifacts.log" 2>&1"

REM Runs follow-up requests typed on an artifact page.
start "remote-coder watch" /min cmd /c ""%PY%" watch.py --artifacts "%ROOT%artifacts" >> "%ROOT%logs\watch.log" 2>&1"

REM The Telegram bot. Last, so a token conflict is obvious on its own.
start "remote-coder bot" /min cmd /c ""%PY%" bot.py >> "%ROOT%logs\bot.log" 2>&1"

exit /b 0
