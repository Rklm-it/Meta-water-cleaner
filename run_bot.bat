@echo off
rem Двойной клик запускает телеграм-бота (Windows).
rem Перед первым запуском: скопируй .env.example в .env и вставь токен.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".env" (
  echo Нет файла .env с токеном.
  echo Скопируй .env.example в .env и впиши BOT_TOKEN от @BotFather.
  pause
  exit /b 1
)

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3 не найден. Установите с https://www.python.org/downloads/
  echo и отметьте галочку "Add Python to PATH".
  pause
  exit /b 1
)

if not exist ".venv" (
  echo Первый запуск: ставлю зависимости...
  %PY% -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo Бот запущен. Закрой окно, чтобы остановить.
".venv\Scripts\python.exe" bot.py
if errorlevel 1 pause
