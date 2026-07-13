#!/usr/bin/env bash
# Двойной клик открывает окно чистки фото (macOS).
# Первый запуск создаёт окружение и ставит зависимости — это разово.
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3 не найден."
  echo "Установите его с https://www.python.org/downloads/ и запустите снова."
  read -n1 -r -p "Нажмите любую клавишу для выхода..."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Первый запуск: создаю окружение и ставлю зависимости (Pillow, numpy)..."
  "$PY" -m venv .venv
  ./.venv/bin/python -m pip install --upgrade pip >/dev/null
  ./.venv/bin/python -m pip install Pillow numpy
  echo "Готово."
fi

exec ./.venv/bin/python gui.py
