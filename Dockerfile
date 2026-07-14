FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY clean_images.py bot.py ./

# Данные (настройки/статистика) — в /app/data (примонтируй том, см. compose)
ENV DATA_FILE=/app/data/botdata.pkl \
    HEARTBEAT_FILE=/tmp/hb
RUN mkdir -p /app/data

# Healthcheck: процесс жив, если heartbeat-файл обновлялся недавно
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,time,sys; p=os.environ.get('HEARTBEAT_FILE','/tmp/hb'); sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<120 else 1)"

# Токен передаётся через .env / переменную окружения BOT_TOKEN.
CMD ["python", "bot.py"]
