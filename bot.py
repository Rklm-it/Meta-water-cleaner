#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — телеграм-бот для чистки фотографий.

Присылаешь картинку (лучше как ФАЙЛ) -> бот удаляет метаданные
(EXIF/GPS/XMP/IPTC/ICC/C2PA) и по желанию подавляет невидимые водяные
знаки (DCT / Scrub), возвращает очищенный файл. Работает и пачкой (альбом).

Возможности:
  • пресеты «для сайта / максимум / метаданные»;
  • выбор режима, силы, качества, формата (JPG/WebP/PNG) и размера;
  • настройки сохраняются между перезапусками;
  • превью до/после с PSNR (/preview);
  • отчёт по пачке (что удалено, PSNR, вес до/после);
  • белый список пользователей (ALLOWED_USERS).

Конфиг (переменные окружения или файл .env рядом):
  BOT_TOKEN      — токен от @BotFather (обязательно)
  ALLOWED_USERS  — id пользователей через запятую (пусто = доступ всем)
  DATA_FILE      — файл сохранения настроек (по умолч. botdata.pkl)
  STATS_FILE     — CSV со статистикой (пусто = не писать)
  HEARTBEAT_FILE — файл живости для healthcheck (по умолч. /tmp/hb)
"""

import asyncio
import csv
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from clean_images import clean_bytes

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaDocument, InputMediaPhoto, BotCommand)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters,
                          PicklePersistence)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("cleaner-bot")

# --- настройки по умолчанию и справочники ---------------------------------
DEFAULTS = {"mode": "dct", "strength": 0.6, "quality": 92,
            "max_side": 0, "fmt": "keep"}
STRENGTHS = [0.3, 0.6, 1.0]
QUALITIES = [80, 90, 95]
SIZES = [(0, "Ориг"), (1600, "1600"), (1200, "1200"), (800, "800")]
FORMATS = [("keep", "Как есть"), ("jpg", "JPG"), ("webp", "WebP"), ("png", "PNG")]
MODES = [("meta", "Meta"), ("dct", "DCT"), ("scrub", "Scrub"), ("both", "Оба")]
MODE_TITLES = {"meta": "Только метаданные", "dct": "DCT — аккуратно",
               "scrub": "Scrub — сильно", "both": "DCT+Scrub — максимум"}
PRESETS = {
    "site": {"mode": "meta", "fmt": "webp", "max_side": 1600,
             "quality": 85, "strength": 0.6},
    "max": {"mode": "both", "fmt": "keep", "max_side": 0,
            "quality": 95, "strength": 1.0},
    "meta": {"mode": "meta", "fmt": "keep", "max_side": 0,
             "quality": 92, "strength": 0.6},
}

MAX_BYTES = 20 * 1024 * 1024        # лимит скачивания Bot API
MAX_FILES = 30                      # лимит файлов за раз
ALBUM_DEBOUNCE = 1.5
HEARTBEAT = Path(os.environ.get("HEARTBEAT_FILE", "/tmp/hb"))
STATS_FILE = os.environ.get("STATS_FILE", "").strip()

# буфер альбомов
_album_buf: dict = {}
_album_last: dict = {}
_album_lock = asyncio.Lock()
# сильные ссылки на фоновые задачи, чтобы их не собрал GC
_bg_tasks: set = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

HELP = (
    "Пришли мне картинку — верну очищенную копию:\n"
    "• удаляю метаданные (EXIF, GPS, XMP, IPTC, ICC, C2PA);\n"
    "• по желанию подавляю невидимые водяные знаки.\n\n"
    "📦 Можно *пачкой* — выдели несколько фото (альбом).\n"
    "⚠️ Присылай как *ФАЙЛ* (скрепка → Файл), иначе телеграм сожмёт "
    "картинку и потеряет часть данных ещё до меня.\n\n"
    "Команды:\n"
    "/settings — режим, сила, качество, формат, размер, пресеты\n"
    "/preview — показать до/после с PSNR (без сохранения)\n"
    "/id — узнать свой Telegram ID\n"
    "/help — эта справка\n\n"
    "Честно: DCT/Scrub *снижают* детектируемость невидимых знаков "
    "(включая SynthID), но не гарантируют их полное удаление."
)


# --- работа с настройками пользователя ------------------------------------
def get_settings(context) -> dict:
    s = dict(DEFAULTS)
    s.update(context.user_data.get("settings", {}))
    return s


def set_setting(context, key, value) -> None:
    s = context.user_data.get("settings", {})
    s[key] = value
    context.user_data["settings"] = s


def apply_preset(context, name) -> None:
    if name in PRESETS:
        context.user_data["settings"] = dict(PRESETS[name])


def mode_to_dct_scrub(mode: str, strength: float):
    if mode == "dct":
        return strength, 0.0
    if mode == "scrub":
        return 0.0, strength
    if mode == "both":
        return strength, strength
    return 0.0, 0.0                 # meta


# --- доступ ----------------------------------------------------------------
def allowed_ids() -> set:
    raw = os.environ.get("ALLOWED_USERS", "").replace(";", ",")
    return {int(p) for p in (x.strip() for x in raw.split(",")) if p.isdigit()}


def is_allowed(uid: int) -> bool:
    ids = allowed_ids()
    return (not ids) or (uid in ids)


async def _guard(update: Update, context) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if is_allowed(uid):
        return True
    if update.callback_query:
        await update.callback_query.answer("Нет доступа", show_alert=True)
    elif update.message:
        await update.message.reply_text(
            f"Доступ к боту только по списку.\nТвой ID: {uid}\n"
            "Передай его администратору, чтобы он тебя добавил.")
    return False


# --- клавиатура и текст настроек ------------------------------------------
def _mark(cond: bool) -> str:
    return "✅ " if cond else ""


def settings_kb(s: dict) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("⚡ Для сайта", callback_data="preset:site"),
        InlineKeyboardButton("⚡ Максимум", callback_data="preset:max"),
        InlineKeyboardButton("⚡ Метаданные", callback_data="preset:meta"),
    ], [
        InlineKeyboardButton(_mark(s["mode"] == m) + t, callback_data=f"mode:{m}")
        for m, t in MODES
    ]]
    if s["mode"] != "meta":
        rows.append([
            InlineKeyboardButton(
                _mark(abs(x - s["strength"]) < 1e-6) + f"сила {x:.1f}",
                callback_data=f"str:{x}") for x in STRENGTHS])
    rows.append([
        InlineKeyboardButton(_mark(s["quality"] == q) + f"q{q}",
                             callback_data=f"q:{q}") for q in QUALITIES])
    rows.append([
        InlineKeyboardButton(_mark(s["fmt"] == f) + t, callback_data=f"fmt:{f}")
        for f, t in FORMATS])
    rows.append([
        InlineKeyboardButton(_mark(s["max_side"] == v) + t,
                             callback_data=f"size:{v}") for v, t in SIZES])
    return InlineKeyboardMarkup(rows)


def settings_text(s: dict) -> str:
    parts = [f"Режим: {MODE_TITLES.get(s['mode'], s['mode'])}"]
    if s["mode"] != "meta":
        parts.append(f"сила {s['strength']:.1f}")
    parts.append(f"качество {s['quality']}")
    parts.append("формат " + dict(FORMATS).get(s["fmt"], s["fmt"]))
    parts.append("размер " + dict(SIZES).get(s["max_side"], str(s["max_side"])))
    return "⚙️ Текущие настройки:\n" + ", ".join(parts)


# --- PSNR и статистика -----------------------------------------------------
def _psnr(a: bytes, b: bytes):
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


def _human(n: int) -> str:
    return f"{n / 1024:.0f} КБ" if n < 1024 * 1024 else f"{n / 1048576:.1f} МБ"


def log_stats(uid, count, size_in, size_out, mode) -> None:
    if not STATS_FILE:
        return
    try:
        is_new = not os.path.exists(STATS_FILE)
        with open(STATS_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["ts", "user", "count", "bytes_in", "bytes_out",
                            "mode"])
            w.writerow([datetime.now(timezone.utc).isoformat(), uid, count,
                        size_in, size_out, mode])
    except Exception:  # noqa: BLE001
        log.exception("stats write failed")


# --- команды ---------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    await update.message.reply_text(f"Твой Telegram ID: `{uid}`",
                                    parse_mode="Markdown")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    s = get_settings(context)
    await update.message.reply_text(settings_text(s), reply_markup=settings_kb(s))


async def cmd_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    context.user_data["preview_once"] = True
    await update.message.reply_text(
        "Пришли одну картинку — покажу ДО/ПОСЛЕ с PSNR (файл не сохраняю).")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("preset:"):
        apply_preset(context, data.split(":", 1)[1])
    elif data.startswith("mode:"):
        set_setting(context, "mode", data.split(":", 1)[1])
    elif data.startswith("str:"):
        set_setting(context, "strength", float(data.split(":", 1)[1]))
    elif data.startswith("q:"):
        set_setting(context, "quality", int(data.split(":", 1)[1]))
    elif data.startswith("fmt:"):
        set_setting(context, "fmt", data.split(":", 1)[1])
    elif data.startswith("size:"):
        set_setting(context, "max_side", int(data.split(":", 1)[1]))
    s = get_settings(context)
    try:
        await q.edit_message_text(settings_text(s), reply_markup=settings_kb(s))
    except Exception:  # noqa: BLE001  (message not modified и т.п.)
        pass


# --- обработка изображений -------------------------------------------------
async def _download_and_clean(msg, s: dict, idx=None):
    """Скачивает и чистит одно изображение.
    Возвращает (out_bytes, имя, было_фото, исходные_байты, info)."""
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
    dct, scrub = mode_to_dct_scrub(s["mode"], s["strength"])
    data = bytes(await tg_file.download_as_bytearray())
    out, name, info = await asyncio.to_thread(
        clean_bytes, data, filename, s["quality"], scrub, dct,
        s["max_side"], s["fmt"])
    return out, name, as_photo, data, info


async def _send_results(bot, chat_id, results, summary: str) -> None:
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


def _summary(s, ok, total, any_photo, removed, psnrs, size_in, size_out) -> str:
    head = f"Готово: {ok}/{total}." if total > 1 else "Готово."
    line = f"Режим: {MODE_TITLES.get(s['mode'], s['mode'])}"
    if s["mode"] != "meta":
        line += f", сила {s['strength']:.1f}"
    text = f"{head} {line}"
    if psnrs:
        if total == 1:
            text += f" · PSNR {psnrs[0]:.1f} dB"
        else:
            text += f" · средн. PSNR {sum(psnrs) / len(psnrs):.1f} dB"
    text += ("\nУдалено: " + ", ".join(sorted(removed))) if removed \
        else "\nМетаданных не найдено."
    text += f"\nВес: {_human(size_in)} → {_human(size_out)}"
    if any_photo:
        text += ("\n⚠️ Часть прислана как «фото» — телеграм их сжал. "
                 "Для чистки оригиналов присылай файлом.")
    return text


async def _process_batch(context, chat_id, msgs) -> None:
    s = get_settings(context)
    msgs = msgs[:MAX_FILES]
    note = await context.bot.send_message(
        chat_id, f"Обрабатываю {len(msgs)} шт…" if len(msgs) > 1
        else "Обрабатываю…")
    results, any_photo, removed, psnrs = [], False, set(), []
    size_in = size_out = 0
    multi = len(msgs) > 1
    for i, m in enumerate(msgs):
        try:
            out, name, as_photo, src, info = await _download_and_clean(
                m, s, idx=i if multi else None)
            results.append((out, name))
            any_photo = any_photo or as_photo
            removed.update(info["removed"])
            size_in += info["size_in"]
            size_out += info["size_out"]
            if s["mode"] != "meta":
                p = _psnr(src, out)
                if p is not None:
                    psnrs.append(p)
        except Exception as e:  # noqa: BLE001
            log.exception("processing failed: %s", e)

    if results:
        summary = _summary(s, len(results), len(msgs), any_photo, removed,
                           psnrs, size_in, size_out)
        await _send_results(context.bot, chat_id, results, summary)
        log_stats(chat_id, len(results), size_in, size_out, s["mode"])
    else:
        await context.bot.send_message(
            chat_id, "Не удалось обработать ни одного файла.")
    try:
        await note.delete()
    except Exception:  # noqa: BLE001
        pass


def _to_jpeg(data: bytes) -> bytes:
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    if im.mode in ("RGBA", "P", "LA"):
        im = im.convert("RGB")
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90)
    return b.getvalue()


async def _preview_single(context, msg) -> None:
    s = get_settings(context)
    note = await msg.reply_text("Готовлю превью…")
    try:
        out, name, as_photo, src, info = await _download_and_clean(msg, s)
        left = await asyncio.to_thread(_to_jpeg, src)
        right = await asyncio.to_thread(_to_jpeg, out)
    except Exception as e:  # noqa: BLE001
        log.exception("preview failed")
        await note.edit_text(f"Не вышло сделать превью: {e}")
        return
    cap = f"ДО / ПОСЛЕ · режим {MODE_TITLES.get(s['mode'], s['mode'])}"
    if s["mode"] != "meta":
        p = _psnr(src, out)
        if p is not None:
            cap += f" · PSNR {p:.1f} dB"
    if info["removed"]:
        cap += "\nУдалено: " + ", ".join(sorted(info["removed"]))
    await context.bot.send_media_group(msg.chat_id, media=[
        InputMediaPhoto(left, caption=cap), InputMediaPhoto(right)])
    try:
        await note.delete()
    except Exception:  # noqa: BLE001
        pass


async def _flush_album(gid: str, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    if not await _guard(update, context):
        return
    msg = update.message
    gid = msg.media_group_id
    if gid:
        async with _album_lock:
            is_new = gid not in _album_buf
            _album_buf.setdefault(gid, []).append(msg)
            _album_last[gid] = time.monotonic()
        if is_new:
            _spawn(_flush_album(gid, context))
        return
    if context.user_data.pop("preview_once", False):
        await _preview_single(context, msg)
    else:
        await _process_batch(context, msg.chat_id, [msg])


# --- служебное -------------------------------------------------------------
async def _heartbeat_loop() -> None:
    while True:
        try:
            HEARTBEAT.write_text(str(int(time.time())))
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(30)


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Справка и начало"),
        BotCommand("settings", "Режим, качество, формат, размер"),
        BotCommand("preview", "Превью до/после"),
        BotCommand("id", "Показать мой ID"),
        BotCommand("help", "Справка"),
    ])
    _spawn(_heartbeat_loop())


def main() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Не задан BOT_TOKEN. Получи токен у @BotFather и положи его в "
            "файл .env (BOT_TOKEN=...) или задай переменной окружения.")
    if not allowed_ids():
        log.warning("ALLOWED_USERS пуст — бот доступен ВСЕМ. Впиши id в .env.")

    persistence = PicklePersistence(
        filepath=os.environ.get("DATA_FILE", "botdata.pkl"), update_interval=30)
    app = (Application.builder().token(token)
           .persistence(persistence).post_init(_post_init).build())
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler(["settings", "mode"], cmd_settings))
    app.add_handler(CommandHandler("preview", cmd_preview))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.IMAGE, on_image))
    log.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
