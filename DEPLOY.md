# Развёртывание бота на VPS

Нужен сервер с Linux (Ubuntu/Debian) и токен от [@BotFather](https://t.me/BotFather).
Один бот-токен = один запущенный экземпляр (два сразу запускать нельзя —
будет конфликт `getUpdates`).

Ниже два способа. **Docker — проще всего, рекомендую.**

---

## Способ 1. Docker (рекомендуется)

```bash
# 1. Поставить docker (если ещё нет)
curl -fsSL https://get.docker.com | sh

# 2. Забрать код
git clone https://github.com/Rklm-it/Meta-water-cleaner.git
cd Meta-water-cleaner

# 3. Прописать токен и (желательно) белый список
cp .env.example .env
nano .env            # BOT_TOKEN=твой_токен ; ALLOWED_USERS=твой_id
                     # свой id узнаешь, написав боту /id после старта

# 4. Запустить (соберётся образ и стартует в фоне)
docker compose up -d --build
```

Управление:

```bash
docker compose logs -f      # смотреть логи
docker compose restart      # перезапустить
docker compose down         # остановить
docker compose up -d --build   # обновить после git pull
```

`restart: unless-stopped` в `docker-compose.yml` поднимет бота после
перезагрузки сервера автоматически. Настройки пользователей и статистика
лежат в томе `./data` (переживают пересборку). В образе есть healthcheck —
`docker compose ps` покажет статус `healthy`, если бот жив.

---

## Способ 2. systemd (без Docker)

```bash
# 1. Зависимости
sudo apt update && sudo apt install -y python3-venv git

# 2. Код в /opt и отдельный пользователь
sudo git clone https://github.com/Rklm-it/Meta-water-cleaner.git /opt/meta-water-cleaner
sudo useradd -r -s /usr/sbin/nologin botuser
cd /opt/meta-water-cleaner

# 3. Виртуальное окружение и зависимости
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

# 4. Токен
sudo cp .env.example .env
sudo nano .env            # вписать BOT_TOKEN=...
sudo chown -R botuser:botuser /opt/meta-water-cleaner

# 5. Служба
sudo cp deploy/cleaner-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cleaner-bot
```

Управление:

```bash
sudo systemctl status cleaner-bot     # состояние
journalctl -u cleaner-bot -f          # логи
sudo systemctl restart cleaner-bot    # перезапуск
```

Обновление после изменений:

```bash
cd /opt/meta-water-cleaner
sudo git pull
sudo .venv/bin/pip install -r requirements.txt
sudo systemctl restart cleaner-bot
```

---

## Проверка

Напиши боту в телеграме `/start`. Если отвечает — всё работает.
Если нет — смотри логи (команды выше): чаще всего это неверный `BOT_TOKEN`
или запущены два экземпляра одновременно.

> Безопасность: `.env` с токеном в репозиторий не коммитится (он в
> `.gitignore`). Если репозиторий приватный на приватном сервере — этого
> достаточно. Токен утёк? Отзови/перевыпусти его у @BotFather.
