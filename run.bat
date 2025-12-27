@echo off
echo Loading environment variables from .env...

REM Read .env and set variables (simple version)
for /f "tokens=1,2 delims==" %%a in (.env) do (
    if "%%a"=="export TELEGRAM_BOT_TOKEN" set TELEGRAM_BOT_TOKEN=%%b
    if "%%a"=="export DEEPSEEK_API_KEY" set DEEPSEEK_API_KEY=%%b
    if "%%a"=="export GOOGLE_SHEET_ID" set GOOGLE_SHEET_ID=%%b
)

REM Remove quotes and "export " prefix
set TELEGRAM_BOT_TOKEN=%TELEGRAM_BOT_TOKEN:"=%
set DEEPSEEK_API_KEY=%DEEPSEEK_API_KEY:"=%
set GOOGLE_SHEET_ID=%GOOGLE_SHEET_ID:"=%

echo Starting bot...
python bot.py
