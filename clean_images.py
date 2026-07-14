#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clean_images.py — очистка фотографий перед публикацией.

Что делает:
  1. Надёжно удаляет ВСЕ метаданные (EXIF, GPS, XMP, IPTC, ICC-профиль,
     C2PA/Content Credentials) — пиксели пересохраняются в новый файл
     без служебных блоков.
  2. Опционально (--scrub) прогоняет агрессивную переобработку, которая
     СНИЖАЕТ вероятность выживания невидимых водяных знаков в пикселях.

Важно про SynthID и подобные водяные знаки:
  SynthID (Google) — невидимый водяной знак, встроенный в сами пиксели и
  спроектированный так, чтобы переживать пережатие, ресайз, кроп и
  цветокоррекцию. Надёжного публичного способа удалить его скриптом НЕ
  существует. Режим --scrub лишь ухудшает условия для сохранения любых
  невидимых знаков (ценой качества картинки) и НЕ гарантирует их удаление.

Зависимость: Pillow  ->  pip install Pillow

Примеры:
  python3 clean_images.py photo.jpg
  python3 clean_images.py ./img -o ./clean --recursive
  python3 clean_images.py ./img -o ./clean --scrub --quality 88
"""

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image, ImageFilter
except ImportError:
    sys.exit("Не найден Pillow. Установите: pip install Pillow")

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}


def strip_metadata(img: Image.Image) -> Image.Image:
    """Возвращает новое изображение только с пикселями, без info-блоков."""
    return Image.frombytes(img.mode, img.size, img.tobytes())


def scrub_pixels(img: Image.Image, strength: float) -> Image.Image:
    """
    Переобработка пикселей: ресайз вниз/вверх + лёгкое размытие + шум.
    Снижает шанс выживания невидимых водяных знаков. Портит качество.
    strength: 0.0..1.0 (насколько агрессивно).
    """
    w, h = img.size
    factor = 1.0 - 0.15 * strength           # уменьшаем до 15%
    small = img.resize((max(1, int(w * factor)), max(1, int(h * factor))),
                        Image.LANCZOS)
    img = small.resize((w, h), Image.LANCZOS)

    img = img.filter(ImageFilter.GaussianBlur(radius=0.4 * strength))

    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = img.convert("RGB")
    amp = int(6 * strength)                  # амплитуда шума
    if amp > 0:
        import numpy as np
        rng = np.random.default_rng(1234)
        bands = img.getbands()
        arr = np.asarray(img).astype(np.int16)
        noise = rng.integers(-amp, amp + 1, size=arr.shape, dtype=np.int16)
        if arr.ndim == 3 and "A" in bands:   # альфу не трогаем
            noise[..., bands.index("A")] = 0
        arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
        img = Image.fromarray(arr, img.mode)
    return img


# --- Частотный режим (DCT) -------------------------------------------------
# Прицельное возмущение средних частот в блоках 8x8 (как в JPEG). Ломает
# частотные паттерны, где часто сидит сигнал водяного знака, оставаясь почти
# незаметным глазу (PSNR ~38 dB при силе 1.0). Требует numpy.
# Как и любой такой метод: снижает детектируемость SynthID, но НЕ гарантирует
# его удаление.

_DCT_CACHE = {}


def _dct_matrix(n: int = 8):
    import numpy as np
    k = np.arange(n)                         # частота (строка)
    i = np.arange(n)                         # отсчёт (столбец)
    M = np.sqrt(2.0 / n) * np.cos(
        np.pi * (2 * i[None, :] + 1) * k[:, None] / (2 * n))
    M[0, :] = np.sqrt(1.0 / n)
    return M


def _midband_mask(n: int = 8, lo: int = 2, hi: int = 5):
    import numpy as np
    u = np.arange(n)
    s = u[:, None] + u[None, :]
    m = ((s >= lo) & (s <= hi)).astype(np.float64)
    m[0, 0] = 0.0                            # не трогаем DC (яркость блока)
    return m


def _scrub_channel_dct(ch, strength: float, rng, amp: float = 6.0):
    """Векторизованное частотное возмущение канала по 8x8 блокам."""
    import numpy as np
    D = _DCT_CACHE.setdefault("D", _dct_matrix(8))
    mask = _DCT_CACHE.setdefault("mask", _midband_mask(8))
    h, w = ch.shape
    ph, pw = (-h) % 8, (-w) % 8
    p = np.pad(ch, ((0, ph), (0, pw)), mode="edge").astype(np.float64)
    H, W = p.shape
    # (H,W) -> блоки (nby, nbx, 8, 8)
    blocks = p.reshape(H // 8, 8, W // 8, 8).transpose(0, 2, 1, 3)
    coef = D @ blocks @ D.T                          # прямой DCT для всех блоков
    sigma = amp * strength
    coef += rng.normal(0, sigma, size=blocks.shape) * mask
    out = D.T @ coef @ D                             # обратный DCT
    p = out.transpose(0, 2, 1, 3).reshape(H, W)
    return np.clip(p[:h, :w], 0, 255)


def scrub_dct(img: Image.Image, strength: float) -> Image.Image:
    """Частотное возмущение по 8x8 блокам. Возвращает новое изображение."""
    import numpy as np
    alpha = None
    if img.mode in ("RGBA", "LA"):
        *_, alpha = img.split()
    work = img.convert("L") if img.mode in ("L", "LA") else img.convert("RGB")
    arr = np.asarray(work).astype(np.float64)
    rng = np.random.default_rng(1234)
    if arr.ndim == 2:
        out = _scrub_channel_dct(arr, strength, rng)
    else:
        out = np.stack(
            [_scrub_channel_dct(arr[:, :, c], strength, rng)
             for c in range(arr.shape[2])], axis=2)
    res = Image.fromarray(out.astype(np.uint8), mode=work.mode)
    if alpha is not None:
        res.putalpha(alpha)
    return res


def transform(im: Image.Image, scrub: float = 0.0,
              dct: float = 0.0) -> Image.Image:
    """Единый пайплайн: снять метаданные + опциональные возмущения пикселей.
    Используется и при сохранении файлов, и для превью в окне."""
    if im.mode == "P":
        im = im.convert("RGBA" if "transparency" in im.info else "RGB")
    img = strip_metadata(im)
    if dct > 0:
        img = scrub_dct(img, dct)
    if scrub > 0:
        img = scrub_pixels(img, scrub)
    return img


def save_image(img: Image.Image, fp, ext: str, quality: int) -> None:
    """Сохраняет изображение в путь или файловый объект. Метаданные не пишем."""
    ext = ext.lower()
    if ext in (".jpg", ".jpeg"):
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        img.save(fp, "JPEG", quality=quality, optimize=True, progressive=True)
    elif ext == ".webp":
        img.save(fp, "WEBP", quality=quality, method=6)
    elif ext == ".png":
        img.save(fp, "PNG", optimize=True)
    else:
        img.save(fp, "PNG")   # неизвестное расширение -> безопасный PNG


def process(src: Path, dst: Path, quality: int, scrub: float,
            dct: float = 0.0) -> None:
    with Image.open(src) as im:
        im.load()
        img = transform(im, scrub=scrub, dct=dct)
        dst.parent.mkdir(parents=True, exist_ok=True)
        save_image(img, dst, dst.suffix, quality)


def inspect_metadata(im: Image.Image) -> list:
    """Возвращает список понятных ярлыков метаданных, найденных в картинке."""
    labels = []
    try:
        ex = im.getexif()
    except Exception:  # noqa: BLE001
        ex = {}
    checks = [
        (34853, "геолокация (GPS)"),
        (271, "камера"), (272, "камера"),
        (306, "дата съёмки"), (36867, "дата съёмки"),
        (305, "софт"),
        (315, "автор"), (33432, "копирайт"),
    ]
    for tag, label in checks:
        if tag in ex and label not in labels:
            labels.append(label)
    info = getattr(im, "info", {}) or {}
    if "xmp" in info:
        labels.append("XMP")
    if info.get("icc_profile"):
        labels.append("ICC-профиль")
    if "photoshop" in info or "iptc" in info:
        labels.append("IPTC")
    if "comment" in info:
        labels.append("комментарий")
    return labels


def fit_within(img: Image.Image, max_side: int) -> Image.Image:
    """Уменьшает так, чтобы наибольшая сторона <= max_side. Не увеличивает."""
    if not max_side:
        return img
    w, h = img.size
    if max(w, h) <= max_side:
        return img
    scale = max_side / float(max(w, h))
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                      Image.LANCZOS)


def _target_ext(filename: str, out_format: str) -> str:
    """Выбирает расширение выходного файла по настройке формата."""
    fmt = (out_format or "keep").lower()
    if fmt in ("jpg", "jpeg"):
        return ".jpg"
    if fmt == "webp":
        return ".webp"
    if fmt == "png":
        return ".png"
    ext = Path(filename).suffix.lower()          # keep
    return ext if ext in SUPPORTED else ".jpg"


def clean_bytes(data: bytes, filename: str, quality: int = 92,
                scrub: float = 0.0, dct: float = 0.0,
                max_side: int = 0, out_format: str = "keep"):
    """Очищает изображение из байтов.
    Возвращает (bytes, имя_файла, info) где info содержит:
      removed  — список удалённых метаданных,
      size_in / size_out — размер в байтах до/после.
    Единый пайплайн с CLI/GUI."""
    import io
    ext = _target_ext(filename, out_format)
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        removed = inspect_metadata(im)
        img = transform(im, scrub=scrub, dct=dct)
        img = fit_within(img, max_side)
    buf = io.BytesIO()
    save_image(img, buf, ext, quality)
    out = buf.getvalue()
    info = {"removed": removed, "size_in": len(data), "size_out": len(out)}
    return out, Path(filename).stem + "_clean" + ext, info


def collect(inp: Path, recursive: bool):
    if inp.is_file():
        return [inp]
    pattern = "**/*" if recursive else "*"
    return [p for p in sorted(inp.glob(pattern))
            if p.is_file() and p.suffix.lower() in SUPPORTED]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Удаление метаданных (и опционально переобработка пикселей) фотографий.")
    ap.add_argument("input", type=Path, help="файл или папка с изображениями")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="папка вывода (по умолчанию рядом, с суффиксом _clean)")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="обходить вложенные папки")
    ap.add_argument("-q", "--quality", type=int, default=92,
                    help="качество JPEG/WEBP, 1..100 (по умолчанию 92)")
    ap.add_argument("--dct", nargs="?", const=1.0, type=float, default=0.0,
                    metavar="STRENGTH",
                    help="частотное возмущение по 8x8 блокам (аккуратный режим, "
                         "почти незаметен глазу); сила 0.1..1.0 (по умолчанию "
                         "1.0). Требует numpy. SynthID НЕ гарантированно удаляется.")
    ap.add_argument("--scrub", nargs="?", const=1.0, type=float, default=0.0,
                    metavar="STRENGTH",
                    help="грубая переобработка пикселей (ресайз+шум) для "
                         "подавления невидимых водяных знаков; сила 0.1..1.0 "
                         "(по умолчанию 1.0). SynthID НЕ гарантированно удаляется.")
    args = ap.parse_args()

    inp = args.input
    if not inp.exists():
        sys.exit(f"Путь не найден: {inp}")

    files = collect(inp, args.recursive)
    if not files:
        sys.exit("Подходящих изображений не найдено.")

    base = inp if inp.is_dir() else inp.parent
    out_dir = args.output or (base.parent / f"{base.name}_clean"
                              if inp.is_dir() else base / "clean")

    scrub = max(0.0, min(1.0, args.scrub))
    dct = max(0.0, min(1.0, args.dct))
    if dct > 0:
        try:
            import numpy  # noqa: F401
        except ImportError:
            sys.exit("Режим --dct требует numpy. Установите: pip install numpy")
        print(f"[!] Режим dct={dct:.2f}: частотное возмущение без гарантий "
              f"удаления SynthID.")
    if scrub > 0:
        print(f"[!] Режим scrub={scrub:.2f}: грубое подавление водяных знаков "
              f"без гарантий, качество будет снижено.")

    ok = 0
    for f in files:
        rel = f.relative_to(base) if inp.is_dir() else Path(f.name)
        dst = out_dir / rel
        try:
            process(f, dst, args.quality, scrub, dct)
            print(f"  ✓ {rel}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {rel}: {e}", file=sys.stderr)

    print(f"\nГотово: {ok}/{len(files)} -> {out_dir}")


if __name__ == "__main__":
    main()
