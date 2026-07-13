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
    import random

    w, h = img.size
    factor = 1.0 - 0.15 * strength           # уменьшаем до 15%
    small = img.resize((max(1, int(w * factor)), max(1, int(h * factor))),
                        Image.LANCZOS)
    img = small.resize((w, h), Image.LANCZOS)

    img = img.filter(ImageFilter.GaussianBlur(radius=0.4 * strength))

    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")
    px = img.load()
    amp = int(6 * strength)                  # амплитуда шума
    if amp > 0:
        rnd = random.Random(1234)
        bands = len(img.getbands())
        for y in range(h):
            for x in range(w):
                cur = px[x, y]
                if bands == 1:
                    px[x, y] = _clamp(cur + rnd.randint(-amp, amp))
                else:
                    px[x, y] = tuple(
                        _clamp(c + rnd.randint(-amp, amp)) for c in cur
                    )
    return img


def _clamp(v: int) -> int:
    return 0 if v < 0 else 255 if v > 255 else v


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
    import numpy as np
    D = _DCT_CACHE.setdefault("D", _dct_matrix(8))
    mask = _DCT_CACHE.setdefault("mask", _midband_mask(8))
    h, w = ch.shape
    ph, pw = (-h) % 8, (-w) % 8
    p = np.pad(ch, ((0, ph), (0, pw)), mode="edge").astype(np.float64)
    sigma = amp * strength
    for by in range(0, p.shape[0], 8):
        for bx in range(0, p.shape[1], 8):
            b = p[by:by + 8, bx:bx + 8]
            c = D @ b @ D.T
            c += rng.normal(0, sigma, (8, 8)) * mask
            p[by:by + 8, bx:bx + 8] = D.T @ c @ D
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


def process(src: Path, dst: Path, quality: int, scrub: float,
            dct: float = 0.0) -> None:
    with Image.open(src) as im:
        im.load()
        img = transform(im, scrub=scrub, dct=dct)

        dst.parent.mkdir(parents=True, exist_ok=True)
        ext = dst.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            img.save(dst, "JPEG", quality=quality, optimize=True,
                     progressive=True)
        elif ext == ".webp":
            img.save(dst, "WEBP", quality=quality, method=6)
        elif ext == ".png":
            img.save(dst, "PNG", optimize=True)
        else:
            img.save(dst)   # exif/icc не передаём -> метаданных нет


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
