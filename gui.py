#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py — простое окно для чистки фотографий (Windows / macOS / Linux).

Запускается двойным кликом по лаунчеру:
  * macOS   -> clean.command
  * Windows -> clean.bat
Лаунчер сам ставит зависимости (Pillow, numpy) в изолированный venv.

Вся обработка идёт локально, файлы никуда не выгружаются.
"""

import os
import sys
import threading
import queue
from pathlib import Path

# Гарантируем, что рядом лежащий clean_images.py импортируется
sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean_images import collect, process, transform, SUPPORTED  # noqa: E402


def _psnr(a, b) -> float:
    """PSNR между двумя RGB-массивами (дБ). >40 — практически незаметно."""
    import numpy as np
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    mse = np.mean((a - b) ** 2)
    return 99.0 if mse == 0 else float(10 * np.log10(255.0 ** 2 / mse))


def run_batch(inp: Path, out_dir: Path, quality: int, mode: str,
              strength: float, recursive: bool, log):
    """
    Обрабатывает партию файлов. Не зависит от GUI — используется и в тестах.
    mode: "meta" | "dct" | "scrub".
    log: функция log(str) для вывода прогресса.
    Возвращает (успешно, всего).
    """
    files = collect(inp, recursive)
    if not files:
        log("Подходящих изображений не найдено.")
        return 0, 0

    base = inp if inp.is_dir() else inp.parent
    dct = strength if mode == "dct" else 0.0
    scrub = strength if mode == "scrub" else 0.0

    log(f"Файлов: {len(files)}. Режим: {mode}"
        + (f", сила {strength:.2f}" if mode != "meta" else "") + ".")

    ok = 0
    for f in files:
        rel = f.relative_to(base) if inp.is_dir() else Path(f.name)
        try:
            process(f, out_dir / rel, quality, scrub, dct)
            log(f"  ✓ {rel}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            log(f"  ✗ {rel}: {e}")
    log(f"Готово: {ok}/{len(files)} -> {out_dir}")
    return ok, len(files)


def build_ui(root):
    """Строит окно на переданном Tk-руте. Возвращает функцию запуска
    (используется в smoke-тестах)."""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root.title("Чистка фото — метаданные и водяные знаки")
    root.minsize(560, 460)

    pad = {"padx": 8, "pady": 4}
    inp_var = tk.StringVar()
    out_var = tk.StringVar()
    mode_var = tk.StringVar(value="dct")
    strength_var = tk.DoubleVar(value=0.6)
    quality_var = tk.IntVar(value=92)
    recursive_var = tk.BooleanVar(value=True)

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)

    def pick_input():
        d = filedialog.askdirectory(title="Папка с фотографиями")
        if d:
            inp_var.set(d)
            if not out_var.get():
                out_var.set(str(Path(d).parent / (Path(d).name + "_clean")))

    def pick_output():
        d = filedialog.askdirectory(title="Куда сохранить результат")
        if d:
            out_var.set(d)

    # --- Ввод / вывод ---
    ttk.Label(frm, text="Папка с фото:").grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=inp_var, width=44).grid(row=0, column=1, **pad)
    ttk.Button(frm, text="Выбрать…", command=pick_input).grid(row=0, column=2, **pad)

    ttk.Label(frm, text="Сохранить в:").grid(row=1, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=out_var, width=44).grid(row=1, column=1, **pad)
    ttk.Button(frm, text="Выбрать…", command=pick_output).grid(row=1, column=2, **pad)

    # --- Режим ---
    mode_box = ttk.LabelFrame(frm, text="Режим", padding=8)
    mode_box.grid(row=2, column=0, columnspan=3, sticky="ew", **pad)
    ttk.Radiobutton(mode_box, text="Только метаданные (EXIF/GPS)",
                    variable=mode_var, value="meta").pack(anchor="w")
    ttk.Radiobutton(mode_box, text="+ DCT — аккуратно, почти незаметно",
                    variable=mode_var, value="dct").pack(anchor="w")
    ttk.Radiobutton(mode_box, text="+ Scrub — грубо, сильнее, но заметнее",
                    variable=mode_var, value="scrub").pack(anchor="w")

    # --- Параметры ---
    ttk.Label(frm, text="Сила подавления:").grid(row=3, column=0, sticky="w", **pad)
    ttk.Scale(frm, from_=0.1, to=1.0, variable=strength_var,
              orient="horizontal").grid(row=3, column=1, sticky="ew", **pad)
    ttk.Label(frm, textvariable=tk.StringVar()).grid(row=3, column=2)
    strength_lbl = ttk.Label(frm, text="0.60")
    strength_lbl.grid(row=3, column=2, **pad)
    strength_var.trace_add(
        "write", lambda *_: strength_lbl.config(text=f"{strength_var.get():.2f}"))

    ttk.Label(frm, text="Качество JPEG/WEBP:").grid(row=4, column=0, sticky="w", **pad)
    ttk.Scale(frm, from_=60, to=100, variable=quality_var,
              orient="horizontal").grid(row=4, column=1, sticky="ew", **pad)
    quality_lbl = ttk.Label(frm, text="92")
    quality_lbl.grid(row=4, column=2, **pad)
    quality_var.trace_add(
        "write", lambda *_: quality_lbl.config(text=str(int(quality_var.get()))))

    ttk.Checkbutton(frm, text="Включая вложенные папки",
                    variable=recursive_var).grid(row=5, column=1, sticky="w", **pad)

    # --- Лог ---
    log_box = tk.Text(frm, height=10, width=64, state="disabled")
    log_box.grid(row=7, column=0, columnspan=3, sticky="nsew", **pad)
    frm.rowconfigure(7, weight=1)
    frm.columnconfigure(1, weight=1)

    log_q: "queue.Queue[str]" = queue.Queue()

    def log(msg: str):
        log_q.put(msg)

    def drain():
        try:
            while True:
                msg = log_q.get_nowait()
                log_box.config(state="normal")
                log_box.insert("end", msg + "\n")
                log_box.see("end")
                log_box.config(state="disabled")
        except queue.Empty:
            pass
        root.after(100, drain)

    run_btn = ttk.Button(frm, text="Очистить")

    def worker(inp, out, quality, mode, strength, rec):
        try:
            run_batch(inp, out, quality, mode, strength, rec, log)
        except Exception as e:  # noqa: BLE001
            log(f"Ошибка: {e}")
        finally:
            root.after(0, lambda: run_btn.config(state="normal"))

    def on_run():
        if not inp_var.get():
            messagebox.showwarning("Нет папки", "Выберите папку с фотографиями.")
            return
        inp = Path(inp_var.get())
        if not inp.exists():
            messagebox.showerror("Ошибка", f"Путь не найден: {inp}")
            return
        out = Path(out_var.get()) if out_var.get() else \
            inp.parent / (inp.name + "_clean")
        run_btn.config(state="disabled")
        threading.Thread(
            target=worker,
            args=(inp, out, int(quality_var.get()), mode_var.get(),
                  float(strength_var.get()), recursive_var.get()),
            daemon=True).start()

    def _sample_image():
        """Возвращает путь к фото для превью или None."""
        if inp_var.get():
            p = Path(inp_var.get())
            if p.is_file():
                return p
            if p.is_dir():
                files = collect(p, recursive_var.get())
                if files:
                    return files[0]
        f = filedialog.askopenfilename(
            title="Выберите фото для превью",
            filetypes=[("Изображения", "*.jpg *.jpeg *.png *.webp")])
        return Path(f) if f else None

    def show_preview():
        from PIL import Image, ImageTk
        src = _sample_image()
        if not src:
            return
        try:
            with Image.open(src) as im:
                im.load()
                orig = im.convert("RGB")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Ошибка", f"Не удалось открыть фото:\n{e}")
            return

        mode = mode_var.get()
        strength = float(strength_var.get())
        scrub = strength if mode == "scrub" else 0.0
        dct = strength if mode == "dct" else 0.0
        after = transform(orig, scrub=scrub, dct=dct).convert("RGB")

        # Масштабируем обе картинки под одинаковый размер для показа
        thumb = orig.copy()
        thumb.thumbnail((420, 420))
        size = thumb.size
        left = thumb
        right = after.resize(orig.size).resize(size)

        win = tk.Toplevel(root)
        win.title(f"Превью: {src.name}")
        info = (f"Режим: {mode}" +
                (f", сила {strength:.2f}" if mode != "meta" else
                 " (пиксели не меняются, только метаданные)"))
        if mode != "meta":
            info += f"   |   PSNR: {_psnr(orig, after):.1f} dB"
            info += "  (>40 — практически незаметно)"
        ttk.Label(win, text=info, padding=8).pack()

        row = ttk.Frame(win, padding=8)
        row.pack()
        tk_left = ImageTk.PhotoImage(left)
        tk_right = ImageTk.PhotoImage(right)
        win._imgs = (tk_left, tk_right)   # держим ссылки от сборщика мусора
        col_l = ttk.Frame(row)
        col_l.grid(row=0, column=0, padx=8)
        ttk.Label(col_l, text="ДО").pack()
        ttk.Label(col_l, image=tk_left).pack()
        col_r = ttk.Frame(row)
        col_r.grid(row=0, column=1, padx=8)
        ttk.Label(col_r, text="ПОСЛЕ").pack()
        ttk.Label(col_r, image=tk_right).pack()

    prev_btn = ttk.Button(frm, text="Превью до/после", command=show_preview)
    prev_btn.grid(row=6, column=0, **pad)

    run_btn.config(command=on_run)
    run_btn.grid(row=6, column=1, **pad)

    drain()
    return on_run, show_preview


def main() -> None:
    import tkinter as tk
    root = tk.Tk()
    build_ui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
