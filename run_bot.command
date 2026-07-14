#!/usr/bin/env bash
# Двойной клик запускает телеграм-бота (macOS).
# Перед первым запуском: скопируй .env.example в .env и вставь токен.
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Нет файла .env с токеном."
  echo "Скопируй .env.example в .env и впиши BOT_TOKEN от @BotFather."
  read -n1 -r -p "Нажмите любую клавишу для выхода..."
  exit 1
fi

if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi

if [ ! -d .venv ]; then
  echo "Первый запуск: ставлю зависимости…"
  "$PY" -m venv .venv
  ./.venv/bin/python -m pip install --upgrade pip >/dev/null
  ./.venv/bin/python -m pip install -r requirements.txt
fi

echo "Бот запущен. Закрой окно, чтобы остановить."
exec ./.venv/bin/python bot.py
