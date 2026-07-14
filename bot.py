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
import time
from pathlib import Path

from clean_images import clean_bytes

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaDocument, BotCommand)
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
ALBUM_DEBOUNCE = 1.5               # сек ожидания остальных фото из альбома

# Буфер альбомов: media_group_id -> список сообщений
_album_buf: dict = {}
_album_last: dict = {}
_album_lock = asyncio.Lock()

MODE_TITLES = {
    "meta": "Только метаданные",
    "dct": "DCT — аккуратно",
    "scrub": "Scrub — сильно",
}

HELP = (
    "Пришли мне картинку — верну очищенную копию:\n"
    "• удаляю метаданные (EXIF, GPS, XMP, IPTC, ICC, C2PA);\n"
    "• по желанию подавляю невидимые водяные знаки.\n\n"
    "📦 Можно *пачкой* — выдели сразу несколько фото (альбом), "
    "я обработаю все и верну одним альбомом.\n\n"
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


async def _download_and_clean(msg, mode: str, strength: float, idx=None):
    """Скачивает картинку и возвращает (bytes, имя, было_фото, исходные_байты)."""
    as_photo = bool(msg.photo)
    if as_photo:
        tg_file = await msg.photo[-1].get_file()
        filename = f"photo_{idx}.jpg" if idx is not None else "photo.jpg"
    else:
        doc = msg.document
        if doc.file_size and doc.file_size > MAX_BYTES:
            raise ValueError("файл больше ~20 МБ")
        tg_file = await doc.get_file()
        filename = doc.file_name or "image.jpg"
    dct = strength if mode == "dct" else 0.0
    scrub = strength if mode == "scrub" else 0.0
    data = bytes(await tg_file.download_as_bytearray())
    out, name = await asyncio.to_thread(
        clean_bytes, data, filename, 92, scrub, dct)
    return out, name, as_photo, data


async def _send_results(bot, chat_id, results, summary: str) -> None:
    """Отправляет очищенные файлы (по одному или альбомами по 10)."""
    first = True
    for i in range(0, len(results), 10):
        chunk = results[i:i + 10]
        if len(chunk) == 1:
            out, name = chunk[0]
            await bot.send_document(chat_id, document=out, filename=name,
                                    caption=summary if first else None)
        else:
            media = [
                InputMediaDocument(
                    media=out, filename=name,
                    caption=summary if (first and j == 0) else None)
                for j, (out, name) in enumerate(chunk)]
            await bot.send_media_group(chat_id, media=media)
        first = False


def _summary(mode: str, strength: float, ok: int, total: int,
             any_photo: bool) -> str:
    text = (f"Готово: {ok}/{total}. Режим: {MODE_TITLES.get(mode, mode)}"
            if total > 1 else f"Готово. Режим: {MODE_TITLES.get(mode, mode)}")
    if mode != "meta":
        text += f", сила {strength:.1f}"
    if ok < total:
        text += f"\nНе удалось обработать: {total - ok}"
    if any_photo:
        text += ("\n⚠️ Часть прислана как «фото» — телеграм их сжал. "
                 "Для чистки оригиналов присылай файлом.")
    return text


async def _process_batch(context, chat_id, msgs) -> None:
    mode = _get(context, "mode", DEFAULT_MODE)
    strength = _get(context, "strength", DEFAULT_STRENGTH)
    note = await context.bot.send_message(
        chat_id, f"Обрабатываю {len(msgs)} шт…" if len(msgs) > 1
        else "Обрабатываю…")
    results, any_photo, psnr = [], False, None
    for i, m in enumerate(msgs):
        try:
            out, name, as_photo, src = await _download_and_clean(
                m, mode, strength, idx=i if len(msgs) > 1 else None)
            results.append((out, name))
            any_photo = any_photo or as_photo
            if len(msgs) == 1 and mode != "meta":
                psnr = _psnr(src, out)
        except Exception as e:  # noqa: BLE001
            log.exception("processing failed: %s", e)

    if results:
        summary = _summary(mode, strength, len(results), len(msgs), any_photo)
        if psnr is not None:
            summary += f" · PSNR {psnr:.1f} dB"
        await _send_results(context.bot, chat_id, results, summary)
    else:
        await context.bot.send_message(
            chat_id, "Не удалось обработать ни одного файла.")
    try:
        await note.delete()
    except Exception:  # noqa: BLE001
        pass


async def _flush_album(gid: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ждёт, пока перестанут приходить фото альбома, затем обрабатывает пачкой."""
    while True:
        await asyncio.sleep(ALBUM_DEBOUNCE)
        async with _album_lock:
            if time.monotonic() - _album_last.get(gid, 0) >= ALBUM_DEBOUNCE:
                msgs = _album_buf.pop(gid, [])
                _album_last.pop(gid, None)
                break
    if msgs:
        await _process_batch(context, msgs[0].chat_id, msgs)


async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    gid = msg.media_group_id
    if gid:
        # часть альбома — копим и обрабатываем группой с задержкой
        async with _album_lock:
            is_new = gid not in _album_buf
            _album_buf.setdefault(gid, []).append(msg)
            _album_last[gid] = time.monotonic()
        if is_new:
            asyncio.create_task(_flush_album(gid, context))
        return
    await _process_batch(context, msg.chat_id, [msg])


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
