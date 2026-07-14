#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — телеграм-бот для чистки фотографий.

Присылаешь картинку -> бот удаляет метаданные (EXIF/GPS/XMP/IPTC/ICC/C2PA)
и по желанию подавляет невидимые водяные знаки (режимы DCT / Scrub),
возвращает очищенный файл.

Токен берётся из переменной окружения BOT_TOKEN (или из файла .env рядом).
Создать бота и получить токен: https://t.me/BotFather -> /newbot

Запуск:
    pip install -r requirements.txt
    BOT_TOKEN=xxxxx python3 bot.py

Важно: обычные "фото" в телеграме сжимаются и теряют метаданные ещё до
бота. Чтобы очистить именно исходный файл без потерь — присылай картинку
как ФАЙЛ (скрепка -> Файл), и бот тоже вернёт её файлом.
"""

import asyncio
import logging
import os
from pathlib import Path

from clean_images import clean_bytes

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      BotCommand)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO)
log = logging.getLogger("cleaner-bot")

DEFAULT_MODE = "dct"
DEFAULT_STRENGTH = 0.6
STRENGTHS = [0.3, 0.6, 1.0]
MAX_BYTES = 20 * 1024 * 1024        # лимит скачивания Bot API

MODE_TITLES = {
    "meta": "Только метаданные",
    "dct": "DCT — аккуратно",
    "scrub": "Scrub — сильно",
}

HELP = (
    "Пришли мне картинку — верну очищенную копию:\n"
    "• удаляю метаданные (EXIF, GPS, XMP, IPTC, ICC, C2PA);\n"
    "• по желанию подавляю невидимые водяные знаки.\n\n"
    "⚠️ Присылай как *ФАЙЛ* (скрепка → Файл), а не как «фото» — иначе "
    "телеграм сожмёт картинку и потеряет часть данных ещё до меня.\n\n"
    "Команды:\n"
    "/mode — выбрать режим и силу\n"
    "/help — эта справка\n\n"
    "Честно: режимы DCT/Scrub *снижают* детектируемость невидимых знаков "
    "(включая SynthID), но не гарантируют их полное удаление."
)


def _load_dotenv() -> None:
    """Простейший разбор .env рядом со скриптом (без зависимостей)."""
    env = Path(__file__).resolve().parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _psnr(a: bytes, b: bytes):
    """PSNR между двумя картинками (дБ) или None, если не удалось."""
    try:
        import io
        import numpy as np
        from PIL import Image
        x = np.asarray(Image.open(io.BytesIO(a)).convert("RGB"), dtype=np.float64)
        y = np.asarray(Image.open(io.BytesIO(b)).convert("RGB"), dtype=np.float64)
        if x.shape != y.shape:
            return None
        mse = np.mean((x - y) ** 2)
        return 99.0 if mse == 0 else float(10 * np.log10(255.0 ** 2 / mse))
    except Exception:  # noqa: BLE001
        return None


def _settings_kb(mode: str, strength: float) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        ("✅ " if m == mode else "") + title, callback_data=f"mode:{m}")]
        for m, title in MODE_TITLES.items()]
    if mode != "meta":
        rows.append([InlineKeyboardButton(
            ("✅ " if abs(s - strength) < 1e-6 else "") + f"сила {s:.1f}",
            callback_data=f"str:{s}") for s in STRENGTHS])
    return InlineKeyboardMarkup(rows)


def _get(context, key, default):
    return context.user_data.get(key, default)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = _get(context, "mode", DEFAULT_MODE)
    strength = _get(context, "strength", DEFAULT_STRENGTH)
    await update.message.reply_text(
        "Выбери режим обработки:", reply_markup=_settings_kb(mode, strength))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("mode:"):
        context.user_data["mode"] = data.split(":", 1)[1]
    elif data.startswith("str:"):
        context.user_data["strength"] = float(data.split(":", 1)[1])
    mode = _get(context, "mode", DEFAULT_MODE)
    strength = _get(context, "strength", DEFAULT_STRENGTH)
    label = MODE_TITLES.get(mode, mode)
    text = f"Режим: {label}"
    if mode != "meta":
        text += f", сила {strength:.1f}"
    await q.edit_message_text(text, reply_markup=_settings_kb(mode, strength))


async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    as_photo = bool(msg.photo)
    if as_photo:
        tg_file = await msg.photo[-1].get_file()
        filename = "photo.jpg"
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        if msg.document.file_size and msg.document.file_size > MAX_BYTES:
            await msg.reply_text("Файл слишком большой (лимит ~20 МБ).")
            return
        tg_file = await msg.document.get_file()
        filename = msg.document.file_name or "image.jpg"
    else:
        await msg.reply_text(
            "Пришли изображение (лучше как файл). /help — подробнее.")
        return

    mode = _get(context, "mode", DEFAULT_MODE)
    strength = _get(context, "strength", DEFAULT_STRENGTH)
    dct = strength if mode == "dct" else 0.0
    scrub = strength if mode == "scrub" else 0.0

    note = await msg.reply_text("Обрабатываю…")
    try:
        data = bytes(await tg_file.download_as_bytearray())
        out, name = await asyncio.to_thread(
            clean_bytes, data, filename, 92, scrub, dct)
    except Exception as e:  # noqa: BLE001
        log.exception("processing failed")
        await note.edit_text(f"Не получилось обработать: {e}")
        return

    caption = f"Готово. Режим: {MODE_TITLES.get(mode, mode)}"
    if mode != "meta":
        caption += f", сила {strength:.1f}"
        psnr = _psnr(data, out)
        if psnr is not None:
            caption += f" · PSNR {psnr:.1f} dB"
    if as_photo:
        caption += ("\n⚠️ Это было «фото» — телеграм уже сжал его. "
                    "Для чистки оригинала присылай как файл.")

    await msg.reply_document(document=out, filename=name, caption=caption)
    try:
        await note.delete()
    except Exception:  # noqa: BLE001
        pass


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Справка и начало"),
        BotCommand("mode", "Выбрать режим и силу"),
        BotCommand("help", "Справка"),
    ])


def main() -> None:
    _load_dotenv()
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Не задан BOT_TOKEN. Получи токен у @BotFather и положи его в "
            "файл .env (BOT_TOKEN=...) или задай переменной окружения.")

    app = Application.builder().token(token).post_init(_post_init).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.IMAGE, on_image))
    log.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
