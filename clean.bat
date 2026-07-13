@echo off
rem Двойной клик открывает окно чистки фото (Windows).
rem Первый запуск создаёт окружение и ставит зависимости — это разово.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3 не найден.
  echo Установите его с https://www.python.org/downloads/ и запустите снова.
  echo При установке отметьте галочку "Add Python to PATH".
  pause
  exit /b 1
)

if not exist ".venv" (
  echo Первый запуск: создаю окружение и ставлю зависимости ^(Pillow, numpy^)...
  %PY% -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install Pillow numpy
  echo Готово.
)

".venv\Scripts\python.exe" gui.py
if errorlevel 1 pause
