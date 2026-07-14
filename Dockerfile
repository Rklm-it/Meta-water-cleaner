FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY clean_images.py bot.py ./

# Токен передаётся переменной окружения BOT_TOKEN при запуске:
#   docker build -t photo-cleaner-bot .
#   docker run -e BOT_TOKEN=xxxxx --restart unless-stopped photo-cleaner-bot
CMD ["python", "bot.py"]
